"""The dashboard's FIRST SHIFT banner and start-up hints (doomtickle/passdesk-ops#181). Display only.

`factory start` prints its hints (factory/hints.py) to the terminal that ran it, and the person is
looking at the browser. This computes the same list on the host for `hints` in /api/state: no Claude
login stored, role sandboxes not created yet (the state after the first /login), then any other hint.
Every text is a fixed string from factory/hints.py or below, cleaned to one printable line; nothing an
agent wrote reaches it.

Two of the checks run `sbx`: `sbx secret ls` (the login, the GitHub token) and `sbx ls -q` (the role
sandboxes). They are never run for a request: a background refresh runs them at most once per HINTS_TTL
seconds, one at a time, off the board's lock, each with a PROBE_TIMEOUT cut-off, and only these two
fixed argv forms (Probe refuses any other). A request reads the last result. If either `sbx` call
fails or times out, the last result stands (a hiccup never flashes FIRST SHIFT up). The stale-sandbox
hint is not computed here: Dashboard.service() already has it from the fingerprint files, without sbx.
Keychain checks (Jira, npm, GitLab tokens) are left out; `factory start` still prints them.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import io
import os
import re
import sys
import threading
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package

from factory import hints as factory_hints  # noqa: E402
from factory.context import Captured, Context, Paths  # noqa: E402

HINTS_TTL = 30       # seconds between the sbx checks
PROBE_TIMEOUT = 5    # seconds one sbx call may take
MAX_HINTS = 6
UNSAFE = re.compile(r"[^\x20-\x7e]")

ALLOWED = (("sbx", "secret", "ls"), ("sbx", "ls", "-q"))
FIRST_SHIFT = {
    factory_hints.LOGIN: "FIRST SHIFT · 1. POWER ON and type /login · 2. press RESTART",
    factory_hints.ROLES: "FIRST SHIFT · login saved, the role sandboxes are not created yet · press RESTART",
}
CODES = (factory_hints.STALE, factory_hints.LOGIN, factory_hints.ROLES, factory_hints.GITHUB,
         factory_hints.KEYCHAIN, factory_hints.CONFIG)


class Probe(Context):
    """A factory Context that runs only ALLOWED argv forms, each cut off after PROBE_TIMEOUT, and
    runs each once per probe. `failed` is set when one did not succeed."""

    def __init__(self, paths: Paths, environ=None):
        sink = io.StringIO()
        super().__init__(paths, environ=dict(os.environ if environ is None else environ),
                         stdout=sink, stderr=sink, stdin=io.StringIO())
        self.failed = False
        self._seen: dict = {}

    def capture(self, argv, **kw):
        key = tuple(argv)
        if key not in ALLOWED:
            self.failed = True
            return Captured(126, "", "refused\n")
        if key not in self._seen:
            got = super().capture(list(key), timeout=PROBE_TIMEOUT)
            self.failed = self.failed or got.status != 0
            self._seen[key] = got
        return self._seen[key]


class Hints:
    """The sbx-derived hints, refreshed in the background at most every HINTS_TTL seconds."""

    def __init__(self, paths: Paths, environ=None):
        self.paths, self.environ = paths, environ
        self._items: list = []
        self._at = float("-inf")
        self._run = threading.Lock()   # one refresh at a time
        self._thread: threading.Thread | None = None

    def get(self, now: float) -> list:
        """The last result: [(code, text)]. Starts a refresh when it is old; never waits for one."""
        if now - self._at >= HINTS_TTL or now < self._at:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._thread = threading.Thread(target=self.refresh, args=(now,), daemon=True)
                self._thread.start()
        return list(self._items)

    def refresh(self, now: float) -> None:
        if not self._run.acquire(blocking=False):
            return
        try:
            probe = Probe(self.paths, self.environ)
            try:
                items = factory_hints.collect(probe, stale=[], keychain_checks=False, roles=True)
            except Exception:  # a banner must not take anything down; the last result stands
                return
            if not probe.failed:
                self._items = items
        finally:
            self._at = now
            self._run.release()


def clean(text: str, cap: int = 240) -> str:
    return UNSAFE.sub("?", str(text))[:cap]


def compose(stale: list, items: list) -> list:
    """The hints as the page gets them, [{"code", "text"}], in `factory start`'s order: the
    out-of-date sandboxes (from the SERVICE DUE lamp's files), then ITEMS. The first-login states
    carry their fixed banner text; the others the same words `factory start` prints."""
    merged = ([(factory_hints.STALE, factory_hints.stale_hint(stale))] if stale else []) + list(items)
    out = []
    for code, text in merged[:MAX_HINTS]:
        out.append({"code": code, "text": FIRST_SHIFT.get(code) or clean(text)})
    return out


def first_shift(hints: list) -> bool:
    """True while the first /login or the first RESTART is still to do."""
    return any(h["code"] in FIRST_SHIFT for h in hints)
