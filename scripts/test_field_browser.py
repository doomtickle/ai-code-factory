"""The Thoughtfield in a real browser (headless Chrome) against `factory dashboard --demo`: every job is one packet,
none sits behind the witness at any viewport size (with the demo's jobs and with 65, one past the shader's 64 job slots, where each star must still land in its own job's packet), and a packet takes hover and a click
across its whole body, opens its job's monitor and stays marked while it is open. The browser steps are in
test_field_browser.js. Skipped when node or chrome-headless-shell is missing, unless FACTORY_REQUIRE_BROWSER=1 (CI), which fails it.

  python3 -m unittest discover -s scripts -p 'test_field_browser.py' -v
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

from browser_test_support import needs_browser, run_bounded

SCRIPTS = Path(__file__).resolve().parent


@needs_browser
class FieldBrowserTests(unittest.TestCase):
    def test_every_job_is_one_packet_clear_of_the_witness_and_takes_a_click(self):
        server = subprocess.Popen([sys.executable, str(SCRIPTS / "dashboard.py"), "--roles", str(SCRIPTS.parent / "roles"), "--demo", "--no-open"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(server.stdout.close)
        self.addCleanup(server.wait)
        self.addCleanup(server.terminate)
        url = re.search(r"http://127\.0\.0\.1:\d+/\?token=\S+", server.stdout.readline())
        self.assertTrue(url, "the demo dashboard printed no URL")
        run = run_bounded(self, ["node", str(SCRIPTS / "test_field_browser.js"), url.group(0)], 2400)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
