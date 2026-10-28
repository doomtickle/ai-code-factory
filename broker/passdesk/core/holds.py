"""The hold: an external write is staged for a person's decision, and the
gateway makes it itself once approved (doomtickle/passdesk-ops#170).

Design: docs/approvals.md. In short: the card gets an id from its stable
fields. A held call stages the write at once and never waits: in one locked
step the tool's job (core.approvals.require: the exact, already-checked
arguments of the write) goes to STATE/approvals/queued/<id>.json and the card
to STATE/approvals/pending/<id>.json, and the hook raises `Staged`, a result
rather than a refusal ("Staged for approval (id X); nothing has been sent
yet..."). The deployment's console writes STATE/approvals/decisions/<id>.json,
echoing the card's `nonce` so a decision never applies to a later card with
the same id. The sweeper (`run_queued`, every `SWEEP_SECONDS`) acts on it:
approved, the gateway makes the write with the tool's registered completer
from the stored job and card only, never the agent's workspace; either way
it files the outcome under done/ (`queued`, `write` = running/done/failed/
skipped, `result`, `finished`). The same call again while its card is
pending gets the same staged result (no second card or notification); once
the write is made it gets that write's stored result (`Made`: the PR URL,
the pushed ref) for as long as its done/ record exists; while it is being
made, or once declined (for `expire_seconds`), it is refused.
Either way nothing is written twice. After a failure or an expiry it stages
a fresh card.

Pending cards expire after `expire_seconds`; an expired card is audited as
`approval-expired` and filed under STATE/approvals/done/ as `expired` (with
`decision`, `approver`, `reason` and `decided` added if a decision had
arrived in time to be seen). The sweep runs inside every held call, once at
server start-up and every `SWEEP_SECONDS` on a daemon thread
(`start_sweeper`), which also prunes patches no pending card names
(core/approvals.py). The record itself is still a best effort, not a
guarantee: a decision that arrives after the card has already been filed as
expired goes unrecorded.

Policy, all optional, in `[approvals]`:
  tools = ["git_push", "github_create_pr", "gitlab_create_mr"]   which writes are held
  expire_seconds = 86400   how long a pending card stays decidable (60 s to 7 days)

(`hold_seconds`, how long a call used to wait for a decision, is gone; the
doctor says so and the gateway ignores it.) Holds are off until `tools`
names at least one tool: `install()` (called at start-up) puts the hook on
the approval point only then, so a gateway whose policy has no
`[approvals].tools` behaves exactly as one without this module, writes no
patch files and holds nothing. The hold was Passdesk Pro's; it moved here
with the licence removed (docs/decisions.md, 2026-10-21).
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
from .errors import Denied, Made, Staged
from .policy import load_policy

# The fields that identify "the same write": the same call again with the
# same bundle (its digest) to the same branch is the same card.
_STABLE_FIELDS = {
    "git_push": ("tool", "principal", "repo", "branch", "base", "head", "bundle_sha256"),
    # The PR and MR cards carry the whole body's digest (their "body" is an
    # excerpt), so bodies that differ only past it are different cards.
    "github_create_pr": ("tool", "principal", "repo", "head", "base", "title", "body", "body_sha256", "draft"),
    "gitlab_create_mr": ("tool", "principal", "repo", "head", "base", "title", "body", "body_sha256", "draft"),
    # An absent title or body is None, which is not the same card as an empty one.
    "github_update_pr": ("tool", "principal", "repo", "number", "title", "body", "body_sha256"),
    "github_create_issue": ("tool", "principal", "repo", "title", "body", "body_sha256", "labels"),
}
# What a deployment that wants holds usually lists (the README's example);
# nothing is held until the policy says so.
SUGGESTED_TOOLS = ["git_push", "github_create_pr", "gitlab_create_mr"]
# How often the sweep (start_sweeper) runs approved writes and looks for
# expired cards: an approved write goes out within about this long of the
# decision (one listdir of queued/ and pending/ a tick).
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
# inside _stage's own locked section (a card expiring as it is staged again).
_lock = threading.RLock()
# Ids of the staged writes this process is making right now (under _lock); a
# done/ record still `running` that isn't here was cut off by a restart.
_running: set[str] = set()


# What the hold adds to a stored card; never part of its id.
_OWN_FIELDS = {"id", "nonce", "created", "expires"}


def card_id(card: dict) -> str:
    """The card's id: a hash of its stable fields (every field the tool put
    on it, for a tool not listed above), so the stored card hashes to it too."""
    fields = _STABLE_FIELDS.get(card.get("tool", ""), tuple(sorted(k for k in card if k not in _OWN_FIELDS)))
    stable = {k: card.get(k) for k in fields}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:16]


def settings(policy: dict) -> tuple[int, list[str]]:
    """(expire_seconds, tools) from `[approvals]`, with the defaults above;
    `tools` is empty (holds off) unless the policy lists tool names."""
    a = policy.get("approvals", {}) if isinstance(policy.get("approvals"), dict) else {}
    expire = min(EXPIRE_MAX, max(EXPIRE_MIN, _seconds(a.get("expire_seconds"), EXPIRE_DEFAULT)))
    tools = a.get("tools", [])
    if not isinstance(tools, list):
        tools = []
    return expire, [t for t in tools if isinstance(t, str) and t]


def _seconds(value, default: int) -> int:
    """A whole number of seconds, or the default for anything else (a bool,
    a string, a list): the doctor reports the odd value; the hold must not
    crash on it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def enabled(policy: dict) -> bool:
    return bool(settings(policy)[1])


