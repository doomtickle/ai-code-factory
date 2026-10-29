"""Factory dispatch bridge: lets the Coordinator VM delegate tasks to role sandboxes.

Runs on the host (only the host can drive sbx), bound to 127.0.0.1, and is
reachable only through the sbx MCP gateway with a bearer token. It runs tasks
for roles defined in roles/*/role.conf, and all it may do to get there is:

- `sbx exec` into an existing factory-<role> sandbox, with a fixed argv (no
  shell), of exactly these commands: `claude -p <task> ...`, `factory-lane
  prepare|export ...` (rw roles' private git clones, "lanes") and a `pkill`
  aimed at one job's claude session (cancel);
- for rw roles, after the job: a fixed `git fetch` of the bundle that
  `factory-lane export` streamed out (size-capped) into the host project's
  remote-tracking refs `refs/remotes/factory-<lane>/factory/*`, with hooks and
  fsmonitor forced off, then `git for-each-ref` to report what arrived, and,
  for the job's review packet, a read-only `git log` and `git diff --stat` of
  each imported branch against the host's HEAD (same flags, external diff and
  textconv off). No other git command, and nothing from the sandbox ever
  picks the host path.

After every job it writes a review packet, <DISPATCH_STATE>/packets/<job>.md:
the task, the branches (commits and diffstat), the tests the role reported,
the broker calls its role made meanwhile, cost, and the report. `factory
packet` prints it.

The job list outlives the bridge: <DISPATCH_STATE>/jobs.json holds a short
record of the last MAX_INDEXED_JOBS jobs (never the full task or the full
report), written atomically at each status change and reloaded at start. A job
recorded as running whose process is gone becomes "interrupted" (and its lane's
transcript is closed with an "interrupted" line), so nothing shows as running
without a live job. The file is untrusted input: a corrupt, oversized or
symlinked one is skipped with a log line.

It also answers the Coordinator's questions about the factory itself
(factory_docs) from a read-only snapshot of the factory's markdown docs that
start-dispatch.sh copies into <DISPATCH_STATE>/docs/.

A role may run up to LANES jobs at once (one per lane, sharing its sandbox).
Each lane's transcript streams to <DISPATCH_STATE>/logs/<lane>.log (followed with
`factory logs`) and the role's final report goes back to the Coordinator.

Standard library only; compatible with the macOS system Python (3.9).
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The checkout's factory package (the host CLI) holds the one role.conf parser and the one
# project_repos. The bridge runs from that checkout (start-dispatch.sh), so its parent
# folder is the checkout root; put it first so nothing installed can stand in for it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factory.conf import match_conf_line, parse_conf  # noqa: E402
from factory.repos import project_repos  # noqa: E402,F401  (also used by the tests)
from factory.roles import ROLE_OVERRIDE_KEYS, parse_enabled_roles  # noqa: E402

SERVER_NAME = "factory-dispatch"
SERVER_VERSION = "1.0.0"

ROOT = Path(os.environ.get("DISPATCH_ROOT", "/nonexistent"))
PROJECT = Path(os.environ.get("DISPATCH_PROJECT", "/nonexistent"))
STATE = Path(os.environ.get("DISPATCH_STATE", "/nonexistent"))
# The roles' scratch folders (FACTORY_STATE/roles/<role>): each role's writable
# primary workspace, mounted at this same path in its sandbox, which the host
# can open. Passed to a job as FACTORY_SCRATCH so a role knows where to leave
# files for a person (screenshots, say) without touching the project.
ROLES_SCRATCH = Path(os.environ.get("DISPATCH_ROLES_SCRATCH", "/nonexistent"))
PORT = int(os.environ.get("DISPATCH_PORT", "18766"))
SBX = os.environ.get("DISPATCH_SBX", "sbx")

MAX_REQUEST_BYTES = 1_000_000
MAX_TASK_CHARS = 50_000
MAX_RESULT_CHARS = 30_000
MAX_WAIT_SECONDS = 600
# A wait shorter than this is stretched to it (a wait still returns the moment the
# job ends). Every poll is a Coordinator turn that re-reads its whole conversation,
# and the audit log showed it picking 90-240 s waits and polling jobs several
# times. 0 is still "the status right now, without waiting".
MIN_WAIT_SECONDS = 300
# While a blocking tools/call runs, a client that accepts text/event-stream gets a
# keepalive this often, so an idle timeout between it and the bridge doesn't drop
# the connection (the job keeps running either way).
KEEPALIVE_SECONDS = 15
# A role's saved session over this many context tokens is not resumed: the next
# task starts fresh (role.conf MAX_SESSION_TOKENS overrides it; "0" = no cap).
# Every turn of a resumed session re-reads its whole history, so a large one
# makes even a one-line task expensive.
DEFAULT_MAX_SESSION_TOKENS = 100_000
# Wall-clock limit for one job's claude (role.conf MAX_JOB_SECONDS overrides it;
# "0" = no limit). A stuck claude otherwise holds its lane until cancel_job.
DEFAULT_MAX_JOB_SECONDS = 3600
MAX_INSTRUCTIONS_CHARS = 20_000
# Fixed environment for every role's claude. Roles keep their context in their
# saved sessions and in the Coordinator's tasks; auto memory would only add
# notes to every prompt that no one reviews.
ROLE_ENV = ("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",)
# How long a sandbox seen by `sbx ls` is assumed to still exist, so delegate
# doesn't wait for `sbx ls` on every task. A missing one is re-checked each time,
# and a sandbox removed in the meantime makes the job itself fail with sbx's error.
SANDBOX_CACHE_SECONDS = 300
# Parallel jobs per role (role.conf LANES="N"; empty or invalid = 1).
MAX_LANES = 8
# A lane's exported git bundle is streamed to disk and refused beyond this size.
MAX_BUNDLE_BYTES = 500 * 1024 * 1024
PREPARE_TIMEOUT_SECONDS = 1800  # cloning a large project into a fresh lane
EXPORT_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 300
# Saved session ids the bridge resumes (and puts in a pkill pattern).
_SESSION_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")
# Host git never runs repo-configured hooks or fsmonitor while importing.
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
# The broker's audit log, for the broker calls a job's role made (review packet).
BROKER_AUDIT = Path(os.environ.get("DISPATCH_BROKER_AUDIT") or STATE.parent / "broker" / "audit.jsonl")
MAX_PACKET_LINES = 80  # per packet section: commits, diffstat lines, broker calls

ROLE_PREAMBLE = (
    "You are the {role} in a multi-agent software factory. You received this task "
    "from the Coordinator, who cannot see your screen. Work autonomously; if something "
    "is ambiguous, make a reasonable choice and say so. When done, end with a concise "
    "report: what you did, the files changed, how you verified it (the command and its "
    "summary line, not its full output) and open issues or questions. Cite file:line "
    "instead of pasting code or diffs; the Coordinator can ask for more."
)


class Denied(Exception):
    """A request refused by the bridge."""


def role_instructions(role: str) -> str:
    """The role's instructions as snapshotted by start-dispatch.sh from
    roles/<role>/instructions.md. Read from the state dir, not the repo, so a role
    editing the factory's own repo can't change another role's prompt until the
    user restarts the factory."""
    try:
        text = (STATE / "instructions" / f"{role}.md").read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not text:
        raise Denied(f"no instructions loaded for {role}; re-run 02-launch-factory.sh")
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        raise Denied(f"instructions for {role} exceed {MAX_INSTRUCTIONS_CHARS} characters")
    return text


def role_system_prompt(role: str, instructions: str) -> str:
    return ROLE_PREAMBLE.format(role=role) + "\n\n" + instructions


# --------------------------------------------------------------------------
# Roles

_ROLE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
# claude's --effort levels the Coordinator may pick ("max" is left out: it has no
# cap on thinking, and not every model supports it).
EFFORTS = ("low", "medium", "high", "xhigh")
# Keep in sync with RESERVED_ROLE_NAMES in factory/roles.py (valid_role_name).
_RESERVED_ROLE_NAMES = {"broker", "coordinator", "dispatch"}
# Lane k>=2 of role R is "R-lane<k>", so no role may itself be named like a lane.
_LANE_SUFFIX = re.compile(r"-lane[0-9]+$")


# Keys this machine's local override of a role may set (FACTORY_CONFIG/roles/
# <role>.conf, snapshotted by start-dispatch.sh into STATE/role-overrides) are
# ROLE_OVERRIDE_KEYS, from factory/roles.py: tuning only, nothing that moves the
# security boundary.


def role_overrides_dir() -> Path:
    return STATE / "role-overrides"


def read_role_override(path: Path) -> dict:
    """A role's local override, as start-dispatch.sh snapshotted it: only blank
    lines, comments and KEY="value" lines for ROLE_OVERRIDE_KEYS (parsed, never
    executed). Anything else is refused, naming the file and line."""
    values = {}
    for line in path.read_text().split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pair = match_conf_line(line)
        if not pair:
            raise Denied(f'{path}: not a KEY="value" line: {line[:80]}')
        if pair[0] not in ROLE_OVERRIDE_KEYS:
            raise Denied(f"{path}: {pair[0]} can't be set in a local override "
                         f"(allowed: {' '.join(ROLE_OVERRIDE_KEYS)})")
        values[pair[0]] = pair[1]
    return values


def enabled_roles_snapshot() -> set:
    """Role names this machine switched on (FACTORY_CONFIG/enabled-roles,
    snapshotted by start-dispatch.sh into STATE/enabled-roles). Missing or
    unreadable means none: optional roles stay off."""
    try:
        text = (STATE / "enabled-roles").read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(parse_enabled_roles(text))


def load_roles(include_disabled: bool = False) -> dict:
    """Roles from ROOT/roles/*/role.conf (parsed, never executed), each with this
    machine's local override (if any) layered on top. Override values go through
    the same parsing as role.conf's. A role whose committed role.conf sets
    OPTIONAL="1" is left out unless the enabled-roles snapshot names it (all of
    them, flagged "enabled", with include_disabled)."""
    roles = {}
    confs = sorted((ROOT / "roles").glob("*/role.conf"))
    overrides = role_overrides_dir()
    if overrides.is_dir():
        known = {conf.parent.name for conf in confs}
        for path in sorted(overrides.glob("*.conf")):
            if path.stem not in known:
                raise Denied(f"{path}: no role named {path.stem!r} in roles/")
    for conf in confs:
        name = conf.parent.name
        if not _ROLE_NAME.match(name) or name in _RESERVED_ROLE_NAMES or _LANE_SUFFIX.search(name):
            continue
        values = parse_conf(conf.read_text())
        enabled = values.get("OPTIONAL") != "1" or name in enabled_roles_snapshot()
        if not enabled and not include_disabled:
            continue
        override = overrides / f"{name}.conf"
        if override.is_file():
            values.update(read_role_override(override))
        roles[name] = {
            "enabled": enabled,
            "description": values.get("DESCRIPTION", ""),
            "workspace": values.get("WORKSPACE", "rw"),
            "broker_tools": values.get("BROKER_TOOLS", "").split(),
            "model": values.get("MODEL", "").strip(),
            "models": parse_models(values.get("MODEL", ""), values.get("MODELS", "")),
            "effort": values.get("EFFORT", "").strip() if values.get("EFFORT", "").strip() in EFFORTS else "",
            "max_session_tokens": parse_session_cap(values.get("MAX_SESSION_TOKENS", "")),
            "max_turns": parse_turns(values.get("MAX_TURNS", "")),
            "max_job_seconds": parse_job_seconds(values.get("MAX_JOB_SECONDS", "")),
            "lanes": parse_lanes(values.get("LANES", "")),
        }
    return roles


