"""factory voice <start|stop|status|build>: LOIS's local voice service, one Docker container
on loopback (voice/README.md). Every docker call is a fixed argv naming the one container or
image in factory/voice.py. Nothing else in the factory needs Docker or this command."""

import os
import shutil

from factory import voice
from factory.context import FactoryError

USAGE = "usage: factory voice <start [--build]|stop|status|build>"
TIMEOUT = 120  # A docker call other than the build, in seconds.
PUBLISH = "127.0.0.1:%d:%d" % (voice.PORT, voice.PORT)
# exec: phonemizer copies libespeak-ng.so to a temp folder and loads the copy, so that each
# language gets its own instance. On a noexec /tmp that fails and words not in Kokoro's lexicon
# go silent. The root filesystem stays read-only, with no capabilities and no-new-privileges.
TMPFS = "/tmp:exec"


def _docker(ctx):
    path = shutil.which("docker", path=ctx.environ.get("PATH"))
    if path is None:
        raise FactoryError("docker not found. The voice service runs in Docker; "
                           "the rest of the factory works without it.")
    return path


def _missing(captured):
    return "no such" in captured.err.lower()


def _container_state(ctx, docker):
    """The container's state (running, exited, created, ...), None if there is no such container."""
    got = ctx.capture([docker, "container", "inspect", "--format", "{{.State.Status}}", voice.CONTAINER],
                      timeout=TIMEOUT)
    if got.status == 0:
        return got.out.strip()
    if _missing(got):
        return None
    raise FactoryError("docker is not answering: %s" % got.err.strip())


def _image_present(ctx, docker):
    got = ctx.capture([docker, "image", "inspect", "--format", "{{.Id}}", voice.IMAGE], timeout=TIMEOUT)
    if got.status != 0 and not _missing(got):
        raise FactoryError("docker is not answering: %s" % got.err.strip())
    return got.status == 0


def _gpu_works(ctx):
    path = shutil.which("nvidia-smi", path=ctx.environ.get("PATH"))
    return path is not None and ctx.capture([path, "-L"], timeout=30).status == 0


def _docker_step(ctx, argv, what):
    got = ctx.capture(argv, timeout=TIMEOUT)
    if got.status != 0:
        raise FactoryError("%s failed: %s" % (what, got.err.strip()))


def _build(ctx, docker):
    folder = os.path.join(ctx.paths.root, "voice")
    ctx.step("Building %s (several GB, takes a while)" % voice.IMAGE)
    status = ctx.run([docker, "build", "-t", voice.IMAGE, "-f", os.path.join(folder, "Dockerfile"), folder])
    if status != 0:
        raise FactoryError("docker build failed (status %d)" % status)
    ctx.ok("built %s" % voice.IMAGE)


def build(ctx, args):
    if args:
        raise FactoryError(USAGE)
    _build(ctx, _docker(ctx))
    return 0


def start(ctx, args):
    if args not in ([], ["--build"]):
        raise FactoryError(USAGE)
    docker = _docker(ctx)
    state = _container_state(ctx, docker)
    if state == "running":
        ctx.ok("already running on 127.0.0.1:%d" % voice.PORT)
        return 0
    if not _image_present(ctx, docker):
        if not args:
            raise FactoryError("the %s image is not built. Run: factory voice build (several GB, takes a "
                               "while), or: factory voice start --build" % voice.IMAGE)
        _build(ctx, docker)
    if state is not None:  # Left over from an earlier run (--restart no, so it stays down).
        _docker_step(ctx, [docker, "rm", voice.CONTAINER], "removing the old container")
    gpu = _gpu_works(ctx)
    argv = [docker, "run", "-d", "--name", voice.CONTAINER, "--restart", "no", "-p", PUBLISH,
            "--read-only", "--tmpfs", TMPFS, "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    if gpu:
        argv += ["--gpus", "all"]
    _docker_step(ctx, argv + [voice.IMAGE], "docker run")
    ctx.ok("started on 127.0.0.1:%d (%s). The model takes a moment to load; see: factory voice status" % (
        voice.PORT, "GPU" if gpu else "CPU"))
    return 0


def stop(ctx, args):
    if args:
        raise FactoryError(USAGE)
    docker = _docker(ctx)
    state = _container_state(ctx, docker)
    if state is None:
        ctx.say("not running")
        return 0
    if state == "running":
        _docker_step(ctx, [docker, "stop", voice.CONTAINER], "docker stop")
    _docker_step(ctx, [docker, "rm", voice.CONTAINER], "docker rm")
    ctx.ok("stopped and removed %s" % voice.CONTAINER)
    return 0


def status(ctx, args):
    if args:
        raise FactoryError(USAGE)
    docker = _docker(ctx)
    state = _container_state(ctx, docker)
    if state != "running":
        ctx.say("not running" if state is None else "not running (container %s)" % state)
        if not _image_present(ctx, docker):
            ctx.say("image %s is not built; see: factory voice build" % voice.IMAGE)
        return 0
    health = voice.health()
    if health is None:
        ctx.say("running on 127.0.0.1:%d, not answering yet (still loading the model?)" % voice.PORT)
    else:
        ctx.say("running on 127.0.0.1:%d, device %s, %d voices" % (
            voice.PORT, health.get("device", "unknown"), len(health.get("voices", []))))
    return 0


SUBCOMMANDS = {"start": start, "stop": stop, "status": status, "build": build}


def run(ctx, args):
    handler = SUBCOMMANDS.get(args[0]) if args else None
    if handler is None:
        raise FactoryError(USAGE)
    return handler(ctx, args[1:])
