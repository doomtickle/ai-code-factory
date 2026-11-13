"""FACTORY_CONFIG/dashboard.toml (scripts/dashboard_config.py, doomtickle/passdesk-ops#196): the strict
reader, the retired classic-view keys it still accepts, the file checks, the example, what the Dashboard
serves and what SETTINGS says, and that an old file still opens the Thoughtfield.

  python3 -m unittest discover -s scripts -p 'test_dashboard_config.py' -v
"""

import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import dashboard as d
import dashboard_config as dc
import dashboard_setup
from factory.context import Paths

UI = Path(__file__).resolve().parent / "dashboard-ui"
REPO = Path(__file__).resolve().parent.parent
FULL = '''# a comment
plant = "NORTH WORKS"   # the plate
'''
OLD = '''# an old file, from the days of the classic view
plant = "NORTH WORKS"
shifts = ["early", 'late', "graveyard"]
crt = 7
sound = true
ui = "classic"

[roles.colors]
designer = "#D08CFF"
build-bot = '#00ff7f'   # green
'''


class ParseTests(unittest.TestCase):
    def refused(self, text, *words):
        with self.assertRaises(dc.Refused) as caught:
            dc.parse(text)
        for word in words:
            self.assertIn(word, str(caught.exception))
        return str(caught.exception)

    def test_a_full_file(self):
        self.assertEqual(dc.parse(FULL), ({"plant": "NORTH WORKS"}, []))

    def test_empty_and_comment_only_text_is_the_defaults(self):
        self.assertEqual(dc.parse(""), (dc.defaults(), []))
        self.assertEqual(dc.parse("# plant = \"X\"\n\n   # sound = true\n"), (dc.defaults(), []))
        self.assertEqual(dc.defaults(), {"plant": None})

    def test_windows_line_endings(self):
        self.assertEqual(dc.parse('plant = "A"\r\nsound = false\r\n'), ({"plant": "A"}, ["sound"]))

    def test_an_unknown_key_names_the_key_and_line(self):
        self.refused('plant = "A"\nbogus = 1\n', 'line 2', 'unknown key "bogus"')

    def test_an_unknown_table_is_refused(self):
        self.refused('[server]\nport = 1\n', "line 1", "unknown table [server]")
        self.refused('[roles]\n', "unknown table [roles]")
        self.refused('[[roles.colors]]\n', "unknown table")

    def test_a_key_twice_is_refused(self):
        self.refused('plant = "A"\nplant = "B"\n', "plant is set twice")
        self.refused('crt = 1\ncrt = 2\n', "crt is set twice")
        self.refused('[roles.colors]\n[roles.colors]\n', "appears twice")

    def test_a_hostile_header_line_is_refused_quickly(self):
        for line in ("[" + " \t" * 8000 + "x", "[" + " \t" * 8000 + "x]" + " \t" * 8000 + "y",
                     "[" * 16000, "[" + " " * 16000):
            started = time.monotonic()
            self.refused(line + "\n", "line 1")
            self.assertLess(time.monotonic() - started, 1.0, line[:12])

    def test_plant_is_a_capped_printable_string(self):
        self.assertEqual(dc.parse('plant = "x' + "y" * 23 + '"')[0]["plant"], "x" + "y" * 23)
        self.refused('plant = "%s"' % ("y" * 25), "1 to 24")
        for bad in ('""', '"   "', '"café"', '"a\\nb"', '"tab\there"', "LOIS", '"unclosed', '"a" "b"', "['x ']"):
            self.refused("plant = %s\n" % bad, "plant must")
        self.assertEqual(dc.parse("plant = 'single # not a comment'")[0]["plant"], "single # not a comment")

    def test_the_retired_keys_and_table_are_accepted_and_ignored(self):
        settings, retired = dc.parse(OLD)
        self.assertEqual(settings, {"plant": "NORTH WORKS"})
        self.assertEqual(retired, ["shifts", "crt", "sound", "ui", "[roles.colors]"])

    def test_retired_values_are_not_read_so_old_mistakes_do_not_refuse_the_file(self):
        text = 'crt = 99\nsound = maybe\nshifts = nonsense\nui = "tube"\n[roles.colors]\nBad Role = red\nx = 3\n'.replace("Bad Role", "Bad-Role")
        self.assertEqual(dc.parse(text), (dc.defaults(), ["crt", "sound", "shifts", "ui", "[roles.colors]"]))

    def test_a_retired_key_twice_is_still_refused(self):
        self.refused('sound = true\nsound = false\n', "sound is set twice")

    def test_junk_lines(self):
        for bad in ("nonsense", "= 3", "crt 3", "'crt' = 3", "crt: 3", "[", "[roles.colors"):
            self.refused(bad + "\n", "line 1")


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.path = os.path.join(self.dir, "dashboard.toml")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data, mode="w"):
        with open(self.path, mode) as f:
            f.write(data)

    def test_a_missing_file_or_folder_is_the_defaults_and_no_problem(self):
        self.assertEqual(dc.load(self.dir), (dc.defaults(), "", ""))
        self.assertEqual(dc.load(os.path.join(self.dir, "nope")), (dc.defaults(), "", ""))
        self.assertEqual(dc.load(None), (dc.defaults(), "", ""))

    def test_a_good_file(self):
        self.write(FULL)
        self.assertEqual(dc.load(self.dir), ({"plant": "NORTH WORKS"}, "", ""))

    def test_an_old_file_loads_with_a_one_line_notice(self):
        self.write(OLD)
        look, problem, notice = dc.load(self.dir)
        self.assertEqual((look, problem), ({"plant": "NORTH WORKS"}, ""))
        self.assertNotIn("\n", notice)
        self.assertTrue(notice.startswith(self.path + ": "), notice)
        for word in ("shifts", "crt", "sound", "ui", "[roles.colors]", "no longer do anything"):
            self.assertIn(word, notice)

    def test_a_bad_file_is_the_defaults_and_a_problem_naming_the_file_and_key(self):
        self.write('plant = "ok"\nbogus = 1\n')
        look, problem, _ = dc.load(self.dir)
        self.assertEqual(look, dc.defaults())
        self.assertTrue(problem.startswith(self.path + ": line 2: "), problem)
        self.assertIn('unknown key "bogus"', problem)

    def test_a_symlink_is_refused(self):
        real = os.path.join(self.dir, "real.toml")
        with open(real, "w") as f:
            f.write("plant = \"A\"\n")
        os.symlink(real, self.path)
        look, problem, _ = dc.load(self.dir)
        self.assertEqual(look, dc.defaults())
        self.assertIn("symlink or not a regular file", problem)

    def test_a_dangling_symlink_is_refused_too(self):
        os.symlink(os.path.join(self.dir, "gone"), self.path)
        self.assertIn("symlink or not a regular file", dc.load(self.dir)[1])

    def test_a_folder_and_a_fifo_are_refused_without_hanging(self):
        os.mkdir(self.path)
        self.assertIn("not a regular file", dc.load(self.dir)[1])
        os.rmdir(self.path)
        os.mkfifo(self.path)
        self.assertIn("not a regular file", dc.load(self.dir)[1])

    def test_a_file_over_the_cap_is_refused(self):
        self.write("# " + "x" * dc.MAX_BYTES + "\n")
        look, problem, _ = dc.load(self.dir)
        self.assertEqual(look, dc.defaults())
        self.assertIn("is over 16 KiB", problem)
        self.write("#" * (dc.MAX_BYTES - 1) + "\n")   # exactly at the cap is fine
        self.assertEqual(dc.load(self.dir)[1], "")

    def test_text_that_is_not_utf8_is_refused(self):
        self.write(b'plant = "\xff\xfe"\n', "wb")
        self.assertIn("not UTF-8", dc.load(self.dir)[1])

    def test_a_file_that_cant_be_read_is_a_problem_not_a_crash(self):
        self.write('plant = "A"\n')
        os.chmod(self.path, 0)
        try:
            if os.access(self.path, os.R_OK):
                self.skipTest("running as a user that can read anything")
            look, problem, _ = dc.load(self.dir)
            self.assertEqual(look, dc.defaults())
            self.assertIn("can't be opened", problem)
        finally:
            os.chmod(self.path, 0o600)


