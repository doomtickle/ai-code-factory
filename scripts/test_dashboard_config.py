"""FACTORY_CONFIG/dashboard.toml (scripts/dashboard_config.py, doomtickle/passdesk-ops#196): the strict
reader, the file checks, the example, what the Dashboard serves and what SETUP says, and the browser's
second check of it (dashboard-ui/look.js, run here under node).

  python3 -m unittest discover -s scripts -p 'test_dashboard_config.py' -v
"""

import json
import os
import shutil
import subprocess
import tempfile
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
shifts = ["early", 'late', "graveyard"]
crt = 7
sound = true

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
        self.assertEqual(dc.parse(FULL), {
            "plant": "NORTH WORKS", "shifts": ["early", "late", "graveyard"], "crt": 7, "sound": True,
            "colors": {"designer": "#d08cff", "build-bot": "#00ff7f"}})

    def test_empty_and_comment_only_text_is_the_defaults(self):
        self.assertEqual(dc.parse(""), dc.defaults())
        self.assertEqual(dc.parse("# plant = \"X\"\n\n   # sound = true\n"), dc.defaults())
        self.assertEqual(dc.defaults(), {"plant": None, "shifts": None, "crt": None, "sound": None, "colors": {}})

    def test_windows_line_endings(self):
        self.assertEqual(dc.parse('crt = 4\r\nsound = false\r\n')["crt"], 4)

    def test_an_unknown_key_names_the_key_and_line(self):
        self.refused('crt = 3\nbogus = 1\n', 'line 2', 'unknown key "bogus"')

    def test_an_unknown_table_is_refused(self):
        self.refused('[server]\nport = 1\n', "line 1", "unknown table [server]")
        self.refused('[roles]\n', "unknown table [roles]")
        self.refused('[[roles.colors]]\n', "unknown table")

    def test_the_colour_table_twice_and_a_key_twice(self):
        self.refused('[roles.colors]\n[roles.colors]\n', "appears twice")
        self.refused('crt = 1\ncrt = 2\n', "crt is set twice")
        self.refused('[roles.colors]\ndesigner = "#aaaaaa"\ndesigner = "#bbbbbb"\n', "designer is set twice")

    def test_a_top_level_key_after_the_table_is_a_colour_not_a_setting(self):
        self.refused('[roles.colors]\ncrt = 3\n', 'line 2', "the colour of crt")
        self.refused('[roles.colors]\nplant = "X"\n', 'line 2', "the colour of plant")

    def test_bad_colours(self):
        for bad in ('"red"', '"#fff"', '"#12345g"', '"#1234567"', '"d08cff"', "#d08cff", "0xd08cff", '"#d08cff" x', '""', "3"):
            self.refused('[roles.colors]\ndesigner = %s\n' % bad, "line 2", "designer")

    def test_role_names_must_match_the_role_name_pattern(self):
        for bad in ("Designer", "x", "9lives", "build_bot", "broker", "coordinator", "dispatch", "a" * 32, "fast-lane2"):
            self.refused('[roles.colors]\n%s = "#aaaaaa"\n' % bad, "not a role name")
        self.refused('[roles.colors]\n"designer" = "#aaaaaa"\n', "expected key = value")
        self.refused('[roles.colors]\ndesigner.x = "#aaaaaa"\n', "expected key = value")
        self.assertEqual(dc.parse('[roles.colors]\ndeveloper = "#aaaaaa"\nresearcher = "#bbbbbb"\n')["colors"],
                         {"developer": "#aaaaaa", "researcher": "#bbbbbb"})

    def test_too_many_colours(self):
        text = "[roles.colors]\n" + "".join('role-%02d = "#aaaaaa"\n' % i for i in range(dc.MAX_COLORS + 1))
        self.refused(text, "more than %d role colours" % dc.MAX_COLORS)
        self.assertEqual(len(dc.parse(text.rsplit("\n", 2)[0] + "\n")["colors"]), dc.MAX_COLORS)

    def test_crt_is_a_whole_number_from_0_to_10(self):
        for good in range(11):
            self.assertEqual(dc.parse("crt = %d" % good)["crt"], good)
        for bad in ("11", "-1", "3.5", '"3"', "true", "0x3", "03", "1e1", "", "3 4", "100"):
            self.refused("crt = %s\n" % bad, "crt must be a whole number from 0 to 10")
        self.assertEqual(dc.parse("crt = 5  # five")["crt"], 5)

    def test_sound_is_true_or_false(self):
        self.assertIs(dc.parse("sound = false")["sound"], False)
        for bad in ("yes", "True", "1", '"true"', "", "truee"):
            self.refused("sound = %s\n" % bad, "sound must be true or false")

    def test_plant_is_a_capped_printable_string(self):
        self.assertEqual(dc.parse('plant = "x' + "y" * 23 + '"')["plant"], "x" + "y" * 23)
        self.refused('plant = "%s"' % ("y" * 25), "1 to 24")
        for bad in ('""', '"   "', '"café"', '"a\\nb"', '"tab\there"', "LOIS", '"unclosed', '"a" "b"', "['x ']"):
            self.refused("plant = %s\n" % bad, "plant must")
        self.assertEqual(dc.parse("plant = 'single # not a comment'")["plant"], "single # not a comment")

    def test_shifts_are_exactly_three_capped_strings_on_one_line(self):
        self.assertEqual(dc.parse('shifts = [ "a" , "b","c", ]  # trailing comma ok')["shifts"], ["a", "b", "c"])
        for bad in ('["a", "b"]', '["a", "b", "c", "d"]', '[]', '"day"', '["a", "b", 3]', '["a" "b" "c"]',
                    '["a", "b", "c"] junk', '["a", "b", ""]', '["a", "b", "%s"]' % ("c" * 17), '["a", "b",\n', '["a", "b", "c"'):
            self.refused("shifts = %s\n" % bad, "shifts")

    def test_a_value_with_a_hash_inside_a_string_is_not_cut_at_the_hash(self):
        self.assertEqual(dc.parse('[roles.colors]\nfoo = "#aabbcc" # c\n')["colors"], {"foo": "#aabbcc"})

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
        self.assertEqual(dc.load(self.dir), (dc.defaults(), ""))
        self.assertEqual(dc.load(os.path.join(self.dir, "nope")), (dc.defaults(), ""))
        self.assertEqual(dc.load(None), (dc.defaults(), ""))

    def test_a_good_file(self):
        self.write(FULL)
        look, problem = dc.load(self.dir)
        self.assertEqual((look["plant"], look["crt"], problem), ("NORTH WORKS", 7, ""))

    def test_a_bad_file_is_the_defaults_and_a_problem_naming_the_file_and_key(self):
        self.write('plant = "ok"\nbogus = 1\n')
        look, problem = dc.load(self.dir)
        self.assertEqual(look, dc.defaults())
        self.assertTrue(problem.startswith(self.path + ": line 2: "), problem)
        self.assertIn('unknown key "bogus"', problem)

    def test_a_symlink_is_refused(self):
        real = os.path.join(self.dir, "real.toml")
        with open(real, "w") as f:
            f.write("crt = 4\n")
        os.symlink(real, self.path)
        look, problem = dc.load(self.dir)
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
        look, problem = dc.load(self.dir)
        self.assertEqual(look, dc.defaults())
        self.assertIn("is over 16 KiB", problem)
        self.write("#" * (dc.MAX_BYTES - 1) + "\n")   # exactly at the cap is fine
        self.assertEqual(dc.load(self.dir)[1], "")

    def test_text_that_is_not_utf8_is_refused(self):
        self.write(b'plant = "\xff\xfe"\n', "wb")
        self.assertIn("not UTF-8", dc.load(self.dir)[1])

    def test_a_file_that_cant_be_read_is_a_problem_not_a_crash(self):
        self.write("crt = 3\n")
        os.chmod(self.path, 0)
        try:
            if os.access(self.path, os.R_OK):
                self.skipTest("running as a user that can read anything")
            look, problem = dc.load(self.dir)
            self.assertEqual(look, dc.defaults())
            self.assertIn("can't be opened", problem)
        finally:
            os.chmod(self.path, 0o600)


