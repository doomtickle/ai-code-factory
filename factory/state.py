"""The factory's runtime state: where it may live, and moving it out of the repo. Ports of
require_state_outside and migrate_state in scripts/lib/common.sh (which stay:
02-launch-factory.sh calls the first, 03-stop-factory.sh the second). factory/test_state.py
runs both migrate_state copies on the same fixtures.
"""

import os
import shutil
import signal
import time

from factory.context import FactoryError

# Tests replace it, so a restart does not wait a real second.
sleep = time.sleep


def require_state_outside(paths, project):
    """Refuse a state dir inside PROJECT (e.g. XDG_STATE_HOME pointed into it), where agents
    could edit it."""
    if (paths.state + "/").startswith(project + "/"):
        raise FactoryError("factory state (%s) is inside the project %s, where agents could edit it; "
                           "set XDG_STATE_HOME elsewhere" % (paths.state, project))


def migrate_state(ctx, root):
    """One-time move of state from its old in-repo location (ROOT/.broker, ROOT/.dispatch,
    ROOT/.roles) to FACTORY_STATE. Stops a bridge still running from the old location first,
    since it writes to the old paths. Sandboxes that mount an old path are caught by the launch
    preflight."""
    paths = ctx.paths
    os.makedirs(paths.state, exist_ok=True)
    os.chmod(paths.state, 0o700)
    try:
        with open(os.path.join(root, ".dispatch", "pid"), encoding="utf-8", errors="surrogateescape") as f:
            pid = f.read().rstrip("\n")
    except OSError:
        pid = ""
    if pid.isdigit() and pid.isascii() and (
            os.path.join(root, "dispatch", "server.py") in ctx.capture(["ps", "-p", pid, "-o", "command="]).out):
        ctx.say("Stopping dispatch bridge (%s) to move its state out of the repo..." % pid)
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError as e:
            raise FactoryError("could not stop the dispatch bridge (%s): %s" % (pid, e.strerror or e))
        sleep(1)
    for old in ("broker", "dispatch", "roles"):
        source = os.path.join(root, "." + old)
        if not os.path.isdir(source):
            continue
        new = os.path.join(paths.state, old)
        if os.path.exists(new):
            raise FactoryError("state exists both in %s and %s; keep one (move or delete the other), then re-run"
                               % (source, new))
        ctx.say("Moving %s to %s..." % (source, new))
        try:
            shutil.move(source, new)
        except (OSError, shutil.Error) as e:
            raise FactoryError("could not move %s to %s: %s" % (source, new, getattr(e, "strerror", None) or e))
