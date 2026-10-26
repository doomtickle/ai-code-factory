"""The host-wide sbx security baseline: the local MCP gateway on, the global network policy
deny-all, and the one allow the factory adds (api.anthropic.com). Port of
scripts/01-init-security.sh, now a shim that runs this as `factory _init-security`. Safe to re-run.

The default network policy is never loosened here: `sbx policy init deny-all` is the only
initialisation, the policy is checked to be deny-all afterwards, and GLOBAL_ALLOW is the only host
allowed.
"""

from factory import sbx
from factory.context import FactoryError
from factory.programs import KEEP, run_tool


def _output(ctx, argv):
    """What argv prints on stdout, minus trailing newlines (its stderr is passed on, a failure
    reads as whatever it printed)."""
    return run_tool(ctx, argv, out=KEEP)[1].decode("utf-8", "replace").rstrip("\n")


class _Stop(Exception):
    """Stop with this exit status; what went wrong has been printed (bash's set -e)."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def _check(status):
    if status != 0:
        raise _Stop(status)


def _init(ctx):
    sbx.require_sbx(ctx)
    ctx.say("Initializing Docker Sandbox baseline security...")

    # The broker listens on the host loopback, which only the local MCP gateway can reach (a hosted
    # gateway cannot). Changing this requires a daemon restart.
    if _output(ctx, ["sbx", "settings", "get", "mcp.forceLocalGateway"]) != "true":
        if _output(ctx, ["sbx", "ls", "-q"]):
            raise FactoryError("stop and remove running sandboxes first; enabling the local MCP gateway "
                               "restarts the sbx daemon")
        _check(ctx.run(["sbx", "settings", "set", "mcp.forceLocalGateway", "true"]))
        _check(ctx.run(["sbx", "daemon", "restart"]))

    # Enforce default-deny egress on the host proxy. One-time: later runs verify it instead.
    got = ctx.capture(["sbx", "policy", "init", "deny-all"], merge=True)
    if got.status != 0:
        if "already initialized" in got.out:
            ctx.say("Global network policy already initialized; verifying it is deny-all.")
        else:
            raise FactoryError(got.out.rstrip("\n"))
    # `sbx policy check` exits non-zero for a denied host, so match on its output.
    if "default deny" not in ctx.capture(["sbx", "policy", "check", "network", "example.com"], merge=True).out:
        raise FactoryError("global network policy is not deny-all; run 'sbx policy reset' and re-run this script")

    # Allow baseline provider access globally.
    if not ctx.capture(["sbx", "policy", "check", "network", sbx.GLOBAL_ALLOW], merge=True).out.startswith("Allowed"):
        _check(ctx.run(["sbx", "policy", "allow", "network", sbx.GLOBAL_ALLOW]))

    ctx.say("Security baseline established. Verification:")
    return ctx.run(["sbx", "policy", "ls"])


def main(ctx, argv):
    """`factory _init-security`: what scripts/01-init-security.sh runs."""
    try:
        return _init(ctx)
    except _Stop as e:
        return e.status
