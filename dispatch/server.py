"""Factory dispatch bridge: lets the Coordinator VM delegate tasks to role sandboxes.

Runs on the host (only the host can drive sbx), bound to 127.0.0.1, and is
reachable only through the sbx MCP gateway with a bearer token. It does one
thing: run `claude -p <task>` inside an existing factory-<role> sandbox via
`sbx exec`, with a fixed argv (no shell), for roles defined in roles/*/role.conf.
It streams each role's transcript to <DISPATCH_STATE>/logs/<role>.log (shown in the
role's Herdr tab) and returns the role's final report to the Coordinator.

Standard library only; compatible with the macOS system Python (3.9).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER_NAME = "factory-dispatch"
SERVER_VERSION = "1.0.0"

ROOT = Path(os.environ.get("DISPATCH_ROOT", "/nonexistent"))
PROJECT = Path(os.environ.get("DISPATCH_PROJECT", "/nonexistent"))
STATE = Path(os.environ.get("DISPATCH_STATE", "/nonexistent"))
PORT = int(os.environ.get("DISPATCH_PORT", "18766"))
SBX = os.environ.get("DISPATCH_SBX", "sbx")

MAX_REQUEST_BYTES = 1_000_000
MAX_TASK_CHARS = 50_000
MAX_RESULT_CHARS = 30_000
MAX_WAIT_SECONDS = 600
# A role's saved session over this many context tokens is not resumed: the next
# task starts fresh (role.conf MAX_SESSION_TOKENS overrides it; "0" = no cap).
# Every turn of a resumed session re-reads its whole history, so a large one
# makes even a one-line task expensive.
DEFAULT_MAX_SESSION_TOKENS = 150_000
MAX_INSTRUCTIONS_CHARS = 20_000

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
_CONF_LINE = re.compile(r'^([A-Z_]+)="(.*)"\s*$')
# Keep in sync with RESERVED_ROLE_NAMES in scripts/lib/roles.sh (valid_role_name).
_RESERVED_ROLE_NAMES = {"broker", "coordinator", "dispatch"}


def load_roles() -> dict:
    """Roles from ROOT/roles/*/role.conf (parsed, never executed)."""
    roles = {}
    for conf in sorted((ROOT / "roles").glob("*/role.conf")):
        name = conf.parent.name
        if not _ROLE_NAME.match(name) or name in _RESERVED_ROLE_NAMES:
            continue
        values = {}
        for line in conf.read_text().splitlines():
            m = _CONF_LINE.match(line)
            if m:
                values[m.group(1)] = m.group(2)
        roles[name] = {
            "description": values.get("DESCRIPTION", ""),
            "workspace": values.get("WORKSPACE", "rw"),
            "broker_tools": values.get("BROKER_TOOLS", "").split(),
            "model": values.get("MODEL", "").strip(),
            "max_session_tokens": parse_session_cap(values.get("MAX_SESSION_TOKENS", "")),
        }
    return roles


def parse_session_cap(value: str) -> int:
    value = value.strip()
    return int(value) if value.isdigit() else DEFAULT_MAX_SESSION_TOKENS


def sandbox_name(role: str) -> str:
    return f"factory-{role}"


def existing_sandboxes() -> set:
    cp = subprocess.run([SBX, "ls", "-q"], capture_output=True, text=True, timeout=60)
    return set(cp.stdout.split()) if cp.returncode == 0 else set()


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
                 start_cost_usd: float | None = 0.0, instructions: str = ""):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        self.role = role
        self.task = task
        self.resume = resume
        self.model = model
        self.instructions = instructions
        self.status = "running"  # running | done | failed | cancelled
        self.started = time.time()
        self.finished: float | None = None
        self.result = ""
        self.session_id: str | None = None
        # Claude reports total_cost_usd for the whole session, including the turns a
        # resumed session restores from its transcript. The job's own cost is the end
        # total minus the session's total when the job started (None if unknown).
        self.start_cost_usd = start_cost_usd
        self.session_cost_usd: float | None = None
        self.context_tokens: int | None = None
        self.model_used: str | None = None
        self.proc: subprocess.Popen | None = None
        self.done = threading.Event()

    def summary(self, with_result: bool = False) -> dict:
        end = self.finished or time.time()
        out = {"job_id": self.id, "role": self.role, "status": self.status,
               "elapsed_seconds": round(end - self.started), "task": one_line(self.task, 200)}
        if self.model_used:
            out["model"] = self.model_used
        if self.session_cost_usd is not None:
            if self.start_cost_usd is not None and self.session_cost_usd >= self.start_cost_usd:
                out["cost_usd"] = round(self.session_cost_usd - self.start_cost_usd, 4)
            out["session_cost_usd"] = round(self.session_cost_usd, 4)
        if self.context_tokens is not None:
            out["context_tokens"] = self.context_tokens
        if with_result and self.status != "running":
            out["report"] = sanitize(self.result)
        return out


