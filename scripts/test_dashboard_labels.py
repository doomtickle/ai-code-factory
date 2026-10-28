"""The lane labels shared by the factory floor and the lane modules
(scripts/dashboard_labels.py, doomtickle/passdesk-ops#194): the desk placard, the developer a
reviewer is checking, the branch a developer is on, and the "DEV 2" tag the browser draws
(dashboard-ui/lanelabel.js, run here under node).

  python3 -m unittest discover -s scripts -p 'test_dashboard_labels.py' -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

import dashboard_labels as L

UI = Path(__file__).resolve().parent / "dashboard-ui"


class DeskLabelTests(unittest.TestCase):
    def test_branch_topic_wins(self):
        for text, want in (
            ("Repo: ai-code-factory. Branch from main; commit to `factory/desk-placards`.", "desk placards"),
            ("Review factory-developer-lane2/factory/rate_limit.v2 now", "rate limit v2"),
            ("push factory/a-very-long-branch-name-indeed", "a very long branch name indeed"),
            ("Fix x, then factory/first and factory/second", "first"),
            ("Put the bundle in .factory/outgoing/ then push factory/fix-x", "fix x"),
            ("See https://example.com/o/factory/pull/1 and push factory/fix-x", "fix x"),
            ("Edit a/factory/b then factory-developer/factory/fix-y", "fix y"),
            ("Review origin/factory/fix-x", "fix x"),
            ("Diff main...factory/fix-x", "fix x"),
            ("git diff origin/main...factory-developer/factory/fix-y", "fix y"),
        ):
            self.assertEqual(L.desk_label(text), want)

    def test_branch_pattern_ignores_lookalikes(self):
        self.assertEqual(L.desk_label("Edit ai-code-factory/scripts today"), "Edit ai-code-factory/scripts today")
        self.assertEqual(L.desk_label("use factory/<topic> branches"), "use factory/ topic branches")  # a placeholder is no branch

    def test_fallback_first_words(self):
        for text, want in (
            ("Reject empty names in greet(), test it, and push", "Reject empty names in greet(), test it,"),
            ("Repo: passdesk-ops. Fix the **dashboard** crash", "Fix the dashboard crash"),
            ("Task: Fix the login crash", "Fix the login crash"),
            ("goal:make tests pass", "make tests pass"),
            ("Repo: ai-code-factory.git Fix x", "Fix x"), ("Repository: a.b. Do it", "Do it"),
            ("Repo: x;Fix y", "Fix y"), ("Project: demo; Tidy up", "Tidy up"),
            ("\n\n  Tidy the `docs`  \nsecond line", "Tidy the docs"),
            ("Supercalifragilisticexpialidocious", "Supercalifragilisticexpialidocious"),
            ("Pneumonoultramicroscopicsilicovolcanoconiosis-dust", "Pneumonoultramicroscopicsilicovolcanoco…"),
            ("Tidy the packaging metadata, changelog and release notes", "Tidy the packaging metadata, changelog"),
            ("", ""), ("   \n ", ""), ("Repo: x.", ""),
        ):
            self.assertEqual(L.desk_label(text), want)

    def test_one_clean_line(self):
        label = L.desk_label("\x1b[31m\u202eevil\x00 <b>thing</b>\ttab")
        self.assertTrue(label.isprintable() and len(label) <= L.DESK_MAX, repr(label))
        self.assertNotRegex(label, r"[<>\u202e]")
        self.assertLessEqual(len(L.desk_label("word " * 50)), L.DESK_MAX)
        self.assertEqual(L.desk_label(None), "")

    def test_reviewed_lane(self):
        for text, want in (
            ("Review factory-developer/factory/x", "developer"),
            ("Review factory-developer-lane3/factory/x and factory-developer/factory/y", "developer-lane3"),
            ("Review factory/x", ""), ("factory-reviewer/factory/x", ""),
            ("factory-developer-lane/factory/x", ""), ("xfactory-developer/factory/x", ""), ("", ""),
        ):
            self.assertEqual(L.reviewed_lane(text), want, text)


class BranchTests(unittest.TestCase):
    def test_branch_topic(self):
        for text, want in (
            ("commit to `factory/desk-placards`.", "desk-placards"),
            ("push factory/fix-x.", "fix-x"),
            ("Review factory-developer-lane2/factory/rate_limit.v2 now", "rate_limit.v2"),
            ("git diff origin/main...factory/fix-x", "fix-x"),
            ("Fix x, then factory/first and factory/second", "first"),
            ("Put the bundle in .factory/outgoing/ then push factory/fix-y", "fix-y"),
            ("use factory/<topic> branches", ""), ("Edit ai-code-factory/scripts", ""), ("", ""), (None, ""),
        ):
            self.assertEqual(L.branch_topic(text), want, text)

    def test_lane_branch_prefers_the_task_then_the_log(self):
        log = ["factory-developer/factory/from-log at 1a2b3c4", "factory/second at 5d6e7f8"]
        self.assertEqual(L.lane_branch("Fix x and push factory/from-task", log), "factory/from-task")
        self.assertEqual(L.lane_branch("Fix x", log), "factory/from-log")
        self.assertEqual(L.lane_branch("", log), "factory/from-log")
        self.assertEqual(L.lane_branch("Fix x", ["no branch here"]), "")
        self.assertEqual(L.lane_branch("Fix x"), "")
        self.assertEqual(L.lane_branch(None, []), "")

    def test_the_desk_label_and_the_branch_agree(self):
        for text in ("commit to `factory/desk-placards`.", "Review origin/factory/fix_x.y"):
            self.assertEqual(L.desk_label(text).split(), L.lane_branch(text).removeprefix("factory/").replace("_", " ").replace(".", " ").replace("-", " ").split())

    def test_branch_is_one_bounded_line(self):
        name = L.lane_branch("push factory/" + "a" * 300)
        self.assertEqual(len(name), L.BRANCH_MAX)
        self.assertTrue(name.isprintable())


@unittest.skipUnless(shutil.which("node"), "needs node")
class DevTagTests(unittest.TestCase):
    def run_js(self, expr):
        code = (f"const L = require({str(UI / 'lanelabel.js')!r}); process.stdout.write(JSON.stringify({expr}));")
        return subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout

    def test_dev_tag(self):
        for name, want in (("developer", "DEV 1"), ("developer-lane2", "DEV 2"), ("developer-lane12", "DEV 12"),
                           ("reviewer", "REVIEWER"), ("a-very-long-lane-name", "A-VERY-…")):
            self.assertEqual(self.run_js(f"L.devTag({name!r})"), f'"{want}"', name)

    def test_review_tag(self):
        self.assertEqual(self.run_js("L.reviewTag('developer-lane3')"), '"▸ DEV 3"')
        self.assertEqual(self.run_js("L.reviewTag('')"), '""')
        self.assertEqual(self.run_js("L.reviewTag(null)"), '""')


if __name__ == "__main__":
    unittest.main()
