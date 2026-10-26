"""Start the factory for a project: the broker VM and the dispatch bridge (host), then one
sandbox per role in roles/ plus the Coordinator sandbox. Port of scripts/02-launch-factory.sh,
now a shim that runs this as `factory _launch <project-dir>`; `factory start` calls `launch`
directly.

The Coordinator's terminal is not opened here: `factory start` ends a normal start by running
`factory dashboard`, which holds it (or `factory chat`). `no_ui` (FACTORY_NO_UI=1 for the
script, `factory start --no-ui`) only changes the closing message.

Safe to re-run: existing sandboxes are reused and missing ones are created (e.g. after adding a
role with `factory role new`).

The credential rules of CLAUDE.md ("Credentials") apply: every program runs from an argv list,
never through a shell on the host, and no value goes into argv, the environment or a file here
(the broker start, factory/broker_start.py, delivers the credentials). The one shell is the fixed
script SETTINGS_WRITE, run inside the Coordinator sandbox; the settings it writes cross on its
stdin. Nothing from the project is run: the only project path written is .factory/outgoing,
created here and refused if it (or .factory, or its .gitignore) is a symlink.
"""

import json
import os
import re
import subprocess

from factory import broker_start, dispatch_start, fingerprints, repos, roles, sbx
from factory.context import COORDINATOR_SANDBOX, FactoryError
from factory.programs import DROP, KEEP, run_tool, to_stderr
from factory.state import require_state_outside

SCRIPT = os.path.join("scripts", "02-launch-factory.sh")

# The managed settings the Coordinator's kit writes (root-owned, 0444), and the fixed script that
# replaces them with what it reads on stdin, still 0444. Byte for byte what the bash ran.
MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"
SETTINGS_WRITE = 'f=/etc/claude-code/managed-settings.json; cat > "$f.tmp" && chmod 0444 "$f.tmp" && mv -f "$f.tmp" "$f"'


class Exit(Exception):
    """Stop with this exit status; what went wrong has been printed (bash's set -e)."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def _check(status):
    if status != 0:
        raise Exit(status)


# --- printf %q -----------------------------------------------------------------------------
# The characters bash's printf %q escapes with a backslash (sh_backslash_quote); "#" only first,
# "~" only first or after "=" or ":".
_BACKSLASHED = set(" \t\n'\"\\|&;()<>!{}*[?]^$`,")
_ANSI_C = {"\a": "\\a", "\b": "\\b", "\x1b": "\\E", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t",
           "\v": "\\v", "\\": "\\\\", "'": "\\'"}


def _utf8_locale(environ):
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = environ.get(var, "")
        if value:
            return re.search(r"utf-?8", value, re.IGNORECASE) is not None
    return False


def shell_quote(text, environ):
    """TEXT as bash 5's `printf %q` writes it: backslashes before shell specials, or $'...' when
    it holds a character the locale can't print (in a UTF-8 locale, letters such as "é" print)."""
    if text == "":
        return "''"
    utf8 = _utf8_locale(environ)

    def printable(ch):
        if " " <= ch <= "~":
            return True
        return utf8 and ord(ch) > 127 and not 0xDC80 <= ord(ch) <= 0xDCFF and ch.isprintable()

    if not all(printable(ch) for ch in text):
        out = []
        for ch in text:
            if ch in _ANSI_C:
                out.append(_ANSI_C[ch])
            elif printable(ch):
                out.append(ch)
            else:
                out.extend("\\%03o" % b for b in ch.encode("utf-8", "surrogateescape"))
        return "$'%s'" % "".join(out)
    out = []
    for i, ch in enumerate(text):
        if ch in _BACKSLASHED or (ch == "#" and i == 0) or (ch == "~" and (i == 0 or text[i - 1] in "=:")):
            out.append("\\")
        out.append(ch)
    return "".join(out)


