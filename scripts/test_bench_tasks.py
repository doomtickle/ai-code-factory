"""The replay-benchmark task files (bench/tasks/*.md, docs/specs/factory-health/07-benchmark.md): every one has well-formed front matter
(name = file stem, base and reference 40 lowercase hex, tier and area from the fixed lists) and a body that works from `base` and ends in
the Coordinator's contract. The test cannot check the shas against git history (CI clones are shallow); `git cat-file -t <sha>` does.

  python3 -m unittest discover -s scripts -p 'test_bench_tasks.py' -v
"""

import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "bench" / "tasks"
TIERS = ("smoke", "full", "holdout")
AREAS = ("bridge", "broker", "dashboard", "docs", "cli")
SHA = re.compile(r"[0-9a-f]{40}")
CONTRACT = ("Done when:", "Tests required:", "Out of scope:", "Risk:")


def split(text):
    """(front matter dict, body) of a task file, or raises ValueError."""
    lines = text.split("\n")
    if lines[0] != "---":
        raise ValueError("no front matter: the file must start with ---")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("front matter is not closed by ---") from None
    meta = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or key.strip() in meta:
            raise ValueError("bad front matter line: %r" % line)
        meta[key.strip()] = value.strip()
    return meta, "\n".join(lines[end + 1:])


def problems(path):
    """What is wrong with the task file at `path` (an empty list when nothing is)."""
    try:
        meta, body = split(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return [str(e)]
    found = []
    missing = [k for k in ("name", "base", "reference", "tier", "area") if k not in meta]
    if missing:
        return ["front matter lacks: " + ", ".join(missing)]
    if meta["name"] != path.stem:
        found.append("name %r is not the file name %r" % (meta["name"], path.stem))
    for key in ("base", "reference"):
        if not SHA.fullmatch(meta[key]):
            found.append("%s is not 40 lowercase hex: %r" % (key, meta[key]))
    if meta["base"] == meta["reference"]:
        found.append("base and reference are the same commit")
    if meta["tier"] not in TIERS:
        found.append("tier %r is not one of %s" % (meta["tier"], "|".join(TIERS)))
    if meta["area"] not in AREAS:
        found.append("area %r is not one of %s" % (meta["area"], "|".join(AREAS)))
    for marker in CONTRACT:
        if marker not in body:
            found.append("the body lacks %r" % marker)
    if SHA.fullmatch(meta["base"]) and meta["base"] not in body:
        found.append("the body does not tell the developer to work from base")
    return found


GOOD = """---
name: sample
base: {base}
reference: {ref}
tier: smoke
area: docs
---
Work only from commit {base}.

Done when:
- it works
Tests required: none
Out of scope: nothing
Risk: trivial
"""


class BenchTasksTest(unittest.TestCase):
    def test_every_task_file_is_valid(self):
        files = sorted(TASKS.glob("*.md"))
        self.assertTrue(files, "bench/tasks has no task files")
        for path in files:
            with self.subTest(task=path.name):
                self.assertEqual(problems(path), [])

    def test_no_two_tasks_share_their_commits(self):
        seen = {}
        for path in sorted(TASKS.glob("*.md")):
            meta, _ = split(path.read_text(encoding="utf-8"))
            pair = (meta.get("base"), meta.get("reference"))
            self.assertNotIn(pair, seen, "%s repeats the commits of %s" % (path.name, seen.get(pair)))
            seen[pair] = path.name


class ValidatorRejectsTest(unittest.TestCase):
    base, ref = "a" * 40, "b" * 40

    def check(self, text, stem="sample"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / (stem + ".md")
            path.write_text(text, encoding="utf-8")
            return problems(path)

    def good(self):
        return GOOD.format(base=self.base, ref=self.ref)

    def test_a_well_formed_file_passes(self):
        self.assertEqual(self.check(self.good()), [])

    def test_a_bad_sha_is_rejected(self):
        for bad in ("abc123", "A" * 40, "g" * 40, "a" * 39, "a" * 41):
            with self.subTest(sha=bad):
                text = self.good().replace("reference: " + self.ref, "reference: " + bad)
                self.assertTrue(any("reference is not 40 lowercase hex" in p for p in self.check(text)))
        text = self.good().replace("base: " + self.base, "base: abc123", 1)
        self.assertTrue(any("base is not 40 lowercase hex" in p for p in self.check(text)))

    def test_a_name_that_is_not_the_file_name_is_rejected(self):
        self.assertTrue(any("not the file name" in p for p in self.check(self.good(), stem="other")))

    def test_an_unknown_tier_or_area_is_rejected(self):
        self.assertTrue(any("tier" in p for p in self.check(self.good().replace("tier: smoke", "tier: nightly"))))
        self.assertTrue(any("area" in p for p in self.check(self.good().replace("area: docs", "area: voice"))))

    def test_a_body_without_the_contract_is_rejected(self):
        for marker in CONTRACT:
            with self.subTest(marker=marker):
                text = self.good().replace(marker, "Nope:")
                self.assertTrue(any(repr(marker) in p for p in self.check(text)))

    def test_a_body_that_never_names_base_is_rejected(self):
        text = self.good().replace("Work only from commit " + self.base, "Work from the start")
        self.assertTrue(any("work from base" in p for p in self.check(text)))

    def test_broken_front_matter_is_rejected(self):
        self.assertTrue(self.check("no front matter\nDone when:\n"))
        self.assertTrue(self.check("---\nname: sample\nDone when:\n"))
        self.assertTrue(self.check(self.good().replace("area: docs\n", "")))
        self.assertTrue(self.check(self.good().replace("area: docs", "area docs")))
        self.assertTrue(self.check(self.good().replace("tier: smoke", "name: sample\ntier: smoke")))


if __name__ == "__main__":
    unittest.main()
