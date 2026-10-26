"""Build the sandbox templates locally and load them into the sbx runtime. Port of
scripts/00-build-templates.sh, now a shim that runs this as `factory _build-templates
[--build-only]`; `factory setup` and `factory update` (bash) still call the script.

No registry push: each image goes docker build -> tar -> sbx template load. --build-only builds the
same images (docker only) without sbx: no load, and nothing recorded in the state dir. CI uses it.

The broker image is built from a "git archive" export of the committed broker/ (HEAD), never from
the broker/ working tree, which an agent working on this repo can write into. Uncommitted changes
there are not in the image. The repository's own .git (config and hooks) is trusted here; see the
broker decision log (2026-10-16) for that open risk.

The lines this prints that start with "Building" are what `factory setup` shows of the build, so
their wording stays.
"""

import os
import shutil
import subprocess
import tempfile

from factory import pins, preflight, sbx
from factory.context import FactoryError
from factory.programs import KEEP, run_tool, to_stderr

SCRIPT = os.path.join("scripts", "00-build-templates.sh")

# The git config forced on every git command the factory runs against its own checkout: no
# fsmonitor command, no hooks.
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]

# The developer and reviewer Dockerfiles have no defaults for these build args.
SHELLCHECK_PINS = ("SHELLCHECK_VERSION", "SHELLCHECK_LINUX_X86_64_SHA256", "SHELLCHECK_LINUX_AARCH64_SHA256")


class Stop(Exception):
    """Stop with this exit status; what went wrong has been printed (bash's set -e)."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def _check(status):
    if status != 0:
        raise Stop(status)


def _cp(source, dest):
    """cp: a new file takes the source's permission bits less the umask."""
    mode = os.stat(source).st_mode & 0o777
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
        shutil.copyfileobj(src, out)


def broker_export(ctx, root, dest):
    """Write the committed tree of ROOT's broker/ (at ROOT's HEAD) into DEST, so DEST/Dockerfile is
    the broker's Dockerfile."""
    got = ctx.capture(["git"] + GIT_SAFE + ["-C", root, "cat-file", "-e", "HEAD:broker/tools.txt"], merge=True)
    if got.status != 0:
        raise FactoryError("refusing to export the broker: broker/tools.txt is not in the committed tree of %s (%s)"
                           % (root, got.out.rstrip("\n") or "HEAD has no broker/ directory"))
    ctx._flush()
    archive = subprocess.Popen(["git"] + GIT_SAFE + ["-C", root, "archive", "--format=tar", "HEAD", "broker/"],
                               env=dict(ctx.environ), stdout=subprocess.PIPE)
    try:
        unpack = subprocess.run(["tar", "-x", "-f", "-", "-C", dest, "--strip-components=1"], env=dict(ctx.environ),
                                stdin=archive.stdout)
    except OSError:
        archive.kill()
        unpack = None
    finally:
        archive.stdout.close()
        archived = archive.wait()
    if unpack is None or unpack.returncode != 0 or archived != 0:
        raise FactoryError("exporting the committed broker/ from %s failed" % root)


def prepare_broker_context(ctx, root, dest):
    """broker_export, and a note on stderr when broker/ holds uncommitted work, which is not in
    the image."""
    broker_export(ctx, root, dest)
    if not os.path.isfile(os.path.join(dest, "Dockerfile")):
        raise FactoryError("the committed broker/ has no Dockerfile")
    dirty = ctx.capture(["git"] + GIT_SAFE + ["-C", root, "status", "--porcelain", "--untracked-files=all", "--",
                                              "broker"]).out.rstrip("\n")
    if dirty:
        to_stderr(ctx, "note: building the broker image from the committed broker/ (HEAD); uncommitted changes in "
                       "%s/broker are not included\n" % root)


def _remove_pycache(folder):
    for top, dirs, _ in os.walk(folder):
        for name in list(dirs):
            path = os.path.join(top, name)
            if name == "__pycache__" and not os.path.islink(path):
                shutil.rmtree(path)
                dirs.remove(name)


