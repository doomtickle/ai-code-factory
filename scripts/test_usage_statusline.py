"""Tests for scripts/usage-statusline.py (factory usage): what it records from a
status line payload, and how enable/disable edit Claude Code's settings file
(keeping a backup and chaining any status line that was already there).

Run on the host with the system Python:
  python3 -m unittest discover -s scripts -p 'test_usage_statusline.py' -v
"""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "usage-statusline.py"
_spec = importlib.util.spec_from_file_location("usage_statusline", SCRIPT)
u = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(u)

PAYLOAD = {"model": {"display_name": "Opus"}, "session_id": "s", "cost": {"total_cost_usd": 1.0},
           "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                           "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}


def run(args, stdin=""):
    return subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args], input=stdin,
                          capture_output=True, text=True, timeout=30)


class ExtractTests(unittest.TestCase):
    def test_keeps_only_the_numbers(self):
        rec = u.extract(PAYLOAD)
        self.assertEqual(rec["five_hour"], {"used_percentage": 23.5, "resets_at": 1738425600.0})
        self.assertEqual(rec["seven_day"]["used_percentage"], 41.2)
        self.assertEqual(set(rec), {"five_hour", "seven_day", "updated"})  # no model, session, cost

    def test_missing_or_malformed(self):
        self.assertIsNone(u.extract({}))
        self.assertIsNone(u.extract(None))
        self.assertIsNone(u.extract({"rate_limits": {}}))
        self.assertIsNone(u.extract({"rate_limits": {"five_hour": {"used_percentage": "23", "resets_at": 1}}}))
        rec = u.extract({"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 9},
                                         "seven_day": {"used_percentage": True, "resets_at": float("nan")}}})
        self.assertIsNone(rec["seven_day"])  # a window can be absent; the other still counts
        self.assertEqual(u.summary(rec), "session 5%")


class StatuslineTests(unittest.TestCase):
    def test_records_and_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = run(["--state", tmp], json.dumps(PAYLOAD))
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.strip(), "session 24% · week 41%")
            rec = json.loads((Path(tmp) / "usage.json").read_text())
            self.assertEqual(rec["five_hour"]["used_percentage"], 23.5)
            self.assertAlmostEqual(rec["updated"], time.time(), delta=60)

    def test_no_limits_keeps_the_last_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(["--state", tmp], json.dumps(PAYLOAD))
            out = run(["--state", tmp], json.dumps({"model": {}}))  # API-key session, or before the first reply
            self.assertEqual((out.returncode, out.stdout.strip()), (0, ""))
            self.assertIn("five_hour", (Path(tmp) / "usage.json").read_text())
            self.assertEqual(run(["--state", tmp], "not json").returncode, 0)

    def test_missing_state_folder_never_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            out = run(["--state", blocker / "state"], json.dumps(PAYLOAD))  # can't mkdir under a file
            self.assertEqual(out.returncode, 0)

    def test_chains_the_previous_status_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            config.mkdir()
            (config / u.PREVIOUS).write_text(json.dumps({"statusLine": {
                "type": "command", "command": "python3 -c 'import sys,json; print(\"mine:\", json.load(sys.stdin)[\"session_id\"])'"}}))
            out = run(["--state", Path(tmp) / "state", "--config", config], json.dumps(PAYLOAD))
            self.assertEqual(out.stdout.strip(), "mine: s")
            self.assertTrue((Path(tmp) / "state" / "usage.json").exists())


class EnableDisableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings, self.state, self.config = root / "claude" / "settings.json", root / "state", root / "config"
        self.settings.parent.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def enable(self):
        with mock.patch("sys.stdout", io.StringIO()):
            return u.enable(self.settings, self.state, self.config, "/usr/bin/python3")

    def disable(self):
        with mock.patch("sys.stdout", io.StringIO()):
            return u.disable(self.settings, self.config)

    def read(self):
        return json.loads(self.settings.read_text())

    def test_fresh_settings_round_trip(self):
        self.assertEqual(self.enable(), 0)
        line = self.read()["statusLine"]
        self.assertEqual(line["type"], "command")
        self.assertIn("usage-statusline.py", line["command"])
        self.assertIn(str(self.state), line["command"])
        self.assertEqual(self.disable(), 0)
        self.assertNotIn("statusLine", self.read())
        self.assertFalse((self.config / u.PREVIOUS).exists())

    def test_keeps_other_settings_and_backs_up(self):
        self.settings.write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}))
        self.enable()
        data = self.read()
        self.assertEqual((data["model"], data["permissions"]), ("opus", {"allow": ["Bash(ls)"]}))
        backup = json.loads((self.settings.parent / "settings.json.factory-backup").read_text())
        self.assertNotIn("statusLine", backup)

    def test_existing_status_line_is_chained_and_restored(self):
        mine = {"type": "command", "command": "~/bin/my-line.sh", "padding": 2}
        self.settings.write_text(json.dumps({"statusLine": mine}))
        self.enable()
        self.assertEqual(self.read()["statusLine"]["padding"], 2)
        self.assertEqual(u.load_previous(self.config), mine)
        self.enable()  # again (say, after moving the checkout): the saved one isn't overwritten by ours
        self.assertEqual(u.load_previous(self.config), mine)
        self.disable()
        self.assertEqual(self.read()["statusLine"], mine)

    def test_disable_leaves_someone_elses_status_line(self):
        other = {"type": "command", "command": "other.sh"}
        self.settings.write_text(json.dumps({"statusLine": other}))
        self.disable()
        self.assertEqual(self.read()["statusLine"], other)

    def test_broken_settings_untouched(self):
        self.settings.write_text("{not json")
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.enable(), 1)
        self.assertEqual(self.settings.read_text(), "{not json")


class ShowTests(unittest.TestCase):
    def test_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("No usage recorded yet", run(["show", "--state", tmp]).stdout)
            now = time.time()
            (Path(tmp) / "usage.json").write_text(json.dumps({
                "five_hour": {"used_percentage": 12.0, "resets_at": now + 3700},
                "seven_day": {"used_percentage": 50.0, "resets_at": now - 5}, "updated": now}))
            out = run(["show", "--state", tmp]).stdout
            self.assertRegex(out, r"Session \(5 h\)\s+12\.0%  resets in 1h0[01]m")
            self.assertRegex(out, r"Week \(7 d\)\s+unknown")  # its window already reset


if __name__ == "__main__":
    unittest.main()
