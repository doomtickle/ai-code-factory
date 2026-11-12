"""Starts the dispatch bridge on the host for a project (if it is not already running for that
project) and registers it with the sbx MCP gateway, for use by the Coordinator sandbox. Port of
scripts/start-dispatch.sh, which is now a shim running this as `factory _start-dispatch`;
the launch (factory/launch.py) calls main directly.

The bridge listens on 127.0.0.1 only and authenticates the gateway with a token minted once: the
value is kept in the sbx secret store, the bridge sees only its SHA-256 (token.sha256 in the
dispatch state dir, outside the repo). The value goes only to `sbx secret set`'s stdin.
"""

import contextlib
import errno
import fcntl
import hashlib
import http.client
import os
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import time

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


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _snapshot_files(ctx, paths, project_name, role_list):
    """{folder: {file name: (bytes, mode)}} for the instructions/ and role-overrides/ snapshot of
    ROLE_LIST, built in memory once every role has passed roles.validate_role (and every override
    has a role), so a bad file raises FactoryError before anything is written. Each role's
    instructions.md, then this project's notes (projects/<project>.md) for every role, then its own
    (projects/<project>.<role>.md); an override is copied as it is. A new file takes its source's
    permission bits less the umask, as cp did."""
    notes = os.path.join(paths.config, "projects", project_name + ".md")
    roles.validate_role_overrides(paths)
    for role in role_list:
        roles.validate_role(paths, role)
    mask = _umask()
    instructions, overrides = {}, {}
    name = os.fsencode(project_name)
    shared = _read(notes) if _nonempty(notes) else None
    for role in role_list:
        override = roles.role_override_file(paths, role)
        if os.path.isfile(override):
            overrides[role + ".conf"] = (_read(override), os.stat(override).st_mode & 0o777 & ~mask)
            ctx.say("Applied local overrides for %s from %s" % (role, override))
        source = os.path.join(paths.root, "roles", role, "instructions.md")
        text = _read(source)
        if shared is not None:
            text += b"\n# This project (" + name + b")\n" + shared
        role_notes = os.path.join(paths.config, "projects", "%s.%s.md" % (project_name, role))
        if _nonempty(role_notes):
            text += b"\n# This project (" + name + b"), for the " + role.encode() + b"\n" + _read(role_notes)
            ctx.say("Added %s notes from %s to its instructions" % (role, role_notes))
        instructions[role + ".md"] = (text, os.stat(source).st_mode & 0o777 & ~mask)
    if shared is not None:
        ctx.say("Added project notes from %s to the role instructions" % notes)
    return {"instructions": instructions, "role-overrides": overrides}