# --- the Coordinator's CLAUDE.md exclude --------------------------------------------------
def template_claude_md(workspace):
    """Where the sbx base template writes its own CLAUDE.md for a sandbox whose primary
    workspace is WORKSPACE: the workspace's parent (sbx mounts it at its host path, so this is
    the path inside too)."""
    stripped = workspace.rstrip("/")
    if not stripped:
        parent = "/" if workspace else "."
    elif "/" not in stripped:
        parent = "."
    else:
        parent = stripped.rsplit("/", 1)[0].rstrip("/") or "/"
    return "%s/CLAUDE.md" % parent


def _compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def with_claude_md_exclude(settings_text, path):
    """The managed settings in SETTINGS_TEXT with PATH last in claudeMdExcludes (added once,
    other keys and entries kept), compact, and whether that changed anything; None if the text
    is not one JSON object whose claudeMdExcludes (if set) is a list."""
    try:
        settings = json.loads(settings_text)
    except ValueError:
        return None
    if not isinstance(settings, dict):
        return None
    excludes = settings.get("claudeMdExcludes")
    if excludes is None or excludes is False:
        excludes = []
    if not isinstance(excludes, list):
        return None
    merged = [e for e in excludes if e != path] + [path]
    changed = settings.get("claudeMdExcludes") != merged
    settings["claudeMdExcludes"] = merged
    return _compact(settings), changed


def exclude_template_claude_md(ctx, name, workspace):
    """Keep Claude Code in sandbox NAME from loading the base template's CLAUDE.md (shell
    persistence, network troubleshooting, port publishing: ~2,750 words that don't apply to a
    factory agent). Only the Coordinator needs it: its cwd is the project, under that file's
    folder; roles' cwds (lanes, review checkouts, the :ro project) aren't. Adds the path to the
    root-owned managed settings the kit wrote and leaves them 0444. Idempotent, so it runs on
    every launch and fixes an existing sandbox without recreating it. The path crosses only in
    the JSON on stdin, never on a command line. True on success."""
    status, current, _ = run_tool(ctx, ["sbx", "exec", "-u", "0", name, "cat", MANAGED_SETTINGS], out=KEEP)
    if status != 0:
        return False
    result = with_claude_md_exclude(current.decode("utf-8", "surrogateescape"), template_claude_md(workspace))
    if result is None:
        return False
    merged, changed = result
    if not changed:
        return True
    data = (merged + "\n").encode("utf-8", "surrogateescape")
    return run_tool(ctx, ["sbx", "exec", "-u", "0", "-i", name, "sh", "-c", SETTINGS_WRITE], data)[0] == 0


# --- preflight -------------------------------------------------------------------------------
def _listing(ctx):
    """The sandboxes `sbx ls --json` lists."""
    got = ctx.capture(["sbx", "ls", "--json"])
    try:
        if got.status != 0:
            raise ValueError("exit status %d" % got.status)
        listing = json.loads(got.out)["sandboxes"]
        if isinstance(listing, dict):
            listing = list(listing.values())
        if not isinstance(listing, list):
            raise ValueError("no sandbox list")
    except (ValueError, KeyError, TypeError) as e:
        raise FactoryError("could not list the sandboxes ('sbx ls --json' failed: %s)%s"
                           % (e, (": " + got.err.strip()) if got.err.strip() else ""))
    return listing


def workspaces(listing, name):
    """The workspace lines sbx lists for sandbox NAME (what jq -r printed for
    `.sandboxes[] | select(.name == NAME) | .workspaces[]`); [] if there is no such sandbox."""
    items = []
    for sandbox in listing:
        if not isinstance(sandbox, dict) or sandbox.get("name") != name:
            continue
        mounts = sandbox.get("workspaces")
        if isinstance(mounts, dict):
            mounts = list(mounts.values())
        if not isinstance(mounts, list):
            raise FactoryError("could not list the sandboxes ('sbx ls --json' gives %s no workspace list)" % name)
        items.extend(m if isinstance(m, str) else _compact(m) for m in mounts)
    text = "\n".join(items).rstrip("\n")
    return text.split("\n") if text else []