class ExampleTests(unittest.TestCase):
    def test_the_example_is_all_comments_and_changes_nothing(self):
        text = (REPO / "config.example" / "dashboard.toml").read_text()
        self.assertEqual([l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")], [])
        self.assertEqual(dc.parse(text), dc.defaults())

    def test_the_example_lines_are_valid_once_uncommented(self):
        lines = (REPO / "config.example" / "dashboard.toml").read_text().splitlines()
        live = [l[2:] for l in lines if l.startswith("# ") and (" = " in l or l.startswith("# [roles"))]
        self.assertEqual(len(live), 6)
        self.assertEqual(dc.parse("\n".join(live)), {
            "plant": "LOIS WORKS", "shifts": ["day", "swing", "night"], "crt": 3, "sound": False,
            "colors": {"designer": "#d08cff"}})


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
        look = dc.parse(FULL)
        self.assertEqual(self.board(look=look).snapshot(1_700_000_000.0)["look"], look)

    def test_without_a_file_the_snapshot_serves_the_defaults(self):
        self.assertEqual(self.board().snapshot(1_700_000_000.0)["look"], dc.defaults())

    def test_the_look_survives_json(self):
        snap = self.board(look=dc.parse(FULL)).snapshot(1_700_000_000.0)
        self.assertEqual(json.loads(json.dumps(snap))["look"]["colors"], {"designer": "#d08cff", "build-bot": "#00ff7f"})

    def test_demo_mode_reads_no_real_config(self):
        self.assertIn("dcfg.defaults()", Path(d.__file__).read_text())
        self.assertNotIn("look=dcfg.load", Path(d.__file__).read_text())

    def test_setup_says_when_the_file_is_in_use_absent_or_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, config, state = (Path(tmp.name) / n for n in ("root", "config", "state"))
        for p in (root / "roles", config, state):
            p.mkdir(parents=True)
        paths = Paths(str(root), str(config), str(state))
        self.assertEqual(dashboard_setup.setup(paths, "proj")["look"], {"file": False, "problem": ""})
        (config / "dashboard.toml").write_text("crt = 3\n")
        self.assertEqual(dashboard_setup.setup(paths, "proj")["look"], {"file": True, "problem": ""})
        got = dashboard_setup.setup(paths, "proj", "/x/dashboard.toml: line 2: unknown key \"bogus\"\x1b[2J")["look"]
        self.assertEqual(got["file"], True)
        self.assertIn('unknown key "bogus"', got["problem"])
        self.assertNotIn("\x1b", got["problem"])
        self.assertIsNone(dashboard_setup.setup(None, "proj", "x"))


@unittest.skipUnless(shutil.which("node"), "needs node")
class LookScriptTests(unittest.TestCase):
    def run_js(self, expr):
        code = (f"const L = require({str(UI / 'look.js')!r}); "
                f"const show = v => JSON.stringify(v, (k, x) => (x instanceof Map ? [...x] : x)); process.stdout.write(show({expr}));")
        return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)

    def test_a_good_look_passes_through(self):
        got = self.run_js('L.read({plant: "A B", shifts: ["a", "b", "c"], crt: 4, sound: true, colors: {designer: "#d08cff"}})')
        self.assertEqual(got, {"plant": "A B", "shifts": ["a", "b", "c"], "crt": 4, "sound": True, "colors": [["designer", "#d08cff"]]})

    def test_anything_off_reads_as_not_set(self):
        none = {"plant": None, "shifts": None, "crt": None, "sound": None, "colors": []}
        for raw in ("null", "undefined", "5", '"x"', "[]", "{}"):
            self.assertEqual(self.run_js(f"L.read({raw})"), none, raw)
        bad = ('{plant: "<b>x</b>\\u2028", shifts: ["a", "b"], crt: 11, sound: "yes", colors: {Designer: "#aaaaaa", ok: "red", "x": "#aaaaaa", "ok-role": "#AAAAAA", '
               'pwn: "red; background:url(//evil)", pwn2: "#aaaaaa; x", "__proto__": "#aaaaaa"}}')
        got = self.run_js(f"L.read({bad})")
        self.assertEqual((got["plant"], got["shifts"], got["crt"], got["sound"], got["colors"]), (None, None, None, None, []))

    def test_each_field_is_checked_alone(self):
        self.assertEqual(self.run_js('L.read({crt: 3.5})["crt"]'), None)
        self.assertEqual(self.run_js('L.read({crt: "3"})["crt"]'), None)
        self.assertEqual(self.run_js('L.read({crt: 0})["crt"]'), 0)
        self.assertEqual(self.run_js('L.read({sound: false})["sound"]'), False)
        self.assertEqual(self.run_js('L.read({plant: "x".repeat(25)})["plant"]'), None)
        self.assertEqual(self.run_js('L.read({shifts: ["a", "b", "c".repeat(17)]})["shifts"]'), None)

    def test_the_glow_is_the_colour_at_half_strength(self):
        self.assertEqual(self.run_js('L.glow("#d08cff")'), "rgba(208, 140, 255, .5)")


if __name__ == "__main__":
    unittest.main()
