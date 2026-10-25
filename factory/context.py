"""Where the factory lives on this machine, and where a command writes."""

import os
import subprocess
import sys

# Keep in sync with scripts/lib/common.sh.
COORDINATOR_SANDBOX = "factory-coordinator"


def _xdg(environ, var, fallback):
    # XDG: a relative value is ignored.
    value = environ.get(var, "")
    base = value if value.startswith("/") else os.path.join(environ.get("HOME", ""), fallback)
    return os.path.join(base, "ai-code-factory")


class Paths:
    """root: this checkout. config: FACTORY_CONFIG (scripts/lib/common.sh), the
    machine's own settings. state: FACTORY_STATE, runtime state."""

    def __init__(self, root, config, state):
        self.root = root
        self.config = config
        self.state = state

    @property
    def broker_state(self):
        return os.path.join(self.state, "broker")

    @property
    def dispatch_state(self):
        return os.path.join(self.state, "dispatch")

    @classmethod
    def from_environ(cls, root, environ=None):
        environ = os.environ if environ is None else environ
        return cls(root, _xdg(environ, "XDG_CONFIG_HOME", ".config"),
                   _xdg(environ, "XDG_STATE_HOME", ".local/state"))


class FactoryError(Exception):
    """A failure to report as `error: <message>` with exit status 1 (bash's die)."""


class Style:
    """The ANSI styles bin/factory uses; empty when the stream is not a terminal."""

    def __init__(self, enabled):
        self.B, self.D, self.G, self.Y, self.R, self.N = (
            ("\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m") if enabled else ("",) * 6)


def bound_project(paths):
    """The project the factory is bound to ($FACTORY_BROKER_STATE/project), "" if none."""
    try:
        with open(os.path.join(paths.broker_state, "project"), encoding="utf-8", errors="surrogateescape") as f:
            return f.read().rstrip("\n")
    except OSError:
        return ""


def basename(path):
    """POSIX basename(1): trailing slashes dropped, "" for "", "/" for "/"."""
    stripped = path.rstrip("/")
    if not stripped:
        return "/" if path else ""
    return stripped.rsplit("/", 1)[-1]


def _call(argv, **kw):
    status = subprocess.call(argv, **kw)
    return 128 - status if status < 0 else status


def _exec(argv, env):
    os.execve(argv[0], argv, env)


class Context:
    """What a command runs with. `run` and `exec` start other programs (always an
    argv list, never a shell); a test passes its own `runner` and `execer` to
    capture or fake them."""

    def __init__(self, paths, environ=None, stdout=None, stderr=None, runner=None, execer=None):
        self.paths = paths
        self.environ = os.environ if environ is None else environ
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.style = Style(self.stdout.isatty())
        self._runner = runner or _call
        self._execer = execer or _exec

    def say(self, text=""):
        self.stdout.write(text + "\n")

    def step(self, text):
        self.say("\n%s==> %s%s" % (self.style.B, text, self.style.N))

    def ok(self, text):
        self.say("  %s\u2713%s %s" % (self.style.G, self.style.N, text))

    def warn(self, text):
        self.say("  %s!%s %s" % (self.style.Y, self.style.N, text))

    def bad(self, text):
        self.say("  %s\u2717%s %s" % (self.style.R, self.style.N, text))

    def _flush(self):
        for stream in (self.stdout, self.stderr):
            try:
                stream.flush()
            except (AttributeError, ValueError):
                pass

    def run(self, argv, **kw):
        """Run argv to completion and return its exit status (128+n if killed by signal n)."""
        self._flush()
        return self._runner(list(argv), env=dict(self.environ), **kw)

    def exec(self, argv):
        """Replace this process with argv, so a signal reaches it directly. Does not
        return, unless a test's `execer` does."""
        self._flush()
        return self._execer(list(argv), dict(self.environ))
