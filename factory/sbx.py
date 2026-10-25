"""What the host's `sbx` CLI says about the factory's sandboxes and setup: ports of
sbx_installed_version, sandbox_exists, sandbox_running, claude_login_stored and
sbx_reference_problem (scripts/lib/common.sh) and of setup_done (bin/factory). The bash
copies stay until their callers are ported; factory/test_sbx.py compares both on the same
stubs. Every query is read-only, and an sbx that is missing or fails reads as "nothing".
"""

import os
import re

from factory.preflight import SBX_VERSION

# Keep in sync with scripts/lib/common.sh (a test compares them).
BROKER_SANDBOX = "factory-broker"
TEMPLATE_TAG = "v3"

_BLANK = " \t\n\v\f\r"


def installed_version(ctx):
    """The version `sbx version` reports ("" if sbx is missing or says nothing parseable)."""
    found = []
    for line in ctx.capture(["sbx", "version"]).out.split("\n"):
        m = re.match(r"sbx version: v?([^ ]*)", line)
        if m:
            found.append(m.group(1))
    return "\n".join(found).rstrip("\n")


def sandbox_names(ctx):
    """`sbx ls -q`: the names of the existing sandboxes."""
    return ctx.capture(["sbx", "ls", "-q"]).out.split("\n")


def sandbox_exists(ctx, name):
    return name in sandbox_names(ctx)


def sandbox_running(ctx, name):
    """True if sbx lists NAME as running (its third column)."""
    states = []
    for line in ctx.capture(["sbx", "ls"]).out.split("\n"):
        fields = line.split()
        if fields and fields[0] == name:
            states.append(fields[2] if len(fields) > 2 else "")
    return "\n".join(states).rstrip("\n") == "running"


def claude_login_stored(ctx):
    """True if sbx holds the global Claude login (the first /login in a factory sandbox,
    or an API key from `sbx secret set anthropic`)."""
    return any(" anthropic " in line for line in ctx.capture(["sbx", "secret", "ls"]).out.split("\n"))


def setup_done(ctx):
    """True once the security baseline is applied and the images of the current
    TEMPLATE_TAG are loaded. The tag matters: after a bump the old images are still
    listed, but the sandboxes are created from factory-<image>:TEMPLATE_TAG."""
    if ctx.capture(["sbx", "settings", "get", "mcp.forceLocalGateway"]).out.rstrip("\n") != "true":
        return False
    lines = ctx.capture(["sbx", "template", "ls", "-q"]).out.split("\n")
    for image in ("developer", "reviewer", "broker"):
        pattern = re.compile(r"factory-%s:%s(?:[%s]|$)" % (image, re.escape(TEMPLATE_TAG), _BLANK))
        if not any(pattern.search(line) for line in lines):
            return False
    return True


def reference_problem(path):
    """Why the saved sbx CLI reference (docs/reference/sbx-cli.md, written by
    scripts/gen-sbx-reference.sh) is not usable, or None when it is current. The file
    carries a stamp line: <!-- sbx-reference: version=X generated=DATE -->, or
    <!-- sbx-reference: not-generated --> while it is the committed placeholder."""
    if not os.path.isfile(path):
        return "%s is missing; run scripts/gen-sbx-reference.sh on the host" % path
    stamp = ""
    with open(path, "rb") as f:
        for line in f.read().decode("utf-8", "replace").split("\n"):
            if line.startswith("<!-- sbx-reference: "):
                stamp = line
                break
    if stamp.startswith("<!-- sbx-reference: not-generated"):
        return ("the sbx CLI reference has not been generated yet; run scripts/gen-sbx-reference.sh on the host "
                "(agents otherwise guess sbx flags)")
    m = re.match(r"<!-- sbx-reference: version=([^ ]*) ", stamp)
    if not m or not m.group(1):
        return "the sbx CLI reference has no version stamp; regenerate it with scripts/gen-sbx-reference.sh"
    if m.group(1) != SBX_VERSION:
        return ("the sbx CLI reference is for sbx %s but the pinned sbx is %s; regenerate it with "
                "scripts/gen-sbx-reference.sh" % (m.group(1), SBX_VERSION))
    return None

