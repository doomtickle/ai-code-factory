"""The Python side of `factory`: dispatch to the command groups ported so far."""

import os
import sys

from factory import dispatch_start
from factory.commands import config, docs, doctor, logs, packet, role, roles, runstate, secret, status, wrappers
from factory.context import Context, FactoryError, Paths

# Command name -> run(ctx, args). bin/factory's PORTED_COMMANDS lists these
# (a test keeps them equal); every other command still runs in bash.
COMMANDS = {
    "analytics": wrappers.analytics,
    "approvals": wrappers.approvals,
    "approve": wrappers.approvals,
    "chat": runstate.chat,
    "config": config.run,
    "coordinator": runstate.chat,
    "dashboard": wrappers.dashboard,
    "down": runstate.stop,
    "docs": docs.run,
    "doctor": doctor.run,
    "gui": wrappers.dashboard,
    "log": logs.run,
    "logs": logs.run,
    "packet": packet.run,
    "packets": packet.run,
    "reset": runstate.reset,
    "restart": runstate.restart,
    "role": role.run,
    "roles": roles.run,
    "secret": secret.run,
    "status": status.run,
    "stop": runstate.stop,
    "watch": wrappers.watch,
}

# Entry points the bash scripts run (`factory _start-dispatch`): dispatched like COMMANDS, but not
# commands a person types, so they are not in COMMANDS, PORTED_COMMANDS or the usage text.
INTERNAL = {
    "_start-dispatch": dispatch_start.main,
}

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def main(argv=None, *, root=None, environ=None, stdout=None, stderr=None, runner=None, execer=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ctx = Context(Paths.from_environ(root or ROOT, environ), environ, stdout, stderr, runner, execer)
    command = argv[0] if argv else ""
    handler = COMMANDS.get(command) or INTERNAL.get(command)
    try:
        if handler is None:
            raise FactoryError("%r is not ported to the Python CLI yet; run it through bin/factory" % command)
        return handler(ctx, argv[1:])
    except FactoryError as e:
        ctx._flush()
        ctx.stderr.write("error: %s\n" % e)
        return 1
