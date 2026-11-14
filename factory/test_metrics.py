import unittest

from factory import metrics


def job(ts, **kw):
    return {"ts": ts, **kw}


class GroupChangesTest(unittest.TestCase):
    def test_groups_by_valid_change(self):
        a1 = job("2026-01-01T00:00:00Z", change="factory/a")
        a2 = job("2026-01-01T01:00:00Z", change="factory/a")
        b = job("2026-01-01T02:00:00Z", change="factory/b-2")
        grouped, unknown = metrics.group_changes([a1, a2, b])
        self.assertEqual(dict(grouped), {"factory/a": [a1, a2], "factory/b-2": [b]})
        self.assertEqual(unknown, [])

    def test_missing_or_invalid_change_is_unknown(self):
        ok = job("2026-01-01T00:00:00Z", change="factory/a")
        missing = {"ts": "2026-01-01T00:00:00Z"}
        bad = [{"change": c} for c in ("main", "factory/A", "factory/a b", "xfactory/a", ["factory/a"], 3, None)]
        grouped, unknown = metrics.group_changes([ok, missing, *bad])
        self.assertEqual(list(grouped), ["factory/a"])
        self.assertEqual(unknown, [missing, *bad])


class ChangeRowTest(unittest.TestCase):
    def test_review_rounds_and_first_pass(self):
        jobs = [job("2026-01-01T00:00:00Z", verdict="REVISE"), job("2026-01-01T01:00:00Z", verdict="REVISE"),
                job("2026-01-01T02:00:00Z", verdict="APPROVED"), job("2026-01-01T03:00:00Z")]
        row = metrics.change_row("factory/a", jobs)
        self.assertEqual(row["review_rounds"], 2)
        self.assertIs(row["first_pass_approval"], False)
        self.assertEqual(row["jobs"], 4)
        first = metrics.change_row("factory/a", [job("2026-01-01T00:00:00Z", verdict="APPROVED")])
        self.assertIs(first["first_pass_approval"], True)

    def test_no_verdict_means_unknown_first_pass(self):
        row = metrics.change_row("factory/a", [job("2026-01-01T00:00:00Z")])
        self.assertIsNone(row["first_pass_approval"])
        self.assertEqual(row["review_rounds"], 0)

    def test_wall_seconds_spans_first_start_to_last_end(self):
        jobs = [job("2026-01-01T01:00:00Z", elapsed_seconds=600),
                job("2026-01-01T00:10:00Z", elapsed_seconds=300)]
        row = metrics.change_row("factory/a", jobs)
        self.assertEqual(row["wall_seconds"], 3300)
        self.assertEqual(row["last_job_at"], "2026-01-01T01:00:00Z")

    def test_wall_seconds_none_without_valid_ts(self):
        row = metrics.change_row("factory/a", [job("garbage", elapsed_seconds=5)])
        self.assertIsNone(row["wall_seconds"])
        self.assertIsNone(row["last_job_at"])

    def test_latest_diffstat_wins(self):
        old = job("2026-01-01T00:00:00Z", size={"lines_added": 10, "lines_removed": 0, "files": 1})
        new = job("2026-01-01T02:00:00Z", size={"lines_added": 30, "lines_removed": 10, "files": 3,
                                                "test_lines_added": 15})
        row = metrics.change_row("factory/a", [new, old])
        self.assertEqual((row["diff_lines"], row["diff_files"], row["test_lines_share"]), (40, 3, 0.5))

    def test_latest_job_without_diffstat_falls_back_to_earlier(self):
        old = job("2026-01-01T00:00:00Z", size={"lines_added": 10, "lines_removed": 2, "files": 1})
        new = job("2026-01-01T02:00:00Z", verdict="APPROVED")
        row = metrics.change_row("factory/a", [old, new])
        self.assertEqual((row["diff_lines"], row["diff_files"]), (12, 1))


if __name__ == "__main__":
    unittest.main()