_jobs: dict = {}
_jobs_lock = threading.Lock()


def busy_job(role: str) -> Job | None:
    with _jobs_lock:
        for job in _jobs.values():
            if job.role == role and job.status == "running":
                return job
    return None


def session_file(role: str) -> Path:
    return STATE / "sessions" / role


def session_meta_file(role: str) -> Path:
    return STATE / "sessions" / f"{role}.json"


def read_session_meta(role: str) -> dict:
    """{session_id, total_cost_usd, context_tokens} after the role's last job, or {}."""
    try:
        meta = json.loads(session_meta_file(role).read_text())
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def saved_session(role: str) -> tuple:
    """(session id the role resumes by default, its metadata if it matches that id)."""
    try:
        sid = session_file(role).read_text().strip() or None
    except OSError:
        return None, {}
    meta = read_session_meta(role)
    total = meta.get("total_cost_usd")
    # json.loads accepts NaN/Infinity; treat those (and non-numbers) as unknown.
    if (not isinstance(total, (int, float)) or isinstance(total, bool)
            or not math.isfinite(total)):
        return sid, {}
    return sid, (meta if sid and meta.get("session_id") == sid else {})


def save_session(job: Job) -> None:
    session_file(job.role).write_text(job.session_id + "\n")
    if job.session_cost_usd is None:
        # End total unknown: drop stale metadata rather than guess the next job's start.
        session_meta_file(job.role).unlink(missing_ok=True)
        return
    meta = {"session_id": job.session_id, "total_cost_usd": job.session_cost_usd,
            "context_tokens": job.context_tokens or 0}
    session_meta_file(job.role).write_text(json.dumps(meta) + "\n")


class Transcript:
    """Human-readable, append-only log a role's Herdr tab tails."""

    def __init__(self, role: str):
        self.path = STATE / "logs" / f"{role}.log"

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
            if block.get("type") == "text" and block.get("text", "").strip():
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
    elif etype == "result":
        job.session_id = event.get("session_id") or job.session_id
        job.result = str(event.get("result") or "")
        total = event.get("total_cost_usd")
        # A non-finite total (claude can report NaN) counts as missing.
        if (isinstance(total, (int, float)) and not isinstance(total, bool)
                and math.isfinite(total)):
            job.session_cost_usd = float(total)
        if event.get("is_error"):
            job.status = "failed"