def parse_lanes(value: str) -> int:
    """role.conf LANES: 1..MAX_LANES; empty or invalid means 1."""
    value = value.strip()
    if value.isascii() and value.isdigit() and 1 <= int(value) <= MAX_LANES:
        return int(value)
    return 1


def lane_name(role: str, k: int) -> str:
    """Lane 1 is the role itself (its state keeps the role's file names)."""
    return role if k == 1 else f"{role}-lane{k}"


def lane_names(role: str, lanes: int) -> list:
    return [lane_name(role, k) for k in range(1, lanes + 1)]


def parse_models(default: str, allowed: str) -> list:
    """Models the Coordinator may pick per task: MODELS, plus the default MODEL."""
    models = [m for m in allowed.split() if _MODEL_NAME.match(m)]
    default = default.strip()
    if default and _MODEL_NAME.match(default) and default not in models:
        models.insert(0, default)
    return models


def parse_session_cap(value: str) -> int:
    value = value.strip()
    return int(value) if value.isdigit() else DEFAULT_MAX_SESSION_TOKENS


def parse_turns(value: str) -> int:
    """role.conf MAX_TURNS: claude's --max-turns for one job; empty or 0 means none."""
    value = value.strip()
    return int(value) if value.isdigit() else 0


def parse_job_seconds(value: str) -> int:
    """role.conf MAX_JOB_SECONDS: wall-clock limit for one job's claude; empty or
    invalid means DEFAULT_MAX_JOB_SECONDS, 0 means none."""
    value = value.strip()
    return int(value) if value.isascii() and value.isdigit() else DEFAULT_MAX_JOB_SECONDS


def sandbox_name(role: str) -> str:
    return f"factory-{role}"


_seen_sandboxes: dict = {}  # sandbox name -> time.monotonic() when `sbx ls` last listed it
_seen_lock = threading.Lock()


def existing_sandboxes() -> set:
    cp = subprocess.run([SBX, "ls", "-q"], capture_output=True, text=True, timeout=60)
    names = set(cp.stdout.split()) if cp.returncode == 0 else set()
    now = time.monotonic()
    with _seen_lock:
        _seen_sandboxes.clear()
        _seen_sandboxes.update(dict.fromkeys(names, now))
    return names


def sandbox_exists(name: str) -> bool:
    with _seen_lock:
        seen = _seen_sandboxes.get(name)
    if seen is not None and time.monotonic() - seen < SANDBOX_CACHE_SECONDS:
        return True
    return name in existing_sandboxes()


# --------------------------------------------------------------------------
# Output filtering

_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    if len(text) > limit:
        text = text[:limit] + f"\n[truncated: {len(text) - limit} more characters]"
    return text


