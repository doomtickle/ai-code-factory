"""The browser-test shards of .github/workflows/ci.yml: the fast set (every pull request) and the visual set (nightly, on demand,
and on pull requests that touch the Thoughtfield's 3D code) together cover every field section exactly once, and every
scripts/test_*_browser.py file; the shown and mic tests and the field behaviour sections are in the fast set; the 3D-code path list
(scripts/ci-changes.sh, the `field3d` output) names the files that matter. A section added to test_field_browser.js and not given to
a shard fails here.

  python3 -m unittest discover -s scripts -p 'test_ci_shards.py' -v
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
ACTION = (ROOT / ".github" / "actions" / "browser-shard" / "action.yml").read_text()
FIELD = "test_field_browser.py"
FAST_JOB, VISUAL_JOB = "browser-shards", "visual-shards"
# The field sections that are behaviour and security checks, so they run on every pull request; every other section is visual.
FAST_FIELD_SECTIONS = ["nogate", "pane", "picking"]
# Browser tests that are fast-set by decision (09-fast-lane.md): they run on every pull request.
FAST_TESTS = ["test_field_shown_browser.py", "test_term_mic_browser.py"]
# What a pull request must touch to run the visual set: the Thoughtfield's 3D code, its page, its model and three.js, and the tests that drive them.
FIELD3D_PATHS = [
    "scripts/dashboard-ui/field-sky.js", "scripts/dashboard-ui/field-face.js", "scripts/dashboard-ui/field-order.js",
    "scripts/dashboard-ui/field.html", "scripts/dashboard-ui/field.css",
    "scripts/dashboard-ui/witness/witness.glb", "scripts/dashboard-ui/witness/ao.png", "scripts/dashboard-ui/witness/README.md",
    "scripts/dashboard-ui/vendor/three/three.min.js", "scripts/dashboard-ui/vendor/three/GLTFLoader.js",
    "scripts/test_field_browser.js", "scripts/test_field_browser.py",
]


def job(name):
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", CI, re.S | re.M)
    assert match, f"no job {name} in ci.yml"
    return match.group(1)


def shards(job_name):
    """[{shard, test, sections}] from a job's matrix include list (sections defaults to "")."""
    block = job(job_name)
    start = block.index("include:")
    end = re.search(r"^    (env|steps):", block[start:], re.M).start() + start
    out = []
    for item in re.split(r"^ +- (?=shard:)", block[start:end], flags=re.M)[1:]:
        entry = {"sections": ""}
        for key, value in re.findall(r"^\s*(?:- )?(shard|test|sections): *\"?([^\"\n]*)\"?\s*$", "shard: " + item, re.M):
            entry[key] = value.strip()
        out.append(entry)
    return out


def field_sections():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("needs node")
    return subprocess.run([node, str(SCRIPTS / "test_field_browser.js"), "--list"], capture_output=True, text=True, check=True).stdout.split()


def sections_of(shard_list):
    return [n for s in shard_list if s["test"] == FIELD for n in s["sections"].split(",") if n]


def coverage_problems(shard_list, everything):
    """What is wrong with how the field shards cover the sections: [] when each is in exactly one."""
    assigned = sections_of(shard_list)
    problems = [f"{n} is in no shard" for n in everything if n not in assigned]
    problems += [f"{n} is in more than one shard" for n in sorted(set(assigned)) if assigned.count(n) > 1]
    problems += [f"{n} is not a section" for n in sorted(set(assigned)) if n not in everything]
    return problems


def field3d_flag(*files):
    out = subprocess.run(["bash", str(SCRIPTS / "ci-changes.sh"), "--stdin"], input="\n".join(files) + "\n", capture_output=True, text=True, check=True).stdout
    return re.search(r"^field3d=(\w+)$", out, re.M).group(1)


