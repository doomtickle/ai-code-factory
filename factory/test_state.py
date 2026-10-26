"""Tests for factory/state.py: migrate_state is a port of the function of the same name in
scripts/lib/common.sh (which stays: start-broker.sh and 03-stop-factory.sh call it), so each
case runs both on identical fixtures and compares what they print, the exit status and the files.

ps is a stub, the "bridge" a sleeping child, and the folders are scratch ones.
"""

import os
import signal
import subprocess
import unittest
from unittest import mock

from factory import state
from factory.context import Context, FactoryError, Paths
from factory.fixtures import REPO, CommandTest

COMMON = os.path.join(REPO, "scripts", "lib", "common.sh")


class MigrateStateTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.sleeps = []
        patcher = mock.patch.object(state, "sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.children = []
        self.addCleanup(self.reap)
        self.tool("ps", 'if [ "$2" = "$FAKE_PS_PID" ]; then echo "python3 $FAKE_PS_ROOT/dispatch/server.py"; fi\n')

    def reap(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()

    def fixture(self, name, old=(), pid_of=None):
        """A scratch checkout and state folder (under tmp/NAME) holding the old in-repo folders OLD
        (each with a file in it); with PID_OF, a live process that .dispatch/pid names."""
        base = os.path.join(self.tmp, name)
        root = os.path.join(base, "repo")
        for folder in old:
            self.write(os.path.join(name, "repo", "." + folder, "file"), folder + "\n")
        environ = dict(self.environ, XDG_STATE_HOME=os.path.join(base, "state"), XDG_CONFIG_HOME=os.path.join(base, "config"))
        os.makedirs(root, exist_ok=True)
        if pid_of:
            child = subprocess.Popen(["sleep", "30"])
            self.children.append(child)
            self.write(os.path.join(name, "repo", ".dispatch", "pid"), "%d\n" % child.pid)
            environ.update(FAKE_PS_PID=str(child.pid), FAKE_PS_ROOT=root if pid_of is True else pid_of)
        return root, environ, os.path.join(base, "state", "ai-code-factory")

    def run_bash(self, root, environ):
        cp = subprocess.run(["bash", "-c", '. "$1"; migrate_state "$2"', "bash", COMMON, root], env=environ,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return cp.returncode, cp.stdout.decode()

    def run_python(self, root, environ):
        import io
        out, err = io.StringIO(), io.StringIO()
        ctx = Context(Paths(root, "", os.path.join(environ["XDG_STATE_HOME"], "ai-code-factory")), environ, out, err)
        try:
            state.migrate_state(ctx, root)
        except FactoryError as e:
            return 1, out.getvalue() + "error: %s\n" % e
        return 0, out.getvalue() + err.getvalue()

    def tree(self, base):
        found = []
        for d, dirs, files in os.walk(base):
            for n in sorted(dirs + files):
                p = os.path.join(d, n)
                found.append((os.path.relpath(p, base), os.stat(p).st_mode & 0o7777))
        return sorted(found)

    def both(self, **kw):
        """Run the bash and the Python on two identical fixtures: (their results, normalised)."""
        results = []
        for name, run in (("bash", self.run_bash), ("python", self.run_python)):
            root, environ, state_dir = self.fixture(name, **kw)
            rc, out = run(root, environ)
            pid = ""
            if kw.get("pid_of"):
                pid = environ["FAKE_PS_PID"]
            out = out.replace(root, "<ROOT>").replace(state_dir, "<STATE>").replace(pid or "\0", "<PID>")
            results.append((rc, out, self.tree(os.path.join(self.tmp, name))))
        return results

    def assertSame(self, **kw):
        bash, python = self.both(**kw)
        self.assertEqual(python[0], bash[0])
        self.assertEqual(python[1], bash[1])
        self.assertEqual(python[2], bash[2])
        return python

    def test_with_nothing_to_move_it_only_makes_the_state_folder_private(self):
        rc, out, tree = self.assertSame()
        self.assertEqual((rc, out), (0, ""))
        self.assertIn(("state/ai-code-factory", 0o700), tree)

    def test_old_state_folders_move_into_the_state_folder(self):
        rc, out, _ = self.assertSame(old=("broker", "dispatch", "roles"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "Moving <ROOT>/.broker to <STATE>/broker...\nMoving <ROOT>/.dispatch to <STATE>/dispatch...\n"
                              "Moving <ROOT>/.roles to <STATE>/roles...\n")

    def test_a_folder_that_exists_in_both_places_is_refused(self):
        for name in ("bash", "python"):
            self.write(os.path.join(name, "state", "ai-code-factory", "broker", "kept"), "x")
        rc, out, _ = self.assertSame(old=("broker",))
        self.assertEqual((rc, out), (1, "error: state exists both in <ROOT>/.broker and <STATE>/broker; keep one "
                                         "(move or delete the other), then re-run\n"))

    def test_a_bridge_still_running_from_the_old_place_is_stopped_first(self):
        rc, out, _ = self.assertSame(old=("dispatch",), pid_of=True)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("Stopping dispatch bridge (<PID>) to move its state out of the repo...\n"), out)
        self.assertEqual(self.sleeps, [1])
        for child in self.children:
            child.wait(5)
            self.assertEqual(child.returncode, -signal.SIGTERM)

    def test_a_pid_that_is_not_this_checkouts_bridge_is_left_alone(self):
        rc, out, _ = self.assertSame(old=("dispatch",), pid_of="/somewhere/else")
        self.assertEqual(rc, 0)
        self.assertNotIn("Stopping", out)
        self.assertEqual(self.sleeps, [])
        self.assertTrue(all(child.poll() is None for child in self.children))

    def test_a_pid_file_that_is_not_a_number_is_ignored(self):
        for name in ("bash", "python"):
            self.write(os.path.join(name, "repo", ".dispatch", "pid"), "not a pid\n")
        rc, out, _ = self.assertSame(old=("dispatch",))
        self.assertEqual(rc, 0)
        self.assertNotIn("Stopping", out)

    def test_a_bridge_that_cannot_be_stopped_is_an_error(self):
        root, environ, _ = self.fixture("gone", old=("dispatch",))
        done = subprocess.Popen(["true"])
        done.wait()
        pid = done.pid
        self.write("gone/repo/.dispatch/pid", "%d\n" % pid)
        self.tool("ps", 'echo "python3 %s/dispatch/server.py"\n' % root)
        rc, out = self.run_python(root, environ)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "Stopping dispatch bridge (%d) to move its state out of the repo...\n"
                              "error: could not stop the dispatch bridge (%d): No such process\n" % (pid, pid))
        self.assertTrue(os.path.isdir(os.path.join(root, ".dispatch")))


if __name__ == "__main__":
    unittest.main()