def one_line(text: str, limit: int = 160) -> str:
    text = " ".join(sanitize(str(text), 10_000).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# Audit log

_audit_lock = threading.Lock()


def audit(record: dict) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    with _audit_lock, open(STATE / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# --------------------------------------------------------------------------
# Jobs

class Job:
    def __init__(self, role: str, task: str, resume: str | None, model: str = "",
                 start_cost_usd: float | None = 0.0, instructions: str = "", effort: str = "",
                 lane: str = "", rw: bool = False):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        self.role = role
        self.lane = lane or role
        self.rw = rw
        self.task = task
        self.resume = resume
        self.model = model
        self.effort = effort
        self.max_turns = 0  # claude --max-turns, from role.conf MAX_TURNS; 0 = none
        # Wall-clock limit for claude, from role.conf MAX_JOB_SECONDS; 0 = none.
        self.max_job_seconds = DEFAULT_MAX_JOB_SECONDS
        self.instructions = instructions
        self.status = "running"  # running | done | failed | cancelled
        self.started = time.time()
        self.finished: float | None = None
        self.result = ""
        self.session_id: str | None = None
        # The id in the current claude argv (--session-id or --resume): what cancel targets.
        self.claude_session: str | None = None
        # Claude reports total_cost_usd for the whole session, including the turns a
        # resumed session restores from its transcript. The job's own cost is the end
        # total minus the session's total when the job started (None if unknown).
        self.start_cost_usd = start_cost_usd
        self.session_cost_usd: float | None = None
        self.context_tokens: int | None = None
        self.model_used: str | None = None
        # rw roles only: what the post-job import brought into the host project.
        self.branches: list | None = [] if rw else None
        self.import_errors: list | None = [] if rw else None
        self.session_mode = ""  # how the session was chosen, for the packet
        self.test_modes: list = []  # "factory-test: mode: ..." lines seen in tool results
        self.packet: str | None = None  # path of the review packet, once written
        self.proc: subprocess.Popen | None = None
        self.done = threading.Event()
        self.restored = False  # reloaded from the job index: no process, a short report only

    def record(self) -> dict:
        """What the job index keeps: a one-line task, never its full text."""
        s = self.summary()
        rec = {"id": self.id, "role": self.role, "lane": self.lane, "rw": self.rw,
               "task": one_line(self.task, 200), "status": self.status,
               "started": self.started, "finished": self.finished,
               "model": self.model_used or self.model or "", "effort": self.effort,
               "context_tokens": self.context_tokens,
               "cost_usd": s.get("cost_usd"), "session_cost_usd": s.get("session_cost_usd"),
               "branches": list(self.branches) if self.branches is not None else None,
               "import_errors": list(self.import_errors) if self.import_errors is not None else None,
               "packet": self.packet}
        if self.status != "running":
            rec["report"] = one_line(self.result, REPORT_SUMMARY_CHARS)
        return rec

    def summary(self, with_result: bool = False) -> dict:
        end = self.finished or time.time()
        out = {"job_id": self.id, "role": self.role, "lane": self.lane, "status": self.status,
               "elapsed_seconds": round(end - self.started), "task": one_line(self.task, 200)}
        if self.model_used:
            out["model"] = self.model_used
        if self.effort:
            out["effort"] = self.effort
        if self.session_cost_usd is not None:
            if self.start_cost_usd is not None and self.session_cost_usd >= self.start_cost_usd:
                out["cost_usd"] = round(self.session_cost_usd - self.start_cost_usd, 4)
            out["session_cost_usd"] = round(self.session_cost_usd, 4)
        if self.context_tokens is not None:
            out["context_tokens"] = self.context_tokens
        if self.branches is not None:
            out["branches"] = list(self.branches)
        if self.import_errors is not None:
            out["import_errors"] = list(self.import_errors)
        if self.packet:
            out["packet"] = self.packet
        if with_result and self.status != "running":
            out["report"] = sanitize(self.result)
        if self.restored:
            out["restored"] = True
            if with_result:
                out["note"] = ("reloaded from the job index after a dispatch restart: the report is "
                               "a short summary" + ("; the full one is in the review packet "
                               f"(factory packet {self.id})" if self.packet else ""))
        return out


_jobs: dict = {}
_jobs_lock = threading.Lock()


# --------------------------------------------------------------------------
# Job index: STATE/jobs.json, so the job list survives a restart of the bridge
# (factory restart/update, a crash, a reboot). The state folder is on the host,
# outside every sandbox. Written atomically at each status change; read back at
# start-up as untrusted data.

JOB_INDEX_VERSION = 1
MAX_INDEXED_JOBS = 200
MAX_INDEX_BYTES = 5_000_000
REPORT_SUMMARY_CHARS = 600
_JOB_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
_LANE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
JOB_STATUSES = ("running", "done", "failed", "cancelled", "interrupted")
_index_lock = threading.Lock()


def job_index_file() -> Path:
    return STATE / "jobs.json"


def log_line(text: str) -> None:
    """A line in dispatch.log (stderr)."""
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} dispatch: {text}", file=sys.stderr, flush=True)


def save_job_index() -> None:
    """Write the newest MAX_INDEXED_JOBS jobs (and every running one) to jobs.json:
    a 0600 temp file in the same folder, then a rename, so a reader never sees half
    a file. Never raises: a full disk must not fail the job. The snapshot is taken
    inside _index_lock, so saves are ordered and the last one to write has the
    newest view; callers must not hold _jobs_lock (it is taken after _index_lock)."""
    try:
        with _index_lock:
            with _jobs_lock:
                jobs = sorted(_jobs.values(), key=lambda j: j.started, reverse=True)
                keep = jobs[:MAX_INDEXED_JOBS] + [j for j in jobs[MAX_INDEXED_JOBS:] if j.status == "running"]
                records = [j.record() for j in keep]
            data = json.dumps({"version": JOB_INDEX_VERSION, "jobs": records}, ensure_ascii=False).encode("utf-8")
            path = job_index_file()
            tmp = path.with_name(f"{path.name}.tmp")
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
    except (OSError, ValueError, TypeError) as e:
        log_line(f"could not write the job index: {type(e).__name__}: {e}")


def _text(value, limit: int) -> str | None:
    return one_line(value, limit) if isinstance(value, str) else None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def job_from_record(rec) -> "Job | None":
    """A finished-looking Job from one index record, or None if the record is not
    well formed. Every field is checked: the file is data, not code."""
    if not isinstance(rec, dict):
        return None
    job_id, role, lane = rec.get("id"), rec.get("role"), rec.get("lane")
    status, started = rec.get("status"), _number(rec.get("started"))
    if (not isinstance(job_id, str) or not _JOB_ID.match(job_id)
            or not isinstance(role, str) or not _ROLE_NAME.match(role)
            or not isinstance(lane, str) or not _LANE_NAME.match(lane)
            or status not in JOB_STATUSES or started is None):
        return None
    rw = rec.get("rw") is True
    job = Job(role, _text(rec.get("task"), 200) or "", None, _text(rec.get("model"), 80) or "",
              None, "", _text(rec.get("effort"), 20) or "", lane=lane, rw=rw)
    job.id, job.started, job.status = job_id, started, status
    job.finished = _number(rec.get("finished"))
    model = _text(rec.get("model"), 80)
    job.model_used = model or None
    tokens = rec.get("context_tokens")
    job.context_tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else None
    session_cost, cost = _number(rec.get("session_cost_usd")), _number(rec.get("cost_usd"))
    if session_cost is not None:
        job.session_cost_usd = session_cost
        job.start_cost_usd = session_cost - cost if cost is not None and cost <= session_cost else None
    if rw:
        branches = []
        for b in rec.get("branches") if isinstance(rec.get("branches"), list) else []:
            if isinstance(b, dict) and all(isinstance(b.get(k), str) for k in ("repo", "ref", "commit")):
                branches.append({k: one_line(b[k], 300) for k in ("repo", "ref", "commit")})
        job.branches = branches[:50]
        job.import_errors = [e for e in (_text(e, 2000) for e in (
            rec.get("import_errors") if isinstance(rec.get("import_errors"), list) else [])) if e][:20]
    job.result = _text(rec.get("report"), REPORT_SUMMARY_CHARS) or ""
    # The packet path is recomputed, never taken from the file.
    if status != "running" and packet_path(job_id).is_file():
        job.packet = str(packet_path(job_id))
    job.restored = True
    job.done.set()
    return job


def read_job_index() -> list:
    """Jobs from jobs.json, newest MAX_INDEXED_JOBS. A missing file is an empty
    list; a symlink, an oversized, unreadable or corrupt file is skipped with a log
    line, and so is any single bad record."""
    path = job_index_file()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return []
    except OSError as e:
        log_line(f"job index {path} skipped (not opened: {type(e).__name__}; a symlink is refused)")
        return []
    try:
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_INDEX_BYTES:
                log_line(f"job index {path} skipped (not a regular file of at most {MAX_INDEX_BYTES} bytes)")
                return []
            data = json.loads(f.read(MAX_INDEX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError) as e:  # UnicodeDecodeError is a ValueError
        log_line(f"job index {path} skipped (unreadable or corrupt: {type(e).__name__})")
        return []
    records = data.get("jobs") if isinstance(data, dict) and data.get("version") == JOB_INDEX_VERSION else None
    if not isinstance(records, list):
        log_line(f"job index {path} skipped (unknown layout)")
        return []
    jobs, seen, bad = [], set(), 0
    for rec in records:
        job = job_from_record(rec)
        if job is None or job.id in seen:
            bad += 1
            continue
        seen.add(job.id)
        jobs.append(job)
    if bad:
        log_line(f"job index: ignored {bad} malformed or duplicate record(s)")
    jobs.sort(key=lambda j: j.started, reverse=True)
    return jobs[:MAX_INDEXED_JOBS]


def interrupt_job(job: "Job", now: float) -> None:
    """A job recorded as running, but with no process in this bridge (it died with the
    bridge or the sandboxes). Its end is the last thing its lane's transcript shows."""
    log = Transcript(job.lane)
    try:
        last = log.path.stat().st_mtime
    except OSError:
        last = now
    job.status = "interrupted"
    job.finished = min(now, max(job.started, last))
    elapsed = round(job.finished - job.started)
    job.result = ("[interrupted: the dispatch bridge stopped while this job was running (factory "
                  "restart or update, a disconnect or a crash), so its claude process ended with it. "
                  "Work it committed in a lane clone may still be there; nothing was imported. "
                  f"Its transcript: factory logs {job.lane}. Send the task again, to the same lane "
                  "to continue its session if that survived (list_roles).]")
    try:
        log.write(f"■ job {job.id} interrupted after {elapsed}s")
    except OSError:
        pass


_TRANSCRIPT_MARK = re.compile(r"^(\d\d):(\d\d):(\d\d)  ([▶■]) job (\S+)", re.M)


def close_dangling_transcripts() -> int:
    """Append an end line to any lane transcript whose last job marker is a start
    with no end: the dashboard shows such a lane as running, and no job of this bridge
    is (the index missed it, or was lost). Reads only the end of each log."""
    closed = 0
    for path in sorted((STATE / "logs").glob("*.log")):
        try:
            with open(path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - 262_144))
                tail = f.read().decode("utf-8", "replace")
            last_active = path.stat().st_mtime
        except OSError:
            continue
        marks = _TRANSCRIPT_MARK.findall(tail)
        if not marks or marks[-1][3] != "▶":
            continue
        hh, mm, ss, _, job_id = marks[-1]
        lt = time.localtime(last_active)
        elapsed = ((lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
                   - (int(hh) * 3600 + int(mm) * 60 + int(ss))) % 86400
        lane = path.stem
        if _LANE_NAME.match(lane) and _JOB_ID.match(job_id):
            try:
                Transcript(lane).write(f"■ job {job_id} interrupted after {elapsed}s")
                closed += 1
            except OSError:
                pass
    return closed


def load_job_index() -> int:
    """Reload the index into _jobs at start-up; running records become interrupted
    (and the lane transcript, which the dashboard reads, gets an end line). Returns
    the number of jobs reloaded."""
    jobs = read_job_index()
    now, interrupted = time.time(), []
    for job in jobs:
        if job.status == "running":
            interrupt_job(job, now)
            interrupted.append(job)
    with _jobs_lock:
        for job in jobs:
            _jobs.setdefault(job.id, job)
    if interrupted:
        save_job_index()
        for job in interrupted:
            audit({"event": "job-interrupted", "job_id": job.id, "role": job.role, "lane": job.lane})
    closed = close_dangling_transcripts()
    if closed:
        log_line(f"closed {closed} lane transcript(s) that showed a job still running")
    if jobs:
        log_line(f"job index: reloaded {len(jobs)} job(s), {len(interrupted)} interrupted")
    return len(jobs)


def active_jobs(role: str) -> dict:
    """{lane name: job} for the role's lanes that are in use. A lane stays in use
    until its job has fully ended, including importing its branches."""
    with _jobs_lock:
        return {j.lane: j for j in _jobs.values() if j.role == role and not j.done.is_set()}


def session_file(lane: str) -> Path:
    return STATE / "sessions" / lane


def session_meta_file(lane: str) -> Path:
    return STATE / "sessions" / f"{lane}.json"


def read_session_meta(lane: str) -> dict:
    """{session_id, total_cost_usd, context_tokens} after the lane's last job, or {}."""
    try:
        meta = json.loads(session_meta_file(lane).read_text())
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def saved_session(lane: str) -> tuple:
    """(session id the lane resumes by default, its metadata if it matches that id)."""
    try:
        sid = session_file(lane).read_text().strip() or None
    except OSError:
        return None, {}
    meta = read_session_meta(lane)
    total = meta.get("total_cost_usd")
    # json.loads accepts NaN/Infinity; treat those (and non-numbers) as unknown.
    if (not isinstance(total, (int, float)) or isinstance(total, bool)
            or not math.isfinite(total)):
        return sid, {}
    return sid, (meta if sid and meta.get("session_id") == sid else {})


def save_session(job: Job) -> None:
    session_file(job.lane).write_text(job.session_id + "\n")
    if job.session_cost_usd is None:
        # End total unknown: drop stale metadata rather than guess the next job's start.
        session_meta_file(job.lane).unlink(missing_ok=True)
        return
    meta = {"session_id": job.session_id, "total_cost_usd": job.session_cost_usd,
            "context_tokens": job.context_tokens, "model": job.model}
    session_meta_file(job.lane).write_text(json.dumps(meta) + "\n")


class Transcript:
    """Human-readable, append-only log `factory logs` tails."""

    def __init__(self, lane: str):
        self.path = STATE / "logs" / f"{lane}.log"

    def write(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}  {line}\n")


def render_event(event: dict, log: Transcript, job: Job) -> None:
    """Turn one stream-json event into transcript lines and job state."""
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        job.session_id = event.get("session_id") or job.session_id
        model = event.get("model")
        if isinstance(model, str) and model.strip() and job.model_used is None:
            job.model_used = one_line(model, 80)
            log.write(f"   model: {job.model_used}")
    elif etype == "assistant":
        usage = (event.get("message") or {}).get("usage")
        # Skip subagent messages: their usage is the subagent's context, not the session's.
        if isinstance(usage, dict) and not event.get("parent_tool_use_id"):
            job.context_tokens = sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                for para in sanitize(block["text"], 4000).strip().splitlines():
                    log.write(f"   {para}")
            elif block.get("type") == "tool_use":
                log.write(f"🔧 {block.get('name')}: {one_line(json.dumps(block.get('input', {})), 140)}")
    elif etype == "user":
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                mark = "✗" if block.get("is_error") else "↳"
                log.write(f"   {mark} {one_line(content or '', 140)}")
                for mode in _TEST_MODE.findall(sanitize(str(content or ""), 20_000)):
                    if mode not in job.test_modes and len(job.test_modes) < 10:
                        job.test_modes.append(mode)
    elif etype == "result":
        job.session_id = event.get("session_id") or job.session_id
        job.result = str(event.get("result") or "")
        if event.get("subtype") == "error_max_turns":
            job.status = "failed"
            job.result = (job.result + "\n" if job.result else "") + (
                f"[stopped at the role's MAX_TURNS cap of {job.max_turns} turns; the lane keeps its "
                "session, so a follow-up task on this lane can continue the work]")
        total = event.get("total_cost_usd")
        # A non-finite total (claude can report NaN) counts as missing.
        if (isinstance(total, (int, float)) and not isinstance(total, bool)
                and math.isfinite(total)):
            job.session_cost_usd = float(total)
        if event.get("is_error"):
            job.status = "failed"


class LaneError(Exception):
    """A lane step (prepare, export, import) that failed; its text is safe to report."""


def kill_group(proc: subprocess.Popen | None, sig: int = signal.SIGKILL) -> None:
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def tail_reader(stream, keep: int = 65536) -> tuple:
    """Drain a binary stream on a thread, keeping only its last `keep` bytes."""
    buf = bytearray()

    def run():
        for chunk in iter(lambda: stream.read1(65536), b""):
            buf.extend(chunk)
            if len(buf) > keep:
                del buf[:len(buf) - keep]

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, buf


def excerpt(data: bytes, limit: int = 2000) -> str:
    text = data.decode("utf-8", "replace").strip()
    return sanitize(text[-limit:], limit) or "(no error output)"


def valid_lane_dir(path: str) -> bool:
    return (path.startswith("/") and len(path) <= 4096
            and not re.search(r"[\x00-\x1f\x7f-\x9f]", path))


# The same rules as lane_identity_problem in factory/policy.py and
# check_identity in templates/agent/factory-lane ("{lane}" is replaced there).
_IDENTITY_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f<>]")
_IDENTITY_EMAIL = re.compile(r"[^-@\s\x00-\x1f\x7f-\x9f<>][^@\s\x00-\x1f\x7f-\x9f<>]*@[^@\s\x00-\x1f\x7f-\x9f<>]+")


def lane_identity() -> list:
    """The lanes' git identity as [name, email] (either may be empty: factory-lane's
    default), from the snapshot start-dispatch.sh wrote of [git] author_name and
    author_email; [] when there is none. Parsed as data and validated; raises
    LaneError on anything else, so a bad value never reaches a lane."""
    path = STATE / "lane-identity.conf"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise LaneError(f"can't read {path}: {type(e).__name__}")
    values = {}
    for line in text.split("\n"):
        if not line:
            continue
        pair = match_conf_line(line)
        if not pair or pair[0] not in ("AUTHOR_NAME", "AUTHOR_EMAIL") or pair[0] in values:
            raise LaneError(f"{path}: not an AUTHOR_NAME or AUTHOR_EMAIL line: {one_line(line, 80)!r}")
        values[pair[0]] = pair[1]
    name, email = values.get("AUTHOR_NAME", ""), values.get("AUTHOR_EMAIL", "")
    if name and (not name.strip() or len(name) > 128 or _IDENTITY_BAD.search(name) or name.startswith("-")):
        raise LaneError(f"{path}: invalid git author name {one_line(name, 80)!r}")
    if email and (len(email) > 254 or not _IDENTITY_EMAIL.fullmatch(email)):
        raise LaneError(f"{path}: invalid git author email {one_line(email, 80)!r}")
    return [name, email] if name or email else []


def prepare_lane(job: Job, log: Transcript) -> str | None:
    """Clone or refresh the lane's private clone(s) in the role sandbox, with the
    lanes' git identity (lane_identity). Returns the lane dir (the job's cwd), None
    if the job was cancelled meanwhile; raises LaneError."""
    log.write(f"   preparing lane {job.lane} (git clone/fetch from the project)")
    argv = [SBX, "exec", sandbox_name(job.role), "factory-lane", "prepare", str(PROJECT), job.lane,
            *lane_identity()]
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as proc:
        job.proc = proc
        out_thread, out = tail_reader(proc.stdout, 8192)
        err_thread, err = tail_reader(proc.stderr)
        try:
            code = proc.wait(timeout=PREPARE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            proc.wait()
            raise LaneError(f"factory-lane prepare timed out after {PREPARE_TIMEOUT_SECONDS}s")
        finally:
            out_thread.join(timeout=5)
            err_thread.join(timeout=5)
    if job.status == "cancelled":
        return None
    if code != 0:
        raise LaneError(f"factory-lane prepare failed (exit {code}): {excerpt(bytes(err))}")
    lines = bytes(out).decode("utf-8", "replace").splitlines()
    lane_dir = lines[-1] if lines else ""
    if not valid_lane_dir(lane_dir):
        raise LaneError("factory-lane prepare did not print a valid lane directory "
                        f"(got {one_line(lane_dir, 200)!r})")
    return lane_dir


def export_bundle(job: Job, rel: str, dest: Path, full: bool = False) -> bool:
    """Stream `factory-lane export` of one repo into dest. True if a bundle was
    written, False if the lane has nothing to export; raises LaneError. full:
    the branches' whole history instead of only what the host lacks."""
    argv = [SBX, "exec", sandbox_name(job.role), "factory-lane", "export", str(PROJECT), job.lane, rel]
    if full:
        argv.append("full")
    written, too_big, timed_out = 0, False, threading.Event()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out, subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True) as proc:
        job.proc = proc
        err_thread, err = tail_reader(proc.stderr)

        def expire():
            timed_out.set()
            kill_group(proc)

        timer = threading.Timer(EXPORT_TIMEOUT_SECONDS, expire)
        timer.daemon = True
        timer.start()
        try:
            for chunk in iter(lambda: proc.stdout.read1(1 << 20), b""):
                written += len(chunk)
                if written > MAX_BUNDLE_BYTES:
                    too_big = True
                    kill_group(proc)
                    break
                out.write(chunk)
            code = proc.wait()
        finally:
            timer.cancel()
            err_thread.join(timeout=5)
    if too_big:
        raise LaneError(f"{rel}: the exported bundle exceeds {MAX_BUNDLE_BYTES} bytes; not imported")
    if timed_out.is_set():
        raise LaneError(f"{rel}: factory-lane export timed out after {EXPORT_TIMEOUT_SECONDS}s")
    if code == 3:
        return False
    if code != 0:
        raise LaneError(f"{rel}: factory-lane export failed (exit {code}): {excerpt(bytes(err))}")
    if written == 0:
        raise LaneError(f"{rel}: factory-lane export succeeded but sent no bundle")
    return True


def host_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """git in a host repo with a fixed argv: no shell, no hooks, no fsmonitor, no
    prompts, and none of the bridge's own GIT_* environment."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", *GIT_SAFE, "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, env=env, timeout=GIT_TIMEOUT_SECONDS)


_import_lock = threading.Lock()  # one git fetch into the host project at a time


def fetch_bundle(repo: Path, bundle: Path, namespace: str) -> subprocess.CompletedProcess:
    """The one fixed fetch of a lane's bundle into the host repo's remote-tracking
    refs. --no-prune: refs from earlier jobs stay; --no-recurse-submodules: no
    submodule fetches from the host's configured remotes."""
    with _import_lock:
        return host_git(repo, "fetch", "--no-tags", "--no-write-fetch-head", "--quiet",
                        "--no-prune", "--no-recurse-submodules", str(bundle),
                        f"+refs/heads/factory/*:{namespace}*")


def lane_refs(repo: Path, namespace: str) -> dict:
    """{short ref: full object id} of the host repo's refs under the lane's namespace."""
    with _import_lock:
        cp = host_git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", namespace)
    if cp.returncode != 0:
        raise LaneError(f"listing imported branches failed: {excerpt(cp.stderr)}")
    refs = {}
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        ref, _, sha = line.strip().rpartition(" ")
        if ref:
            refs[ref] = sha
    return refs


def import_lane(job: Job, log: Transcript) -> None:
    """Bring the lane's committed factory/* branches into the host project as
    refs/remotes/factory-<lane>/factory/*, one repo of project_repos at a time.
    The host repos come from the host's own listing, never from the sandbox.
    job.branches gets the refs this job created or moved, not every ref the lane
    ever brought back (the import never prunes)."""
    repos = project_repos(PROJECT)
    if not repos:
        job.import_errors.append("the project has no git repository to import into")
        return
    imports = STATE / "imports"
    imports.mkdir(exist_ok=True)
    namespace = f"refs/remotes/factory-{job.lane}/factory/"
    for i, rel in enumerate(repos):
        repo = PROJECT if rel == "." else PROJECT / rel
        bundle = imports / f"{job.id}-{i}.bundle"
        try:
            if not export_bundle(job, rel, bundle):
                continue
            before = lane_refs(repo, namespace)
            cp = fetch_bundle(repo, bundle, namespace)
            if cp.returncode != 0 and b"prerequisite" in cp.stderr.lower():
                # The lane left out commits the host turned out not to have (its
                # view of what the host has was stale, or the host repo changed
                # since). Once more with the branches' whole history.
                log.write(f"   {rel}: the project lacks some of the bundle's base commits; "
                          "exporting the branches with their full history instead")
                if not export_bundle(job, rel, bundle, full=True):
                    raise LaneError(f"{rel}: the lane had nothing to export on the second try")
                cp = fetch_bundle(repo, bundle, namespace)
            if cp.returncode != 0:
                raise LaneError(f"{rel}: importing the bundle failed: {excerpt(cp.stderr)}")
            changed = 0
            for ref, sha in lane_refs(repo, namespace).items():
                if before.get(ref) == sha:
                    continue  # an earlier job's branch, untouched by this one
                changed += 1
                branch = {"repo": rel, "ref": one_line(ref, 200), "commit": one_line(sha[:7], 40)}
                job.branches.append(branch)
                where = "" if rel == "." else f" in {one_line(rel, 100)}"
                log.write(f"⎇ branch {branch['ref']} at {branch['commit']}{where}")
            if not changed:
                log.write(f"   {rel}: the bundle brought no new commits")
        except (LaneError, OSError, subprocess.SubprocessError) as e:
            msg = one_line(str(e), 2000) if isinstance(e, LaneError) else \
                one_line(f"{rel}: import error: {type(e).__name__}: {e}", 2000)
            job.import_errors.append(msg)
            log.write(f"✗ {msg}")
        finally:
            try:
                bundle.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------
# Review packet: one markdown file per job in STATE/packets/, with what the user
# needs to judge the job in one place: the task, the branches it produced
# (commits and diffstat against the host's checked-out branch), the tests it
# reported, the broker calls its role made meanwhile, its cost and its report.

# Real factory-test output: at a line start, with one of its mode labels. A
# mention elsewhere (a source comment in a diff, an instruction) doesn't count.
_TEST_MODE = re.compile(r"^factory-test: mode(?: was)?: "
                        r"(?:clean install|host node_modules fallback|project files only)[^\n]{0,200}", re.M)
# The reviewer's report starts with "VERDICT: APPROVED" or "VERDICT: REVISE" (its
# instructions); a report without that line falls back to the last bare word.
_VERDICT_LINE = re.compile(r"^\s*VERDICT:\s*(APPROVED|REVISE)\b", re.M)
_VERDICT = re.compile(r"\b(APPROVED|REVISE)\b")
_AUDIT_TS = "%Y-%m-%dT%H:%M:%SZ"


def packet_path(job_id: str) -> Path:
    return STATE / "packets" / f"{job_id}.md"


def branch_detail(repo: Path, ref: str) -> list:
    """Commits and diffstat of an imported branch against the host repo's HEAD.
    Read-only git with a fixed argv: hooks and fsmonitor off (GIT_SAFE), and
    external diff drivers and textconv off, since the branch's own
    .gitattributes could name one."""
    full = ref if ref.startswith("refs/") else f"refs/remotes/{ref}"
    lines = []
    cp = host_git(repo, "log", "--no-color", "--no-decorate", "--format=%h %s",
                  f"--max-count={MAX_PACKET_LINES}", f"HEAD..{full}")
    if cp.returncode == 0:
        commits = cp.stdout.decode("utf-8", "replace").splitlines()
        more = "+" if len(commits) >= MAX_PACKET_LINES else ""
        lines.append(f"Commits past HEAD ({len(commits)}{more}):")
        lines += [f"    {one_line(c, 160)}" for c in commits] or ["    (none: HEAD already has this branch)"]
    else:
        lines.append(f"Commits: git log failed: {excerpt(cp.stderr, 300)}")
    cp = host_git(repo, "diff", "--stat=120", "--no-color", "--no-ext-diff", "--no-textconv",
                  f"HEAD...{full}")
    if cp.returncode == 0:
        stat = cp.stdout.decode("utf-8", "replace").splitlines()
        lines.append("Diffstat (HEAD...branch):")
        lines += [f"    {one_line(s, 160)}" for s in stat[:MAX_PACKET_LINES]] or ["    (no changes)"]
        if len(stat) > MAX_PACKET_LINES:
            lines.append(f"    ... {len(stat) - MAX_PACKET_LINES} more lines")
    else:
        lines.append(f"Diffstat: git diff failed: {excerpt(cp.stderr, 300)}")
    return lines


def broker_calls(role: str, start: float, end: float) -> list:
    """The broker audit records of this role between start and end (epoch seconds),
    one line each. Lanes share a role, so parallel lanes' calls show up together.
    [] when the log is missing or unreadable; bad lines are skipped."""
    try:
        if BROKER_AUDIT.stat().st_size > 50_000_000:
            return ["(the broker audit log is too large to scan)"]
        text = BROKER_AUDIT.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = []
    for raw in text.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not rec.get("tool") or rec.get("principal", rec.get("role")) != role:
            continue
        try:
            ts = calendar.timegm(time.strptime(str(rec.get("ts")), _AUDIT_TS))
        except (TypeError, ValueError):
            continue
        if ts < start - 2 or ts > end + 2:
            continue
        # A call has a decision (allow, deny, error, staged); the gateway's approval
        # events (approval-requested, approval-granted, ...) have an event name instead.
        line = f"{str(rec['ts'])[11:19]}  {one_line(rec['tool'], 40)}  {one_line(rec.get('decision') or rec.get('event') or '', 24)}"
        if rec.get("reason"):
            line += f"  {one_line(rec['reason'], 200)}"
        lines.append(line)
    if len(lines) > MAX_PACKET_LINES:
        lines = lines[:MAX_PACKET_LINES] + [f"... {len(lines) - MAX_PACKET_LINES} more"]
    return lines


def write_packet(job: Job) -> None:
    """Write STATE/packets/<job id>.md and record its path on the job."""
    s = job.summary()
    model = job.model_used or job.model or "default"
    rows = [("Lane", job.lane),
            ("Started", f"{time.strftime(_AUDIT_TS, time.gmtime(job.started))}, {s['elapsed_seconds']} s"),
            ("Model", model + (f", effort {job.effort}" if job.effort else "")),
            ("Session", job.session_mode or "unknown")]
    cost = []
    if "cost_usd" in s:
        cost.append(f"${s['cost_usd']:.4f} this job")
    if "session_cost_usd" in s:
        cost.append(f"${s['session_cost_usd']:.4f} the session so far")
    if job.context_tokens is not None:
        cost.append(f"context ~{round(job.context_tokens / 1000)}k tokens")
    if cost:
        rows.append(("Cost", ", ".join(cost)))
    line = _VERDICT_LINE.search(job.result)
    verdicts = [line.group(1)] if line else _VERDICT.findall(job.result)
    if verdicts:
        rows.append(("Verdict", verdicts[-1]))
    parts = ["\n".join([f"# {job.role} job {job.id}: {job.status}", ""] + [f"- {k}: {v}" for k, v in rows]),
             "## Task\n\n" + sanitize(job.task, 8000).strip()]
    if job.branches is not None:
        lines = []
        for b in job.branches:
            repo = PROJECT if b["repo"] == "." else PROJECT / b["repo"]
            where = "" if b["repo"] == "." else f" in {b['repo']}"
            lines.append(f"### {b['ref']} at {b['commit']}{where}\n")
            try:
                lines += branch_detail(repo, b["ref"])
            except (OSError, subprocess.SubprocessError) as e:
                lines.append(f"(could not read the branch: {type(e).__name__}: {one_line(str(e), 200)})")
            lines.append("")
        lines += [f"Import error: {err}" for err in job.import_errors or []]
        parts.append("## Branches\n\n" + ("\n".join(lines).strip() or "(none)"))
    parts.append("## Tests\n\n" + ("\n".join(f"- {m}" for m in job.test_modes)
                                   or "(no factory-test run reported)"))
    calls = broker_calls(job.role, job.started, job.finished or time.time())
    parts.append(f"## Broker calls by the {job.role} role during the job\n\n"
                 + ("\n".join(f"- {c}" for c in calls) or "(none)"))
    parts.append("## Report\n\n" + (sanitize(job.result).strip() or "(no report)"))
    path = packet_path(job.id)
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    job.packet = str(path)


def claude_argv(job: Job, workdir: str, lane_root: str | None, resume: str | None,
                fresh_id: str | None) -> list:
    argv = [SBX, "exec", "-w", workdir]
    env = list(ROLE_ENV) + [f"FACTORY_PROJECT={PROJECT}", f"FACTORY_LANE={job.lane}",
                            f"FACTORY_SCRATCH={ROLES_SCRATCH / job.role}"]
    if lane_root:
        env.append(f"FACTORY_LANE_ROOT={lane_root}")
    for item in env:
        argv += ["-e", item]
    argv += [sandbox_name(job.role),
             "claude", "-p", job.task,
             "--output-format", "stream-json", "--verbose",
             "--permission-mode", "bypassPermissions",
             "--append-system-prompt", role_system_prompt(job.role, job.instructions)]
    if job.model:
        argv += ["--model", job.model]
    if job.effort:
        argv += ["--effort", job.effort]
    if job.max_turns:
        argv += ["--max-turns", str(job.max_turns)]
    # The session id in the argv is also what cancel_job's pkill pattern matches.
    argv += ["--resume", resume] if resume else ["--session-id", fresh_id]
    return argv


def run_claude(job: Job, log: Transcript, workdir: str, lane_root: str | None) -> None:
    attempt_resume = job.resume if job.resume and _SESSION_ID.match(job.resume) else None
    while job.status != "cancelled":
        fresh_id = None if attempt_resume else str(uuid.uuid4())
        job.claude_session = attempt_resume or fresh_id
        argv = claude_argv(job, workdir, lane_root, attempt_resume, fresh_id)
        stderr_lines: list = []
        timed_out = threading.Event()
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                              start_new_session=True) as proc:
            job.proc = proc
            if job.status == "cancelled":  # cancelled while this run was starting
                kill_group(proc, signal.SIGTERM)

            def expire():
                timed_out.set()
                stop_claude_in_sandbox(job)
                kill_group(proc)

            timer = threading.Timer(job.max_job_seconds, expire) if job.max_job_seconds else None
            if timer:
                timer.daemon = True
                timer.start()
            err_thread = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
            err_thread.start()
            try:
                for raw in proc.stdout:
                    try:
                        render_event(json.loads(raw), log, job)
                    except json.JSONDecodeError:
                        if raw.strip():
                            log.write(f"   {one_line(raw, 200)}")
                    except Exception as e:  # noqa: BLE001 - one odd event must not wedge the job
                        log.write(f"   (skipped a malformed event: {one_line(f'{type(e).__name__}: {e}', 200)})")
                code = proc.wait()
            finally:
                if timer:
                    timer.cancel()
            err_thread.join(timeout=5)
        stderr = "".join(stderr_lines)
        if job.status == "cancelled":
            break
        if timed_out.is_set():
            job.status = "failed"
            job.result = (job.result + "\n" if job.result else "") + (
                f"[stopped: the job ran past the role's MAX_JOB_SECONDS limit of {job.max_job_seconds}s "
                "and was killed; the lane is free again]")
            log.write(f"   (killed after the {job.max_job_seconds}s MAX_JOB_SECONDS limit)")
            break
        # A saved session can vanish (e.g. the sandbox was recreated): retry fresh once.
        if code != 0 and attempt_resume and "No conversation found" in (stderr + job.result):
            log.write("   (previous session not found; starting a fresh session)")
            attempt_resume, job.result, job.status = None, "", "running"
            job.start_cost_usd, job.session_cost_usd, job.context_tokens = 0.0, None, None
            job.model_used = None
            continue
        if code != 0 and job.status == "running":
            job.status = "failed"
            job.result = job.result or f"claude exited with status {code}: {sanitize(stderr, 3000)}"
        break


