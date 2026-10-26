"""The pins that scripts/lib/common.sh holds and bash still uses (scripts/lint.sh), read from it so
there is one copy: `readonly NAME="value"` lines, with ${NAME} of an earlier pin expanded. They go
into this package when common.sh is deleted (docs/py-cli-migration.md, step 9). The sbx version and
the Linux .deb pins `factory doctor` uses are copies in factory/preflight.py, which a test compares.
"""

import os
import re

from factory.context import FactoryError

_LINE = re.compile(r'^readonly ([A-Z][A-Z0-9_]*)="([^"]*)"$')
_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def read(root, *names):
    """{NAME: value} for each of NAMES as scripts/lib/common.sh pins it. Raises FactoryError if
    one is not there."""
    path = os.path.join(root, "scripts", "lib", "common.sh")
    try:
        with open(path, "rb") as f:
            lines = f.read().decode("utf-8", "replace").split("\n")
    except OSError as e:
        raise FactoryError("%s: %s" % (path, e.strerror or e))
    values = {}
    for line in lines:
        m = _LINE.match(line)
        if m:
            values[m.group(1)] = _REF.sub(lambda r: values.get(r.group(1), r.group(0)), m.group(2))
    missing = [n for n in names if n not in values]
    if missing:
        raise FactoryError("%s is not pinned in %s" % (missing[0], path))
    return {n: values[n] for n in names}