class ShardCoverageTests(unittest.TestCase):
    def test_every_field_section_is_in_exactly_one_shard_across_both_sets(self):
        self.assertEqual(coverage_problems(shards(FAST_JOB) + shards(VISUAL_JOB), field_sections()), [])

    def test_a_section_left_out_or_repeated_or_unknown_is_reported(self):
        two = [{"test": FIELD, "sections": "a,b"}, {"test": FIELD, "sections": "c"}]
        self.assertEqual(coverage_problems(two, ["a", "b", "c"]), [])
        self.assertEqual(coverage_problems(two, ["a", "b", "c", "d"]), ["d is in no shard"])
        self.assertEqual(coverage_problems(two + [{"test": FIELD, "sections": "b"}], ["a", "b", "c"]), ["b is in more than one shard"])
        self.assertEqual(coverage_problems(two, ["a", "b"]), ["c is not a section"])

    def test_a_section_in_both_sets_is_reported(self):
        self.assertEqual(coverage_problems([{"test": FIELD, "sections": "a"}, {"test": FIELD, "sections": "a,b"}], ["a", "b"]), ["a is in more than one shard"])

    def test_the_fast_set_has_the_shown_and_mic_tests_and_the_behaviour_sections(self):
        fast = shards(FAST_JOB)
        for test in FAST_TESTS:
            self.assertIn(test, [s["test"] for s in fast])
        self.assertEqual(sorted(sections_of(fast)), FAST_FIELD_SECTIONS)

    def test_the_visual_set_runs_only_the_remaining_field_sections(self):
        visual = shards(VISUAL_JOB)
        self.assertEqual({s["test"] for s in visual}, {FIELD})
        self.assertEqual(sorted(field_sections()), sorted(sections_of(shards(FAST_JOB)) + sections_of(visual)))
        self.assertEqual([n for n in sections_of(visual) if n in FAST_FIELD_SECTIONS], [])

    def test_every_field_shard_names_its_sections_and_the_others_name_none(self):
        for s in shards(FAST_JOB) + shards(VISUAL_JOB):
            self.assertEqual(bool(s["sections"]), s["test"] == FIELD, s)

    def test_every_browser_test_file_has_a_shard(self):
        files = sorted(p.name for p in SCRIPTS.glob("test_*_browser.py"))
        self.assertEqual(sorted({s["test"] for s in shards(FAST_JOB) + shards(VISUAL_JOB)}), files)

    def test_shard_names_are_unique_across_both_sets_and_each_shard_gets_twenty_minutes(self):
        names = [s["shard"] for s in shards(FAST_JOB) + shards(VISUAL_JOB)]
        self.assertEqual(len(names), len(set(names)), names)
        for name in (FAST_JOB, VISUAL_JOB):
            self.assertRegex(job(name), r"(?m)^    timeout-minutes: 20$", name)
            self.assertRegex(job(name), r"(?m)^      fail-fast: false$", name)


class Field3dTests(unittest.TestCase):
    def test_the_3d_code_and_its_tests_turn_the_visual_set_on(self):
        for path in FIELD3D_PATHS:
            self.assertEqual(field3d_flag(path), "true", path)

    def test_other_files_leave_the_visual_set_off(self):
        for path in ("README.md", "docs/specs/factory-health/09-fast-lane.md", "scripts/dashboard.py", "scripts/dashboard-ui/term.js",
                     "scripts/dashboard-ui/field-monitor.js", "scripts/test_field_shown_browser.py", "factory/repos.py"):
            self.assertEqual(field3d_flag(path), "false", path)

    def test_the_changes_job_exports_field3d_and_a_push_to_main_leaves_it_off(self):
        self.assertIn("field3d: ${{ steps.flags.outputs.field3d }}", job("changes"))
        self.assertRegex(job("changes"), r'if \[ "\$EVENT" = push \]; then field3d=false; else field3d=true; fi')


class WorkflowTests(unittest.TestCase):
    def test_the_visual_set_runs_nightly_on_demand_and_when_field3d_is_not_false(self):
        self.assertRegex(CI, r"(?m)^  schedule:\n    - cron: ")
        self.assertRegex(CI, r"(?m)^  workflow_dispatch:$")
        self.assertIn("needs.changes.outputs.field3d != 'false'", job(VISUAL_JOB))
        # The fast set is never conditional on field3d.
        self.assertNotIn("field3d", job(FAST_JOB))

    def test_the_browser_tests_job_needs_both_sets_and_reads_both_results(self):
        block = job("browser")
        self.assertRegex(block, rf"(?m)^    needs: \[changes, {FAST_JOB}, {VISUAL_JOB}\]$")
        self.assertIn("needs.browser-shards.result", block)
        self.assertIn("needs.visual-shards.result", block)
        self.assertIn("name: browser tests", block)

    def test_both_sets_use_the_one_setup_and_no_pin_is_copied(self):
        for name in (FAST_JOB, VISUAL_JOB):
            self.assertIn("uses: ./.github/actions/browser-shard", job(name), name)
        self.assertIn("scripts/ci-chrome.sh pin CHROME_VERSION", ACTION)
        self.assertIn("scripts/ci-chrome.sh pin CHROME_SHA256_AMD64", ACTION)
        for text in (CI, ACTION):
            self.assertNotRegex(text, r"[0-9a-f]{64}")
            self.assertNotRegex(text, r"chrome-for-testing-public/\d")

    def test_every_action_is_pinned_by_full_sha_with_its_tag_and_node_and_python_exactly(self):
        for text in (CI, ACTION):
            for line in re.findall(r"^\s*(?:- )?uses: (\S.*)$", text, re.M):
                if line.startswith("./"):
                    continue
                self.assertRegex(line, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line)
        self.assertRegex(ACTION, r'python-version: "\d+\.\d+\.\d+"')
        self.assertRegex(ACTION, r'NODE_VERSION=\d+\.\d+\.\d+"')

    def test_failing_tests_are_listed_in_the_step_summary_with_their_shard(self):
        self.assertIn('scripts/ci_failure_summary.py --shard "$SHARD" < "$log" >> "$GITHUB_STEP_SUMMARY"', ACTION)


if __name__ == "__main__":
    unittest.main()
