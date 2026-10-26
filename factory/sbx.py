"""What the host's `sbx` CLI says about the factory's sandboxes and setup: ports of
sbx_installed_version, sandbox_exists, sandbox_running, claude_login_stored and
sbx_reference_problem (scripts/lib/common.sh), of coordinator_attached (its bash copy is gone with
`factory chat`; the rule is the dashboard's `runs_sandbox`) and of setup_done (bin/factory). The bash
copies stay until their callers are ported; factory/test_sbx.py compares both on the same
stubs. Every query is read-only, and an sbx that is missing or fails reads as "nothing".
"""

import os
import re

from factory import preflight
from factory.context import FactoryError
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


def require_sbx(ctx):
    """Raise FactoryError (bash dies) unless the pinned sbx is installed."""
    if not preflight.which(ctx, "sbx"):
        raise FactoryError("'sbx' not found in PATH (see README.md)")
    got = installed_version(ctx)
    if got != SBX_VERSION:
        raise FactoryError("sbx %s required, found '%s' (see README.md)" % (SBX_VERSION, got or "unknown"))


def sandbox_names(ctx):
    """`sbx ls -q`: the names of the existing sandboxes."""
    return ctx.capture(["sbx", "ls", "-q"]).out.split("\n")


def sandbox_exists(ctx, name):
    return name in sandbox_names(ctx)


def runs_sandbox(argv, name):
    """True if argv is `sbx [global flags] run ...` naming the sandbox: as `--name NAME`,
    `--name=NAME`, or a bare argument NAME before any `--` (`sbx run` takes a sandbox name
    positionally too; sbx has no short form of --name). Errs on the side of a match: a second
    attachment is the harm. The same rule as runs_sandbox in scripts/dashboard_term.py (a
    security rule of the dashboard, scripts/CLAUDE.md); factory/test_sbx.py runs both on the
    same fixtures."""
    if not argv or os.path.basename(argv[0]) != "sbx":
        return False
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 1
    if i >= len(argv) or argv[i] != "run":
        return False
    for arg in argv[i + 1:]:
        if arg == "--":
            break
        if arg == name or arg == "--name=" + name:
            return True
    return False


def coordinator_attached(ctx, name):
    """True if some process on this machine is already running `sbx run` on NAME (`factory
    chat`, or the dashboard's Terminal 0): a second one would start a second Coordinator in
    the same VM. A `ps` that fails reads as nobody."""
    return any(runs_sandbox(line.split(), name) for line in ctx.capture(["ps", "-axo", "args="]).out.split("\n"))


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


# The one network allow the factory adds to the global policy (scripts/01-init-security.sh).
GLOBAL_ALLOW = "api.anthropic.com"
_COLUMN = re.compile(r"CREATED VIA|APPLIES TO|\S+")
_NEEDED = ("SOURCE", "APPLIES TO", "RULE_ID", "TYPE", "DECISION", "RESOURCES")


def extra_global_allows(ctx):
    """The network allows in the host's global sbx policy (local scope, applies to all)
    other than GLOBAL_ALLOW, as [(resource, rule_id)]; [] when there are none, None when
    `sbx policy ls` fails or times out, its table can't be read, or a rule's scope is neither
    `all` nor `sandbox:*`. Read from the --wide table (the JSON layout isn't in
    docs/reference/sbx-cli.md): columns are cut at the header's offsets, and
    a row with an empty SOURCE continues the RESOURCES of the one above."""
    got = ctx.capture(["sbx", "policy", "ls", "--wide", "--source", "local", "--type", "network", "--decision", "allow"],
                      timeout=15)
    if got.status != 0:
        return None
    lines = got.out.split("\n")
    header = next((i for i, line in enumerate(lines) if line.startswith("SOURCE")), None)
    if header is None:
        return None
    cols = [(m.group(0), m.start()) for m in _COLUMN.finditer(lines[header])]
    if any(name not in dict(cols) for name in _NEEDED):
        return None
    ends = [start for _, start in cols[1:]] + [None]
    spans = {name: (start, end) for (name, start), end in zip(cols, ends)}
    rules = []
    for line in lines[header + 1:]:
        if not line.strip():
            continue
        cell = {name: line[a:b].strip() for name, (a, b) in spans.items()}
        if cell["SOURCE"] == "":
            if rules:
                rules[-1]["RESOURCES"] += " " + cell["RESOURCES"]
            continue
        rules.append(cell)
    found = []
    for rule in rules:
        if (rule["SOURCE"], rule["TYPE"], rule["DECISION"]) != ("local", "network", "allow"):
            continue
        scope = rule["APPLIES TO"]
        if scope != "all" and not scope.startswith("sandbox:"):
            return None
        if scope != "all":
            continue
        for resource in re.split(r"[,\s]+", rule["RESOURCES"]):
            if resource and resource not in ("-", GLOBAL_ALLOW, GLOBAL_ALLOW + ":443"):
                found.append((resource, rule["RULE_ID"]))
    return found