def run_job(job: Job) -> None:
    log = Transcript(job.lane)
    log.write("─" * 60)
    log.write(f"▶ job {job.id} from the Coordinator")
    for para in sanitize(job.task, 4000).strip().splitlines():
        log.write(f"   > {para}")
    claude_started = False
    try:
        # The job holds its starting total; until save_session records the new end
        # total, the session's cost is unknown (a crash or kill must not leave the old one).
        session_meta_file(job.lane).unlink(missing_ok=True)
        workdir, lane_root = str(PROJECT), None
        if job.rw:
            try:
                lane_root = prepare_lane(job, log)
            except LaneError as e:
                job.status = "failed"
                job.result = str(e)
            if lane_root:
                workdir = lane_root
        if job.status == "running":
            claude_started = True
            run_claude(job, log, workdir, lane_root)
        if job.status == "running":
            job.status = "done"
        if job.session_id:
            save_session(job)
    except Exception as e:  # noqa: BLE001 - surface every failure to the Coordinator
        if job.status != "cancelled":
            job.status = "failed"
        error = f"dispatch error: {type(e).__name__}: {e}"
        job.result = f"{job.result}\n[{error}]" if job.result else error
    finally:
        try:
            if job.rw and claude_started:
                import_lane(job, log)
        except Exception as e:  # noqa: BLE001
            job.import_errors.append(one_line(f"import error: {type(e).__name__}: {e}", 2000))
        job.finished = time.time()
        try:
            write_packet(job)
            log.write(f"📄 review packet: factory packet {job.id}")
        except Exception as e:  # noqa: BLE001 - the packet is a convenience, never the job's verdict
            log.write(f"   (no review packet: {one_line(f'{type(e).__name__}: {e}', 300)})")
        log.write(f"■ job {job.id} {job.status} after {round(job.finished - job.started)}s")
        audit({"event": "job-end", **job.summary()})
        save_job_index()
        job.done.set()


