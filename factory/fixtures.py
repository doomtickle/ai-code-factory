"""Shared fixtures for the command tests: a scratch checkout, config and state folder,
and a Context whose output is captured and whose `exec` is recorded, not performed."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from factory.context import Context, Paths

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Tty(io.StringIO):
    def isatty(self):
        return True


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "repo")
        self.config = os.path.join(self.tmp, "config", "ai-code-factory")
        self.state = os.path.join(self.tmp, "state", "ai-code-factory")
        os.makedirs(self.root)
        self.paths = Paths(self.root, self.config, self.state)
        self.environ = {"HOME": self.tmp, "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
                        "XDG_STATE_HOME": os.path.join(self.tmp, "state"), "PATH": os.path.join(self.tmp, "bin") + os.pathsep + "/usr/bin:/bin"}
        self.execs = []

    def write(self, path, text="", mode=None):
        path = os.path.join(self.tmp, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        if mode:
            os.chmod(path, mode)
        return path

    def tool(self, name, body):
        """An executable in the PATH folder the commands under test see first."""
        return self.write(os.path.join("bin", name), "#!/bin/sh\n" + body, 0o755)

    def execer(self, argv, env):
        self.execs.append((argv, env))
        return 0

    def ctx(self, tty=False):
        self.out, self.err = (Tty() if tty else io.StringIO()), io.StringIO()
        self.ran = []

        def runner(argv, env=None, **kw):
            # Really run it, with its output captured into self.out / self.err.
            self.ran.append(argv)
            cp = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=kw.get("stdout", subprocess.PIPE),
                                stderr=subprocess.PIPE)
            self.out.write((cp.stdout or b"").decode())
            self.err.write(cp.stderr.decode())
            return cp.returncode

        return Context(self.paths, self.environ, self.out, self.err, runner, self.execer)

    def script(self, name):
        return [sys.executable, os.path.join(self.root, "scripts", name)]