def preflight(ctx, project, role_list, rw_roles):
    paths = ctx.paths
    # Layout before roles/: one broker registration shared by all agents.
    if sbx.BROKER_SANDBOX in ctx.capture(["sbx", "mcp", "ls", "-q"]).out.split("\n"):
        raise FactoryError("""found the pre-roles broker setup. Migrate once with:
  sbx rm --force factory-developer factory-reviewer
  sbx mcp rm --force %(b)s
  sbx secret rm -f mcp:%(b)s:token; sbx secret rm -f mcp:%(b)s:token:endpoint
  rm -f '%(state)s/token.sha256'
then re-run this script.""" % {"b": sbx.BROKER_SANDBOX, "state": paths.broker_state})

    # rw roles work in git clones of the project's repositories (lanes), so the project must be
    # a git repository or a folder of them.
    if rw_roles and not repos.project_repos(project):
        raise FactoryError("%s is not a git repository or a folder of git repositories; the %s role(s) work in git "
                           "clones of it (each commits to factory/* branches you merge). Run 'git init' and commit, "
                           "or start the factory on the repo itself." % (project, " ".join(rw_roles)))

    # Existing sandboxes are bound to the project they were created for: the project is the
    # Coordinator's workspace, mounted read-only into role sandboxes (rw roles also mount its
    # .factory/outgoing).
    listing = _listing(ctx)
    allowed = (project, project + ":ro", project + "/.factory/outgoing")
    for name in [COORDINATOR_SANDBOX] + [roles.role_sandbox(r) for r in role_list]:
        mounts = workspaces(listing, name)
        if mounts and not any(m in allowed for m in mounts):
            raise FactoryError("%s was created for a different project. To switch projects, see README "
                               "(Switching projects)." % name)

    # Shared folders are fixed when a sandbox is created: the broker and read-only roles must
    # mount their state dir where it is now (outside the repo), not an old location (e.g.
    # .broker/ or .roles/ inside the repo, before state moved).
    require_state_outside(paths, project)
    scratch_root = os.path.join(paths.state, "roles")
    # rw roles from before lanes mounted the project itself read-write, .git included (no
    # scratch dir); a role switched from ro to rw lacks the outgoing mount. Their fingerprint
    # (RW_LAYOUT and workspace, factory/fingerprints.py) marks them out of date, so `factory
    # update` recreates them; refuse to run one until then.
    old_layout = ""
    for role in rw_roles:
        name = roles.role_sandbox(role)
        mounts = workspaces(listing, name)
        if mounts and (os.path.join(scratch_root, role) not in mounts or project + "/.factory/outgoing" not in mounts):
            old_layout += " " + name
    if old_layout:
        raise FactoryError("these sandboxes were created with an older layout (the project mounted read-write, or no "
                           ".factory/outgoing) and must be recreated:%s\nRun 'factory update': it recreates them (your "
                           "Claude login, the logs and your project are kept; those roles start a fresh conversation)."
                           % old_layout)
    stale = ""
    names = [sbx.BROKER_SANDBOX] + [roles.role_sandbox(r) for r in role_list if roles.role_get(paths, r, "WORKSPACE") == "ro"]
    for name in names:
        want = paths.broker_state if name == sbx.BROKER_SANDBOX else os.path.join(scratch_root, name[len("factory-"):])
        mounts = workspaces(listing, name)
        if mounts and want not in mounts:
            stale += " " + name
    if stale:
        raise FactoryError("these sandboxes mount an old state location and must be recreated (your Claude login and "
                           "the logs are kept; those roles start a fresh conversation):\n  sbx rm --force%s\n"
                           "then re-run." % stale)


