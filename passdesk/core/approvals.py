"""The approval point: where a deployment can hold an external write for a
person (docs/approvals.md; the hold itself is core/holds.py).

A tool that is about to write outside the broker (push, open a pull request)
calls `require(card)` after every check of its own has passed, with a card
describing exactly what is about to happen. By default `require` returns at
once. A plugin may install a hook with `set_hook`; the hook returns to allow
the write or raises Denied to refuse it, and may take its time deciding.

Cards hold only what the broker itself verified (never a credential, never
content from the network) so they can be shown to a person and written to
the record as they are.

Cards never carry content either. When a hook is installed, git_push also
stores the pushed commits' patch as a broker-local file,
STATE/approvals/patches/<sha256>.patch, for the person at the machine to
inspect; the card carries only its digest, size and whether it was cut
(`patch_sha256`, `patch_bytes`, `patch_truncated`). `store_patch` writes it
and prunes patches no pending card names any more.
"""

from __future__ import annotations

import contextvars
import errno
import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import config, filters

Hook = Callable[[dict], None]

# The principal whose call is running, set by the protocol layer per call so
# a card can say who asked.
PRINCIPAL: contextvars.ContextVar[str] = contextvars.ContextVar("passdesk_principal", default="")

_hook: Hook | None = None


def set_hook(fn: Hook | None) -> None:
    """Install (or, with None, remove) the hook every external write goes through."""
    global _hook
    _hook = fn


def hook_installed() -> bool:
    return _hook is not None


def hook_is(fn: Hook) -> bool:
    """Whether fn is the installed hook (not replaced by a plugin's)."""
    return _hook is fn


def _redacted(value):
    """Recursively redact known secret patterns in every string value.

    A credential-looking string (a leaked token pasted into a PR title, say)
    must not reach a notification webhook, the pending card file or the
    console verbatim, however it got into the card. Non-string values (ints,
    bools, the digest in `bundle_sha256`) pass through untouched.
    """
    if isinstance(value, str):
        return filters.redact(value)
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redacted(v) for v in value)
    return value


def require(card: dict) -> None:
    """Hold here until the write described by `card` may proceed.

    Returns to allow it; raises Denied (from the hook) to refuse it. The
    card gains the calling principal before the hook sees it, and every
    string value in the copy the hook sees has gone through
    `filters.redact` - so a leaked credential cannot reach a notification,
    the pending card file or the console through a card field (the title,
    say). The write itself, which runs only after `require` returns
    (`passdesk/tools/git.py:187,209`), still uses the original arguments;
    only the copy shown to the hook is scrubbed.
    """
    if _hook is None:
        return
    _hook(_redacted({**card, "principal": PRINCIPAL.get()}))


# --- the patch an approver can inspect (doomtickle/passdesk-ops#113) --------

PATCH_MAX_BYTES = 262_144
# Bounds for [approvals].patch_max_bytes. The ceiling stays under what a
# console is expected to read (the AI Code Factory's reads at most 2 MiB).
PATCH_MAX_BYTES_MIN = 4_096
PATCH_MAX_BYTES_MAX = 2_000_000
# A patch no pending card names is kept this long, so one written for a
# card the hook has not recorded yet (or a retry of it) is never removed.
PATCH_KEEP_SECONDS = 2 * 3600
_CARD_MAX_BYTES = 1_000_000
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def patches_dir() -> Path:
    return config.STATE / "approvals" / "patches"


def patch_max_bytes(policy: dict) -> int:
    """[approvals].patch_max_bytes, clamped to the bounds above."""
    table = policy.get("approvals") if isinstance(policy.get("approvals"), dict) else {}
    value = table.get("patch_max_bytes", PATCH_MAX_BYTES)
    if isinstance(value, bool) or not isinstance(value, int):
        value = PATCH_MAX_BYTES
    return min(PATCH_MAX_BYTES_MAX, max(PATCH_MAX_BYTES_MIN, value))


def _read_card(name: str, dir_fd: int) -> dict | None:
    """A pending card as a dict, or None if it is not a readable regular
    file of JSON (never through a symlink)."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, _CARD_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _CARD_MAX_BYTES:
        return None
    try:
        card = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    return card if isinstance(card, dict) else None


def _pending_patch_digests() -> set[str]:
    """Every patch_sha256 named by a card under STATE/approvals/pending/."""
    try:
        dfd = os.open(config.STATE / "approvals" / "pending", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return set()
    digests = set()
    try:
        for name in os.listdir(dfd):
            if not name.endswith(".json"):
                continue
            card = _read_card(name, dfd)
            if card and isinstance(card.get("patch_sha256"), str):
                digests.add(card["patch_sha256"])
    finally:
        os.close(dfd)
    return digests


def prune_patches(now: float | None = None) -> None:
    """Remove each stored patch (and leftover temp file) that no pending card
    names and that is older than PATCH_KEEP_SECONDS. Symlinks and anything
    else that is not a regular file are left alone."""
    now = time.time() if now is None else now
    keep = _pending_patch_digests()
    try:
        entries = list(os.scandir(patches_dir()))
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.name.endswith(".patch"):
            if entry.name[:-len(".patch")] in keep:
                continue
        elif not entry.name.startswith(".tmp-"):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode) or now - st.st_mtime <= PATCH_KEEP_SECONDS:
            continue
        try:
            os.unlink(entry.path)
        except FileNotFoundError:
            pass


def _patches_dir_ready() -> Path:
    d = patches_dir()
    for path in (d.parent, d):
        try:
            os.mkdir(path, 0o755)
            os.chmod(path, 0o755)  # whatever the umask
        except FileExistsError:
            pass
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise RuntimeError(f"{path} is not a directory")
    return d


def store_patch(data: bytes) -> str:
    """Store data as STATE/approvals/patches/<sha256>.patch (0644, written
    atomically) after pruning stale patches; returns the sha256. Rewriting
    an existing patch refreshes its age."""
    digest = hashlib.sha256(data).hexdigest()
    d = _patches_dir_ready()
    prune_patches()
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o644)
            fh.write(data)
        os.replace(tmp, d / f"{digest}.patch")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
        raise
    return digest
