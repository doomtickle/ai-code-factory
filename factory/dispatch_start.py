"""Starts the dispatch bridge on the host for a project (if it is not already running for that
project) and registers it with the sbx MCP gateway, for use by the Coordinator sandbox. Port of
scripts/start-dispatch.sh, which is now a shim running this as `factory _start-dispatch`;
`02-launch-factory.sh` calls it.

The bridge listens on 127.0.0.1 only and authenticates the gateway with a token minted once: the
value is kept in the sbx secret store, the bridge sees only its SHA-256 (token.sha256 in the
dispatch state dir, outside the repo). The value goes only to `sbx secret set`'s stdin.
"""

import hashlib
import http.client
import os
import secrets
import shutil
import signal
import subprocess

from factory import policy, preflight, roles, sbx, state
from factory.context import FactoryError, basename
from factory.programs import DROP, run_tool

# Keep in sync with scripts/lib/common.sh (a test compares them).
DISPATCH_MCP = "factory-dispatch"
DISPATCH_PORT = 18766


def _url():
    return "http://127.0.0.1:%d/mcp" % DISPATCH_PORT


def _status(method, body=None):
    """The HTTP status the bridge's port answers to METHOD (a POST carries BODY as a form), or
    None if nothing answers with HTTP within 3 seconds. Straight to 127.0.0.1, never through a
    proxy. curl -s -o /dev/null -m 3 does the same: any answer counts, whatever its status."""
    conn = http.client.HTTPConnection("127.0.0.1", DISPATCH_PORT, timeout=3)
    try:
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
        conn.request(method, "/mcp", body=body, headers=headers)
        response = conn.getresponse()
        response.read()
        return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def _alive(pid, started=None):
    """kill -0: the process exists. A child this run started is polled, since its zombie
    would still answer to a signal."""
    if started is not None and started.pid == pid:
        return started.poll() is None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _cp(source, dest):
    """cp: a new file takes the source's permission bits less the umask."""
    mode = os.stat(source).st_mode & 0o777
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
        shutil.copyfileobj(src, out)


def _append(path, data):
    with open(path, "ab") as f:
        f.write(data)


