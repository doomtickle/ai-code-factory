"""The approval counts `factory status` and `factory doctor` read from
scripts/approvals.py. Port of approvals_summary and approvals_status_line (bin/factory), whose
bash copies went with `status` since nothing else in bash called them; factory/test_approvals.py pins the wording.
"""

import os
import re

from factory import preflight
from factory.context import FactoryError


def summary(ctx):
    """The fields `scripts/approvals.py summary` prints: pending, oldest_min,
    expired_unhandled, approved, running, made, failed (as text; fewer when the script
    prints fewer). None when approvals aren't in use (no approvals folder yet, or ever),
    python3 isn't usable or the script fails: callers then stay inert rather than erroring."""
    paths = ctx.paths
    if not os.path.isdir(os.path.join(paths.broker_state, "approvals", "pending")):
        return None
    try:
        python = preflight.resolve_python(ctx)
    except FactoryError:
        return None
    got = ctx.capture([python, os.path.join(paths.root, "scripts", "approvals.py"), "--state", paths.broker_state,
                       "summary"])
    if got.status != 0:
        return None
    return [f for f in re.split("[ \t\n]+", got.out) if f]


def status_line(ctx):
    """The line `factory status` shows when there is at least one staged card, approved
    card or recent deferred write; None otherwise (approvals off, or nothing to show).
    Counts: pending is the staged cards no one has decided; approved ones wait for the
    gateway, not for a person (declined ones aren't counted); running, made and failed
    count its deferred writes."""
    fields = summary(ctx)
    if fields is None:
        return None
    fields += ["0"] * (7 - len(fields))
    try:
        pending, approved, running, made, failed = (int(fields[i]) for i in (0, 3, 4, 5, 6))
    except ValueError:
        return None
    if pending + approved + running + made + failed <= 0:
        return None
    line = "%d staged (oldest %s min)" % (pending, fields[1]) if pending > 0 else "none staged"
    if approved + running > 0:
        line += "; %d approved, being written" % (approved + running)
    if made > 0:
        line += "; %d done" % made
    if failed > 0:
        line += "; %d failed" % failed
    return "%sApprovals:%s %s - factory approvals" % (ctx.style.B, ctx.style.N, line)
