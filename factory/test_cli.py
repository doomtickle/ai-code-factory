"""Tests for factory/cli.py and the bin/factory shim that routes to it."""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from factory import cli

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(REPO, "bin", "factory")


def shim_ported_commands():
    with open(SHIM) as f:
        return re.search(r'^PORTED_COMMANDS="([^"]*)"', f.read(), re.M).group(1).split()


class ShimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = dict(os.environ, HOME=self.tmp, XDG_CONFIG_HOME=os.path.join(self.tmp, "config"),
                        XDG_STATE_HOME=os.path.join(self.tmp, "state"), PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("PYTHONPATH", None)

    def run_shim(self, *args, **kw):
        return subprocess.run([SHIM] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=kw.pop("env", self.env), cwd=kw.pop("cwd", self.tmp))

    def test_the_shim_and_the_package_list_the_same_commands(self):
        self.assertEqual(sorted(shim_ported_commands()), sorted(cli.COMMANDS))

    def test_internal_entry_points_are_not_commands(self):
        # Run by the host scripts that are shims now (scripts/start-broker.sh,
        # scripts/start-dispatch.sh), never by a user.
        self.assertEqual(list(cli.INTERNAL), ["_start-broker", "_start-dispatch"])  # alphabetical
        for name in cli.INTERNAL:
            self.assertTrue(name.startswith("_"))
            self.assertNotIn(name, cli.COMMANDS)
            self.assertNotIn(name, shim_ported_commands())
        out = self.run_shim("help").stdout.decode()
        self.assertNotIn("_start", out)
        cp = self.run_shim("_start-broker")
        self.assertEqual(cp.returncode, 1)
        self.assertIn(b"unknown command: _start-broker", cp.stderr)

    def test_a_ported_command_runs_in_python(self):
        cp = self.run_shim("roles")
        self.assertEqual((cp.returncode, cp.stderr), (0, b""))
        out = cp.stdout.decode()
        self.assertIn("developer          branches  3 lanes  ", out)
        self.assertIn('researcher         off (optional)  enable: add "researcher" to %s/config/ai-code-factory/enabled-roles'
                      % self.tmp, out)
        direct = subprocess.run([sys.executable, "-m", "factory", "roles"], stdout=subprocess.PIPE, env=self.env,
                                cwd=REPO).stdout.decode()
        self.assertEqual(out, direct)

    def test_an_unported_command_still_runs_in_bash(self):
        cp = self.run_shim("help")
        self.assertEqual(cp.returncode, 0)
        self.assertIn(b"factory roles ", cp.stdout)
        cp = self.run_shim("no-such-command")
        self.assertEqual(cp.returncode, 1)
        self.assertIn(b"unknown command: no-such-command", cp.stderr)

    def test_the_working_directory_cannot_stand_in_for_the_package(self):
        hostile = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(hostile, "factory"))
        for name in ("__init__.py", "cli.py", "conf.py", "roles.py"):
            with open(os.path.join(hostile, "factory", name), "w") as f:
                f.write("raise SystemExit('hostile ' + __name__)\n")
        with open(os.path.join(hostile, "cli.py"), "w") as f:
            f.write("raise SystemExit('hostile cli')\n")
        cp = self.run_shim("roles", cwd=hostile)
        self.assertEqual((cp.returncode, cp.stderr), (0, b""))
        self.assertIn(b"developer", cp.stdout)

    def test_sourcing_the_shim_does_not_route(self):
        cp = subprocess.run(["bash", "-c", '. "$1" roles; echo "sourced:$PORTED_COMMANDS"', "_", SHIM],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        self.assertEqual(cp.stdout.decode().strip(), "sourced:" + " ".join(shim_ported_commands()))


class MainTest(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(list(argv), environ={}, stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    def test_a_command_not_ported_yet_is_an_error(self):
        rc, out, err = self.run_main("start")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, "error: 'start' is not ported to the Python CLI yet; run it through bin/factory\n")

    def test_no_command_is_an_error(self):
        self.assertEqual(self.run_main()[0], 1)

    def test_an_internal_entry_point_runs_but_is_not_a_command(self):
        rc, out, err = self.run_main("_start-dispatch")
        self.assertEqual((rc, out), (1, ""))
        self.assertRegex(err, r"^error: usage: .*/scripts/start-dispatch\.sh <project-dir>\n$")
        with open(SHIM) as f:
            self.assertNotIn("_start-dispatch", f.read())


if __name__ == "__main__":
    unittest.main()
