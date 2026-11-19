"""The Thoughtfield in a real browser (headless Chrome) against `factory dashboard --demo`: every job is one packet,
none sits behind the witness at any viewport size (with the demo's jobs and with 65, one past the shader's 64 job slots, where each star must still land in its own job's packet), and a packet takes hover and a click
across its whole body, opens its job's monitor and stays marked while it is open. The browser steps are in
test_field_browser.js. Skipped when node or chrome-headless-shell is missing, unless FACTORY_REQUIRE_BROWSER=1 (CI), which fails it.

  python3 -m unittest discover -s scripts -p 'test_field_browser.py' -v
  FIELD_SECTIONS=picking,crystals python3 -m unittest discover -s scripts -p 'test_field_browser.py' -v   # just those sections

FIELD_SECTIONS (names: `node scripts/test_field_browser.js --list`) is how CI runs the sections as parallel shards; unset runs all.
FIELD_CPU_THROTTLE=4 slows the page's CPU fourfold, to try a slow runner's timing locally (off by default; CI doesn't set it).
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from browser_test_support import needs_browser, run_bounded

SCRIPTS = Path(__file__).resolve().parent
JS = str(SCRIPTS / "test_field_browser.js")
# A CI shard (FIELD_SECTIONS set) must finish inside its job's 20 minutes, with about 3 for setup; a whole run on a slow host may take 40.
TIMEOUT = 2400 if os.environ.get("FIELD_SECTIONS") is None else 1020


def all_sections():
    return subprocess.run(["node", JS, "--list"], capture_output=True, text=True, check=True).stdout.split()


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
        run = run_bounded(self, ["node", JS, url.group(0)], TIMEOUT)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        # The sections that ran are the ones asked for (FIELD_SECTIONS, else all): a shard that ran none, or fewer, is a failure, never a pass.
        wanted = os.environ.get("FIELD_SECTIONS")
        wanted = all_sections() if wanted is None else [n.strip() for n in wanted.split(",")]
        self.assertEqual(run.stdout.strip(), "field browser checks passed: " + ", ".join(wanted), run.stdout + run.stderr)


@needs_browser
class FieldSectionSelectionTests(unittest.TestCase):
    def select(self, value):
        env = {**os.environ, "FIELD_SECTIONS": value}
        # An unusable selection is refused before any browser starts, so the url is never fetched.
        return subprocess.run(["node", JS, "http://127.0.0.1:9/"], capture_output=True, text=True, env=env, timeout=60, check=False)

    def test_the_section_names_are_listed(self):
        names = all_sections()
        self.assertGreater(len(names), 1)
        self.assertEqual(len(names), len(set(names)))

    def test_an_unknown_empty_or_repeated_section_is_an_error_not_a_skip(self):
        for value, said in (("picking,nope", '"nope"'), ("", "no such section"), ("arc,arc", "more than once")):
            run = self.select(value)
            self.assertEqual(run.returncode, 2, (value, run.stdout, run.stderr))
            self.assertIn(said, run.stderr, value)
            self.assertNotIn("passed", run.stdout, value)


if __name__ == "__main__":
    unittest.main()
