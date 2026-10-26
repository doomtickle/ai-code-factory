"""factory start (and up): start the factory for a project and open the dashboard. Port of
cmd_start, require_project_commits and onboarding_hints in bin/factory and resolve_layout in
scripts/lib/common.sh, with the output unchanged. The launch itself is factory/launch.py.

`hints` is also `factory _onboarding-hints`, which bash `factory setup` (and so `factory
update`) runs; with FACTORY_UPDATING set (update's setup step) it leaves out the out-of-date
warning, since update itself recreates those sandboxes next.
"""

import os
import re

from factory import fingerprints, launch, policy, preflight, repos, roles, sbx
from factory.commands import runstate, wrappers
from factory.commands.status import warn_missing
from factory.context import FactoryError, bound_project

USAGE = "usage: factory start [dir] [--no-ui] [--layout web]"

# The git config forced on the one git command run in the project (bash's BROKER_GIT_SAFE):
# no fsmonitor command, no hooks.
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


def resolve_layout(ctx, flag, env):
    """"web", the only layout. FLAG (--layout's value) or else ENV (FACTORY_LAYOUT), or the
    saved FACTORY_CONFIG/layout, naming a layout that no longer exists ("watch", "tabs") gets a
    one-line warning on stderr and "web"; the stale file is removed so the warning shows once.
    Any other FLAG or ENV value is a usage error; any other saved value is ignored. Nothing is
    saved."""
    chosen = flag or env
    if chosen in ("watch", "tabs"):
        ctx._flush()
        ctx.stderr.write("warning: the '%s' layout is gone; using the dashboard (web)\n" % chosen)
    elif chosen not in ("", "web"):
        raise FactoryError(USAGE)
    saved_file = os.path.join(ctx.paths.config, "layout")
    try:
        with open(saved_file, "rb") as f:
            saved = f.read().decode("utf-8", "surrogateescape").rstrip("\n")
    except OSError:
        saved = ""
    if saved in ("watch", "tabs"):
        ctx._flush()
        ctx.stderr.write("warning: your saved '%s' layout is gone; using the dashboard (web)\n" % saved)
        try:
            os.unlink(saved_file)
        except OSError:
            pass
    return "web"


