"""factory roles: list the roles this machine runs, and the optional ones that are off."""

import os

from factory import roles as rl


def lanes_label(paths, role):
    n = rl.role_lanes(paths, role)
    return "1 lane" if n == "1" else n + " lanes"


def run(ctx, args):
    p, s = ctx.paths, ctx.style
    for r in rl.list_roles(p):
        access = "branches" if rl.role_get(p, r, "WORKSPACE") == "rw" else "read-only"
        ctx.say("%s%-18s%s %-9s %-8s %s" % (
            s.B, r, s.N, access, lanes_label(p, r), rl.role_get(p, r, "DESCRIPTION")))
        tools = rl.role_get(p, r, "BROKER_TOOLS").replace(" ", ", ") or "none"
        ctx.say("  %snetwork tools: %s%s" % (s.D, tools, s.N))
        override = rl.role_override_file(p, r)
        if os.path.isfile(override):
            ctx.say("  %slocal overrides: %s%s" % (s.D, override, s.N))
    for r in rl.list_all_roles(p):
        if rl.role_optional(p, r) and not rl.role_listed_enabled(p, r):
            ctx.say('%s%-18s%s off (optional)  enable: add "%s" to %s, then factory start' % (
                s.D, r, s.N, r, rl.enabled_roles_file(p)))
    return 0
