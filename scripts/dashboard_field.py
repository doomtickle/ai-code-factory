"""The Thoughtfield's data (doomtickle/passdesk-ops#207): today's jobs and what each one did, for the galaxy.

GET /api/field on the main port, display only, one more read of what /api/state already reads: the dispatch
audit and the lanes' parsed transcripts, plus the broker audit's denials. No git, no command, no write. An action
leaves here as a time and a kind from a fixed list, never its text, so a command or a path an agent wrote does not
reach the page through this route. A job's words are the same cut one-line labels the lane modules show, drawn
with textContent. Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import re
import time

import dashboard_labels as labels

ACTIONS_MAX = 800   # actions kept per job (evenly thinned past that: the galaxy needs the shape, not every one)
JOBS_MAX = 60       # jobs per day the page can draw
DENIALS_MAX = 300
TASK_MAX = 140
# Kinds, in the order the page's table has them.
READ, EDIT, RUN, REQUEST, REVIEW = 0, 1, 2, 3, 4
CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
READS = ("read ", "glob ", "grep ", "tool search ", "websearch ", "webfetch ", "subagent:")
EDITS = ("edit ", "write ", "multiedit ", "notebookedit ")


def action_kind(text: str, role: str) -> int:
    """A condensed transcript tool line (watch.condense_tool) as a kind. A reviewer's every action is reviewing."""
    if role == "reviewer":
        return REVIEW
    if text.startswith("$ "):
        return RUN
    if text.startswith("broker "):
        return REQUEST
    if text.startswith(EDITS):
        return EDIT
    return READ


def clock_minutes(ts) -> float | None:
    """A transcript's local time of day as minutes since midnight."""
    m = CLOCK.fullmatch(str(ts or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3) or 0) / 60


def epoch_minutes(epoch: float) -> float:
    t = time.localtime(epoch)
    return t.tm_hour * 60 + t.tm_min + t.tm_sec / 60


def one_line(text, limit: int = TASK_MAX) -> str:
    line = " ".join(str(text or "").split())
    return "".join(c for c in line if c.isprintable())[:limit]


def thin(items: list, limit: int) -> list:
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def lane_runs(lane) -> list:
    """A lane's transcript cut into jobs: [{id, start, end, status, model, task, brief, branches, actions}]."""
    runs, cur = [], None
    for e in list(lane.entries):
        if e.kind == "start":
            cur = {"id": e.text, "start": clock_minutes(e.ts), "end": None, "status": "running", "model": "",
                   "task": "", "brief": "", "branches": [], "actions": []}
            runs.append(cur)
        elif cur is None:
            continue
        elif e.kind == "end":
            cur["end"] = clock_minutes(e.ts)
            cur["status"] = str(e.text).split()[0] if str(e.text).split() else "done"
        elif e.kind == "model":
            cur["model"] = str(e.text).strip()
        elif e.kind == "task":
            if not cur["task"]:
                cur["task"] = str(e.text).strip()
            cur["brief"] = (cur["brief"] + "\n" + str(e.text).strip())[:600]
        elif e.kind == "branch":
            cur["branches"].append(str(e.text))
        elif e.kind == "tool":
            at = clock_minutes(e.ts)
            if at is not None:
                cur["actions"].append([round(at, 2), action_kind(str(e.text), lane.role)])
    return runs


def build(lanes, audit_jobs: dict, denials, midnight: float, now: float, verdict, epoch_of) -> dict:
    """Today's jobs. `audit_jobs` is the dispatch audit (job id -> record), `denials` the broker's call records,
    `verdict(job_id)` a review's verdict or None and `epoch_of(ts)` an audit timestamp as epoch seconds or None."""
    today_ids = {jid for jid, rec in audit_jobs.items() if (epoch_of(rec.get("ts")) or 0) >= midnight}
    now_min = epoch_minutes(now)
    jobs = []
    seen = set()
    for lane in lanes:
        for run in lane_runs(lane):
            jid, rec = run["id"], audit_jobs.get(run["id"]) or {}
            # Today's: named in today's audit, or still running. A transcript has no date of its own.
            if jid in seen or (jid not in today_ids and run["status"] != "running"):
                continue
            # A running job's start can't be later than now, so a clock past now is yesterday's lane.
            if run["status"] == "running" and (run["start"] is None or run["start"] > now_min + 1):
                continue
            seen.add(jid)
            jobs.append(job_record(jid, lane, run, rec, verdict, epoch_of, now_min))
    # Today's jobs whose transcript has rolled away still count: their spend is real.
    for jid in today_ids - seen:
        rec = audit_jobs[jid]
        ended = epoch_of(rec.get("ts"))
        if ended is None:
            continue
        secs = rec.get("elapsed_seconds")
        secs = secs if isinstance(secs, (int, float)) else 0
        run = {"id": jid, "start": epoch_minutes(ended - secs), "end": epoch_minutes(ended),
               "status": str(rec.get("status") or "done"), "model": str(rec.get("model") or ""), "task": "",
               "brief": "", "branches": [], "actions": []}
        lane = type("L", (), {"name": str(rec.get("lane") or rec.get("role") or "?"), "role": str(rec.get("role") or "?")})
        jobs.append(job_record(jid, lane, run, rec, verdict, epoch_of, now_min))
    jobs.sort(key=lambda j: j["start"])
    blocked = []
    for rec in denials:
        if rec.get("decision") != "deny":
            continue
        at = epoch_of(rec.get("ts"))
        if at is not None and at >= midnight:
            blocked.append({"t": round(epoch_minutes(at), 2), "role": one_line(rec.get("role") or rec.get("principal"), 40)})
    return {"now": round(now_min, 2), "jobs": jobs[-JOBS_MAX:], "blocked": blocked[-DENIALS_MAX:]}


def rewind_past(t, now_min: float):
    """A transcript clock has no date: a time later than now is yesterday's (a job that crossed midnight), so it is negative."""
    return t - 1440 if t is not None and t > now_min + 1 else t


def job_record(jid: str, lane, run: dict, rec: dict, verdict, epoch_of, now_min: float) -> dict:
    run["start"], run["end"] = rewind_past(run["start"], now_min), rewind_past(run["end"], now_min)
    run["actions"] = [[rewind_past(a, now_min), k] for a, k in run["actions"]]
    brief = run["brief"] or run["task"]
    role = lane.role
    # Jobs on one branch are one order, a reviewer's included. Any other job stands alone.
    topic = labels.branch_topic(brief) or next((labels.branch_topic(b) or b for b in run["branches"]), "")
    cost = rec.get("cost_usd")
    return {
        "id": jid, "lane": lane.name, "role": role,
        "order": one_line(topic, 80) or jid,
        "label": one_line(labels.desk_label(brief), 40) or one_line(lane.name, 40),
        "task": one_line(run["task"]),
        "model": one_line(str(run["model"] or rec.get("model") or "").replace("claude-", ""), 40),
        "status": one_line(rec.get("status") or run["status"], 20),
        "verdict": (verdict(jid) or "").lower() if run["status"] != "running" else "",
        "start": round(run["start"], 2) if run["start"] is not None else 0,
        "end": round(run["end"], 2) if run["end"] is not None else None,
        "cost": round(float(cost), 4) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        "actions": thin(run["actions"], ACTIONS_MAX),
    }