def _replace_file(folder, name, data, mode):
    """FOLDER/NAME holds DATA, swapped in with os.replace from a temp file in the same folder, so a
    reader sees the old file or the new one, never a gap or a part. The temp name starts with a dot
    and ends in .tmp: the bridge reads only <role>.md and *.conf."""
    fd, tmp = tempfile.mkstemp(prefix="." + name + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(folder, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_snapshot(state_dir, files):
    """Swap each snapshot folder's files in one by one (_replace_file), and only then remove what
    is no longer in it (a role's override deleted since, a role gone, leftover temp files). A
    folder that isn't a real folder is replaced first; the old whole-folder temp copies go too."""
    for folder_name, wanted in files.items():
        folder = os.path.join(state_dir, folder_name)
        _rm_rf(folder + ".tmp")
        if os.path.islink(folder) or (os.path.lexists(folder) and not os.path.isdir(folder)):
            os.unlink(folder)
        os.makedirs(folder, exist_ok=True)
        for name in sorted(wanted):
            _replace_file(folder, name, *wanted[name])
        for name in sorted(os.listdir(folder)):
            if name not in wanted and not _fresh_temp(folder, name):
                _rm_rf(os.path.join(folder, name))


# A writer's temp file (_replace_file) younger than this is left alone by the sweep: it may be
# another writer's, mid-swap. Older ones are what a killed writer left behind.
TEMP_GRACE_SECONDS = 3600


def _fresh_temp(folder, name):
    """Whether NAME in FOLDER is a _replace_file temp file (.<name>.*.tmp) modified in the last
    TEMP_GRACE_SECONDS."""
    if not (name.startswith(".") and name.endswith(".tmp")):
        return False
    try:
        return time.time() - os.lstat(os.path.join(folder, name)).st_mtime < TEMP_GRACE_SECONDS
    except FileNotFoundError:
        return True


# The lock both snapshot writers hold, in the dispatch state dir: `factory start` (_snapshot_roles,
# the files and then enabled-roles) and `factory config apply` (_apply_snapshot, from reading
# project and enabled-roles to the last write). Without it each one's sweep removes the other's
# temp files mid-swap, and an Apply working from the old enabled-roles removes the instructions of
# a role the start is switching on.
SNAPSHOT_LOCK = "snapshot.lock"


@contextlib.contextmanager
def _snapshot_lock(ctx, state_dir, wait):
    """Hold the snapshot lock (an fcntl.flock on STATE_DIR/snapshot.lock, opened without following
    a symlink and created 0600). WAIT: block until the other writer is done (start, telling the
    user once); otherwise refuse at once (apply). The lock goes when the process does, so a killed
    writer never leaves it held."""
    path = os.path.join(state_dir, SNAPSHOT_LOCK)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise FactoryError("%s is a symlink; remove it and run this again" % path)
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FactoryError("%s is not a regular file; remove it and run this again" % path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                raise FactoryError("a start is running (or another Apply): it writes the same snapshot. "
                                   "Nothing was applied; run this again once it is done")
            ctx.say("Waiting for a 'factory config apply' to finish...")
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _snapshot_roles(ctx, state_dir, project_name):
    """Each role's instructions (dispatch passes them to every task) and this machine's role
    overrides. The bridge reads these copies, not roles/ or FACTORY_CONFIG, so edits take effect
    on the next start (or `factory config apply`). Notes for this project from the factory config
    folder (e.g. its test command), if any, are appended for every role, then notes for one role
    only (projects/<project>.<role>.md, e.g. a docs style for the developer). They live outside the
    project, so the project needs no factory files and no role can change what the others are told."""
    paths = ctx.paths
    with _snapshot_lock(ctx, state_dir, wait=True):
        _write_snapshot(state_dir, _snapshot_files(ctx, paths, project_name, roles.list_roles(paths)))
        # Which optional roles this machine switched on (FACTORY_CONFIG/enabled-roles, validated
        # above): the bridge treats an optional role not named here as off. Written even when empty,
        # so an old list never outlives its source file.
        listed = os.path.join(state_dir, "enabled-roles")
        with open(listed + ".tmp", "wb") as f:
            f.write("".join(n + "\n" for n in roles.enabled_roles_listed(paths)).encode("utf-8", "surrogateescape"))
        os.replace(listed + ".tmp", listed)


# What the bridge refuses a role's instructions past (dispatch/server.py MAX_INSTRUCTIONS_CHARS).
MAX_INSTRUCTIONS_CHARS = 20_000


def apply_snapshot(ctx):
    """`factory config apply`: refresh the bridge's snapshot of role overrides and instructions
    (with this project's notes) from FACTORY_CONFIG and roles/, as `factory start` makes it, for
    the roles the running snapshot has on. The bridge reads it on every delegate, so it applies
    from the next job; nothing is stopped, started or cancelled, and no sandbox, broker or job is
    touched. enabled-roles and the lane identity are left to `factory start`: turning a role on
    needs its sandbox and broker token, and the identity lives in policy.toml with the gateway's
    settings. Validates as start does, and also refuses instructions the bridge would refuse (too
    long, or not UTF-8); on any refusal nothing is written. Holds the snapshot lock without
    waiting, so it refuses while a start is writing the snapshot. Returns the names of the roles
    refreshed."""
    try:
        return _apply_snapshot(ctx)
    except OSError as e:
        raise FactoryError("%s: %s" % (e.filename or "I/O error", e.strerror or e))


def _apply_snapshot(ctx):
    paths = ctx.paths
    state_dir = paths.dispatch_state
    if not os.path.isdir(os.path.join(state_dir, "instructions")):
        raise FactoryError(NO_SNAPSHOT)
    with _snapshot_lock(ctx, state_dir, wait=False):
        return _apply_locked(ctx, paths, state_dir)


NO_SNAPSHOT = ("there is no running snapshot to refresh: 'factory start' has not run here yet "
               "(it makes the snapshot itself)")


def _apply_locked(ctx, paths, state_dir):
    try:
        project = _read(os.path.join(state_dir, "project")).decode("utf-8", "surrogateescape").rstrip("\n")
        listed = _read(os.path.join(state_dir, "enabled-roles")).decode("utf-8", "surrogateescape")
    except OSError:
        project = listed = ""
    if not project:
        raise FactoryError(NO_SNAPSHOT)
    on = set(roles.parse_enabled_roles(listed))
    role_list = [r for r in roles.list_all_roles(paths) if not roles.role_optional(paths, r) or r in on]
    files = _snapshot_files(ctx, paths, basename(project), role_list)
    for name, (data, _) in sorted(files["instructions"].items()):
        role = name[:-len(".md")]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # The bridge reads instructions as strict UTF-8 and would refuse every task for the role.
            raise FactoryError("role %s: its instructions or this project's notes are not UTF-8 text; save the "
                               "notes as UTF-8. Nothing was applied" % role)
        chars = len(text.strip())
        if chars > MAX_INSTRUCTIONS_CHARS:
            raise FactoryError("role %s: its instructions plus this project's notes come to %d characters, past the "
                               "bridge's %d; shorten the notes. Nothing was applied"
                               % (role, chars, MAX_INSTRUCTIONS_CHARS))
    _write_snapshot(state_dir, files)
    return role_list


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