def _rm_rf(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def _nonempty(path):
    """[ -s path ]"""
    try:
        return os.stat(path).st_size > 0
    except OSError:
        return False


def _running_pid(ctx, state_dir, root):
    """The pid in STATE/pid if it is a live process running this checkout's bridge, else None."""
    try:
        text = _read(os.path.join(state_dir, "pid")).decode("utf-8", "surrogateescape").rstrip("\n")
    except OSError:
        return None
    if not (text.isdigit() and text.isascii()) or not _alive(int(text)):
        return None
    if os.path.join(root, "dispatch", "server.py") not in ctx.capture(["ps", "-p", text, "-o", "command="]).out:
        return None
    return int(text)


def _port_holder(ctx):
    """A description of whatever listens on the bridge's port ("" if unknown; best effort: lsof
    is standard on macOS but not on every Linux)."""
    if not preflight.which(ctx, "lsof"):
        return ""
    got = ctx.capture(["lsof", "-nP", "-t", "-iTCP:%d" % DISPATCH_PORT, "-sTCP:LISTEN"])
    holder = got.out.split("\n")[0] if got.out else ""
    if not holder:
        return ""
    return "pid %s: %s" % (holder, ctx.capture(["ps", "-p", holder, "-o", "command="]).out.rstrip("\n"))


def _mint_token(ctx, state_dir, secret_name):
    ctx.say("Minting dispatch token...")
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    # The hash is written only once the secret is stored: a failed set must leave the old hash
    # matching the old secret (-f: without it an existing secret silently stays).
    status = run_tool(ctx, ["sbx", "secret", "set", "-f", secret_name], data=token.encode(), out=DROP, err=DROP)[0]
    if status != 0:
        raise FactoryError("could not store the dispatch token secret '%s' with sbx; the token hash was not "
                           "changed. Check 'sbx secret ls' and that sbx is signed in, then run this again" % secret_name)
    with open(os.path.join(state_dir, "token.sha256"), "w", encoding="ascii") as f:
        f.write(token_hash + "\n")


def _snapshot_roles(ctx, state_dir, project_name):
    """Each role's instructions (dispatch passes them to every task) and this machine's role
    overrides. The bridge reads these copies, not roles/ or FACTORY_CONFIG, so edits take effect
    on the next start. Notes for this project from the factory config folder (e.g. its test
    command), if any, are appended for every role, then notes for one role only
    (projects/<project>.<role>.md, e.g. a docs style for the developer). They live outside the
    project, so the project needs no factory files and no role can change what the others are told."""
    paths = ctx.paths
    notes = os.path.join(paths.config, "projects", project_name + ".md")
    roles.validate_role_overrides(paths)
    instructions, overrides = os.path.join(state_dir, "instructions"), os.path.join(state_dir, "role-overrides")
    _rm_rf(instructions + ".tmp")
    _rm_rf(overrides + ".tmp")
    os.makedirs(instructions + ".tmp", exist_ok=True)
    os.makedirs(overrides + ".tmp", exist_ok=True)
    name = os.fsencode(project_name)
    for role in roles.list_roles(paths):
        roles.validate_role(paths, role)
        override = roles.role_override_file(paths, role)
        if os.path.isfile(override):
            _cp(override, os.path.join(overrides + ".tmp", role + ".conf"))
            ctx.say("Applied local overrides for %s from %s" % (role, override))
        target = os.path.join(instructions + ".tmp", role + ".md")
        _cp(os.path.join(paths.root, "roles", role, "instructions.md"), target)
        if _nonempty(notes):
            _append(target, b"\n# This project (" + name + b")\n" + _read(notes))
        role_notes = os.path.join(paths.config, "projects", "%s.%s.md" % (project_name, role))
        if _nonempty(role_notes):
            _append(target, b"\n# This project (" + name + b"), for the " + role.encode() + b"\n" + _read(role_notes))
            ctx.say("Added %s notes from %s to its instructions" % (role, role_notes))
    if _nonempty(notes):
        ctx.say("Added project notes from %s to the role instructions" % notes)
    _rm_rf(instructions)
    os.rename(instructions + ".tmp", instructions)
    _rm_rf(overrides)
    os.rename(overrides + ".tmp", overrides)
    # Which optional roles this machine switched on (FACTORY_CONFIG/enabled-roles, validated
    # above): the bridge treats an optional role not named here as off. Written even when empty,
    # so an old list never outlives its source file.
    listed = os.path.join(state_dir, "enabled-roles")
    with open(listed + ".tmp", "wb") as f:
        f.write("".join(n + "\n" for n in roles.enabled_roles_listed(paths)).encode("utf-8", "surrogateescape"))
    os.replace(listed + ".tmp", listed)


def _write_lane_identity(ctx, path):
    """The lanes' git identity ([git] author_name and author_email in policy.toml) as
    AUTHOR_NAME="..." / AUTHOR_EMAIL="..." lines in PATH, for the dispatch bridge to pass to
    `factory-lane prepare`. Neither set: no file (factory-lane's default). Raises FactoryError,
    leaving no file, on an invalid value."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    values = []
    for key in ("author_name", "author_email"):
        try:
            values.append(policy.policy_toml_string(ctx.paths, "git", key) or "")
        except ValueError:
            raise FactoryError("[git] %s in policy.toml must be a one-line string (\"...\" without escapes, "
                               "or '...')" % key)
    name, email = values
    problem = policy.lane_identity_problem(name, email)
    if problem:
        raise FactoryError("[git] in policy.toml: %s" % problem)
    if name or email:
        with open(path, "wb") as f:
            f.write(('AUTHOR_NAME="%s"\nAUTHOR_EMAIL="%s"\n' % (name, email)).encode("utf-8", "surrogateescape"))


def _snapshot_docs(root, state_dir):
    """The factory's own docs, for the Coordinator's factory_docs tool. A snapshot, like the
    instructions: the bridge reads this copy, so a role working on the factory's own repo can't
    change what the Coordinator is told."""
    tmp, final = os.path.join(state_dir, "docs.tmp"), os.path.join(state_dir, "docs")
    _rm_rf(tmp)
    os.makedirs(os.path.join(tmp, "guides"), exist_ok=True)
    _cp(os.path.join(root, "README.md"), os.path.join(tmp, "README.md"))
    wsl = os.path.join(root, "docs", "setup-wsl.md")
    if os.path.isfile(wsl):
        _cp(wsl, os.path.join(tmp, "setup-wsl.md"))
    guides = os.path.join(root, "docs", "guides")
    try:
        names = sorted(os.listdir(guides), key=lambda n: n.encode("utf-8", "surrogateescape"))
    except OSError:
        names = []
    for guide in names:
        path = os.path.join(guides, guide)
        if guide.endswith(".md") and not guide.startswith(".") and os.path.isfile(path):
            _cp(path, os.path.join(tmp, "guides", guide))
    _rm_rf(final)
    os.rename(tmp, final)


def _start_bridge(ctx, python, project, state_dir):
    ctx.say("Starting dispatch bridge for %s..." % project)
    with open(os.path.join(state_dir, "project"), "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(project + "\n")
    paths = ctx.paths
    env = dict(ctx.environ, DISPATCH_ROOT=paths.root, DISPATCH_PROJECT=project, DISPATCH_STATE=state_dir,
               DISPATCH_ROLES_SCRATCH=os.path.join(paths.state, "roles"), DISPATCH_PORT=str(DISPATCH_PORT),
               DISPATCH_SBX=preflight.which(ctx, "sbx") or "",
               DISPATCH_BROKER_AUDIT=os.path.join(paths.broker_state, "audit.jsonl"))
    ctx._flush()
    # Detached like `nohup ... &`: its own session, no terminal input, output appended to the log.
    with open(os.path.join(state_dir, "dispatch.log"), "ab") as log:
        proc = subprocess.Popen([python, os.path.join(paths.root, "dispatch", "server.py")], env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    with open(os.path.join(state_dir, "pid"), "w", encoding="ascii") as f:
        f.write("%d\n" % proc.pid)
    return proc


def main(ctx, argv):
    try:
        return _main(ctx, argv)
    except OSError as e:
        raise FactoryError("%s: %s" % (e.filename or "I/O error", e.strerror or e))


def _main(ctx, argv):
    paths = ctx.paths
    root = paths.root
    if len(argv) != 1:
        raise FactoryError("usage: %s <project-dir>" % os.path.join(root, "scripts", "start-dispatch.sh"))
    if not os.path.isdir(argv[0]):
        raise FactoryError("project directory not found: %s" % argv[0])

    sbx.require_sbx(ctx)
    python = preflight.resolve_python(ctx)
    roles.known_broker_tools(paths)  # broker/tools.txt must be there before anything is started
    project = os.path.realpath(argv[0])
    state_dir = paths.dispatch_state
    secret_name = "mcp:%s:token" % DISPATCH_MCP

    state.migrate_state(ctx, root)
    os.makedirs(state_dir, exist_ok=True)
    os.chmod(state_dir, 0o700)

    if not _nonempty(os.path.join(state_dir, "token.sha256")) or not any(
            " %s " % secret_name in line for line in ctx.capture(["sbx", "secret", "ls"]).out.split("\n")):
        _mint_token(ctx, state_dir, secret_name)

    _snapshot_roles(ctx, state_dir, basename(project))

    # The git identity lane commits are authored with ([git] author_name and author_email in this
    # machine's policy.toml), validated and snapshotted like the overrides above; absent,
    # factory-lane uses factory-<lane>.
    identity = os.path.join(state_dir, "lane-identity.conf")
    _write_lane_identity(ctx, identity)
    if os.path.isfile(identity):
        ctx.say("Lane commits use the git identity from %s" % os.path.join(paths.config, "policy.toml"))

    _snapshot_docs(root, state_dir)

    pid = _running_pid(ctx, state_dir, root)
    try:
        bound = _read(os.path.join(state_dir, "project")).decode("utf-8", "surrogateescape").rstrip("\n")
    except OSError:
        bound = ""
    if pid is not None and bound != project:
        ctx.say("Dispatch is running for another project; restarting it...")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            raise FactoryError("could not stop the dispatch bridge (%d): %s" % (pid, e.strerror or e))
        state.sleep(1)
        pid = None

    # Anything answering on the port now is not this checkout's bridge (e.g. one started from
    # another copy of this repo). Our bridge would fail to bind and the readiness check below
    # would be answered by the stranger.
    if pid is None and _status("GET") is not None:
        holder = _port_holder(ctx)
        raise FactoryError(
            "port %d is already in use by another process%s.\n"
            "It is not this checkout's dispatch bridge; it may be one started from another copy of the factory repo.\n"
            "Stop it (e.g. run that copy's scripts/03-stop-factory.sh, or kill the process), then re-run."
            % (DISPATCH_PORT, " (%s)" % holder if holder else ""))

    started = None
    if pid is None:
        started = _start_bridge(ctx, python, project, state_dir)
        pid = started.pid

    # Ready = our bridge process is alive and the port answers 401 (auth required). The liveness
    # check catches a bridge that exited (e.g. could not bind the port) while something else
    # answers in its place.
    log = os.path.join(state_dir, "dispatch.log")
    code = "000"
    for _ in range(15):
        if not _alive(pid, started):
            raise FactoryError("dispatch bridge exited; see %s" % log)
        answer = _status("POST", "{}")
        code = "%03d" % answer if answer is not None else "000"
        if code == "401" and _alive(pid, started):
            break
        state.sleep(1)
    if code != "401":
        raise FactoryError("dispatch did not become ready (last HTTP status: %s); see %s" % (code, log))

    if DISPATCH_MCP not in ctx.capture(["sbx", "mcp", "ls", "-q"]).out.split("\n"):
        status = run_tool(ctx, ["sbx", "mcp", "add", DISPATCH_MCP, "--url", _url(), "--header",
                                "Authorization: Bearer ${token}", "--skip-auth"], out=DROP)[0]
        if status != 0:
            return status

    ctx.say("Dispatch ready: %s (audit log: %s/audit.jsonl, transcripts: %s/logs/)" % (_url(), state_dir, state_dir))
    return 0
