"""browser_test_support.needs_browser: skips when node or Chrome is missing, but fails when FACTORY_REQUIRE_BROWSER=1.

  python3 -m unittest discover -s scripts -p 'test_browser_test_support.py' -v
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

from browser_test_support import needs_browser, run_bounded


def run_guarded(env, path):
    """Run a guarded test under a PATH and environment; return (result, whether its body ran).

    The class is built here, not at module level, so discovery never runs it as a test of its own."""
    ran = []

    @needs_browser
    class Guarded(unittest.TestCase):
        def test_it(self):
            ran.append(True)

    result = unittest.TestResult()
    with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict(os.environ, {"PATH": path}):
        if "FACTORY_REQUIRE_BROWSER" not in env:
            os.environ.pop("FACTORY_REQUIRE_BROWSER", None)
        unittest.defaultTestLoader.loadTestsFromTestCase(Guarded).run(result)
    return result, bool(ran)


class NeedsBrowserTests(unittest.TestCase):
    def setUp(self):
        self.empty = tempfile.TemporaryDirectory()
        self.addCleanup(self.empty.cleanup)

    def test_skips_when_the_tools_are_missing_and_the_variable_is_unset(self):
        result, ran = run_guarded({}, self.empty.name)
        self.assertEqual((len(result.skipped), len(result.failures), ran), (1, 0, False))
        self.assertIn("needs node and chrome-headless-shell", result.skipped[0][1])

    def test_fails_when_the_tools_are_missing_and_the_variable_is_1(self):
        result, ran = run_guarded({"FACTORY_REQUIRE_BROWSER": "1"}, self.empty.name)
        self.assertEqual((len(result.skipped), len(result.failures), ran), (0, 1, False))
        self.assertIn("needs node and chrome-headless-shell", result.failures[0][1])

    def test_names_only_what_is_missing(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            node = os.path.join(bin_dir, "node")
            with open(node, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(node, 0o755)
            result, _ = run_guarded({"FACTORY_REQUIRE_BROWSER": "1"}, bin_dir)
        self.assertIn("needs chrome-headless-shell (", result.failures[0][1])

    def test_runs_the_test_when_the_tools_are_there(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            for tool in ("node", "chrome-headless-shell"):
                path = os.path.join(bin_dir, tool)
                with open(path, "w") as f:
                    f.write("#!/bin/sh\n")
                os.chmod(path, 0o755)
            result, ran = run_guarded({"FACTORY_REQUIRE_BROWSER": "1"}, bin_dir)
        self.assertEqual((result.wasSuccessful(), len(result.skipped), ran), (True, 0, True))


class RunBoundedTests(unittest.TestCase):
    def test_a_timeout_fails_the_test_with_the_output_so_far(self):
        code = "import sys, time; print('reached section two', flush=True); print('[field] picking +3s', file=sys.stderr, flush=True); time.sleep(30)"
        with self.assertRaises(self.failureException) as caught:
            run_bounded(self, [sys.executable, "-c", code], 2)
        message = str(caught.exception)
        self.assertTrue(message.startswith("timed out after 2 s"), message)
        self.assertIn("reached section two", message)
        self.assertIn("[field] picking +3s", message)

    def test_returns_the_finished_process(self):
        run = run_bounded(self, [sys.executable, "-c", "print('ok')"], 30)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "ok"))


if __name__ == "__main__":
    unittest.main()
