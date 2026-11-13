"""The browser-test shards of .github/workflows/ci.yml together cover every field section exactly once, and every
scripts/test_*_browser.py file. A section added to test_field_browser.js and not given to a shard fails here.

  python3 -m unittest discover -s scripts -p 'test_ci_shards.py' -v
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
CI = (SCRIPTS.parent / ".github" / "workflows" / "ci.yml").read_text()
FIELD = "test_field_browser.py"


def job(name):
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", CI, re.S | re.M)
    assert match, f"no job {name} in ci.yml"
    return match.group(1)


def shards():
    """[{shard, test, sections}] from the browser-shards job's matrix include list (sections defaults to "")."""
    block = job("browser-shards")
    matrix = block[block.index("include:"):block.index("    env:")]
    out = []
    for item in re.split(r"^ +- (?=shard:)", matrix, flags=re.M)[1:]:
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


def coverage_problems(shard_list, everything):
    """What is wrong with how the field shards cover the sections: [] when each is in exactly one."""
    assigned = [n for s in shard_list if s["test"] == FIELD for n in s["sections"].split(",") if n]
    problems = [f"{n} is in no shard" for n in everything if n not in assigned]
    problems += [f"{n} is in more than one shard" for n in sorted(set(assigned)) if assigned.count(n) > 1]
    problems += [f"{n} is not a section" for n in sorted(set(assigned)) if n not in everything]
    return problems


class ShardCoverageTests(unittest.TestCase):
    def test_every_field_section_is_in_exactly_one_shard(self):
        self.assertEqual(coverage_problems(shards(), field_sections()), [])

    def test_a_section_left_out_or_repeated_or_unknown_is_reported(self):
        two = [{"test": FIELD, "sections": "a,b"}, {"test": FIELD, "sections": "c"}]
        self.assertEqual(coverage_problems(two, ["a", "b", "c"]), [])
        self.assertEqual(coverage_problems(two, ["a", "b", "c", "d"]), ["d is in no shard"])
        self.assertEqual(coverage_problems(two + [{"test": FIELD, "sections": "b"}], ["a", "b", "c"]), ["b is in more than one shard"])
        self.assertEqual(coverage_problems(two, ["a", "b"]), ["c is not a section"])

    def test_every_field_shard_names_its_sections_and_the_others_name_none(self):
        for s in shards():
            self.assertEqual(bool(s["sections"]), s["test"] == FIELD, s)

    def test_every_browser_test_file_has_a_shard(self):
        files = sorted(p.name for p in SCRIPTS.glob("test_*_browser.py"))
        self.assertEqual(sorted({s["test"] for s in shards()}), files)

    def test_shard_names_are_unique_and_each_shard_gets_twenty_minutes(self):
        names = [s["shard"] for s in shards()]
        self.assertEqual(len(names), len(set(names)), names)
        self.assertRegex(job("browser-shards"), r"(?m)^    timeout-minutes: 20$")
        self.assertRegex(job("browser-shards"), r"(?m)^      fail-fast: false$")


if __name__ == "__main__":
    unittest.main()
