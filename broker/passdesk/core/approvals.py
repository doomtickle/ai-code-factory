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

A core tool that can have its write completed later by the gateway (the
approve-later queue, doomtickle/passdesk-ops#91) passes `require` a `job` as
well: the exact, already-checked arguments of the write, and registers with
`register_deferred` the function that performs it from those. Core's hold
stores the job if nobody decides within `hold_seconds` and raises `Queued`;
it is the only reader of the job (a plugin's hook is given the card, as
before), and a call without one keeps the retry-the-call behaviour.
"""

from __future__ import annotations

import contextvars
import errno
import hashlib
import json
import math
import os
import stat
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Callable

from . import config, filters
from .errors import Queued

# Returns None to allow, or (core/holds.py) the outcome of a hold that was
# approved; raises Denied to refuse.
Hook = Callable[[dict], "dict | None"]

# The principal whose call is running, set by the protocol layer per call so
# a card can say who asked.
PRINCIPAL: contextvars.ContextVar[str] = contextvars.ContextVar("passdesk_principal", default="")
# The id of the hold the running call passed (`require` sets it, the protocol
# layer resets it per call and puts it on the call's audit record).
HELD_ID: contextvars.ContextVar[str] = contextvars.ContextVar("passdesk_held_id", default="")
# The job of the write `require` is holding, while its hook runs (see job()).
_JOB: contextvars.ContextVar[dict | None] = contextvars.ContextVar("passdesk_job", default=None)

_hook: Hook | None = None
# tool name -> fn(job, card, policy) -> result text; raises on any failure.
Deferred = Callable[[dict, dict, dict], str]
_deferred: dict[str, Deferred] = {}


def register_deferred(tool: str, fn: Deferred) -> None:
    """Register how the gateway completes a queued `tool` write itself, from
    the job the tool passed to `require` and the stored card (core tools
    only: tools/git.py). fn must re-check the job against the policy and
    the card, use nothing but them (never the agent's workspace), return the
    result text and raise (Denied, or anything else) if nothing was written."""
    _deferred[tool] = fn


def deferred(tool: str) -> Deferred | None:
    return _deferred.get(tool)


def job() -> dict | None:
    """The job passed to the `require` call that is running (core's hold
    reads it), else None."""
    return _JOB.get()


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


def require(card: dict, job: dict | None = None) -> dict | None:
    """Hold here until the write described by `card` may proceed.

    Returns to allow it; raises Denied (from the hook) to refuse it. The
    return value is the hold's outcome when the write was held and approved
    (`{"id", "approver", "decided", "waited_seconds"}`, see `held_note`),
    else None: no hook, a tool the policy doesn't hold, or a hook (a
    plugin's) that returns None or anything that isn't such an outcome. The
    card gains the calling principal before the hook sees it, and every
    string value in the copy the hook sees has gone through
    `filters.redact` - so a leaked credential cannot reach a notification,
    the pending card file or the console through a card field (the title,
    say). The write itself, which runs only after `require` returns
    (`passdesk/tools/git.py`), still uses the original arguments; only the
    copy shown to the hook is scrubbed.

    `job`, from a tool registered with `register_deferred`, is what the
    gateway needs to make the write itself later (JSON-serializable, no
    credential, every field already checked; not redacted, since it is
    the write). Core's hold may then raise `Queued` instead of returning:
    the write is queued (or was already completed), not refused.
    """
    if _hook is None:
        return None
    token = _JOB.set(job)
    try:
        outcome = _outcome(_hook(_redacted({**card, "principal": PRINCIPAL.get()})))
    except Queued as e:
        HELD_ID.set(_one_line(e.cid, 64))
        raise
    finally:
        _JOB.reset(token)
    if outcome is not None:
        HELD_ID.set(outcome["id"])
    return outcome


def _one_line(value, limit: int) -> str:
    """value as one line of at most limit characters, filtered as anything
    returned to an agent is (the approver's name comes from a file the
    deployment's console wrote), and without the format characters
    (Unicode category Cf: bidi overrides and isolates, zero-width
    characters) and C1 controls that filters.sanitize leaves, which could
    make the line read differently from what it holds."""
    text = filters.sanitize(str(value), 10 * limit)
    text = "".join(c for c in text if not ("\x80" <= c <= "\x9f" or unicodedata.category(c) == "Cf"))
    return " ".join(text.split())[:limit]


def _outcome(value) -> dict | None:
    """A hook's return value as an outcome, or None if it isn't one: a dict
    with a non-empty `id`. The other fields are made safe to show. A
    `completed` time (core's hold, for a write it made itself) is kept."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"].strip():
        return None
    waited = value.get("waited_seconds")
    if isinstance(waited, bool) or not isinstance(waited, (int, float)) or not math.isfinite(waited):
        waited = None
    # The note quotes the approver; a double quote in the name would end it early.
    approver = _one_line(value.get("approver") or "unknown", 100).replace('"', "'")
    outcome = {"id": _one_line(value["id"], 64), "approver": approver,
               "decided": _one_line(value.get("decided") or "", 40),
               "waited_seconds": None if waited is None else max(0, round(waited))}
    if value.get("completed"):
        outcome["completed"] = _one_line(value["completed"], 40)
    return outcome


def as_outcome(value) -> dict | None:
    """_outcome, for core's hold to build the note of a queued write."""
    return _outcome(value)


def _held_clause(outcome: dict) -> str:
    """'held for approval (id X), approved by "NAME" at T after N s' (no
    leading capital, no final full stop) plus '; written by the gateway at T'
    for a write core's hold made itself."""
    clause = f"held for approval (id {outcome['id']}), approved by \"{outcome['approver']}\""
    if outcome.get("decided"):
        clause += f" at {outcome['decided']}"
    if outcome.get("waited_seconds") is not None:
        clause += f" after {outcome['waited_seconds']} s"
    if outcome.get("completed"):
        clause += f"; written by the gateway at {outcome['completed']}"
    return clause


def held_note(outcome: dict | None) -> str:
    """The standalone sentence for a held write:
    'Held for approval (id X), approved by "NAME" at T after N s.' Empty for
    a write that wasn't held (outcome None). Tools use with_held_note."""
    if outcome is None:
        return ""
    clause = _held_clause(outcome)
    return f"{clause[0].upper()}{clause[1:]}."


def with_held_note(text: str, outcome: dict | None) -> str:
    """A tool's result with the hold folded into its first line, so the one
    line says both what happened and that it was held:
    'Pushed 2 commit(s) to o/r:b (sha); held for approval (id X), approved by
    "NAME" at T after N s.' A first line that introduces the lines below it
    ('Push rejected by GitHub:') keeps its colon at the end, after the
    clause. The rest of the result is unchanged, and so is the whole result
    when the write wasn't held. A result with no first line gets the
    standalone sentence in front of it."""
    if outcome is None:
        return text
    if not text.strip():
        return held_note(outcome)
    if text.startswith("\n"):
        return held_note(outcome) + text
    first, sep, rest = text.partition("\n")
    end = "."
    if first.endswith(":"):
        first, end = first[:-1], ":"
    elif first.endswith("."):
        first = first[:-1]
    return f"{first}; {_held_clause(outcome)}{end}{sep}{rest}"


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