def make_outgoing(project):
    """PROJECT/.factory/outgoing is the one project path rw roles can write: they put git
    bundles there for the broker's git_push. Create it before the sandboxes mount it,
    git-ignored (the broker writes the same .gitignore). Refuse symlinks, which would lead out
    of the project; nothing here follows one (O_NOFOLLOW, and each folder is checked again once
    it exists)."""
    for rel in (".factory", ".factory/outgoing", ".factory/.gitignore"):
        path = os.path.join(project, rel)
        if os.path.islink(path):
            raise FactoryError("%s is a symlink; refusing to use it (remove it, then re-run)" % path)
        if rel.endswith(".gitignore"):
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w") as f:
                f.write("*\n")
            continue
        try:
            os.mkdir(path)
        except FileExistsError:
            pass
        if os.path.islink(path):
            raise FactoryError("%s is a symlink; refusing to use it (remove it, then re-run)" % path)
        if not os.path.isdir(path):
            raise FactoryError("%s: File exists" % path)


# --- sandboxes -------------------------------------------------------------------------------
def create_sandbox(ctx, project, name, template, kit, mcps, mode):
    """Create sandbox NAME from factory-TEMPLATE with KIT and one --static-mcp per MCPS entry,
    unless it exists, and record its fingerprint. sbx requires a writable primary workspace.
    Modes:
      project (the Coordinator only): the project is the primary workspace; its kit's managed
        settings keep the agent from changing it.
      rw: a scratch dir is primary, the project is mounted :ro, and only PROJECT/.factory/outgoing
        (nested on top) is writable. The role works in git clones on the sandbox's own disk
        (lanes, templates/agent/factory-lane); the dispatch bridge imports their factory/*
        branches as git bundles. Keep in sync with RW_LAYOUT in factory/fingerprints.py.
      ro: a scratch dir is primary and the project is mounted :ro."""
    paths = ctx.paths
    if sbx.sandbox_exists(ctx, name):
        return
    ctx.say("Creating sandbox %s..." % name)
    argv = ["sbx", "create", "--name", name, "--pull", "never", "-t", "factory-%s:%s" % (template, sbx.TEMPLATE_TAG),
            "--kit", kit]
    for mcp in mcps:
        argv += ["--static-mcp", mcp]
    scratch = os.path.join(paths.state, "roles", name[len("factory-"):] if name.startswith("factory-") else name)
    if mode == "project":
        argv += ["claude", project]
    elif mode == "rw":
        os.makedirs(scratch, exist_ok=True)
        argv += ["claude", scratch, project + ":ro", project + "/.factory/outgoing"]
    elif mode == "ro":
        os.makedirs(scratch, exist_ok=True)
        argv += ["claude", scratch, project + ":ro"]
    else:
        raise FactoryError("create_sandbox: unknown workspace mode '%s'" % mode)
    _check(ctx.run(argv, stdout=subprocess.DEVNULL))
    fingerprints.record_sandbox_fingerprint(paths, name)


