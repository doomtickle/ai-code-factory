"""The hold: an external write waits for a person's decision.

Design: docs/approvals.md. In short: the card gets an id from its stable
fields; it is recorded under STATE/approvals/pending/; the deployment's
console writes STATE/approvals/decisions/<id>.json, echoing the card's
`nonce` so a decision never applies to a later card with the same id; the
hook waits up to `hold_seconds` for that file, then tells the agent to retry
the same call later. A retry with the same card has the same id, so a
decision made meanwhile is honoured. Pending cards expire after
`expire_seconds`; an expired card is normally audited as `approval-expired`
and filed under STATE/approvals/done/ as `expired` (with `decision`,
`approver`, `reason` and `decided` added if a decision had arrived in time
to be seen), whether it is caught on a later retry of the same call or by
the sweep every held call runs over the rest of `pending/`. That is a best
effort, not a guarantee: a decision written in the narrow window between a
read and the files being removed, or one that arrives after the card has
already been filed as expired, can still go unrecorded.

Policy, all optional, in `[approvals]`:
  tools = ["git_push", "github_create_pr", "gitlab_create_mr"]   which writes are held
  hold_seconds = 45        how long one call waits before asking for a retry
  expire_seconds = 1800    how long a pending card stays decidable

Holds are off until `tools` names at least one tool: `install()` (called at
start-up) puts the hook on the approval point only then, so a gateway whose
policy has no `[approvals].tools` behaves exactly as one without this
module, writes no patch files and holds nothing. The hold was Passdesk
Pro's; it moved here with the licence removed (docs/decisions.md,
2026-10-21).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from . import approvals as core_approvals
from . import config, notify
from .audit import audit
from .errors import Denied
from .policy import load_policy

# The fields that identify "the same write": a retry with the same bundle
# (its digest) to the same branch is the same card.
_STABLE_FIELDS = {
    "git_push": ("tool", "principal", "repo", "branch", "base", "head", "bundle_sha256"),
    "github_create_pr": ("tool", "principal", "repo", "head", "base", "title", "body", "draft"),
    # The MR card carries the whole body's digest (its "body" is an excerpt).
    "gitlab_create_mr": ("tool", "principal", "repo", "head", "base", "title", "body", "body_sha256", "draft"),
}
# What a deployment that wants holds usually lists (the README's example);
# nothing is held until the policy says so.
SUGGESTED_TOOLS = ["git_push", "github_create_pr", "gitlab_create_mr"]
POLL_SECONDS = 1.0

# Reentrant: _expire_pending is called both standalone (the sweep) and from
# inside _ensure_pending's own locked section (a card expiring on retry).
_lock = threading.RLock()


def card_id(card: dict) -> str:
    fields = _STABLE_FIELDS.get(card.get("tool", ""), tuple(sorted(card)))
    stable = {k: card.get(k) for k in fields}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:16]


def settings(policy: dict) -> tuple[int, int, list[str]]:
    """(hold_seconds, expire_seconds, tools) from `[approvals]`, with the
    defaults above; `tools` is empty (holds off) unless the policy lists
    tool names."""
    a = policy.get("approvals", {}) if isinstance(policy.get("approvals"), dict) else {}
    hold = max(0, _seconds(a.get("hold_seconds"), 45))
    expire = max(60, _seconds(a.get("expire_seconds"), 1800))
    tools = a.get("tools", [])
    if not isinstance(tools, list):
        tools = []
    return hold, expire, [t for t in tools if isinstance(t, str) and t]


def _seconds(value, default: int) -> int:
    """A whole number of seconds, or the default for anything else (a bool,
    a string, a list): the doctor reports the odd value; the hold must not
    crash on it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def enabled(policy: dict) -> bool:
    return bool(settings(policy)[2])


def install(policy: dict) -> bool:
    """Put the hook on the approval point if the policy holds anything.
    Returns whether it did. Called once at start-up, after the policy loads
    and before plugins (a plugin's own hook replaces this one)."""
    hold, expire, tools = settings(policy)
    if not tools:
        return False
    core_approvals.set_hook(hook)
    audit({"event": "approvals-enabled", "tools": tools, "hold_seconds": hold, "expire_seconds": expire})
    return True


def base_dir() -> Path:
    return config.STATE / "approvals"


def _path(kind: str, cid: str) -> Path:
    return base_dir() / kind / f"{cid}.json"


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _expire_pending(cid: str, nonce: str | None, now: float) -> None:
    """Record a pending card past its expiry: audit it (with any decision
    that arrived too late to be applied, matched by nonce) and file it
    under done/ as `expired`, so an un-retried card still leaves a record. A
    decision with a non-matching nonce stays unrecorded, same as a live
    retry would treat it.

    Re-reads the pending file fresh under the lock and acts only if it is
    still the exact card the caller saw (same nonce) and is still past its
    own `expires`. A caller's snapshot can be stale (the sweep reads
    outside the lock; a retry's existing card may already have been handled
    by the sweep or another thread by the time this runs), so acting on the
    caller's copy instead of a fresh read could delete or re-expire a card
    that has since been replaced. The nonce check also means a given card
    can never be audited as expired twice: once it is gone or replaced, a
    second caller's re-read no longer matches."""
    with _lock:
        pending = _read(_path("pending", cid))
        if not pending or pending.get("nonce") != nonce:
            return
        try:
            if float(pending.get("expires", 0)) > now:
                return
        except (TypeError, ValueError):
            return
        decision = _read(_path("decisions", cid))
        event = {"event": "approval-expired", "id": cid, "tool": pending.get("tool"), "role": pending.get("principal")}
        done = {**pending, "decision": "expired"}
        if decision and decision.get("nonce") == pending.get("nonce"):
            approver = str(decision.get("approver") or "unknown")
            reason = str(decision.get("reason") or "")
            verdict = str(decision.get("decision") or "")
            decided = decision.get("ts")
            decided = str(decided) if decided is not None else None
            event.update({"decision": verdict, "approver": approver, "reason": reason, "decided": decided})
            done.update({"approver": approver, "reason": reason, "decided": decided})
        _write_atomic(_path("done", cid), done)
        _remove(_path("pending", cid))
        _remove(_path("decisions", cid))
    audit(event)


