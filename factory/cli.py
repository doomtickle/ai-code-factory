"""The Python side of `factory`: dispatch to the command groups ported so far."""

import os
import sys

from factory.commands import roles
from factory.context import Context, FactoryError, Paths

# Command name -> run(ctx, args). bin/factory's PORTED_COMMANDS lists these
# (a test keeps them equal); every other command still runs in bash.
COMMANDS = {
    "roles": roles.run,
}

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def main(argv=None, *, root=None, environ=None, stdout=None, stderr=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ctx = Context(Paths.from_environ(root or ROOT, environ), environ, stdout, stderr)
    command = argv[0] if argv else ""
    handler = COMMANDS.get(command)
    try:
        if handler is None:
            raise FactoryError("%r is not ported to the Python CLI yet; run it through bin/factory" % command)
        return handler(ctx, argv[1:])
    except FactoryError as e:
        ctx.stderr.write("error: %s\n" % e)
        return 1