def run_job(job: Job) -> None:
    log = Transcript(job.role)
    log.write("─" * 60)
    log.write(f"▶ job {job.id} from the Coordinator")
    for para in sanitize(job.task, 4000).strip().splitlines():
        log.write(f"   > {para}")
    try:
        # The job holds its starting total; until save_session records the new end
        # total, the session's cost is unknown (a crash or kill must not leave the old one).
        session_meta_file(job.role).unlink(missing_ok=True)
        attempt_resume = job.resume
        while True:
            argv = [SBX, "exec", "-w", str(PROJECT), sandbox_name(job.role),
                    "claude", "-p", job.task,
                    "--output-format", "stream-json", "--verbose",
                    "--permission-mode", "bypassPermissions",
                    "--append-system-prompt", role_system_prompt(job.role, job.instructions)]
            if job.model:
                argv += ["--model", job.model]
            if attempt_resume:
                argv += ["--resume", attempt_resume]
            stderr_lines: list = []
            with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True) as proc:
                job.proc = proc
                err_thread = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
                err_thread.start()
                for raw in proc.stdout:
                    try:
                        render_event(json.loads(raw), log, job)
                    except json.JSONDecodeError:
                        if raw.strip():
                            log.write(f"   {one_line(raw, 200)}")
                code = proc.wait()
                err_thread.join(timeout=5)
            stderr = "".join(stderr_lines)
            if job.status == "cancelled":
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
        if job.status == "running":
            job.status = "done"
        if job.session_id:
            save_session(job)
    except Exception as e:  # noqa: BLE001 - surface every failure to the Coordinator
        job.status = "failed"
        job.result = f"dispatch error: {type(e).__name__}: {e}"
    finally:
        job.finished = time.time()
        log.write(f"■ job {job.id} {job.status} after {round(job.finished - job.started)}s")
        audit({"event": "job-end", **job.summary()})
        job.done.set()


# --------------------------------------------------------------------------
# Tools

def session_note(meta: dict) -> str:
    tokens = meta.get("context_tokens") or 0
    size = f"~{round(tokens / 1000)}k" if tokens >= 1000 else str(tokens)
    return f"{size} tokens, ${float(meta.get('total_cost_usd') or 0):.2f} so far"


def tool_list_roles(args: dict) -> str:
    roles = load_roles()
    sandboxes = existing_sandboxes()
    lines = []
    for name, info in roles.items():
        job = busy_job(name)
        state = f"busy ({job.id})" if job else ("idle" if sandbox_name(name) in sandboxes else "not created")
        access = "edits the project" if info["workspace"] == "rw" else "read-only"
        net = ", ".join(info["broker_tools"]) or "none"
        model = info["model"] or "default"
        line = (f"- {name} [{state}]: {info['description']} "
                f"({access}; network tools: {net}; model: {model})")
        _, meta = saved_session(name)
        try:
            if meta:
                line += f"; session: {session_note(meta)}"
        except (TypeError, ValueError):
            pass  # malformed metadata: show nothing rather than fail list_roles
        lines.append(line)
    return "Roles:\n" + "\n".join(lines) if lines else "No roles are defined."


