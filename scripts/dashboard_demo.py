"""factory dashboard --demo: the dashboard on made-up data, with no factory running.

Builds a throwaway state folder (a fresh 0700 temp folder, removed on exit)
from the committed fixture in scripts/dashboard-demo/ and points the dashboard
at it, so a README screenshot or a first look needs no sbx, broker or
Coordinator. The real factory state folder is never read.

The fixture is plain text. The tokens in it are made relative to the moment the
dashboard starts, so cards don't expire and lanes don't go stale:
  {{clock:-90}}   a transcript time of day (HH:MM:SS, local), 90 s ago
  {{iso:-300}}    an audit timestamp (UTC), 300 s ago, but never before local midnight
  {{epoch:+600}}  epoch seconds, 600 s from now
  {{patch_sha256:demo.patch}}  the SHA-256 of that file under broker/approvals/patches/
The patch files are copied as they are, named by their digest, which is how
the gateway stores them, so INSPECT reads one through the same code as a real one.

Nothing in demo mode acts. DemoControls refuses APPROVE, DECLINE, RESTART,
UPDATE and DIAG (the control unit's decide, prepare and run) before they reach
a decision file or a host command; there is no Terminal 0. A small ticker
appends a broker call to the temp folder's audit log every so often, so the
floor has crates to move for a GIF.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import dashboard_controls as dc
from dashboard_service import Paths

FIXTURE = Path(__file__).resolve().parent / "dashboard-demo"
PROJECT = "DEMO"
REFUSED = "demo mode: this dashboard shows made-up data and nothing here acts"
TOKEN = re.compile(r"\{\{(clock|iso|epoch|patch_sha256):([^{}]*)\}\}")
PATCHES = ("broker", "approvals", "patches")
DEMO_REPOS = ("app", "docs")  # empty repos made under setup/project/ for the shift briefing
SETUP = "setup"  # the fixture's factory checkout (root/) and config folder (config/), for the SETUP screen
# What the ticker appends (cycled): the printer's ALLOW and DENY lines, and the floor's crates.
TICKS = (
    {"role": "developer", "tool": "web_fetch", "decision": "deny", "args": {"url": "https://paste.example.net/raw/x2"},
     "reason": "host not on the allow list"},
    {"role": "reviewer", "tool": "git_fetch", "decision": "allow", "args": {"repo": "example/app"}},
    {"role": "developer", "tool": "npm_fetch", "decision": "allow", "args": {"path": "."}},
)


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def local_midnight(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def render(text: str, now: float, fixture: Path = FIXTURE) -> str:
    """text with every fixture token replaced, relative to now."""
    def sub(m):
        kind, arg = m.groups()
        if kind == "patch_sha256":
            return hashlib.sha256((fixture.joinpath(*PATCHES) / arg).read_bytes()).hexdigest()
        at = now + int(arg)
        if kind == "clock":
            return time.strftime("%H:%M:%S", time.localtime(at))
        if kind == "iso":
            return iso(max(at, local_midnight(now)))
        return str(int(at))
    return TOKEN.sub(sub, text)


def build(dest: Path, now: float | None = None, fixture: Path = FIXTURE) -> None:
    """Write the fixture, rendered for now, into dest/dispatch and dest/broker."""
    now = time.time() if now is None else now
    for src in sorted(fixture.rglob("*")):
        rel = src.relative_to(fixture)
        if src.is_symlink() or not src.is_file():
            continue
        if rel.parts[:3] == PATCHES:
            data = src.read_bytes()
            out = dest.joinpath(*PATCHES, hashlib.sha256(data).hexdigest() + ".patch")
        else:
            data = render(src.read_text(encoding="utf-8"), now, fixture).encode("utf-8")
            out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    # The bridge lamp reads a pid file and checks the process is alive: this one is.
    (dest / "dispatch").mkdir(parents=True, exist_ok=True)
    (dest / "broker").mkdir(parents=True, exist_ok=True)
    (dest / "dispatch" / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (dest / "broker" / "project").write_text(PROJECT + "\n", encoding="utf-8")
    # The shift briefing lists the project's repos with the real lister, which wants a .git in each.
    for repo in DEMO_REPOS:
        (dest / SETUP / "project" / repo / ".git").mkdir(parents=True, exist_ok=True)


def start_ticker(dest: Path, interval: float = 20.0, first: float = 8.0) -> threading.Event:
    """Append one of TICKS to dest's broker audit log after `first` seconds and then
    every `interval`, until the returned event is set."""
    stop = threading.Event()

    def run():
        n, wait = 0, first
        while not stop.wait(wait):
            rec = dict(TICKS[n % len(TICKS)], ts=iso(time.time()))
            try:
                with open(dest / "broker" / "audit.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:  # the folder was removed at shutdown: stop quietly, don't recreate it
                return
            n, wait = n + 1, interval

    threading.Thread(target=run, daemon=True).start()
    return stop


class DemoControls(dc.Controls):
    """The control unit with every action refused: it can show the cards, the
    fixture's patch and the fixture's configuration (the same read-only paths as
    a real run, inside the temp folder) but can't write a decision or start a command."""

    def setup_source(self) -> tuple:
        base = self.state / SETUP
        return Paths(str(base / "root"), str(base / "config"), str(self.state)), self.board.project

    def api_decide(self, body: dict) -> tuple:
        raise dc.Refused(403, REFUSED)

    api_prepare = api_run = api_decide

    def argv(self, action: str) -> list:
        raise RuntimeError("demo mode runs no commands")
