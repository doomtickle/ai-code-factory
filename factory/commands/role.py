"""factory role new <name> [like]: scaffold roles/<name>/ by copying an existing role
(default: reviewer, which is read-only and whose only network tool is npm_fetch: the
safest starting point). Edit the new role.conf and instructions.md, then run
'factory start' again: it creates the sandbox, and the Coordinator sees the role in
list_roles."""

import os
import re
import shutil
import subprocess

from factory.context import FactoryError
from factory.roles import RESERVED_ROLE_NAMES, valid_role_name

USAGE = "usage: factory role new <name> [like]"

# validate_role still lives in scripts/lib/roles.sh until role validation is ported
# (docs/py-cli-migration.md, step 2); the new role is checked by running it from there.
VALIDATE_ROLE = '. "$1/scripts/lib/common.sh"; ROOT="$1"; . "$1/scripts/lib/roles.sh"; validate_role "$2"'


def _lines(data):
    return re.findall(rb"[^\n]*\n|[^\n]+", data)


def rewrite_spec(data, name):
    name = name.encode()
    data = re.sub(rb"^name: .*", lambda m: b"name: factory-" + name + b"-kit", data, flags=re.M)
    return re.sub(rb"^displayName: .*", lambda m: b"displayName: Factory " + name, data, flags=re.M)


def rewrite_role_conf(data, name):
    """The new role's role.conf: a TODO description, and not optional. A copy of an optional
    role (OPTIONAL="1", with its comment) is a normal role; the comment is the lines from
    '# Off by default' through OPTIONAL=."""
    todo = b'DESCRIPTION="TODO: one line describing what the %s does"' % name.encode()
    data = re.sub(rb"^DESCRIPTION=.*", lambda m: todo, data, flags=re.M)
    kept, skipping = [], False
    for line in _lines(data):
        if skipping:
            skipping = not line.startswith(b"OPTIONAL=")
        elif line.startswith(b"# Off by default"):
            skipping = True
        else:
            kept.append(line)
    return b"".join(kept)


def _edit(path, rewrite, name):
    with open(path, "rb") as f:
        data = f.read()
    with open(path, "wb") as f:
        f.write(rewrite(data, name))


def new(ctx, args):
    if not 1 <= len(args) <= 2:
        raise FactoryError(USAGE)
    name = args[0]
    like = args[1] if len(args) > 1 else "reviewer"
    root = ctx.paths.root
    if not valid_role_name(name):
        raise FactoryError("invalid role name '%s' (lowercase letters, digits, dashes; not ending in -lane<N>; "
                           "not: %s)" % (name, " ".join(RESERVED_ROLE_NAMES)))
    target = os.path.join(root, "roles", name)
    if os.path.lexists(target):
        raise FactoryError("roles/%s already exists" % name)
    if not os.path.isfile(os.path.join(root, "roles", like, "role.conf")):
        raise FactoryError("no role named '%s' to copy" % like)
    try:
        shutil.copytree(os.path.join(root, "roles", like), target, symlinks=True)
        _edit(os.path.join(target, "kit", "spec.yaml"), rewrite_spec, name)
        _edit(os.path.join(target, "role.conf"), rewrite_role_conf, name)
    except OSError as e:
        raise FactoryError("cannot scaffold roles/%s: %s" % (name, e.strerror or e))
    status = ctx.run(["bash", "-c", "set -euo pipefail; " + VALIDATE_ROLE, "factory", root, name])
    if status != 0:
        return status
    if not shutil.which("sbx", path=ctx.environ.get("PATH", os.defpath)):
        raise FactoryError("sbx not found in PATH (needed to validate the role's kit)")
    status = ctx.run(["sbx", "kit", "validate", os.path.join(target, "kit")], stdout=subprocess.DEVNULL)
    if status != 0:
        return status
    ctx.say("Created roles/%s (copied from %s). Next:" % (name, like))
    ctx.say("  1. Edit roles/%s/role.conf: DESCRIPTION, WORKSPACE (rw/ro), BROKER_TOOLS, LANES, MAX_TURNS, "
            "MODEL and MODELS (optional)." % name)
    ctx.say("  2. Edit its instructions in roles/%s/instructions.md." % name)
    ctx.say("  3. Run 'factory start' again.")
    return 0


def run(ctx, args):
    if args and args[0] == "new":
        return new(ctx, args[1:])
    raise FactoryError(USAGE)
