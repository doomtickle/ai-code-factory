"""Commands that locate one of the host Python scripts in scripts/ and hand over to it:
analytics, approvals (and approve), dashboard (and gui), watch. The script keeps its own tests and its own
argument parsing; this only adds the paths it needs from the factory's state."""

import os
import sys

from factory.context import COORDINATOR_SANDBOX, basename, bound_project


def script_argv(ctx, name, *leading):
    return [sys.executable, os.path.join(ctx.paths.root, "scripts", name)] + list(leading)


def analytics(ctx, args):
    return ctx.exec(script_argv(ctx, "analytics.py", "--state", ctx.paths.state) + args)


def approvals(ctx, args):
    return ctx.exec(script_argv(ctx, "approvals.py", "--state", ctx.paths.broker_state) + args)


def watch(ctx, args):
    p = ctx.paths
    return ctx.exec(script_argv(
        ctx, "watch.py", "--state", p.dispatch_state, "--roles", os.path.join(p.root, "roles"),
        "--project", basename(bound_project(p))) + args)


def dashboard(ctx, args):
    """--demo serves made-up data and is given no state, project or Coordinator: the
    page can then neither read the real factory nor act on it."""
    p = ctx.paths
    roles = os.path.join(p.root, "roles")
    if "--demo" in args:
        return ctx.exec(script_argv(ctx, "dashboard.py", "--roles", roles) + args)
    return ctx.exec(script_argv(
        ctx, "dashboard.py", "--state", p.state, "--roles", roles, "--config", p.config,
        "--project", basename(bound_project(p)),
        "--coordinator", COORDINATOR_SANDBOX) + args)
