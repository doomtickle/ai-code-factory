"""The Thoughtfield's "What do you need from me?" chip and pop-up in a real browser (headless Chrome) against
`factory dashboard --demo`: the steps are in test_field_shown_browser.js. Skipped when node or chrome-headless-shell is missing.

  python3 -m unittest discover -s scripts -p 'test_field_shown_browser.py' -v
"""

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("node") and shutil.which("chrome-headless-shell"), "needs node and chrome-headless-shell")
class FieldShownBrowserTests(unittest.TestCase):
    def test_chip_popup_copy_hold_and_display_only(self):
        server = subprocess.Popen([sys.executable, str(SCRIPTS / "dashboard.py"), "--roles", str(SCRIPTS.parent / "roles"), "--demo", "--no-open"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(server.stdout.close)
        self.addCleanup(server.wait)
        self.addCleanup(server.terminate)
        url = re.search(r"http://127\.0\.0\.1:\d+/\?token=\S+", server.stdout.readline())
        self.assertTrue(url, "the demo dashboard printed no URL")
        run = subprocess.run(["node", str(SCRIPTS / "test_field_shown_browser.js"), url.group(0)], capture_output=True, text=True, timeout=240, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
