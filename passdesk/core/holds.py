"""The hold: an external write waits for a person's decision.

Design: docs/approvals.md. In short: the card gets an id from its stable
fields; it is recorded under STATE/approvals/pending/; the deployment's
console writes STATE/approvals/decisions/<id>.json, echoing the card's
`nonce` so a decision never applies to a later card with the same id; the
hook waits up to `hold_seconds` for that file. An approved hold returns the
outcome (id, approver, when, how long it waited) so the tool can say in its
result that the write was held (doomtickle/passdesk-ops#141).

Still undecided after `hold_seconds`:
- a write whose tool passed a job (core.approvals.require; git_push,
  github_create_pr, gitlab_create_mr) is queued (doomtickle/passdesk-ops#91):
  the job goes to STATE/approvals/queued/<id>.json and the hook raises
  `Queued`, a result rather than a refusal. On approval the gateway makes
  the write itself (`run_queued`, from the sweeper or a retry of the same
  call) with the tool's registered completer, and files the outcome under
  done/ (`queued`, `write` = running/done/failed/skipped, `result`,
  `finished`). A retry of the same call reports the queued status, then the
  result (a decline, a failure once, a written result until
  `expire_seconds` after it was written); see `_settled`.
- any other write is refused with a request to retry the same call later.
  A retry with the same card has the same id, so a decision made meanwhile
  is honoured.

Pending cards expire after
`expire_seconds`; an expired card is normally audited as `approval-expired`
and filed under STATE/approvals/done/ as `expired` (with `decision`,
`approver`, `reason` and `decided` added if a decision had arrived in time
to be seen), whether it is caught on a later retry of the same call or by
a sweep over the rest of `pending/`. The sweep runs inside every held call,
once at server start-up and every `SWEEP_SECONDS` on a daemon thread
(`start_sweeper`), so an expired card leaves `pending/` even while no agent
is calling; the idle sweep also runs approved queued writes and prunes
patches no pending card names (core/approvals.py). The record itself is
still a best effort, not a guarantee: a decision written in the narrow window between a read and the
files being removed, or one that arrives after the card has already been
filed as expired, can still go unrecorded.

Policy, all optional, in `[approvals]`:
  tools = ["git_push", "github_create_pr", "gitlab_create_mr"]   which writes are held
  hold_seconds = 45        how long one call waits before queueing (or asking for a retry)
  expire_seconds = 86400   how long a pending card stays decidable (60 s to 7 days)

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
import math
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from . import approvals as core_approvals
from . import config, filters, notify
from .audit import audit
from .errors import Denied, Queued
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
# How often the idle sweep (start_sweeper) runs approved queued writes and
# looks for expired cards: an approved queued write goes out within about
# this long of the decision (one listdir of queued/ and pending/ a tick).
SWEEP_SECONDS = 5.0
# [approvals].expire_seconds: the default and the bounds (docs/decisions.md,
# 2026-10-22). A day covers an overnight agent; past a week the base has
# usually moved on and the mirror ref and patch would linger for nothing.
EXPIRE_DEFAULT = 86_400
EXPIRE_MIN = 60
EXPIRE_MAX = 7 * 86_400
# Every card id card_id makes; a queued/ file named otherwise is ignored.
_CID_RE = re.compile(r"[0-9a-f]{16}")
_RESULT_MAX = 3000

# Reentrant: _expire_pending is called both standalone (the sweep) and from
# inside _ensure_pending's own locked section (a card expiring on retry).
_lock = threading.RLock()
# Ids of the queued writes this process is making right now (under _lock); a
# done/ record still `running` that isn't here was cut off by a restart.
_running: set[str] = set()


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
    expire = min(EXPIRE_MAX, max(EXPIRE_MIN, _seconds(a.get("expire_seconds"), EXPIRE_DEFAULT)))
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
        queued = _read(_path("queued", cid))
        if queued and queued.get("nonce") == pending.get("nonce"):
            event["queued"] = True
            done.update({"queued": True, "write": "skipped", "finished": time.time(),
                         "result": "the card expired before it was approved; nothing was written"})
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
        _remove(_path("queued", cid))
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


def _sweep_safely(now: float) -> None:
    """_sweep_expired that never raises: a failure (a full disk, a
    permission error under STATE) is logged by type only and the next sweep
    tries again, so it can neither fail the held call that runs it nor stop
    the idle sweeper."""
    try:
        _sweep_expired(now)
    except Exception as e:  # noqa: BLE001 - nothing here may escape
        print(f"approvals: the expiry sweep failed: {type(e).__name__}", file=sys.stderr)


def sweep_idle(now: float | None = None) -> None:
    """One idle sweep: make the approved queued writes and file the declined
    ones (run_queued), expire past-due cards (as a held call's sweep does),
    then prune the patches that leaves unnamed (core/approvals.py keeps any
    younger than PATCH_KEEP_SECONDS). Does nothing unless this module's hook
    is the installed one: with holds off there are no cards, and a plugin
    that replaced the hook owns whatever is pending. Never raises."""
    if not core_approvals.hook_is(hook):
        return
    now = time.time() if now is None else now
    # Approved queued writes first, so one approved just before its card's
    # expiry is made rather than filed as expired.
    run_queued(now)
    _sweep_safely(now)
    try:
        core_approvals.prune_patches(now)
    except Exception as e:  # noqa: BLE001 - as above
        print(f"approvals: pruning patches failed: {type(e).__name__}", file=sys.stderr)


def _sweep_loop(stop: threading.Event, interval: float, clock) -> None:
    while not stop.wait(interval):
        sweep_idle(clock())


def start_sweeper(interval: float | None = None, clock=time.time,
                  stop: threading.Event | None = None) -> threading.Thread | None:
    """Sweep once now, then every `interval` seconds (SWEEP_SECONDS) on a
    daemon thread until `stop` is set. Called by the server at start-up,
    after plugins load; returns None without starting anything unless this
    module's hook is installed. A card that expired while the server was
    down (or idle) is filed before the first call is served, a write approved
    meanwhile is made, and one cut off by the restart is filed as failed
    (_recover_interrupted)."""
    if not core_approvals.hook_is(hook):
        return None
    _recover_interrupted()
    sweep_idle(clock())
    thread = threading.Thread(target=_sweep_loop, name="approvals-sweeper", daemon=True,
                              args=(stop or threading.Event(), SWEEP_SECONDS if interval is None else interval,
                                    clock))
    thread.start()
    return thread


def _ensure_pending(card: dict, cid: str, expire: int, now: float, queueable: bool = False) -> dict:
    """The pending card for cid: the existing one, or a fresh one (also when
    the existing one has expired). Notifies once per fresh card, off this
    thread, so a slow webhook never eats into the hold. For a queueable
    call, a settled queued outcome is reported (_report_settled) instead of
    making a fresh card: the queue's runner may have claimed the card since
    the hook last looked, and a second card would make the write twice."""
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
                _remove(_path("queued", cid))
                audit({"event": "approval-discarded", "id": cid, "tool": existing.get("tool"),
                       "reason": "the pending card's expiry is unreadable"})
            else:
                _expire_pending(cid, existing.get("nonce"), now)
        if queueable:
            _report_settled(cid, now, expire)  # under the lock, so no claim slips in before the card
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


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _timestamp(value, now: float) -> float:
    """`value` if it is a usable Unix time, else `now`. Not usable: not a
    number (or a bool), not finite (json reads 1e999 as inf, and NaN), or
    out of gmtime's range (1e20) - any of those would raise after the
    approval was already filed and audited, so the write would never run."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return now
    try:
        if not math.isfinite(value):
            return now
        time.gmtime(value)
    except (OverflowError, ValueError, OSError):
        return now
    return value


def _outcome(pending: dict, cid: str, approver: str, ts) -> dict:
    """What an approved hold returns: the card id, the approver, when they
    decided (ISO 8601 UTC) and how long the write waited for them, from the
    card's creation to the decision. A decision time or card creation time
    that isn't a usable Unix time (the console writes time.time()) counts as
    now; see _timestamp."""
    now = time.time()
    decided = _timestamp(ts, now)
    created = _timestamp(pending.get("created"), now)
    return {"id": cid, "approver": approver, "decided": _iso(decided),
            "waited_seconds": max(0, round(decided - created))}


def _consume(decision: dict, pending: dict, cid: str) -> dict:
    """Apply a decision: file the card as done, record it, allow (returning
    the outcome, see _outcome) or refuse.

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
        _remove(_path("queued", cid))
    record = {"id": cid, "tool": pending["tool"], "role": pending.get("principal"), "approver": approver,
              "reason": reason}
    if verdict == "approve":
        audit({"event": "approval-granted", **record})
        return _outcome(pending, cid, approver, decision.get("ts"))
    audit({"event": "approval-declined", **record})
    raise Denied(f"declined by {approver}" + (f": {reason}" if reason else ""))


# --- the approve-later queue (doomtickle/passdesk-ops#91) -------------------

def _span(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"


def _what(card: dict) -> str:
    """The write a card describes, for the queued result and the webhook."""
    tool = card.get("tool")
    if tool == "git_push":
        return f"push of {card.get('branch')} to {card.get('repo')}"
    if tool in ("github_create_pr", "gitlab_create_mr"):
        noun = "merge request" if tool == "gitlab_create_mr" else "pull request"
        return f"{noun} {card.get('head')} -> {card.get('base')} on {card.get('repo')}"
    return f"{tool} call"


def _queued_text(pending: dict, cid: str) -> str:
    """The result an agent gets for a queued write, and for a retry of it."""
    now = time.time()
    expires = _timestamp(pending.get("expires"), now)
    return (f"Queued for approval (id {cid}); nothing has been sent yet. The gateway has verified this "
            f"{_what(pending)} and will make it itself once a person approves it (e.g. `factory approvals "
            f"approve {cid}`); if it is declined, or not decided by {_iso(expires)} (in "
            f"{_span(expires - now)}), nothing is written. Report it as queued, not done. Calling this exact "
            f"tool again with the same arguments reports its status, and its result once the gateway has "
            f"made it.")


def _running_text(cid: str) -> str:
    return (f"Approved (id {cid}); the gateway is making this write now. Call this exact tool again with the "
            f"same arguments in a minute for its result; until then, report it as queued, not done.")


def _queued_job(cid: str, pending: dict) -> dict | None:
    """The queued/ record of this exact pending card (same id and nonce)."""
    rec = _read(_path("queued", cid))
    if rec and rec.get("id") == cid and rec.get("nonce") == pending.get("nonce"):
        return rec
    return None


def _queue(pending: dict, cid: str, job: dict) -> bool:
    """Store the job of a write still undecided after the hold, so the
    gateway can make it on approval. False (nothing stored) if a decision
    arrived in the meantime: the caller applies it in this call, so the
    write happens once, either here or from the queue. Under the lock, so
    the queue's runner (which acts only on a stored job) can't race it."""
    with _lock:
        current = _read(_path("pending", cid))
        if not current or current.get("nonce") != pending.get("nonce"):
            raise Denied(f"the approval card {cid} was already decided or has expired; retry the same call")
        if _decision_for(current, cid):
            return False
        _write_atomic(_path("queued", cid), {"id": cid, "nonce": pending.get("nonce"), "tool": pending.get("tool"),
                                             "queued": time.time(), "job": job})
    audit({"event": "approval-queued", "id": cid, "tool": pending.get("tool"), "role": pending.get("principal"),
           "repo": pending.get("repo"), "branch": pending.get("branch") or pending.get("head")})
    return True


def _settled(cid: str, now: float, expire: int) -> dict | None:
    """The done/ record of this write's queued outcome, if a retry should
    report it rather than ask again: the write is being made, was made (for
    `expire` seconds after), was declined (likewise) or failed (reported
    once; the call after that is a new request). An expired card is not
    reported: the retry makes a fresh one, as before. Only a record that is
    still this card (its stable fields hash to cid) counts."""
    done = _read(_path("done", cid))
    if not done or done.get("queued") is not True or card_id(done) != cid:
        return None
    write = done.get("write")
    if write == "running":
        return done
    finished = done.get("finished")
    if isinstance(finished, bool) or not isinstance(finished, (int, float)) or not math.isfinite(finished) \
            or now - finished > expire:
        return None
    if write == "done" or (write == "skipped" and done.get("decision") != "expired") \
            or (write == "failed" and not done.get("reported")):
        return done
    return None


def _report_settled(cid: str, now: float, expire: int) -> None:
    """Raise what a retry of a settled queued write gets (see _settled):
    Queued with the result (or that it is being made), or Denied."""
    done = _settled(cid, now, expire)
    if done is None:
        return
    write = done.get("write")
    if write == "running":
        raise Queued(_running_text(cid), cid)
    result = filters.sanitize(str(done.get("result") or ""), _RESULT_MAX)
    if write == "done":
        raw = done.get("outcome") if isinstance(done.get("outcome"), dict) else {}
        outcome = core_approvals.as_outcome({**raw, "id": cid, "approver": raw.get("approver") or done.get("approver"),
                                             "completed": _iso(_timestamp(done.get("finished"), now))})
        raise Queued(f"{result}\n{core_approvals.held_note(outcome)}", cid, "completed")
    if write == "failed":
        with _lock:
            current = _read(_path("done", cid))
            if current and current.get("nonce") == done.get("nonce") and current.get("write") == "failed":
                _write_atomic(_path("done", cid), {**current, "reported": True})
        raise Denied(f"the gateway could not make the approved write (id {cid}): {result}\n"
                     f"A new call with the same arguments starts a new approval.")
    who = core_approvals.as_outcome({"id": cid, "approver": done.get("approver") or "unknown"})["approver"]
    reason = filters.sanitize(" ".join(str(done.get("reason") or "").split()), 500)
    raise Denied(f"declined by {who}" + (f": {reason}" if reason else "") + f" (approval {cid}, queued)")


def _check_queued(cid: str, pending: dict, rec: dict) -> str | None:
    """Why a stored card and job can't be trusted to make a write, or None.
    The completer then checks the job against the card and the policy."""
    if pending.get("id") != cid or card_id(pending) != cid:
        return "the stored approval card does not match its id; nothing was written"
    if rec.get("tool") != pending.get("tool") or not isinstance(rec.get("job"), dict):
        return "the stored job does not match its approval card; nothing was written"
    if core_approvals.deferred(str(pending.get("tool"))) is None:
        return f"the gateway cannot make a queued {pending.get('tool')} write; nothing was written"
    return None


def _claim(cid: str, now: float) -> tuple[list[dict], tuple[dict, dict, dict] | None]:
    """Under the lock, settle the queued write cid if it can be: returns the
    audit events to record and, for an approved write to make now, its
    `running` done/ record, its stored card and its job (else None). The card, the decision and the job
    leave pending/, decisions/ and queued/ in the same locked step that
    files the done/ record, so a write is claimed exactly once, by the
    sweeper or by a retry. Nothing happens while the card is undecided."""
    with _lock:
        rec = _read(_path("queued", cid))
        if not rec:
            return [], None
        pending = _read(_path("pending", cid))
        if not pending or rec.get("nonce") != pending.get("nonce"):
            # Nothing removes one without the other but a hand edit or a
            # crash: no card, nothing to approve, nothing written.
            _remove(_path("queued", cid))
            return [{"event": "approval-write-failed", "id": cid, "tool": rec.get("tool"),
                     "reason": "the queued write has no matching pending card; nothing was written"}], None
        try:
            expires = float(pending.get("expires"))
        except (TypeError, ValueError):
            # Never make a write on a card whose expiry can't be read;
            # discard it as a retry would (_ensure_pending).
            for kind in ("pending", "decisions", "queued"):
                _remove(_path(kind, cid))
            return [{"event": "approval-discarded", "id": cid, "tool": pending.get("tool"), "queued": True,
                     "reason": "the pending card's expiry is unreadable; nothing was written"}], None
        if expires <= now:
            _expire_pending(cid, pending.get("nonce"), now)  # audits it itself
            return [], None
        decision = _read(_path("decisions", cid))
        if not decision:
            return [], None
        if decision.get("nonce") != pending.get("nonce"):
            _remove(_path("decisions", cid))  # stale: decided an earlier card with this id
            return [], None
        approver = str(decision.get("approver") or "unknown")
        reason = str(decision.get("reason") or "")
        verdict = str(decision.get("decision") or "")
        decided = decision.get("ts")
        done = {**pending, "decision": verdict, "approver": approver, "reason": reason,
                "decided": str(decided) if decided is not None else None, "queued": True}
        record = {"id": cid, "tool": pending.get("tool"), "role": pending.get("principal"), "approver": approver,
                  "reason": reason, "queued": True}
        problem = _check_queued(cid, pending, rec)
        if verdict != "approve":
            done.update(write="skipped", finished=time.time(), result="declined; nothing was written")
            events = [{"event": "approval-declined", **record}]
        elif problem:
            done.update(write="failed", finished=time.time(), result=problem)
            events = [{"event": "approval-granted", **record},
                      {"event": "approval-write-failed", "id": cid, "tool": pending.get("tool"), "reason": problem}]
        else:
            done.update(write="running", started=time.time(), outcome=_outcome(pending, cid, approver, decided))
            events = [{"event": "approval-granted", **record}]
            _running.add(cid)
        _write_atomic(_path("done", cid), done)
        for kind in ("pending", "decisions", "queued"):
            _remove(_path(kind, cid))
    return events, ((done, pending, rec["job"]) if done["write"] == "running" else None)


def _run_one(cid: str, now: float) -> None:
    """Act on the queued write cid if its card has a decision (_claim):
    declined, it is filed; approved, the tool's completer makes it, outside
    the lock (it takes the tool lock and talks to the forge), from the
    stored job and card only. The outcome goes to done/<cid>.json and the
    audit log, and a write made (or failed) is posted to the webhook."""
    events, work = _claim(cid, now)
    for event in events:
        audit(event)
    if work is None:
        return
    running, card, job = work
    try:
        complete = core_approvals.deferred(card["tool"])
        result, state = filters.sanitize(complete(job, card, load_policy()), _RESULT_MAX), "done"
    except Denied as e:
        result, state = filters.sanitize(str(e), _RESULT_MAX), "failed"
    except Exception as e:  # noqa: BLE001 - every failure is recorded, none escapes
        result, state = filters.sanitize(f"{type(e).__name__}: {e}", _RESULT_MAX), "failed"
    with _lock:
        _write_atomic(_path("done", cid), {**running, "write": state, "result": result, "finished": time.time()})
        _running.discard(cid)
    target = card.get("branch") or card.get("head")
    if state == "done":
        audit({"event": "approval-write-done", "id": cid, "tool": card["tool"], "repo": card.get("repo"),
               "branch": target, "result": filters.sanitize(result, 500)})
    else:
        audit({"event": "approval-write-failed", "id": cid, "tool": card["tool"], "repo": card.get("repo"),
               "branch": target, "reason": filters.sanitize(result, 500)})
    notify.send_result_async(card, cid, _what(card), state == "done", result)


def run_queued(now: float | None = None) -> None:
    """Act on every queued write that has a decision (_run_one). Never
    raises: a failure is logged by type and the next sweep tries again."""
    now = time.time() if now is None else now
    try:
        cids = sorted(p.stem for p in (base_dir() / "queued").glob("*.json") if _CID_RE.fullmatch(p.stem))
    except OSError:
        return
    for cid in cids:
        try:
            _run_one(cid, now)
        except Exception as e:  # noqa: BLE001 - one bad job must not stop the others
            print(f"approvals: a queued write failed to run: {type(e).__name__}", file=sys.stderr)


def _recover_interrupted() -> None:
    """At start-up: a queued write still `running` in done/ was cut off by a
    restart. It may or may not have reached the forge, so it is filed as
    failed (never retried) and audited. Never raises."""
    try:
        paths = list((base_dir() / "done").glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            with _lock:
                rec = _read(path)
                if not rec or rec.get("queued") is not True or rec.get("write") != "running" \
                        or path.stem in _running:
                    continue
                why = ("the gateway stopped while making this write; it may or may not have reached the forge, "
                       "so check there before calling again")
                _write_atomic(path, {**rec, "write": "failed", "result": why, "finished": time.time()})
            audit({"event": "approval-write-failed", "id": path.stem, "tool": rec.get("tool"), "reason": why})
        except Exception as e:  # noqa: BLE001 - as above
            print(f"approvals: recovering a queued write failed: {type(e).__name__}", file=sys.stderr)


def hook(card: dict) -> dict | None:
    """The approval point's hook (core.approvals.set_hook). Returns None for
    a tool the policy doesn't hold, the outcome (_outcome) for a write that
    was held and approved (in this call, or before this retry); raises
    Denied for a decline or a hold that is still pending, and Queued for a
    write the gateway will make (or has made) itself."""
    policy = load_policy()
    hold, expire, tools = settings(policy)
    if card.get("tool") not in tools:
        return None
    cid = card_id(card)
    now = time.time()
    _sweep_safely(now)  # other cards nobody retried: give them a record too
    job = core_approvals.job()
    queueable = isinstance(job, dict) and core_approvals.deferred(card["tool"]) is not None
    if queueable:
        _report_settled(cid, now, expire)  # this write's queued outcome, if there is one
    pending = _ensure_pending(card, cid, expire, now, queueable)
    if queueable and _queued_job(cid, pending):
        # A retry while queued: apply a decision made since (the write is
        # then made here, once), else report the same queued status.
        _run_one(cid, time.time())
        _report_settled(cid, time.time(), expire)
        raise Queued(_queued_text(pending, cid), cid)
    deadline = time.monotonic() + hold
    while True:
        decision = _decision_for(pending, cid)
        if decision:
            return _consume(decision, pending, cid)
        if time.monotonic() >= deadline or time.time() >= float(pending["expires"]):
            break
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    if queueable:
        if _queue(pending, cid, job):
            raise Queued(_queued_text(pending, cid), cid)
        # Decided just as the hold ran out: applied in this call, as above.
        decision = _decision_for(pending, cid)
        if decision:
            return _consume(decision, pending, cid)
    minutes = max(1, int((float(pending["expires"]) - time.time()) // 60))
    raise Denied(f"held for approval (id {cid}). A person must decide within {minutes} min "
                 f"(with the deployment's approver console, e.g. `factory approvals approve {cid}` "
                 f"or `factory approvals decline {cid}`); then retry this exact call. Nothing was sent.")
