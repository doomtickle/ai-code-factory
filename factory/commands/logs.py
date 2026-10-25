"""factory logs [name]: follow the broker's or dispatch bridge's audit log, or a role's
(or one lane's) transcripts. Following is left to tail(1), which already survives log
rotation (-F) and heads each file's chunk with its name; the audit lines go through a
pipe and are formatted here, so the command no longer needs jq."""

import json
import os
import re
import shutil
import subprocess

from factory import roles as rl
from factory.context import FactoryError

_NAME = re.compile(r"[a-z][a-z0-9-]{1,40}")
_LANE = re.compile(r".*-lane[0-9]", re.S)


def _alt(*values):
    """jq's `a // b // c`: the first value that is neither null nor false."""
    for v in values:
        if v is not None and v is not False:
            return v
    return None


def _text(value):
    """What jq's join prints for one element: null is empty, a string itself."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def format_audit_line(line):
    """One audit record as `time  role  tool-or-event  decision  reason`, empty fields
    dropped; the same as the jq filter this replaces. A line that is not a JSON object is
    returned as it is (jq would have stopped the whole pipeline)."""
    try:
        rec = json.loads(line)
    except ValueError:
        return line
    if not isinstance(rec, dict):
        return line
    ts = rec.get("ts")
    # jq keeps a null time as an empty element (it only drops ""), so the line starts with a separator.
    time = ts[11:19] if isinstance(ts, str) else None
    tool = rec.get("tool")
    decision = _alt(rec.get("decision"), rec.get("status"), rec.get("event") if _alt(tool) is not None else None)
    parts = [time, _alt(rec.get("role"), ""), _alt(tool, rec.get("event"), ""), _alt(decision, ""),
             _alt(rec.get("reason"), "")]
    return "  ".join(_text(p) for p in parts if p != "")


def _find_tail(ctx):
    path = shutil.which("tail", path=ctx.environ.get("PATH"))
    if path is None:
        raise FactoryError("tail not found")
    return path


def _follow_audit(ctx, path):
    ctx._flush()
    proc = subprocess.Popen([_find_tail(ctx), "-n", "30", "-F", path], env=dict(ctx.environ),
                            stdout=subprocess.PIPE)
    try:
        for raw in proc.stdout:
            ctx.say(format_audit_line(raw.decode("utf-8", "replace").rstrip("\n")))
            ctx.stdout.flush()
        return proc.wait()
    except (KeyboardInterrupt, BrokenPipeError):
        proc.terminate()
        proc.wait()
        return 130
    finally:
        proc.stdout.close()


def _transcript_lanes(ctx, name):
    p = ctx.paths
    if not _NAME.fullmatch(name):
        raise FactoryError("unknown role '%s' (see: factory roles)" % name)
    if os.path.isfile(rl.role_conf_path(p, name)):
        return rl.role_lane_names(p, name)
    role = name[:name.rindex("-lane")] if _LANE.match(name) else ""
    if role and os.path.isfile(rl.role_conf_path(p, role)) and name in rl.role_lane_names(p, role):
        return [name]
    raise FactoryError("unknown role '%s' (see: factory roles; one lane: <role>-lane<N>, N up to its LANES)" % name)


def run(ctx, args):
    name = args[0] if args else ""
    p = ctx.paths
    if name == "":
        ctx.say("Usage: factory logs <name>   where <name> is one of:")
        ctx.say("  %sbroker dispatch" % "".join(r + " " for r in rl.list_roles(p)))
        return 0
    if name in ("broker", "dispatch"):
        path = os.path.join(p.state, name, "audit.jsonl")
        if not os.path.isfile(path):
            raise FactoryError("no %s log yet" % name)
        return _follow_audit(ctx, path)
    lanes = _transcript_lanes(ctx, name)
    logs = os.path.join(p.dispatch_state, "logs")
    files = [os.path.join(logs, lane + ".log") for lane in lanes]
    try:
        os.makedirs(logs, exist_ok=True)
        for f in files:
            open(f, "a").close()
    except OSError as e:
        raise FactoryError("cannot create %s: %s" % (e.filename, e.strerror))
    return ctx.exec([_find_tail(ctx), "-n", "100", "-F"] + files)
