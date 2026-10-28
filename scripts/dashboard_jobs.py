"""A lane's past jobs, for the lane monitor's PACKET tab (doomtickle/passdesk-ops#191).

Display only, read only. The jobs that ran on one lane come from four places the dashboard already
reads, merged by job id: the bridge's job index (dispatch/jobs.json), the audit's job-end records,
the lane's own transcript (the `▶ job` lines, and the job it is on now). Only ids in the bridge's
form are kept, newest first (the id starts with the date and time), at most LIST_MAX of them.
Everything here is agent- or bridge-written data: it is checked, cut to one line and sent as JSON
for the page to draw with textContent. Nothing is joined onto a path except a job id that matched
JOB_ID.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import dashboard_outbox as ob

LIST_MAX = 50       # job ids /api/lane returns for a lane (the newest)
TASK_MAX = 200      # characters of a job's one-line task
STATUSES = ("running", "done", "failed", "cancelled", "interrupted")


def one_line(value, limit: int = TASK_MAX) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def when(epoch, now: float) -> str:
    """HH:MM for today, else "Mon DD HH:MM" (this machine's time zone), "" if unknown."""
    if epoch is None:
        return ""
    try:
        t = time.localtime(epoch)
        today = time.localtime(now)
    except (OverflowError, OSError, ValueError):
        return ""
    if (t.tm_year, t.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M", t)
    return time.strftime("%b %d %H:%M", t)


def _blank() -> dict:
    return {"status": "", "task": "", "model": "", "cost": None, "started": None, "elapsed": None}


def _index(state: Path, lane: str) -> dict:
    """{job id: info} for this lane's records in dispatch/jobs.json."""
    raw = ob.read_regular(state / "dispatch" / "jobs.json", ob.INDEX_MAX + 1)
    if raw is None or len(raw) > ob.INDEX_MAX:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    records = data.get("jobs") if isinstance(data, dict) and data.get("version") == 1 else None
    out: dict = {}
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict) or rec.get("lane") != lane:
            continue
        job = rec.get("id")
        if not isinstance(job, str) or not ob.JOB_ID.fullmatch(job):
            continue
        started, finished = ob._finite(rec.get("started")), ob._finite(rec.get("finished"))
        status = rec.get("status")
        out[job] = {
            "status": status if status in STATUSES else "", "task": one_line(rec.get("task")),
            "model": _text(rec.get("model")), "cost": ob._finite(rec.get("cost_usd")),
            "started": started,
            "elapsed": finished - started if started is not None and finished is not None and finished >= started else None,
        }
    return out


def lane_jobs(state: Path, lane, audit_jobs: dict) -> dict:
    """{job id: info} for every job known to have run on this watch.Lane. info: status, task,
    model, cost, started (epoch), elapsed (seconds); None or "" for what no source knows. The
    audit's cost, model, elapsed time and status win over the index's, as on the lane's module."""
    jobs = _index(state, lane.name)
    for entry in lane.entries:
        if entry.kind == "start" and ob.JOB_ID.fullmatch(entry.text):
            jobs.setdefault(entry.text, _blank())
    for job, rec in audit_jobs.items():
        if not isinstance(job, str) or not ob.JOB_ID.fullmatch(job) or rec.get("lane") != lane.name:
            continue
        info = jobs.setdefault(job, _blank())
        if rec.get("status") in STATUSES:
            info["status"] = rec["status"]
        if _text(rec.get("model")):
            info["model"] = _text(rec.get("model"))
        for key, field in (("cost", "cost_usd"), ("elapsed", "elapsed_seconds")):
            value = ob._finite(rec.get(field))
            if value is not None:
                info[key] = value
    current = lane.job
    if current and isinstance(current.get("id"), str) and ob.JOB_ID.fullmatch(current["id"]):
        info = jobs.setdefault(current["id"], _blank())
        if current.get("status") in STATUSES:
            info["status"] = current["status"]
        info["task"] = info["task"] or one_line(current.get("task"))
        info["model"] = info["model"] or _text(current.get("model"))
        if current.get("seconds") is not None:
            info["elapsed"] = info["elapsed"] if info["elapsed"] is not None else float(current["seconds"])
    return jobs


def newest(jobs: dict, pick: str = "") -> list:
    """The newest LIST_MAX ids, newest first (an id sorts by the time it was made). The picked
    job is always on the list: if it is older than the rest it comes last."""
    ids = sorted(jobs, reverse=True)[:LIST_MAX]
    if pick and pick in jobs and pick not in ids:
        ids.append(pick)
    return ids