def _build(ctx, tmp, build_only, image, dockerfile, context, build_args=()):
    tag = "factory-%s:%s" % (image, sbx.TEMPLATE_TAG)
    ctx.say("Building %s..." % tag)
    _check(ctx.run(["docker", "build", "-t", tag, "-f", dockerfile] + list(build_args) + [context]))
    if build_only:
        return
    ctx.say("Loading %s into sbx..." % tag)
    tar = os.path.join(tmp, image + ".tar")
    _check(ctx.run(["docker", "image", "save", tag, "-o", tar]))
    _check(ctx.run(["sbx", "template", "load", tar]))
    # The image id tells sandbox fingerprints (factory/fingerprints.py) whether a rebuild changed
    # anything: an unchanged, cached build keeps its id.
    images = os.path.join(ctx.paths.state, "images")
    os.makedirs(images, exist_ok=True)
    status, got, _ = run_tool(ctx, ["docker", "image", "inspect", "--format", "{{.Id}}", tag], out=KEEP)
    _check(status)
    with open(os.path.join(images, "factory-%s-%s.id" % (image, sbx.TEMPLATE_TAG)), "wb") as f:
        f.write(got)


def _build_all(ctx, tmp, build_only):
    root, config = ctx.paths.root, ctx.paths.config
    broker = os.path.join(tmp, "broker")
    os.mkdir(broker)
    prepare_broker_context(ctx, root, broker)

    # developer/reviewer use the repo root as the context for shared files; .dockerignore limits it
    # to what they COPY. They install the ShellCheck pinned in scripts/lib/common.sh.
    pinned = pins.read(root, *SHELLCHECK_PINS)
    shellcheck_args = []
    for name in SHELLCHECK_PINS:
        shellcheck_args += ["--build-arg", "%s=%s" % (name, pinned[name])]
    for image in ("developer", "reviewer"):
        _build(ctx, tmp, build_only, image, os.path.join(root, "templates", image, "Dockerfile"), root,
               shellcheck_args)

    # The broker builds from the export made above, with that directory as the build context. This
    # machine's policy overlay (an empty one if there is none) goes in as its own build context, so
    # it never has to sit in the repo.
    config_ctx = os.path.join(tmp, "config")
    os.mkdir(config_ctx)
    policy = os.path.join(config, "policy.toml")
    if os.path.isfile(policy):
        ctx.say("Including local broker policy from %s" % policy)
        _cp(policy, os.path.join(config_ctx, "policy.toml"))
    else:
        open(os.path.join(config_ctx, "policy.toml"), "wb").close()
    # Optional plugin packages (docs/plugin-tools.md in broker/) from the local config folder, as
    # their own build context too: an empty directory when there are none. A plugin loads only when
    # the broker start names it in PASSDESK_PLUGINS.
    plugins_ctx = os.path.join(tmp, "plugins")
    os.mkdir(plugins_ctx)
    plugins = os.path.join(config, "plugins")
    try:
        entries = os.listdir(plugins) if os.path.isdir(plugins) else []
    except OSError:
        entries = []
    if entries:
        listed = "".join(n + " " for n in sorted(entries) if not n.startswith("."))
        ctx.say("Including broker plugins from %s: %s" % (plugins, listed))
        _check(ctx.run(["cp", "-R", plugins + "/.", plugins_ctx + "/"]))
        _remove_pycache(plugins_ctx)
    _build(ctx, tmp, build_only, "broker", os.path.join(broker, "Dockerfile"), broker,
           ["--build-context", "factory-config=" + config_ctx, "--build-context", "plugins=" + plugins_ctx])

    if not build_only:
        _check(ctx.run(["sbx", "template", "ls"]))


def main(ctx, argv):
    """`factory _build-templates [--build-only]`: what scripts/00-build-templates.sh runs (it
    checks the usage itself, naming the script as it was called)."""
    if argv not in ([], ["--build-only"]):
        raise FactoryError("usage: %s [--build-only]" % os.path.join(ctx.paths.root, SCRIPT))
    build_only = bool(argv)
    if not preflight.which(ctx, "docker"):
        raise FactoryError("'docker' not found in PATH (see README.md)")
    if not build_only:
        sbx.require_sbx(ctx)
    tmp = tempfile.mkdtemp()
    try:
        _build_all(ctx, tmp, build_only)
    except Stop as e:
        return e.status
    except OSError as e:
        raise FactoryError("%s: %s" % (e.filename or "I/O error", e.strerror or e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0