def _sweep_expired(now: float) -> None:
    """Pending cards past their expiry that nobody has retried yet: give
    each the same treatment as one caught expired on retry, so a call that
    is never retried still reaches the audit log. Cheap: one listdir. A
    pending file that is missing, not a dict, or has an `expires` that
    won't convert to a number is skipped, so one malformed file can't break
    every held call that follows."""
    try:
        cids = [p.stem for p in (base_dir() / "pending").glob("*.json")]
    except OSError:
        return
    for cid in cids:
        pending = _read(_path("pending", cid))
        if not pending:
            continue
        try:
            expired = float(pending.get("expires", 0)) <= now
        except (TypeError, ValueError):
            continue
        if expired:
            _expire_pending(cid, pending.get("nonce"), now)


def _ensure_pending(card: dict, cid: str, expire: int, now: float) -> dict:
    """The pending card for cid: the existing one, or a fresh one (also when
    the existing one has expired). Notifies once per fresh card, off this
    thread, so a slow webhook never eats into the hold."""
    with _lock:
        existing = _read(_path("pending", cid))
        if existing:
            try:
                expires = float(existing.get("expires", 0))
            except (TypeError, ValueError):
                expires = None
            if expires is not None and expires > now:
                return existing
            if expires is None:
                # An unreadable expiry would wedge the card: every retry
                # would fail here and the sweep skips it. Discard it and
                # start over; the decision (if any) can't be trusted either.
                _remove(_path("pending", cid))
                _remove(_path("decisions", cid))
                audit({"event": "approval-discarded", "id": cid, "tool": existing.get("tool"),
                       "reason": "the pending card's expiry is unreadable"})
            else:
                _expire_pending(cid, existing.get("nonce"), now)
        # The nonce ties a decision to this card, not an earlier one with the
        # same id; timestamps would do that only if the clock never jumped.
        pending = {**card, "id": cid, "nonce": secrets.token_hex(8), "created": now, "expires": now + expire}
        _write_atomic(_path("pending", cid), pending)
    audit({"event": "approval-requested", "id": cid, "tool": card["tool"], "role": card.get("principal"),
           "repo": card.get("repo"), "branch": card.get("branch") or card.get("head"),
           "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + expire))})
    notify.send_async(card, cid, max(1, expire // 60))
    return pending


def _decision_for(pending: dict, cid: str) -> dict | None:
    decision = _read(_path("decisions", cid))
    if not decision:
        return None
    if decision.get("nonce") != pending.get("nonce"):
        _remove(_path("decisions", cid))  # stale: decided an earlier card with this id
        return None
    return decision


def _consume(decision: dict, pending: dict, cid: str) -> None:
    """Apply a decision: file the card as done, record it, allow or refuse.

    Re-checks under the lock that pending/<cid> is still the exact card this
    decision was read against (same nonce). The decision was read outside
    the lock, so the card can have expired - and already been filed and
    audited as `approval-expired` by the sweep or a concurrent retry - in
    the time since; applying it anyway would grant or decline a card that
    no longer exists and audit it a second time."""
    approver = str(decision.get("approver") or "unknown")
    reason = str(decision.get("reason") or "")
    verdict = str(decision.get("decision") or "")
    decided = decision.get("ts")
    decided = str(decided) if decided is not None else None
    with _lock:
        current = _read(_path("pending", cid))
        if not current or current.get("nonce") != pending.get("nonce"):
            raise Denied(f"the approval card {cid} was already decided or has expired; retry the same call")
        _write_atomic(_path("done", cid), {**pending, "decision": verdict, "approver": approver, "reason": reason,
                                           "decided": decided})
        _remove(_path("pending", cid))
        _remove(_path("decisions", cid))
    record = {"id": cid, "tool": pending["tool"], "role": pending.get("principal"), "approver": approver,
              "reason": reason}
    if verdict == "approve":
        audit({"event": "approval-granted", **record})
        return
    audit({"event": "approval-declined", **record})
    raise Denied(f"declined by {approver}" + (f": {reason}" if reason else ""))


def hook(card: dict) -> None:
    """The approval point's hook (core.approvals.set_hook)."""
    policy = load_policy()
    hold, expire, tools = settings(policy)
    if card.get("tool") not in tools:
        return
    cid = card_id(card)
    now = time.time()
    _sweep_expired(now)  # other cards nobody retried: give them a record too
    pending = _ensure_pending(card, cid, expire, now)
    deadline = time.monotonic() + hold
    while True:
        decision = _decision_for(pending, cid)
        if decision:
            return _consume(decision, pending, cid)
        if time.monotonic() >= deadline or time.time() >= float(pending["expires"]):
            break
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    minutes = max(1, int((float(pending["expires"]) - time.time()) // 60))
    raise Denied(f"held for approval (id {cid}). A person must decide within {minutes} min "
                 f"(with the deployment's approver console, e.g. `factory approvals approve {cid}` "
                 f"or `factory approvals decline {cid}`); then retry this exact call. Nothing was sent.")
