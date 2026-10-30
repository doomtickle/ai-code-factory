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
import os
import re
import stat
import time
from pathlib import Path

import patchview

LIST_MAX = 50       # job ids /api/lane returns for a lane (the newest)
TASK_MAX = 200      # characters of a job's one-line task
STATUSES = ("running", "done", "failed", "cancelled", "interrupted")
INDEX_MAX = 5_000_000  # bytes read of jobs.json (the bridge writes at most this)

DIFF_FILE_MAX = 8_000_000   # bytes read of dispatch/packets/<job>.diff.json (the bridge keeps its patch text to 1 MB)
DIFF_BYTES_MAX = 1_000_000  # characters of patch text shown per job, whatever the file holds (the bridge's own cap)
DIFF_LINES_MAX = 20_000     # and lines of it
DIFF_BRANCHES_MAX = 20      # branches of one job shown
HEX = re.compile(r"[0-9a-f]{4,64}")

JOB_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")
LANE = re.compile(r"[a-z][a-z0-9-]{1,40}")


def read_regular(path: Path, cap: int) -> bytes | None:
    """At most cap bytes of a regular file opened without following a symlink or blocking on a
    FIFO, else None (missing, unreadable, or not a regular file)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(cap)
    except OSError:
        return None
    finally:
        os.close(fd)


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value - value == 0 else None


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
    raw = read_regular(state / "dispatch" / "jobs.json", INDEX_MAX + 1)
    if raw is None or len(raw) > INDEX_MAX:
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
        if not isinstance(job, str) or not JOB_ID.fullmatch(job):
            continue
        started, finished = _finite(rec.get("started")), _finite(rec.get("finished"))
        status = rec.get("status")
        out[job] = {
            "status": status if status in STATUSES else "", "task": one_line(rec.get("task")),
            "model": _text(rec.get("model")), "cost": _finite(rec.get("cost_usd")),
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
        if entry.kind == "start" and JOB_ID.fullmatch(entry.text):
            jobs.setdefault(entry.text, _blank())
    for job, rec in audit_jobs.items():
        if not isinstance(job, str) or not JOB_ID.fullmatch(job) or rec.get("lane") != lane.name:
            continue
        info = jobs.setdefault(job, _blank())
        if rec.get("status") in STATUSES:
            info["status"] = rec["status"]
        if _text(rec.get("model")):
            info["model"] = _text(rec.get("model"))
        for key, field in (("cost", "cost_usd"), ("elapsed", "elapsed_seconds")):
            value = _finite(rec.get(field))
            if value is not None:
                info[key] = value
    current = lane.job
    if current and isinstance(current.get("id"), str) and JOB_ID.fullmatch(current["id"]):
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


def job_diff(packets: Path, job: str) -> dict:
    """The finished job's diff for the lane monitor's DIFF tab, from the sidecar the dispatch bridge wrote
    next to the packet (packets/<job>.diff.json: the patch of each branch the job brought back, made from
    the commits imported, so nothing here runs git). {"job", "state", "message", "branches"}; state is
    "ok", "none" (no file: a job that brought back no branch, an older bridge, or one still running) or
    "bad" (a file that cannot be used). Each branch: ref, repo, commit, tip, error, truncated, files
    (patchview.parse's file records: path, old_path, status, adds, dels, flags, hunks), adds, dels. The file
    is untrusted: it is a regular file opened O_NOFOLLOW and read up to DIFF_FILE_MAX, the patch text is cut
    to the bridge's caps whatever it says, and every string that reaches the page is cleaned by patchview
    (control, bidi and invisible characters made visible), for the page to draw with textContent. `job` must
    already match JOB_ID."""
    out: dict = {"job": job, "state": "none", "message": "NO DIFF FOR THIS JOB", "branches": [],
                 "limits": {"bytes": DIFF_BYTES_MAX, "lines": DIFF_LINES_MAX}}
    raw = read_regular(packets / f"{job}.diff.json", DIFF_FILE_MAX + 1)
    if raw is None:
        return out
    bad = dict(out, state="bad", message="THE DIFF FILE CANNOT BE READ")
    if len(raw) > DIFF_FILE_MAX:
        return dict(bad, message="THE DIFF FILE IS TOO LARGE TO SHOW")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return bad
    if (not isinstance(data, dict) or data.get("version") != 1 or data.get("job") != job
            or not isinstance(data.get("branches"), list)):
        return bad
    for rec in data["branches"][:DIFF_BRANCHES_MAX]:
        if isinstance(rec, dict):
            out["branches"].append(_diff_branch(rec))
    out["state"], out["message"] = "ok", ""
    return out


def _diff_branch(rec: dict) -> dict:
    flags: set = set()
    patch = rec.get("patch") if isinstance(rec.get("patch"), str) else ""
    truncated = rec.get("truncated") is True
    if len(patch) > DIFF_BYTES_MAX:
        patch, truncated = patch[:patch.rfind("\n", 0, DIFF_BYTES_MAX) + 1], True
    lines = patch.split("\n")
    if len(lines) > DIFF_LINES_MAX + 1:
        patch, truncated = "\n".join(lines[:DIFF_LINES_MAX]) + "\n", True
    files = []
    for commit in patchview.parse(patch, truncated)["commits"]:
        files += commit["files"]
    error = rec.get("error")
    return {
        "ref": patchview.clean(_text(rec.get("ref"))[:200], flags),
        "repo": patchview.clean(_text(rec.get("repo"))[:100], flags),
        "commit": _hex(rec.get("commit"))[:12], "tip": _hex(rec.get("tip"))[:12],
        "error": patchview.clean(one_line(error, 300), flags) if isinstance(error, str) else "",
        "truncated": truncated, "files": files,
        "adds": sum(f["adds"] for f in files), "dels": sum(f["dels"] for f in files),
    }


def _hex(value) -> str:
    return value if isinstance(value, str) and HEX.fullmatch(value) else ""