def launch(ctx, target, no_ui=False):
    """Start the factory for the project folder TARGET; 0, or the status to stop with."""
    paths = ctx.paths
    if not os.path.isdir(target):
        raise FactoryError("project directory not found: %s" % target)
    sbx.require_sbx(ctx)
    roles.known_broker_tools(paths)  # refuses a checkout with no broker/tools.txt, as roles.sh did
    project = os.path.realpath(target)

    role_list = roles.list_roles(paths)
    if not role_list:
        raise FactoryError("no roles defined in roles/")
    roles.validate_role_overrides(paths)
    for role in role_list:
        roles.validate_role(paths, role)
    rw_roles = [r for r in role_list if roles.role_get(paths, r, "WORKSPACE") == "rw"]

    preflight(ctx, project, role_list, rw_roles)
    if rw_roles:
        make_outgoing(project)

    for start in (broker_start.main, dispatch_start.main):
        _check(start(ctx, [project]))

    # The Coordinator's main workspace is the project so Claude opens there; its kit makes it
    # read-only through Claude managed settings (sbx needs a writable main workspace). It also
    # gets the broker MCP directly (no role in between) when kits/coordinator/broker.conf grants
    # it tools.
    mcps = [dispatch_start.DISPATCH_MCP]
    if roles.coordinator_broker_tools(paths):
        mcps.append(broker_start.broker_mcp("coordinator"))
    create_sandbox(ctx, project, COORDINATOR_SANDBOX, "developer", os.path.join(paths.root, "kits", "coordinator"),
                   mcps, "project")
    if not exclude_template_claude_md(ctx, COORDINATOR_SANDBOX, project):
        raise FactoryError("could not add the sbx template's CLAUDE.md to claudeMdExcludes in %s's managed settings"
                           % COORDINATOR_SANDBOX)
    # sbx gives a sandbox the stored Claude login only when it creates it: a role sandbox
    # created before the first /login stays logged out, and role sandboxes take no input to
    # /login there. So until a login is stored, create only the Coordinator (where the user logs
    # in); the next launch creates the roles.
    waiting = ""
    if sbx.claude_login_stored(ctx):
        for role in role_list:
            mcp = [broker_start.broker_mcp(role)] if roles.role_get(paths, role, "BROKER_TOOLS") else []
            create_sandbox(ctx, project, roles.role_sandbox(role), roles.role_get(paths, role, "TEMPLATE"),
                           os.path.join(paths.root, "roles", role, "kit"), mcp, roles.role_get(paths, role, "WORKSPACE"))
    else:
        for role in role_list:
            if not sbx.sandbox_exists(ctx, roles.role_sandbox(role)):
                waiting += " " + role

    # A Coordinator sandbox created before its kit installed the briefing doesn't know it is a
    # factory Coordinator (not recreated automatically: it holds the user's conversation history).
    if run_tool(ctx, ["sbx", "exec", COORDINATOR_SANDBOX, "test", "-f", "/etc/claude-code/CLAUDE.md"],
                out=DROP, err=DROP)[0] != 0:
        ctx.say("note: %s predates the Coordinator briefing; to install it run:\n  sbx rm --force %s && %s %s"
                % (COORDINATOR_SANDBOX, COORDINATOR_SANDBOX, os.path.join(paths.root, SCRIPT),
                   shell_quote(project, ctx.environ)))

    # Point out sandboxes of removed roles (not deleted automatically: that is the user's call).
    got = ctx.capture(["sbx", "ls", "-q"])
    if got.err:
        to_stderr(ctx, got.err)
    listed = " ".join(role_list) + " "
    for line in got.out.split("\n"):
        if not line.startswith("factory-"):
            continue
        for name in line.split():
            if name in (sbx.BROKER_SANDBOX, COORDINATOR_SANDBOX):
                continue
            if not roles._in_list(name[len("factory-"):] if name.startswith("factory-") else name, listed):
                ctx.say("note: %s belongs to a removed role; delete it with: sbx rm --force %s" % (name, name))

    if waiting:
        ctx.say("note: no Claude login is stored yet, so these role sandboxes were not created:%s\n"
                "  Type /login in the Coordinator (POWER ON on the dashboard), then run 'factory start' again (or "
                "press RESTART on the dashboard)." % waiting)

    if no_ui:
        ctx.say("Factory ready (no UI): talk to the Coordinator with 'factory chat'; watch every lane with 'factory watch'.")
    else:
        ctx.say("Factory ready: the dashboard ('factory dashboard') is the Coordinator's terminal.")
    ctx.say("Roles: %s" % "".join(r + " " for r in role_list))
    return 0


def run(ctx, target, no_ui=False):
    """launch, with the exit statuses and messages of the script: a failed step's status, or
    `error: <path>: <reason>` and 1 for a file that can't be written."""
    try:
        return launch(ctx, target, no_ui)
    except Exit as e:
        return e.status
    except OSError as e:
        to_stderr(ctx, "error: %s: %s\n" % (e.filename, e.strerror or e) if e.filename else "error: %s\n" % e)
        return 1


def main(ctx, args):
    """`factory _launch <project-dir>`: what scripts/02-launch-factory.sh runs (it checks the
    usage itself). FACTORY_NO_UI=1 changes only the closing message."""
    if len(args) != 1:
        raise FactoryError("usage: %s <project-dir>" % os.path.join(ctx.paths.root, SCRIPT))
    return run(ctx, args[0], ctx.environ.get("FACTORY_NO_UI", "") == "1")
