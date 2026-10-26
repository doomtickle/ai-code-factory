"""Where the factory lives on this machine, and where a command writes."""

import collections
import os
import signal
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
    # Searches env's PATH for a bare name such as "sbx"; argv[0] stays as given.
    os.execvpe(argv[0], argv, env)


Captured = collections.namedtuple("Captured", "status out err")


def _stop(proc, own_group):
    """End a child that is still running and wait for it, so none is left behind."""
    try:
        if own_group:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    for pipe in (proc.stdout, proc.stderr):
        if pipe:
            pipe.close()


def _capture(argv, env, timeout=None, merge=False):
    """Run argv with stdin from /dev/null and collect its output as text. The status is a
    shell's: 127 if the program is not found, 126 if it cannot run, 124 if it ran past
    `timeout` seconds (it is killed with its children), 128+n if a signal n ended it.
    With a timeout the program gets its own session, so the whole group can be killed;
    without one it stays in this terminal's process group, so Ctrl-C reaches it too, and
    an interrupt here still ends and reaps it before it propagates."""
    own_group = timeout is not None
    try:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                                start_new_session=own_group)
    except FileNotFoundError:
        return Captured(127, "", "%s: command not found\n" % argv[0])
    except OSError as e:
        return Captured(126, "", "%s: %s\n" % (argv[0], e.strerror or e))
    status = None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        status = 124
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        except BaseException:
            _stop(proc, own_group)
            raise
    except BaseException:
        _stop(proc, own_group)
        raise
    if status is None:
        status = 128 - proc.returncode if proc.returncode < 0 else proc.returncode
    return Captured(status, (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"))


class Context:
    """What a command runs with. `run` and `exec` start other programs (always an
    argv list, never a shell); a test passes its own `runner` and `execer` to
    capture or fake them."""

    def __init__(self, paths, environ=None, stdout=None, stderr=None, runner=None, execer=None, stdin=None):
        self.paths = paths
        # What `uname -s` and `uname -m` say, and the files that describe the host; a
        # test replaces them to stand in for another machine.
        self.uname = (os.uname().sysname, os.uname().machine)
        self.host_files = {"os_release": "/etc/os-release", "proc_version": "/proc/version", "kvm": "/dev/kvm"}
        self.environ = os.environ if environ is None else environ
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.stdin = sys.stdin if stdin is None else stdin
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

    def confirm(self, question):
        """Ask QUESTION [y/N] and read one answer; True on y/Y/yes/YES. False without asking when
        stdin is not a terminal (bash's `confirm`)."""
        try:
            if not self.stdin.isatty():
                return False
        except (AttributeError, ValueError):
            return False
        self.stdout.write("%s [y/N] " % question)
        self._flush()
        try:
            answer = self.stdin.readline()
        except (OSError, ValueError):
            return False
        return answer.strip(" \t\n") in ("y", "Y", "yes", "YES")

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

    def capture(self, argv, **kw):
        """Run argv and return Captured(status, out, err) as text instead of passing its
        output through (see _capture for the options and statuses)."""
        self._flush()
        return _capture(list(argv), dict(self.environ), **kw)

    def exec(self, argv):
        """Replace this process with argv, so a signal reaches it directly. Does not
        return, unless a test's `execer` does."""
        self._flush()
        return self._execer(list(argv), dict(self.environ))
