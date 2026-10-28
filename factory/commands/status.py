"""factory status: what is running, and for which project. Read-only.

Port of cmd_status in bin/factory, with the output unchanged: the same lines, styles and
order (see docs/py-cli-migration.md for the differences). Arguments are ignored, as before.
"""

import json
import os

from factory import approvals, hints, policy, roles, sbx
from factory.commands.roles import lanes_label
from factory.context import FactoryError, bound_project

# Keep in sync with scripts/lib/common.sh (a test compares them).
BROKER_PORT = 18765

# Folders of local settings that make the config folder count as "set up" (besides
# policy.toml, enabled-roles and dashboard.toml).
SETTINGS_FOLDERS = ("broker-kit", "roles", "projects", "plugins")


def local_settings_present(paths):
    """True if the config folder holds any local settings: policy.toml, enabled-roles or dashboard.toml,
    or a broker-kit/, roles/, projects/ or plugins/ folder."""
    if any(os.path.isfile(os.path.join(paths.config, f)) for f in ("policy.toml", "enabled-roles", "dashboard.toml")):
        return True
    return any(os.path.isdir(os.path.join(paths.config, d)) for d in SETTINGS_FOLDERS)


def broker_running(ctx):
    """True if the broker answers on its loopback port (an unauthenticated call gets 401)."""
    got = ctx.capture(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "2", "-X", "POST", "-d", "{}",
                       "http://127.0.0.1:%d/mcp" % BROKER_PORT])
    return got.out == "401"


def dispatch_pid(ctx):
    """The pid the dispatch bridge recorded, if a process with that pid is running the bridge
    from this checkout; else None."""
    try:
        with open(os.path.join(ctx.paths.dispatch_state, "pid"), encoding="utf-8", errors="surrogateescape") as f:
            pid = f.read().rstrip("\n")
    except OSError:
        return None
    if not pid:
        return None
    command = ctx.capture(["ps", "-p", pid, "-o", "command="]).out
    return pid if os.path.join(ctx.paths.root, "dispatch", "server.py") in command else None


def warn_missing(ctx, service, message):
    """Warn MESSAGE if the keychain has no SERVICE; if it is locked or unavailable, say that
    instead, since whether SERVICE is stored can't be told."""
    text = hints.missing_message(ctx, service, message)
    if text:
        ctx.warn(text)


def sandbox_rows(ctx):
    """(name, status) of every factory sandbox sbx lists, from `sbx ls --json`."""
    got = ctx.capture(["sbx", "ls", "--json"])
    try:
        if got.status != 0:
            raise ValueError("exit status %d" % got.status)
        listing = json.loads(got.out)["sandboxes"]
        if not isinstance(listing, list):
            raise ValueError("no sandbox list")
    except (ValueError, KeyError, TypeError) as e:
        raise FactoryError("could not list the sandboxes ('sbx ls --json' failed: %s)%s"
                           % (e, (": " + got.err.strip()) if got.err.strip() else ""))
    rows = []
    for sandbox in listing:
        name = sandbox.get("name") if isinstance(sandbox, dict) else None
        if isinstance(name, str) and name.startswith("factory-"):
            state = sandbox.get("status")
            rows.append((name, state if isinstance(state, str) else json.dumps(state)))
    return rows


def sandbox_table(ctx, rows):
    """The sandboxes as `column -t` laid them out: names indented two spaces and padded to the
    widest, two spaces, then the status, `running` green and `stopped` dim."""
    st = ctx.style
    width = max([len(name) + 2 for name, _ in rows] + [0])
    lines = []
    for name, state in rows:
        state = state.replace("running", st.G + "running" + st.N, 1).replace("stopped", st.D + "stopped" + st.N, 1)
        lines.append("  " + name.ljust(width - 2) + "  " + state)
    return lines


def run(ctx, args):
    st, paths = ctx.style, ctx.paths
    sbx.require_sbx(ctx)
    project = bound_project(paths)
    ctx.say("%sProject%s   %s" % (st.B, st.N, project or "%snone yet (run factory start)%s" % (st.D, st.N)))
    if local_settings_present(paths):
        ctx.say("%sLocal settings%s %s" % (st.B, st.N, paths.config))
    else:
        ctx.say("%sLocal settings%s %snone (see config.example/)%s" % (st.B, st.N, st.D, st.N))
    if any(os.path.isdir(os.path.join(paths.root, d)) for d in (".broker", ".dispatch", ".roles")):
        ctx.warn("state is still inside the repo; factory start moves it to %s" % paths.state)

    if broker_running(ctx):
        ctx.ok("broker running (127.0.0.1:%d)" % BROKER_PORT)
    else:
        ctx.bad("broker not running")
    pid = dispatch_pid(ctx)
    if pid is not None:
        ctx.ok("dispatch bridge running (pid %s)" % pid)
    else:
        ctx.bad("dispatch bridge not running")
    if sbx.claude_login_stored(ctx):
        ctx.ok("Claude login stored")
    else:
        ctx.warn("no Claude login yet: type /login in the Coordinator (POWER ON on the dashboard), then run 'factory start' again")

    ctx.say("%sCoordinator broker tools%s %s" % (st.B, st.N, roles.coordinator_broker_tools(paths) or "none"))
    if policy.any_jira_tool_granted(paths):
        warn_missing(ctx, "factory-jira", 'Jira keychain entry ("factory-jira") not found')
        if not policy.jira_cloud_id_set(paths):
            ctx.warn("[jira].cloud_id is not set (see README: Enabling Jira)")
    if policy.npm_private_registry_needs_token(paths):
        warn_missing(ctx, "factory-npm-registry", 'private npm registry keychain entry ("factory-npm-registry") not found')
    if policy.gitlab_needs_token(paths):
        warn_missing(ctx, "factory-gitlab", 'GitLab keychain entry ("factory-gitlab") not found')
    why = policy.gitlab_mr_unheld(paths)
    if why:
        ctx.warn("GitLab merge requests open without approval: " + why)
    line = approvals.status_line(ctx)
    if line:
        ctx.say(line)

    listed = ", ".join("%s (%s)" % (r, lanes_label(paths, r)) for r in roles.list_roles(paths))
    ctx.say("%sRoles%s %s" % (st.B, st.N, listed or "none"))

    ctx.say("")
    ctx.say("%sSandboxes%s" % (st.B, st.N))
    for row in sandbox_table(ctx, sandbox_rows(ctx)):
        ctx.say(row)
    ctx.say("")
    ctx.say("%sFollow a role: factory logs <role>   Roles: factory roles%s" % (st.D, st.N))
    return 0