def install(policy: dict) -> bool:
    """Put the hook on the approval point if the policy holds anything.
    Returns whether it did. Called once at start-up, after the policy loads
    and before plugins (a plugin's own hook replaces this one)."""
    expire, tools = settings(policy)
    if not tools:
        return False
    core_approvals.set_hook(hook)
    audit({"event": "approvals-enabled", "tools": tools, "expire_seconds": expire})
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
    under done/ as `expired`, so an undecided card still leaves a record. A
    decision with a non-matching nonce stays unrecorded.

    Re-reads the pending file fresh under the lock and acts only if it is
    still the exact card the caller saw (same nonce) and is still past its
    own `expires`. A caller's snapshot can be stale (the sweep reads
    outside the lock; a card the same call meets again may already have been
    handled by the sweep or another thread by the time this runs), so acting on the
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
    """Pending cards past their expiry: audit and file each (_expire_pending),
    so every card reaches the audit log. Cheap: one listdir. A
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
    """One idle sweep: make the approved staged writes and file the declined
    ones (run_queued), expire past-due cards (as a held call's sweep does),
    then prune the patches that leaves unnamed (core/approvals.py keeps any
    younger than PATCH_KEEP_SECONDS). Does nothing unless this module's hook
    is the installed one: with holds off there are no cards, and a plugin
    that replaced the hook owns whatever is pending. Never raises."""
    if not core_approvals.hook_is(hook):
        return
    now = time.time() if now is None else now
    # Approved writes first, so one approved just before its card's
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


def _span(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"


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
    """The approval as a done/ record keeps it: the card id, the approver,
    when they decided (ISO 8601 UTC) and how long the write waited for them,
    from the card's creation to the decision. A decision time or card
    creation time that isn't a usable Unix time (the console writes
    time.time()) counts as now; see _timestamp."""
    now = time.time()
    decided = _timestamp(ts, now)
    created = _timestamp(pending.get("created"), now)
    return {"id": cid, "approver": approver, "decided": _iso(decided),
            "waited_seconds": max(0, round(decided - created))}


def _what(card: dict) -> str:
    """The write a card describes, for the staged result and the webhook."""
    tool = card.get("tool")
    if tool == "git_push":
        return f"push of {card.get('branch')} to {card.get('repo')}"
    if tool in ("github_create_pr", "gitlab_create_mr"):
        noun = "merge request" if tool == "gitlab_create_mr" else "pull request"
        return f"{noun} {card.get('head')} -> {card.get('base')} on {card.get('repo')}"
    if tool == "github_update_pr":
        return f"update of pull request #{card.get('number')} on {card.get('repo')}"
    if tool == "github_create_issue":
        return f"issue on {card.get('repo')}"
    return f"{tool} call"


def _staged_text(pending: dict, cid: str) -> str:
    """The one result an agent gets for a held write (and for the same call
    again while its card is pending)."""
    now = time.time()
    expires = _timestamp(pending.get("expires"), now)
    return (f"Staged for approval (id {cid}); nothing has been sent yet. The gateway has verified this "
            f"{_what(pending)} and will make it itself once a person approves it (e.g. `factory approvals "
            f"approve {cid}`); if it is declined, or not decided by {_iso(expires)} (in "
            f"{_span(expires - now)}), nothing is written. Report it as staged for approval (id {cid}), not "
            f"done. The gateway records the outcome with the approval (`factory approvals`); once it has made "
            f"the write, the same call again returns its result (e.g. the URL) and sends nothing new.")


def _queued_job(cid: str, pending: dict) -> dict | None:
    """The queued/ record of this exact pending card (same id and nonce)."""
    rec = _read(_path("queued", cid))
    if rec and rec.get("id") == cid and rec.get("nonce") == pending.get("nonce"):
        return rec
    return None


def _settled(cid: str, now: float, expire: int) -> None:
    """The same call again once its staged write is settled: made, it raises
    Made with the write's stored result for as long as the done/ record
    exists (a fresh card, approved again, would make the write twice: a
    second issue); being made, or declined (for `expire` seconds after it
    finished), it raises Denied. Either way nothing is written twice and a
    decline is not just asked again. A failure, an expiry or a decline past
    that window lets a fresh card be staged (returns). Only a done/ record that
    is this card counts: its stored id is cid (the record's own fields,
    the decision and the result, are not part of the card's id)."""
    done = _read(_path("done", cid))
    if not done or done.get("queued") is not True or done.get("id") != cid:
        return
    write = done.get("write")
    if write == "running":
        raise Denied(f"this exact write was approved (approval {cid}) and the gateway is making it now; nothing "
                     f"new was staged. Its outcome is recorded with the approval (`factory approvals`).")
    finished = done.get("finished")
    if write == "done":
        result = filters.sanitize(str(done.get("result") or ""), _RESULT_MAX)
        raise Made(f"Made by the gateway at {_iso(_timestamp(finished, now))} once approved (approval {cid}); "
                   f"this call staged and sent nothing new. The write's result:\n{result}", cid)
    if isinstance(finished, bool) or not isinstance(finished, (int, float)) or not math.isfinite(finished) \
            or now - finished > expire:
        return
    if write == "skipped" and done.get("decision") != "expired":
        who = core_approvals.one_line(done.get("approver") or "unknown", 100)
        reason = filters.sanitize(" ".join(str(done.get("reason") or "").split()), 500)
        raise Denied(f"declined by {who}" + (f": {reason}" if reason else "")
                     + f" (approval {cid}); nothing was written and nothing new was staged.")


def _stage(card: dict, cid: str, job: dict, expire: int, now: float) -> dict:
    """The pending card for cid with its job staged: the existing one if it
    is still pending with its job (the same call again), else a fresh one.
    The job is written before the card, in one locked step, so a pending
    card always has its job by the time anyone can read it. Notifies once
    per fresh card, off this thread, so a slow webhook never delays the
    call. Raises Made or Denied if this write is already settled (_settled)."""
    with _lock:
        existing = _read(_path("pending", cid))
        if existing:
            try:
                expires = float(existing.get("expires", 0))
            except (TypeError, ValueError):
                expires = None
            if expires is not None and expires > now and _queued_job(cid, existing):
                return existing
            if expires is not None and expires <= now:
                _expire_pending(cid, existing.get("nonce"), now)
            else:
                # An unreadable expiry would wedge the card (the sweep skips
                # it), and a card with no job of its own (left by a gateway
                # that held calls open) can't be made by this one. Discard it
                # and start over; its decision (if any) can't be trusted either.
                for kind in ("pending", "decisions", "queued"):
                    _remove(_path(kind, cid))
                audit({"event": "approval-discarded", "id": cid, "tool": existing.get("tool"),
                       "reason": "the pending card's expiry is unreadable" if expires is None
                       else "the pending card has no staged job"})
        _settled(cid, now, expire)
        # The nonce ties a decision to this card, not an earlier one with the
        # same id; timestamps would do that only if the clock never jumped.
        pending = {**card, "id": cid, "nonce": secrets.token_hex(8), "created": now, "expires": now + expire}
        _write_atomic(_path("queued", cid), {"id": cid, "nonce": pending["nonce"], "tool": pending.get("tool"),
                                             "queued": now, "job": job})
        _write_atomic(_path("pending", cid), pending)
    audit({"event": "approval-requested", "id": cid, "tool": card["tool"], "role": card.get("principal"),
           "repo": card.get("repo"), "branch": card.get("branch") or card.get("head"), "expires": _iso(now + expire)})
    notify.send_async(card, cid, max(1, expire // 60))
    return pending


def _check_queued(cid: str, pending: dict, rec: dict) -> str | None:
    """Why a stored card and job can't be trusted to make a write, or None.
    The completer then checks the job against the card and the policy."""
    if pending.get("id") != cid or card_id(pending) != cid:
        return "the stored approval card does not match its id; nothing was written"
    if rec.get("tool") != pending.get("tool") or not isinstance(rec.get("job"), dict):
        return "the stored job does not match its approval card; nothing was written"
    if core_approvals.deferred(str(pending.get("tool"))) is None:
        return f"the gateway cannot make a staged {pending.get('tool')} write; nothing was written"
    return None


def _claim(cid: str, now: float) -> tuple[list[dict], tuple[dict, dict, dict] | None]:
    """Under the lock, settle the staged write cid if it can be: returns the
    audit events to record and, for an approved write to make now, its
    `running` done/ record, its stored card and its job (else None). The card, the decision and the job
    leave pending/, decisions/ and queued/ in the same locked step that
    files the done/ record, so a write is claimed exactly once, however
    often the decision is recorded or the sweep runs. Nothing happens while
    the card is undecided."""
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
                     "reason": "the staged write has no matching pending card; nothing was written"}], None
        try:
            expires = float(pending.get("expires"))
        except (TypeError, ValueError):
            # Never make a write on a card whose expiry can't be read;
            # discard it as staging the same call again would (_stage).
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
    """Act on the staged write cid if its card has a decision (_claim):
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
    """Act on every staged write that has a decision (_run_one). Never
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
            print(f"approvals: a staged write failed to run: {type(e).__name__}", file=sys.stderr)


def _recover_interrupted() -> None:
    """At start-up: a staged write still `running` in done/ was cut off by a
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
            print(f"approvals: recovering a staged write failed: {type(e).__name__}", file=sys.stderr)


def hook(card: dict) -> None:
    """The approval point's hook (core.approvals.set_hook). Returns None for
    a tool the policy doesn't hold (the tool writes at once); for a held one
    it stages the write (_stage) and raises Staged. The same write already
    made raises Made (its stored result); being made or declined, Denied;
    and Denied if the tool passed no job or has no completer (nothing is
    staged then)."""
    policy = load_policy()
    expire, tools = settings(policy)
    if card.get("tool") not in tools:
        return None
    cid = card_id(card)
    now = time.time()
    _sweep_safely(now)  # other cards past their expiry: give them a record too
    job = core_approvals.job()
    if not isinstance(job, dict) or core_approvals.deferred(card["tool"]) is None:
        raise Denied(f"{card['tool']} is held for approval, but the gateway cannot make it itself; nothing was sent")
    pending = _stage(card, cid, job, expire, now)
    raise Staged(_staged_text(pending, cid), cid)
