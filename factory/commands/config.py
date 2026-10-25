"""factory config init [--force]: copy config.example/ into FACTORY_CONFIG.

Every placeholder in the example is commented out, so the copy is inert (the notes
in projects/ apply only to a project folder of that exact name). Writes nothing at
all if any target file exists, unless --force."""

import os
import shutil
import stat

from factory.context import FactoryError

USAGE = "usage: factory config init [--force]"


def example_files(example):
    """Relative paths of the regular files under `example` (symlinks skipped, as
    `find -type f` does), in byte order."""
    found = []
    for folder, dirs, names in os.walk(example):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isfile(path) and not os.path.islink(path):
                found.append(os.path.relpath(path, example))
    return sorted(found, key=lambda rel: os.fsencode(rel))


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _copy(src, dest):
    # Like cp: a new file gets the example's mode minus the umask; an existing one keeps its own.
    existed = os.path.lexists(dest)
    shutil.copyfile(src, dest)
    if not existed:
        os.chmod(dest, stat.S_IMODE(os.stat(src).st_mode) & ~_umask())


def run(ctx, args):
    if not args or args[0] != "init":
        raise FactoryError(USAGE)
    force = False
    for arg in args[1:]:
        if arg != "--force":
            raise FactoryError(USAGE)
        force = True
    root, config = ctx.paths.root, ctx.paths.config
    example = os.path.join(root, "config.example")
    if not os.path.isdir(example):
        raise FactoryError("no config.example/ in %s" % root)
    files = example_files(example)
    if not files:
        raise FactoryError("config.example/ in %s is empty" % root)
    # lexists: a dangling symlink is "absent" to exists(), and the copy would write through it.
    existing = [os.path.join(config, rel) for rel in files if os.path.lexists(os.path.join(config, rel))]
    if existing and not force:
        for dest in existing:
            ctx.bad("exists: " + dest)
        raise FactoryError("nothing written; re-run with --force to overwrite the files above")
    ctx.step("Config init (%s)" % config)
    for rel in files:
        dest = os.path.join(config, rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # --force replaces a symlink itself; a copy would write through it to its target.
            if os.path.islink(dest):
                os.unlink(dest)
            _copy(os.path.join(example, rel), dest)
        except OSError as e:
            raise FactoryError("cannot write %s: %s" % (dest, e.strerror or e))
        ctx.ok("wrote " + dest)
    ctx.say("Every value in them is commented out: uncomment and fill in only what you need.")
    ctx.say("Next: factory doctor")
    return 0