def tool_delegate(args: dict) -> str:
    role, task = args["role"], args["task"].strip()
    roles = load_roles()
    if role not in roles:
        raise Denied(f"unknown role {role!r}; available: {', '.join(roles) or 'none'}")
    if not task:
        raise Denied("task is empty")
    if len(task) > MAX_TASK_CHARS:
        raise Denied(f"task exceeds {MAX_TASK_CHARS} characters")
    wait = args.get("wait_seconds", 0)
    if not isinstance(wait, int) or isinstance(wait, bool) or wait < 0:
        raise Denied("wait_seconds must be a non-negative integer")
    instructions = role_instructions(role)
    if sandbox_name(role) not in existing_sandboxes():
        raise Denied(f"the {role} sandbox has not been created; ask the user to run 'factory start' "
                     "again (role sandboxes are created once a Claude login is stored)")
    with _jobs_lock:
        for job in _jobs.values():
            if job.role == role and job.status == "running":
                raise Denied(f"{role} is busy with job {job.id}; wait for it or cancel it")
        resume, start_cost, mode = None, 0.0, "fresh session (requested)"
        if not args.get("fresh_session"):
            resume, meta = saved_session(role)
            cap, size = roles[role]["max_session_tokens"], (meta or {}).get("context_tokens") or 0
            if resume and cap and isinstance(size, int) and size > cap:
                resume = None
                mode = (f"fresh session (automatic: the previous one carried ~{round(size / 1000)}k tokens, "
                        f"over the {round(cap / 1000)}k cap; it starts without that history)")
            elif resume:
                start_cost = meta.get("total_cost_usd")  # None if unknown
                mode = "continuing its previous session"
            else:
                mode = "fresh session"
        job = Job(role, task, resume, roles[role]["model"], start_cost, instructions)
        _jobs[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    if wait:
        return job_report(job, wait, session=mode)
    return f"Started job {job.id} on {role} ({mode}). Call wait_for_job with this job id to get the report."


def job_report(job: "Job", timeout: int, **extra) -> str:
    job.done.wait(min(timeout, MAX_WAIT_SECONDS))
    summary = {**job.summary(with_result=True), **extra}
    if job.status == "running":
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
    with _jobs_lock:
        recent = sorted(_jobs.values(), key=lambda j: j.started, reverse=True)[:20]
    if not recent:
        return "No jobs yet."
    return json.dumps([j.summary() for j in recent], ensure_ascii=False)


def tool_cancel_job(args: dict) -> str:
    job = _jobs.get(args["job_id"])
    if job is None:
        raise Denied(f"unknown job {args['job_id']!r}")
    if job.status != "running":
        return f"Job {job.id} already {job.status}."
    job.status = "cancelled"
    # Stop the claude process inside the sandbox, then the local sbx exec client.
    subprocess.run([SBX, "exec", sandbox_name(job.role), "pkill", "-INT", "-f", "^claude -p"],
                   capture_output=True, timeout=60)
    if job.proc and job.proc.poll() is None:
        try:
            os.killpg(job.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    job.done.wait(30)
    return f"Cancelled job {job.id} on {job.role}."


TOOLS = {
    "list_roles": (tool_list_roles, "List the role agents, what each does, and whether it is idle or busy. "
                   "Roles with a saved session show its context size and total cost so far.",
                   {}, []),
    "delegate": (tool_delegate, "Start a task on a role agent in its own sandbox. With wait_seconds (max 600) "
                 "it waits for the job and returns the same result as wait_for_job, so one call usually "
                 "covers a whole job; without it, it returns the job id at once (use that to run roles in "
                 "parallel). The role continues its previous session unless fresh_session is true or the "
                 "session is over the role's size cap. Write a self-contained task: roles cannot see your "
                 "conversation.",
                 {"role": {"type": "string"}, "task": {"type": "string"},
                  "fresh_session": {"type": "boolean"}, "wait_seconds": {"type": "integer"}},
                 ["role", "task"]),
    "wait_for_job": (tool_wait_for_job, "Wait up to timeout_seconds (default and max 600) for a job; "
                     "returns its status and, when finished, the role's report. model is the job "
                     "session's main model (omitted until known; subagents may use others), cost_usd "
                     "is this job's cost (omitted if unknown), session_cost_usd the role session's "
                     "total so far, and context_tokens the approximate context the session now carries.",
                     {"job_id": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, ["job_id"]),
    "job_status": (tool_job_status, "Show the 20 most recent jobs and their status.", {}, []),
    "cancel_job": (tool_cancel_job, "Stop a running job.", {"job_id": {"type": "string"}}, ["job_id"]),
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
    for name, info in load_roles().items():
        access = "edits the project" if info["workspace"] == "rw" else "project read-only"
        lines.append(f"- {name} ({access}): {one_line(info['description'], 200)}")
    lines += [
        "",
        "Delegate self-contained tasks (goal, files, known facts, constraints, what done "
        "looks like), passing wait_seconds=600 so one call returns the report. Typical "
        "flow: researcher (only if outside information is needed) -> developer -> "
        "reviewer, sending REVISE feedback back to the developer. Ask the user before "
        "anything irreversible.",
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
        resp = handle_rpc(msg) if isinstance(msg, dict) else None
        if resp is None:
            return self._send(202)
        self._send(200, resp)

    def do_GET(self):
        self._send(405)

    def do_DELETE(self):
        self._send(405)


def main() -> None:
    for p, what in ((ROOT / "roles", "DISPATCH_ROOT/roles"), (PROJECT, "DISPATCH_PROJECT"), (STATE, "DISPATCH_STATE")):
        if not p.is_dir():
            sys.exit(f"{what} must be an existing directory (got {p})")
    for sub in ("logs", "sessions"):
        (STATE / sub).mkdir(exist_ok=True)
    for role in load_roles():
        (STATE / "logs" / f"{role}.log").touch()
    audit({"event": "start", "version": SERVER_VERSION, "project": str(PROJECT)})
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
