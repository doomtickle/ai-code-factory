"""Shared guard for the headless-Chrome tests (scripts/test_*_browser.py).

Without FACTORY_REQUIRE_BROWSER a test whose node or chrome-headless-shell is missing is skipped, as in the
factory sandboxes' own checkouts and on a laptop. With FACTORY_REQUIRE_BROWSER=1 (CI's `browser tests` job) it
fails instead, naming what is missing, so a runner without Chrome can never pass by skipping.
"""

import os
import shutil
import subprocess

TOOLS = ("node", "chrome-headless-shell")


def missing_tools():
    return [tool for tool in TOOLS if not shutil.which(tool)]


def needs_browser(cls):
    """Class decorator: check for the tools before each test, at run time (so PATH changes are seen)."""
    original = cls.setUp

    def setUp(self):
        missing = missing_tools()
        if missing:
            message = "needs " + " and ".join(missing)
            if os.environ.get("FACTORY_REQUIRE_BROWSER") == "1":
                self.fail(f"{message} (FACTORY_REQUIRE_BROWSER=1: a browser test may not be skipped)")
            self.skipTest(message)
        original(self)

    cls.setUp = setUp
    return cls


def run_bounded(test, command, timeout):
    """subprocess.run with captured text output; on a timeout fail `test` with the output produced so far, so the log shows how far it got."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        def text(data):
            return data.decode("utf-8", "replace") if isinstance(data, bytes) else data or ""
        test.fail(f"timed out after {timeout} s\n--- stdout ---\n{text(e.stdout)}\n--- stderr ---\n{text(e.stderr)}")
