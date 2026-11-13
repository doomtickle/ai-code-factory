"""The Thoughtfield's "What do you need from me?" chip and pop-up in a real browser (headless Chrome) against
`factory dashboard --demo`: the steps are in test_field_shown_browser.js. Skipped when node or chrome-headless-shell is missing, unless FACTORY_REQUIRE_BROWSER=1 (CI), which fails it.

  python3 -m unittest discover -s scripts -p 'test_field_shown_browser.py' -v
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

from browser_test_support import needs_browser, run_bounded

SCRIPTS = Path(__file__).resolve().parent


@needs_browser
class FieldShownBrowserTests(unittest.TestCase):
    def test_chip_popup_copy_hold_and_display_only(self):
        server = subprocess.Popen([sys.executable, str(SCRIPTS / "dashboard.py"), "--roles", str(SCRIPTS.parent / "roles"), "--demo", "--no-open"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(server.stdout.close)
        self.addCleanup(server.wait)
        self.addCleanup(server.terminate)
        url = re.search(r"http://127\.0\.0\.1:\d+/\?token=\S+", server.stdout.readline())
        self.assertTrue(url, "the demo dashboard printed no URL")
        run = run_bounded(self, ["node", str(SCRIPTS / "test_field_shown_browser.js"), url.group(0)], 240)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
