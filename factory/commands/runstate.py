"""The commands that change what is running: chat, stop, restart and reset.

Ports of cmd_chat, cmd_stop, cmd_restart and cmd_reset in bin/factory, with the output
unchanged. Stopping stays scripts/03-stop-factory.sh, which these run: it is also the script
README.md and start-dispatch.sh name for stopping a copy of the factory by hand, and `update`
(still bash) calls it. `restart` starts the factory again with `factory start`
(factory/commands/start.py), called directly.
"""

import os
import subprocess

from factory import sbx
from factory.context import COORDINATOR_SANDBOX, FactoryError, bound_project


def _stop_script(ctx):
    return os.path.join(ctx.paths.root, "scripts", "03-stop-factory.sh")


def chat(ctx, args):
    """Open the Coordinator sandbox in this terminal: what `sbx run --name factory-coordinator`
    does, which `factory start` creates. Extra arguments go to sbx run."""
    sbx.require_sbx(ctx)
    if not sbx.sandbox_exists(ctx, COORDINATOR_SANDBOX):
        raise FactoryError("the coordinator sandbox doesn't exist yet; run 'factory start' first")
    # A second 'sbx run' would start a second Coordinator in the same VM.
    if sbx.coordinator_attached(ctx, COORDINATOR_SANDBOX):
        raise FactoryError("the Coordinator is already open elsewhere (the dashboard or another 'factory chat'); "
                           "close it there first")
    return ctx.exec(["sbx", "run", "--name", COORDINATOR_SANDBOX] + list(args))


def stop(ctx, args):
    """Stop the dispatch bridge and every factory-* sandbox (never the dashboard)."""
    return ctx.run([_stop_script(ctx)])


def restart(ctx, args):
    """Stop the factory, then start it again for the project it is bound to. It opens no
    dashboard (one may be open already: the control unit's RESTART key runs this)."""
    if args:
        raise FactoryError("usage: factory restart")
    project = bound_project(ctx.paths)
    if not project:
        raise FactoryError("the factory isn't bound to a project yet: run 'factory start' in one")
    if not os.path.isdir(project):
        raise FactoryError("the factory's project %s no longer exists: run 'factory start' in another" % project)
    sbx.require_sbx(ctx)
    ctx.step("Stopping the factory")
    status = ctx.run([_stop_script(ctx)])
    if status != 0:
        return status
    ctx.step("Starting it again for %s" % project)
    from factory.commands import start  # start imports this module (for reset)
    return start.run(ctx, [project, "--no-dashboard"])


def rm_failed_after_stop(ctx, command, sandboxes, retry):
    """`sbx rm` failed after COMMAND had already stopped the factory: say what is left stopped
    and how to recover. The project stays bound, so `factory start` brings it back. The bash
    copy in bin/factory stays for `update`; factory/test_command_runstate.py compares them."""
    s = ctx.style
    ctx.bad("factory %s: could not remove %s" % (command, "".join(n + " " for n in sandboxes)))
    ctx.say("  Left stopped: the agent sandboxes, the broker and the dispatch bridge. Nothing was restarted.")
    ctx.say("  Some of those sandboxes may already be gone ('sbx ls' shows what is left).")
    ctx.say("  To bring the factory back up: %sfactory start%s (in your project)." % (s.B, s.N))
    ctx.say("  To retry the removal: %s%s%s." % (s.B, retry, s.N))
    return 1


def _remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        # Linux says EISDIR and macOS EPERM for a directory; bash's rm -f fails on both.
        why = "Is a directory" if os.path.isdir(path) and not os.path.islink(path) else (e.strerror or e)
        raise FactoryError("could not remove %s: %s" % (path, why))


def reset(ctx, args):
    """Remove the factory-* sandboxes (to switch projects) and forget the bound project and the
    saved lane sessions. The Claude login is kept. `--yes` skips the question."""
    sbx.require_sbx(ctx)
    sandboxes = []
    for name in sbx.sandbox_names(ctx):
        if name.startswith("factory-"):
            # Same word-splitting as bash's unquoted $(... | grep '^factory-') on purpose.
            sandboxes.extend(name.split())
    if not args or args[0] != "--yes":
        ctx.say("This removes these sandboxes: %s" % "".join(n + " " for n in sandboxes))
        ctx.say("Your Claude login, roles and settings are kept.")
        if not ctx.confirm("Continue?"):
            raise FactoryError("cancelled")
    status = ctx.run([_stop_script(ctx)], stdout=subprocess.DEVNULL)
    if status != 0:
        return status
    if sandboxes:
        if ctx.run(["sbx", "rm", "--force"] + sandboxes, stdout=subprocess.DEVNULL) != 0:
            return rm_failed_after_stop(ctx, "reset", sandboxes, "factory reset")
        ctx.ok("removed %s" % "".join(n + " " for n in sandboxes))
    _remove(os.path.join(ctx.paths.broker_state, "project"))
    _remove(os.path.join(ctx.paths.dispatch_state, "project"))
    sessions = os.path.join(ctx.paths.dispatch_state, "sessions")
    try:
        names = sorted(n for n in os.listdir(sessions) if not n.startswith("."))
    except OSError:
        names = []
    for name in names:
        try:
            os.unlink(os.path.join(sessions, name))
        except OSError:
            pass
    ctx.ok("factory is no longer bound to a project")
    return 0