class ExampleTests(unittest.TestCase):
    def test_the_example_is_all_comments_and_changes_nothing(self):
        text = (REPO / "config.example" / "dashboard.toml").read_text()
        self.assertEqual([l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")], [])
        self.assertEqual(dc.parse(text), (dc.defaults(), []))

    def test_the_example_line_is_valid_once_uncommented(self):
        lines = (REPO / "config.example" / "dashboard.toml").read_text().splitlines()
        live = [l[2:] for l in lines if l.startswith("# ") and " = " in l]
        self.assertEqual(live, ['plant = "LOIS WORKS"'])
        self.assertEqual(dc.parse("\n".join(live)), ({"plant": "LOIS WORKS"}, []))


class DashboardWiringTests(unittest.TestCase):
    def board(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state, roles = Path(tmp.name) / "state", Path(tmp.name) / "roles"
        (state / "dispatch").mkdir(parents=True)
        (roles / "developer").mkdir(parents=True)
        (roles / "developer" / "role.conf").write_text('WORKSPACE="rw"\nLANES=1\n')
        return d.Dashboard(state, roles, "proj", **kw)

    def test_the_snapshot_serves_the_look(self):
        look = dc.parse(FULL)[0]
        self.assertEqual(self.board(look=look).snapshot(1_700_000_000.0)["look"], look)

    def test_without_a_file_the_snapshot_serves_the_defaults(self):
        self.assertEqual(self.board().snapshot(1_700_000_000.0)["look"], dc.defaults())

    def test_the_look_survives_json(self):
        snap = self.board(look=dc.parse(FULL)[0]).snapshot(1_700_000_000.0)
        self.assertEqual(json.loads(json.dumps(snap))["look"], {"plant": "NORTH WORKS"})

    def test_demo_mode_reads_no_real_config(self):
        self.assertIn("dcfg.defaults()", Path(d.__file__).read_text())
        self.assertNotIn("look=dcfg.load", Path(d.__file__).read_text())

    def test_an_old_file_with_ui_classic_shifts_and_sound_still_opens_the_thoughtfield(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "dashboard.toml").write_text(OLD)
        look, problem, notice = dc.load(tmp.name)
        self.assertEqual((problem, bool(notice)), ("", True))
        board = self.board(look=look, look_problem=problem)
        server, _url = d.serve(board, 0, "tok", manual=None)
        self.addCleanup(d.stop, server)
        port = server.server_address[1]
        headers = {"Host": f"127.0.0.1:{port}", "Cookie": f"{d.cookie_name(port)}=tok"}

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            return resp, resp.read()

        resp, body = get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b'id="plate-name"', body)
        self.assertEqual(body, (UI / "field.html").read_bytes())
        self.assertEqual(json.loads(get("/api/state")[1])["look"], {"plant": "NORTH WORKS"})

    def test_setup_says_when_the_file_is_in_use_absent_or_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, config, state = (Path(tmp.name) / n for n in ("root", "config", "state"))
        for p in (root / "roles", config, state):
            p.mkdir(parents=True)
        paths = Paths(str(root), str(config), str(state))
        def look(*a):
            got = dashboard_setup.setup(paths, "proj", *a)["look"]
            got.pop("values")
            return got
        self.assertEqual(look(), {"file": False, "problem": ""})
        (config / "dashboard.toml").write_text('plant = "A"\n')
        self.assertEqual(look(), {"file": True, "problem": ""})
        got = dashboard_setup.setup(paths, "proj", "/x/dashboard.toml: line 2: unknown key \"bogus\"\x1b[2J")["look"]
        self.assertEqual(got["file"], True)
        self.assertIn('unknown key "bogus"', got["problem"])
        self.assertNotIn("\x1b", got["problem"])
        self.assertIsNone(dashboard_setup.setup(None, "proj", "x"))


if __name__ == "__main__":
    unittest.main()
