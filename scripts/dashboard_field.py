"""The Thoughtfield's data (doomtickle/passdesk-ops#207): today's jobs and what each one did, for the galaxy.

GET /api/field on the main port, display only, one more read of what /api/state already reads: the dispatch
audit and the lanes' parsed transcripts, plus the broker audit's denials. No git, no command, no write. An action
leaves here as a time and a kind from a fixed list, never its text, so a command or a path an agent wrote does not
reach the page through this route. A job's words are cut one-line labels, drawn
with textContent. Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import math
import re
import time

import dashboard_jobs as jobs_mod
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


def finite(value):
    """A number fit to use, or None: audit files are parsed JSON, so this can be anything (inf and NaN included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and abs(value) < 1e12 else None


def lane_runs(lane) -> list:
    """A lane's transcript cut into jobs: [{id, start, end, status, model, task, brief, branches, actions}]. Only the
    lane's own current job can still be running: Lane.job holds what watch knows about it (a dead bridge marks it
    interrupted, and the model line isn't kept in entries), and any earlier job left open was interrupted."""
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
    live = getattr(lane, "job", None) or {}
    for run in runs:
        if run["id"] == live.get("id"):
            run["status"] = str(live.get("status") or run["status"])
            run["model"] = str(live.get("model") or run["model"])
            run["task"] = run["task"] or str(live.get("task") or "")
        elif run["status"] == "running":
            run["status"] = "interrupted"
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
            # Today's: named in today's audit, or still running (lane_runs: only the lane's live job can be).
            # A transcript has no date of its own. One that crossed midnight comes out with a negative start.
            if jid in seen or (jid not in today_ids and run["status"] != "running") or run["start"] is None:
                continue
            seen.add(jid)
            jobs.append(job_record(jid, lane, run, rec, verdict, epoch_of, now_min))
    # Today's jobs whose transcript has rolled away still count: their spend is real.
    for jid in today_ids - seen:
        rec = audit_jobs[jid]
        ended = epoch_of(rec.get("ts"))
        if ended is None:
            continue
        secs = finite(rec.get("elapsed_seconds")) or 0
        run = {"id": jid, "start": epoch_minutes(ended - secs), "end": epoch_minutes(ended),
               "status": str(rec.get("status") or "done"), "model": str(rec.get("model") or ""), "task": "",
               "brief": "", "branches": [], "actions": []}
        lane = type("L", (), {"name": str(rec.get("lane") or rec.get("role") or "?"), "role": str(rec.get("role") or "?")})
        jobs.append(job_record(jid, lane, run, rec, verdict, epoch_of, now_min))
    jobs.sort(key=lambda j: j["start"])
    blocked = []
    for rec in denials:
        if not isinstance(rec, dict) or rec.get("decision") != "deny":
            continue
        at = epoch_of(rec.get("ts"))
        if at is not None and at >= midnight:
            blocked.append({"t": round(epoch_minutes(at), 2), "role": one_line(rec.get("role") or rec.get("principal"), 40)})
    return {"now": round(now_min, 2), "date": time.strftime("%Y-%m-%d", time.localtime(now)), "jobs": jobs[-JOBS_MAX:],
            "blocked": blocked[-DENIALS_MAX:]}


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
    return {
        "id": one_line(jid, 64), "lane": one_line(lane.name, 41), "role": one_line(role, 40),
        "order": one_line(topic, 80) or one_line(jid, 64),
        "label": one_line(labels.desk_label(brief), 40) or one_line(lane.name, 40),
        "task": one_line(run["task"]),
        "model": one_line(str(run["model"] or rec.get("model") or "").replace("claude-", ""), 40),
        "status": one_line(rec.get("status") or run["status"], 20),
        "verdict": (verdict(jid) or "").lower() if run["status"] != "running" and jobs_mod.JOB_ID.fullmatch(jid) else "",
        "start": round(run["start"], 2) if run["start"] is not None else 0,
        "end": round(run["end"], 2) if run["end"] is not None else None,
        "cost": None if finite(rec.get("cost_usd")) is None else round(finite(rec.get("cost_usd")), 4),
        "actions": thin(run["actions"], ACTIONS_MAX),
    }