def require_project_commits(ctx, project):
    """Refuse, naming the repo, if any repository the roles would work in (project_repos) has
    no commits: the lane clone and the review packet both need a HEAD, and would fail much later
    without one."""
    for repo in repos.project_repos(project):
        folder = "%s/%s" % (project, repo)
        got = ctx.capture(["git"] + GIT_SAFE + ["-C", folder, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
        if got.status != 0:
            raise FactoryError("%s is a git repository with no commits yet, so the developer has nothing to branch from. "
                               "Make the first commit there (git -C '%s' commit --allow-empty -m 'Initial commit'), "
                               "then run 'factory start' again." % (folder, folder))


def _fields(line):
    """awk's default fields: split on runs of spaces and tabs."""
    return [f for f in re.split("[ \t]+", line) if f]


def hints(ctx):
    """Warnings about what the factory still needs: sandboxes out of date, the first Claude
    login, and the credentials the configured broker tools need."""
    paths = ctx.paths
    if not ctx.environ.get("FACTORY_UPDATING"):
        stale = fingerprints.stale_sandboxes(ctx)
        if stale:
            ctx.warn("out of date (made from an older image or kit): %s; run 'factory update' to recreate them"
                     % " ".join(stale))
    if not sbx.claude_login_stored(ctx):
        ctx.warn("first time: type /login in the Coordinator (POWER ON on the dashboard) to sign in with your Claude "
                 "subscription, then run 'factory start' again (or press RESTART on the dashboard) to create the role "
                 "sandboxes")
    if policy.git_push_enabled(paths):
        lines = ctx.capture(["sbx", "secret", "ls"]).out.rstrip("\n").split("\n")
        if any(f[:1] == ["(global)"] and f[2:3] == ["github"] for f in map(_fields, lines)):
            ctx.warn("a global GitHub token is stored, so sbx injects it for every sandbox, not just the broker. Run "
                     "'factory secret save github', then remove the global one: sbx secret rm github")
        elif policy.github_repos_listed(paths) and not any(" github " in line for line in lines):
            ctx.warn("git push/PR is enabled but no GitHub token is stored: run 'factory secret save github' (see "
                     "README: Enabling git push and pull requests)")
    if policy.any_jira_tool_granted(paths):
        warn_missing(ctx, "factory-jira",
                     "Jira tools are granted but no token is stored in the keychain (see README: Enabling Jira)")
        if not policy.jira_cloud_id_set(paths):
            ctx.warn("Jira tools are granted but [jira].cloud_id is not set; set it in %s/policy.toml (see README: "
                     "Enabling Jira)" % paths.config)
    if policy.npm_private_registry_needs_token(paths):
        warn_missing(ctx, "factory-npm-registry",
                     "npm_fetch can't download private npm packages until a token is stored in the keychain (see "
                     "README: Enabling a private npm registry)")
    if policy.gitlab_needs_token(paths):
        warn_missing(ctx, "factory-gitlab",
                     "[gitlab].url is set but no GitLab token is stored: run 'factory secret save gitlab' (see README: "
                     "Enabling GitLab)")
    why = policy.gitlab_mr_unheld(paths)
    if why:
        ctx.warn("GitLab merge requests open without approval: %s (see README: Enabling approvals)" % why)


def onboarding_hints(ctx, args):
    """`factory _onboarding-hints`: what bash `factory setup` prints at its end."""
    hints(ctx)
    return 0


def _factory_sandboxes(ctx):
    """True if `sbx ls -q` lists any factory-* sandbox."""
    got = ctx.capture(["sbx", "ls", "-q"])
    if got.err:
        ctx._flush()
        ctx.stderr.write(got.err)
    return any(line.startswith("factory-") for line in got.out.split("\n"))


def start(ctx, target=".", no_ui=False, no_dashboard=False):
    """Start the factory for TARGET and, unless NO_UI or NO_DASHBOARD, run the dashboard."""
    s = ctx.style
    if not os.path.isdir(target):
        raise FactoryError("no such directory: %s" % target)
    project = os.path.realpath(target)
    sbx.require_sbx(ctx)
    if not sbx.setup_done(ctx):
        raise FactoryError("the factory is not set up yet: run 'factory setup' first")
    require_project_commits(ctx, project)
    if re.match("/mnt/[a-z]/", project) and preflight.is_wsl(ctx.host_files["proc_version"]):
        ctx.warn("%s is on a Windows drive: sandboxes will be slow and file permissions unreliable. Clone it into "
                 "the Linux filesystem (e.g. ~/code) instead." % project)

    current = bound_project(ctx.paths)
    if current and current != project and _factory_sandboxes(ctx):
        ctx.say("The factory is currently set up for %s%s%s." % (s.B, current, s.N))
        if not ctx.confirm("Switch to %s? This removes the agent sandboxes (your Claude login is kept)." % project):
            raise FactoryError("not switching. Run 'factory start %s' to open the current project." % current)
        status = runstate.reset(ctx, ["--yes"])
        if status != 0:
            return status

    status = launch.run(ctx, project, no_ui)
    if status != 0:
        return status
    hints(ctx)
    ctx.say("")
    if no_ui:
        ctx.say("Factory running, without the dashboard.")
        ctx.say("  Talk to the Coordinator:  %sfactory chat%s" % (s.B, s.N))
        ctx.say("  Watch every lane:         %sfactory watch%s" % (s.B, s.N))
        ctx.say("  Follow one role's log:    %sfactory logs <role>%s  (roles: %s)"
                % (s.B, s.N, "".join(r + " " for r in roles.list_roles(ctx.paths))))
        ctx.say("  Stop everything:          %sfactory stop%s" % (s.B, s.N))
        return 0
    if no_dashboard:
        ctx.say("Factory running. Run %sfactory dashboard%s to open it." % (s.B, s.N))
        return 0
    ctx.say("Factory running. Opening the dashboard: talk to the Coordinator on its screen.")
    ctx.say("Ctrl-C closes the dashboard (and the Coordinator's terminal); the factory keeps")
    ctx.say("running. Reopen it with %sfactory dashboard%s; stop everything with %sfactory stop%s." % (s.B, s.N, s.B, s.N))
    return wrappers.dashboard(ctx, [])


def run(ctx, args):
    """factory start [dir] [--no-ui] [--layout web]. --no-ui (or FACTORY_NO_UI=1): everything
    but the dashboard; talk to the Coordinator with `factory chat` and follow roles with
    `factory watch` or `factory logs`. Otherwise the start ends by running `factory dashboard`,
    which holds the Coordinator's terminal. --layout (or FACTORY_LAYOUT) is kept only so old
    habits and saved choices don't break: "web" is the one layout, and resolve_layout warns
    about the removed "watch" and "tabs". --no-dashboard (internal, for `factory update` and
    `factory restart`): finish instead of running the dashboard in the foreground, so they
    return."""
    roles.known_broker_tools(ctx.paths)  # refuses a checkout with no broker/tools.txt, as roles.sh did
    target, layout_flag = "", ""
    no_ui = ctx.environ.get("FACTORY_NO_UI", "") == "1"
    no_dashboard = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--no-ui":
            no_ui = True
        elif arg == "--no-dashboard":
            no_dashboard = True
        elif arg == "--layout":
            if i + 1 >= len(args):
                raise FactoryError(USAGE)
            i += 1
            layout_flag = args[i]
        elif arg.startswith("-") or target:
            raise FactoryError(USAGE)
        else:
            target = arg
        i += 1
    resolve_layout(ctx, layout_flag, ctx.environ.get("FACTORY_LAYOUT", ""))
    return start(ctx, target or ".", no_ui, no_dashboard)