# --------------------------------------------------------------------------
# Tools

def session_note(meta: dict) -> str:
    tokens = meta.get("context_tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        size = "unknown context size"
    else:
        size = (f"~{round(tokens / 1000)}k" if tokens >= 1000 else str(tokens)) + " tokens"
    return f"{size}, ${float(meta.get('total_cost_usd') or 0):.2f} so far"


def tool_list_roles(args: dict) -> str:
    roles = load_roles()
    sandboxes = existing_sandboxes()
    lines = []
    for name, info in roles.items():
        lanes = lane_names(name, info["lanes"])
        active = active_jobs(name)
        created = sandbox_name(name) in sandboxes

        def lane_state(lane):
            job = active.get(lane)
            return f"busy ({job.id})" if job else ("idle" if created else "not created")

        if len(lanes) == 1:
            state = lane_state(name)
        elif not created and not active:
            state = "not created"
        else:
            state = f"{len(lanes)} lanes: " + ", ".join(f"{l} {lane_state(l)}" for l in lanes)
        access = "edits private git clones" if info["workspace"] == "rw" else "read-only"
        net = ", ".join(info["broker_tools"]) or "none"
        model = info["model"] or "default"
        others = [m for m in info["models"] if m != info["model"]]
        if others:
            model += f"; can also run on: {', '.join(others)}"
        if info["effort"]:
            model += f"; effort: {info['effort']}"
        line = (f"- {name} [{state}]: {info['description']} "
                f"({access}; network tools: {net}; model: {model})")
        notes = []
        for lane in lanes:
            _, meta = saved_session(lane)
            try:
                if meta:
                    notes.append((lane, session_note(meta)))
            except (TypeError, ValueError):
                pass  # malformed metadata: show nothing rather than fail list_roles
        if len(lanes) == 1 and notes:
            line += f"; session: {notes[0][1]}"
        elif notes:
            line += "; sessions: " + "; ".join(f"{lane} {note}" for lane, note in notes)
        lines.append(line)
    return "Roles:\n" + "\n".join(lines) if lines else "No roles are defined."


def tool_delegate(args: dict) -> str:
    role, task = args["role"], args["task"].strip()
    roles = load_roles(include_disabled=True)
    if role in roles and not roles[role]["enabled"]:
        raise Denied(f"the {role} role is disabled on this machine (it is optional and off by default). "
                     f"To enable it, the user adds a line '{role}' to the enabled-roles file in the "
                     "factory config folder (~/.config/ai-code-factory/enabled-roles) and runs "
                     "'factory start'. Until then, do the work with the roles in list_roles.")
    roles = {name: info for name, info in roles.items() if info["enabled"]}
    if role not in roles:
        raise Denied(f"unknown role {role!r}; available: {', '.join(roles) or 'none'}")
    if not task:
        raise Denied("task is empty")
    if len(task) > MAX_TASK_CHARS:
        raise Denied(f"task exceeds {MAX_TASK_CHARS} characters")
    # Waiting is the default: one call per job. wait_seconds: 0 starts the job and
    # returns its id at once, for running several roles in parallel.
    wait = args.get("wait_seconds", MAX_WAIT_SECONDS)
    if not isinstance(wait, int) or isinstance(wait, bool) or wait < 0:
        raise Denied("wait_seconds must be a non-negative integer")
    lanes = roles[role]["lanes"]
    lane_arg = args.get("lane")
    if lane_arg is not None and (not isinstance(lane_arg, int) or isinstance(lane_arg, bool)
                                 or not 1 <= lane_arg <= lanes):
        raise Denied(f"lane must be an integer from 1 to {lanes} for {role}")
    model = roles[role]["model"]
    if "model" in args:
        model = args["model"].strip()
        if model not in roles[role]["models"]:
            allowed = ", ".join(roles[role]["models"]) or "none (only its default)"
            raise Denied(f"{role} can't run on model {model!r}; allowed: {allowed}")
    effort = roles[role]["effort"]
    if "effort" in args:
        effort = args["effort"].strip()
        if effort not in EFFORTS:
            raise Denied(f"effort must be one of: {', '.join(EFFORTS)}")
    rw = roles[role]["workspace"] == "rw"
    if rw and not project_repos(PROJECT):
        raise Denied(f"{role} works in git clones of the project, but the project is not a git "
                     "repository and has no git repositories in its immediate subfolders")
    instructions = role_instructions(role)
    if not sandbox_exists(sandbox_name(role)):
        raise Denied(f"the {role} sandbox has not been created; ask the user to run 'factory start' "
                     "again (role sandboxes are created once a Claude login is stored)")
    with _jobs_lock:
        busy = {j.lane: j for j in _jobs.values() if j.role == role and not j.done.is_set()}
        if lane_arg is not None:
            lane = lane_name(role, lane_arg)
            if lane in busy:
                raise Denied(f"{lane} is busy with job {busy[lane].id}; wait for it, cancel it "
                             "or pick another lane")
        else:
            idle = [l for l in lane_names(role, lanes) if l not in busy]
            if not idle:
                ids = ", ".join(j.id for j in busy.values())
                if lanes == 1:
                    raise Denied(f"{role} is busy with job {ids}; wait for it or cancel it")
                raise Denied(f"all {lanes} lanes of {role} are busy (jobs {ids}); wait for one or cancel it")
            lane = idle[0]
        resume, start_cost, mode = None, 0.0, "fresh session (requested)"
        if not args.get("fresh_session"):
            resume, meta = saved_session(lane)
            cap, size = roles[role]["max_session_tokens"], (meta or {}).get("context_tokens", 0)  # absent: saved before the size was tracked
            previous = (meta or {}).get("model")
            if resume and not _SESSION_ID.match(resume):
                resume = None
                mode = "fresh session (automatic: the saved session id is malformed)"
            elif resume and cap and (isinstance(size, bool) or not isinstance(size, int)):
                resume = None
                mode = ("fresh session (automatic: the previous one's context size is unknown, so the "
                        f"{round(cap / 1000)}k cap can't be checked; it starts without that history)")
            elif resume and cap and size > cap:
                resume = None
                mode = (f"fresh session (automatic: the previous one carried ~{round(size / 1000)}k tokens, "
                        f"over the {round(cap / 1000)}k cap; it starts without that history)")
            elif resume and isinstance(previous, str) and previous != model:
                # A resumed session keeps the model it was saved with, and each model has
                # its own prompt cache: a model switch starts over rather than resume.
                resume = None
                mode = (f"fresh session (automatic: the previous one ran on {previous or 'the default model'}, "
                        f"this task on {model or 'the default model'}; it starts without that history)")
            elif resume:
                start_cost = meta.get("total_cost_usd")  # None if unknown
                mode = "continuing its previous session"
            else:
                mode = "fresh session"
        job = Job(role, task, resume, model, start_cost, instructions, effort, lane=lane, rw=rw)
        job.session_mode = mode
        job.max_turns = roles[role]["max_turns"]
        job.max_job_seconds = roles[role]["max_job_seconds"]
        _jobs[job.id] = job
    save_job_index()
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    if wait:
        return job_report(job, wait, session=mode)
    return f"Started job {job.id} on {lane} ({mode}). Call wait_for_job with this job id to get the report."


def job_report(job: "Job", timeout: int, **extra) -> str:
    """The job's summary after waiting for it: not at all for timeout 0, else
    between MIN_WAIT_SECONDS and MAX_WAIT_SECONDS, returning as soon as it ends."""
    if timeout:
        job.done.wait(min(max(timeout, MIN_WAIT_SECONDS), MAX_WAIT_SECONDS))
    summary = {**job.summary(with_result=True), **extra}
    if not job.done.is_set():
        summary["note"] = "still running; call wait_for_job again"
    return json.dumps(summary, ensure_ascii=False)


def tool_wait_for_job(args: dict) -> str:
    job = _jobs.get(args["job_id"])
    if job is None:
        raise Denied(f"unknown job {args['job_id']!r}")
    timeout = args.get("timeout_seconds", MAX_WAIT_SECONDS)
    if not isinstance(timeout, int) or timeout < 0:
        raise Denied("timeout_seconds must be a non-negative integer")
    return job_report(job, timeout)


def tool_job_status(args: dict) -> str:
    limit = args.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_INDEXED_JOBS:
        raise Denied(f"limit must be an integer from 1 to {MAX_INDEXED_JOBS}")
    with _jobs_lock:
        recent = sorted(_jobs.values(), key=lambda j: j.started, reverse=True)[:limit]
    if not recent:
        return "No jobs yet."
    return json.dumps([j.summary() for j in recent], ensure_ascii=False)


def cancel_pattern(session: str) -> str:
    """pkill -f pattern for the one claude whose argv carries this session id."""
    return f"^claude -p .*--(session-id|resume) {session}( |$)"


def stop_claude_in_sandbox(job: Job) -> None:
    """SIGINT this job's claude inside its sandbox (other lanes keep running); killing
    the local sbx exec client alone isn't known to stop the remote process."""
    session = job.claude_session
    if session and _SESSION_ID.match(session):
        try:
            subprocess.run([SBX, "exec", sandbox_name(job.role), "pkill", "-INT", "-f",
                            cancel_pattern(session)], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass


def tool_cancel_job(args: dict) -> str:
    job = _jobs.get(args["job_id"])
    if job is None:
        raise Denied(f"unknown job {args['job_id']!r}")
    if job.status != "running":
        return f"Job {job.id} already {job.status}."
    job.status = "cancelled"
    save_job_index()
    stop_claude_in_sandbox(job)
    kill_group(job.proc, signal.SIGTERM)
    job.done.wait(30)
    return f"Cancelled job {job.id} on {job.lane}."



# --------------------------------------------------------------------------
# Factory docs (factory_docs): the factory's own markdown, as snapshotted by
# start-dispatch.sh into STATE/docs/ (README.md, setup-wsl.md, guides/*.md),
# split into sections the Coordinator can list, search and read.

MAX_DOCS_CHARS = 12_000
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_STOPWORDS = set("""a an and are as at be but by can do does for from get got have how i if in into is it
its me my not of on or so that the their them then there this to too was we what when where which who
why will with you your""".split())


def doc_slug(heading: str) -> str:
    """GitHub-style anchor slug: lowercase, punctuation dropped, spaces to dashes."""
    text = re.sub(r"[`*_\[\]()]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def doc_sources() -> list:
    """[(doc id, path)] of the snapshotted docs, guides first, in order."""
    base = STATE / "docs"
    found = []
    for path in sorted((base / "guides").glob("*.md")):
        found.append(("guide-" + re.sub(r"^\d+-", "", path.stem), path))
    for doc_id, name in (("readme", "README.md"), ("wsl", "setup-wsl.md")):
        if (base / name).is_file():
            found.append((doc_id, base / name))
    return found


def load_doc_sections() -> list:
    """Every ##/### section of every snapshotted doc, as dicts with id, doc, title,
    level and text (a ## section's text includes its ### subsections). Headings
    inside fenced code blocks don't count."""
    sections = []
    for doc_id, path in doc_sources():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        doc_title, fence, seen = doc_id, False, {}
        heads = []  # (line index, level, title)
        for i, line in enumerate(lines):
            if _FENCE.match(line):
                fence = not fence
                continue
            m = None if fence else _HEADING.match(line)
            if not m:
                continue
            level, title = len(m.group(1)), m.group(2).strip()
            if level == 1:
                doc_title = title
            heads.append((i, level, title))
        intro_end = next((i for i, lvl, _ in heads if lvl > 1), len(lines))
        intro = "\n".join(lines[:intro_end]).strip()
        if intro:
            sections.append({"id": f"{doc_id}/intro", "doc": doc_title, "title": doc_title,
                             "level": 1, "text": intro, "own": intro})
        for n, (i, level, title) in enumerate(heads):
            if level == 1:
                continue
            end = next((j for j, lvl, _ in heads[n + 1:] if lvl <= level), len(lines))
            slug = doc_slug(title) or "section"
            if slug in seen:
                seen[slug] += 1
                slug = f"{slug}-{seen[slug]}"
            else:
                seen[slug] = 0
            own_end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
            sections.append({"id": f"{doc_id}/{slug}", "doc": doc_title, "title": title,
                             "level": level, "text": "\n".join(lines[i:end]).strip(),
                             "own": "\n".join(lines[i:own_end]).strip()})
    return sections


def _capped(parts: list, limit: int = MAX_DOCS_CHARS) -> str:
    out, used = [], 0
    for part in parts:
        if used + len(part) > limit:
            out.append(part[: max(0, limit - used)] + "\n[truncated; read a narrower section]")
            break
        out.append(part)
        used += len(part)
    return "\n\n".join(out)


def tool_factory_docs(args: dict) -> str:
    sections = load_doc_sections()
    if not sections:
        raise Denied("the factory docs are not loaded; ask the user to run 'factory start' again "
                     "(it copies them into the dispatch state folder)")
    section, topic = args.get("section", "").strip(), args.get("topic", "").strip()
    if section:
        match = next((s for s in sections if s["id"] == section), None)
        if match is None:
            near = [s["id"] for s in sections if section.split("/")[-1] in s["id"]][:5]
            raise Denied(f"no docs section {section!r}" + (f"; did you mean: {', '.join(near)}" if near else
                                                         "; call factory_docs with no arguments for the list"))
        return sanitize(_capped([f"[{match['doc']}] {match['id']}\n\n{match['text']}"]))
    if topic:
        terms = [t for t in dict.fromkeys(_WORD.findall(topic.lower()))
                 if len(t) > 1 and t not in _STOPWORDS]
        if not terms:
            raise Denied("topic needs at least one meaningful word, e.g. \"git push\"")
        scored = []
        for s in sections:
            # A section's own text, without its subsections: they are scored on their own.
            title, body = s["title"].lower(), s["own"].lower()
            hits = [t for t in terms if t in title or t in body]
            if not hits:
                continue
            score = sum(5 * title.count(t) + min(body.count(t), 10) for t in hits)
            # Sections that match more of the question beat ones that repeat one word.
            # Long sections mention everything somewhere; don't let size alone win.
            scored.append((score * (len(hits) / len(terms)) ** 2 / (1 + len(body) / 6000), s))
        if not scored:
            return (f"Nothing in the factory docs matches {topic!r}. Call factory_docs with no "
                    "arguments for the table of contents.")
        scored.sort(key=lambda x: -x[0])
        top = [s for _, s in scored[:3]]
        parts = [f"[{s['doc']}] {s['id']}\n\n{s['own']}" for s in top]
        more = [s["id"] for _, s in scored[3:8]]
        text = _capped(parts)
        if more:
            text += "\n\nAlso relevant (read with section): " + ", ".join(more)
        return sanitize(text)
    lines = ["Factory docs (read one with section=<id>, or search with topic=<words>):"]
    doc = None
    for s in sections:
        if s["doc"] != doc:
            doc = s["doc"]
            lines.append(f"\n{doc}")
        indent = "  " * max(0, s["level"] - 1)
        lines.append(f"{indent}- {s['id']}: {s['title']}")
    return sanitize("\n".join(lines))


TOOLS = {
    "list_roles": (tool_list_roles, "List the role agents, what each does, and whether it is idle or busy "
                   "(per lane for roles with several lanes). Roles with a saved session show its "
                   "context size and total cost so far.",
                   {}, []),
    "delegate": (tool_delegate, "Start a task on a role agent in its own sandbox and wait for it: by "
                 "default it waits up to 600 s (wait_seconds; a shorter value is stretched to at least "
                 "300 s, and it returns the moment the job ends) and returns the same result as "
                 "wait_for_job, so one call covers a whole job. wait_seconds: 0 returns the job id at "
                 "once, to run several roles in parallel and wait_for_job each. The role continues its "
                 "previous session unless fresh_session is true or the "
                 "session is over the role's size cap. Write a self-contained task: roles cannot see your "
                 "conversation. model (optional) runs this task on another of the role's allowed models "
                 "(list_roles shows them): a cheaper one for mechanical work, a stronger one for hard "
                 "problems or after a weaker one fell short. A model other than the one the role's saved "
                 "session ran on starts a fresh session. effort (optional: low, medium, high or xhigh) "
                 "sets how much the model thinks; omit it for the role's default. A role with several "
                 "lanes (list_roles) runs that many tasks at once, each lane with its own session and, "
                 "for roles that edit code, its own private clone; lane (optional, 1..lanes) picks one, "
                 "otherwise the lowest idle lane is used. A lane's next task continues that lane's "
                 "session, so send follow-ups (e.g. REVISE feedback) to the same lane.",
                 {"role": {"type": "string"}, "task": {"type": "string"},
                  "fresh_session": {"type": "boolean"}, "wait_seconds": {"type": "integer"},
                  "model": {"type": "string"}, "effort": {"type": "string"},
                  "lane": {"type": "integer"}},
                 ["role", "task"]),
    "wait_for_job": (tool_wait_for_job, "Wait for a job and return its status and, when finished, the "
                     "role's report. timeout_seconds defaults to 600 (the maximum); a shorter value is "
                     "stretched to at least 300, since every call costs you a turn, and the wait returns "
                     "the moment the job ends anyway; 0 returns the status right now. model is the job "
                     "session's main model (omitted until known; subagents may use others), cost_usd "
                     "is this job's cost (omitted if unknown), session_cost_usd the role session's "
                     "total so far, and context_tokens the approximate context the session now carries. "
                     "lane is the lane that ran it. For roles that edit code, branches lists the "
                     "factory/* branches this job created or moved in the project, as {repo, ref, commit} where ref "
                     "(e.g. factory-developer/factory/x) is a remote-tracking ref in that repo; "
                     "import_errors lists anything that could not be brought back. packet is the "
                     "path, on the user's machine, of the job's review packet (task, branch commits "
                     "and diffstat, tests, broker calls, cost, report); the user reads it with "
                     "`factory packet <job_id>`.",
                     {"job_id": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, ["job_id"]),
    "job_status": (tool_job_status, "Show the most recent jobs (20 by default; limit up to 200) and their "
                   "status. The list is kept on the user's machine across factory restarts and updates: "
                   "a job that was running when the bridge stopped shows as interrupted (its transcript "
                   "is `factory logs <lane>`), and wait_for_job on an old id returns its saved status, "
                   "a short report and, if one was written, its review packet.",
                   {"limit": {"type": "integer"}}, []),
    "cancel_job": (tool_cancel_job, "Stop a running job (other lanes of its role keep running).", {"job_id": {"type": "string"}}, ["job_id"]),
    "factory_docs": (tool_factory_docs, "The AI Code Factory's own documentation (the README, setup "
                     "guide and how-to guides of the factory itself, not of the user's project), for "
                     "questions about the factory: setup, factory commands, roles and lanes, broker "
                     "tools, local settings, getting and merging the developer's work, and error "
                     "messages. No arguments: the table of contents. topic: search (a few words, e.g. "
                     "\"git push\" or \"sandbox out of date\"), returns the best sections. section: one "
                     "section by its id from the contents (e.g. readme/local-settings).",
                     {"topic": {"type": "string"}, "section": {"type": "string"}}, []),
}

_TYPES = {"string": str, "boolean": bool, "integer": int}


def tool_list() -> list:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": p, "required": r}}
            for n, (_, d, p, r) in TOOLS.items()]


def call_tool(name: str, args: dict) -> tuple:
    started = time.monotonic()
    record = {"tool": name, "args": {k: one_line(v, 300) for k, v in args.items()}}
    try:
        if name not in TOOLS:
            raise Denied(f"unknown tool {name!r}")
        fn, _, props, required = TOOLS[name]
        for key in required:
            if key not in args:
                raise Denied(f"missing required argument {key!r}")
        for key, val in args.items():
            if key not in props:
                raise Denied(f"unexpected argument {key!r}")
            expected = _TYPES[props[key]["type"]]
            if not isinstance(val, expected) or (expected is int and isinstance(val, bool)):
                raise Denied(f"argument {key!r} must be a {props[key]['type']}")
        text = fn(args)
        record["decision"] = "allow"
        return text, False
    except Denied as e:
        record.update(decision="deny", reason=str(e))
        return f"DENIED: {e}", True
    except Exception as e:  # noqa: BLE001
        record.update(decision="error", reason=one_line(f"{type(e).__name__}: {e}", 500))
        return f"Dispatch error: {sanitize(str(e), 2000)}", True
    finally:
        record["ms"] = round((time.monotonic() - started) * 1000)
        audit(record)


# --------------------------------------------------------------------------
# MCP over streamable HTTP (JSON responses)

def auth_problem(header: str | None) -> str | None:
    try:
        expected = (STATE / "token.sha256").read_text().strip()
    except OSError:
        return "dispatch has no token configured"
    if not header or not header.startswith("Bearer "):
        return "missing or malformed Authorization header"
    got = hashlib.sha256(header[len("Bearer "):].strip().encode()).hexdigest()
    if not expected or not hmac.compare_digest(got, expected):
        return "token mismatch"
    return None


def server_instructions() -> str:
    """Briefing the Coordinator's Claude puts in its system prompt, with the live roles."""
    lines = [
        "You are the Coordinator of the AI Code Factory, a sandboxed multi-agent coding "
        "system. You run in an isolated microVM and cannot edit the project or run "
        "commands; you plan and delegate. Each role agent below works in its own sandbox "
        "and does not see your conversation. Know this before starting any task, "
        "whatever the project's own docs say.",
        "",
        "Roles:",
    ]
    roles = load_roles()
    for name, info in roles.items():
        access = "edits a private clone" if info["workspace"] == "rw" else "project read-only"
        if info["lanes"] > 1:
            access += f", {info['lanes']} lanes"
        lines.append(f"- {name} ({access}): {one_line(info['description'], 200)}")
    lines += [
        "",
        "Delegate self-contained tasks (goal, files, known facts, constraints, what done "
        "looks like); delegate waits for the job by default, so one call returns the report. Typical "
        "flow: " + ("researcher (only if outside information is needed) -> " if "researcher" in roles else "")
        + "developer -> reviewer, sending REVISE feedback back to the developer. Pick each task's model and "
        "effort (delegate's optional model and effort) as the cheapest that will do it well, "
        "stepping up after a weaker run falls short. Ask the user before anything irreversible.",
        "",
        "Roles that edit code work in private git clones of the project (its committed state; "
        "the user's uncommitted changes are not visible to them), never in the user's working "
        "tree. Tell them which branch to start from and to commit their work to a branch named "
        "factory/<topic>. When their job ends, those committed branches appear in the project "
        "as remote-tracking refs factory-<lane>/factory/<topic> (e.g. "
        "factory-developer/factory/rate-limit; the job result lists them under branches). "
        "Give the reviewer that exact ref to review, and tell the user the ref to merge. Every "
        "job leaves a review packet on the user's machine (the branch's commits and diffstat, "
        "tests, broker calls, cost and the report in one file): point the user at "
        "`factory packet <job_id>` instead of quoting a report at length.",
        "Questions about the factory itself (how to set it up or change it, factory commands, "
        "roles and lanes, broker tools, settings, error messages from factory commands): call "
        "factory_docs and answer from it, not from memory, citing the section. You can't run "
        "factory commands; tell the user the exact command to run in a terminal on their "
        "machine (the host), not in the dashboard.",
        "Roles with several lanes run that many tasks at once. Split large work into "
        "independent chunks, each on its own factory/<topic> branch in its own lane "
        "(delegate with wait_seconds: 0, then wait_for_job each, with the default timeout: "
        "short polls only cost turns), and send follow-ups for a chunk to the lane that "
        "wrote it (delegate's lane).",
    ]
    return "\n".join(lines)


def handle_rpc(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                  "instructions": server_instructions()}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        text, is_error = call_tool(params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def may_block(msg: dict) -> bool:
    """True for a tools/call that can wait minutes: delegate or wait_for_job with a
    positive wait (their defaults wait)."""
    params = msg.get("params")
    if msg.get("method") != "tools/call" or not isinstance(params, dict):
        return False
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return False
    key = {"delegate": "wait_seconds", "wait_for_job": "timeout_seconds"}.get(params.get("name"))
    if key is None:
        return False
    wait = args.get(key, MAX_WAIT_SECONDS)
    return isinstance(wait, int) and not isinstance(wait, bool) and wait > 0


def sse_event(obj: dict) -> bytes:
    return b"event: message\ndata: " + json.dumps(obj).encode() + b"\n\n"


class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: dict | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            return self._send(404)
        problem = auth_problem(self.headers.get("Authorization"))
        if problem:
            audit({"event": "auth-failure", "reason": problem})
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BYTES:
            return self._send(413, {"error": "request too large"})
        try:
            msg = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "parse error"}})
        if (isinstance(msg, dict) and msg.get("id") is not None and may_block(msg)
                and "text/event-stream" in (self.headers.get("Accept") or "").lower()):
            return self._stream_rpc(msg)
        resp = handle_rpc(msg) if isinstance(msg, dict) else None
        if resp is None:
            return self._send(202)
        self._send(200, resp)

    def _stream_rpc(self, msg: dict) -> None:
        """Answer a blocking tools/call as an SSE stream (MCP Streamable HTTP): the call
        runs in a worker while keepalives (and progress notifications, if the client
        sent a progressToken) go out every KEEPALIVE_SECONDS, then the response ends the
        stream. If the client goes away the job is untouched; the worker finishes alone."""
        meta = (msg.get("params") or {}).get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        if isinstance(token, bool) or not isinstance(token, (str, int)):
            token = None
        box: list = []

        def work() -> None:
            try:
                box.append(handle_rpc(msg))
            except Exception as e:  # noqa: BLE001
                box.append({"jsonrpc": "2.0", "id": msg.get("id"),
                            "error": {"code": -32603, "message": sanitize(f"internal error: {e}", 500)}})

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            beats = 0
            while True:
                worker.join(KEEPALIVE_SECONDS)
                if not worker.is_alive():
                    break
                beats += 1
                self.wfile.write(b": keepalive\n\n")
                if token is not None:
                    self.wfile.write(sse_event({"jsonrpc": "2.0", "method": "notifications/progress",
                                                "params": {"progressToken": token, "progress": beats,
                                                           "message": "still waiting"}}))
                self.wfile.flush()
            self.wfile.write(sse_event(box[0]))
            self.wfile.flush()
        except (OSError, ValueError) as e:
            tool = (msg.get("params") or {}).get("name")
            log_line(f"client closed the {tool} stream early ({type(e).__name__}); "
                     "any job keeps running, wait_for_job returns its result")

    def do_GET(self):
        self._send(405)

    def do_DELETE(self):
        self._send(405)


def main() -> None:
    for p, what in ((ROOT / "roles", "DISPATCH_ROOT/roles"), (PROJECT, "DISPATCH_PROJECT"), (STATE, "DISPATCH_STATE")):
        if not p.is_dir():
            sys.exit(f"{what} must be an existing directory (got {p})")
    for sub in ("logs", "sessions", "imports", "packets"):
        (STATE / sub).mkdir(exist_ok=True)
    load_job_index()  # before the touch below: a lane log's mtime is when its last job was last active
    for stale in (STATE / "imports").glob("*.bundle"):  # left by a bridge that died mid-import
        stale.unlink(missing_ok=True)
    for role, info in load_roles().items():
        for lane in lane_names(role, info["lanes"]):
            (STATE / "logs" / f"{lane}.log").touch()
    audit({"event": "start", "version": SERVER_VERSION, "project": str(PROJECT)})
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
