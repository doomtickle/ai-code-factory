"""Where the factory lives on this machine, and where a command writes."""

import os
import sys


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


class Context:
    def __init__(self, paths, environ=None, stdout=None, stderr=None):
        self.paths = paths
        self.environ = os.environ if environ is None else environ
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.style = Style(self.stdout.isatty())

    def say(self, text=""):
        self.stdout.write(text + "\n")
