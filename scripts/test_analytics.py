"""Tests for the factory analytics report.

Run:
  python3 -m unittest discover -s scripts -v
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analytics as a  # noqa: E402


def ts(dt: datetime) -> str:
    return dt.strftime(a.TS_FORMAT)


def job(role, job_id, status="done", cost=None, elapsed=60, context=None, model=None,
        task="add a feature", when=None):
    record = {"ts": ts(when or datetime.now(timezone.utc)), "event": "job-end",
              "job_id": job_id, "role": role, "status": status,
              "elapsed_seconds": elapsed, "task": task}
    if cost is not None:
        record["cost_usd"] = cost
    if context is not None:
        record["context_tokens"] = context
    if model is not None:
        record["model"] = model
    return record


def broker_call(role, tool, decision="allow", ms=50):
    return {"ts": ts(datetime.now(timezone.utc)), "role": role, "tool": tool, "decision": decision, "ms": ms}


class _TTYStringIO(io.StringIO):
    """A stdout stand-in that reports itself as a terminal, for exercising
    the --html "open in a browser" path under redirect_stdout."""

    def isatty(self) -> bool:
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()  # macOS: /var -> /private/var
        (self.root / "dispatch" / "sessions").mkdir(parents=True)
        (self.root / "broker").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_jsonl(self, rel_path: str, records) -> Path:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for r in records:
                if isinstance(r, str):
                    f.write(r + "\n")
                else:
                    f.write(json.dumps(r) + "\n")
        return path

    def write_session(self, role: str, session_id="sess-1", total_cost_usd=1.0, context_tokens=1000):
        (self.root / "dispatch" / "sessions" / f"{role}.json").write_text(json.dumps(
            {"session_id": session_id, "total_cost_usd": total_cost_usd, "context_tokens": context_tokens}))


class HelperTests(unittest.TestCase):
    def test_num_rejects_non_finite_and_non_numeric(self):
        self.assertEqual(a._num(5), 5)
        self.assertEqual(a._num(5.5), 5.5)
        self.assertIsNone(a._num(float("nan")))
        self.assertIsNone(a._num(float("inf")))
        self.assertIsNone(a._num(float("-inf")))
        self.assertIsNone(a._num(True))
        self.assertIsNone(a._num("5"))
        self.assertIsNone(a._num(None))

    def test_num_rejects_oversized_magnitudes(self):
        self.assertEqual(a._num(5e11), 5e11)
        self.assertIsNone(a._num(1e13))
        self.assertIsNone(a._num(-1e13))
        self.assertIsNone(a._num(10**400))  # a huge int must not crash abs()/comparison

    def test_finite_sum_detects_overflow(self):
        self.assertEqual(a._finite_sum([1.0, 2.0, 3.0]), 6.0)
        self.assertEqual(a._finite_sum([]), 0)
        self.assertIsNone(a._finite_sum([1e308, 1e308]))


class LoadingTests(Base):
    def test_read_jsonl_missing_file_is_empty(self):
        records, malformed = a.read_jsonl(self.root / "nope.jsonl")
        self.assertEqual((records, malformed), ([], 0))

    def test_read_jsonl_skips_malformed_and_non_object_lines(self):
        path = self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j1", cost=0.5), "not json {{{", "42", "", "  "])
        records, malformed = a.read_jsonl(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(malformed, 2)  # bad json line + a bare number (not an object)

    def test_read_jsonl_replaces_invalid_utf8_instead_of_crashing(self):
        path = self.root / "dispatch" / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b'{"event": "job-end", "job_id": "j1", "role": "developer", '
            b'"task": "bad \xff byte", "cost_usd": 1.0}\n')
        records, malformed = a.read_jsonl(path)
        self.assertEqual(malformed, 0)
        self.assertEqual(len(records), 1)
        self.assertIn("�", records[0]["task"])

    def test_read_jsonl_rejects_nan_and_infinity_values(self):
        path = self.root / "dispatch" / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"event": "job-end", "job_id": "j1", "role": "developer", '
            '"cost_usd": NaN, "elapsed_seconds": Infinity}\n', encoding="utf-8")
        jobs, malformed = a.load_jobs(path)
        self.assertEqual(malformed, 0)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(a._num(jobs[0]["cost_usd"]))
        self.assertIsNone(a._num(jobs[0]["elapsed_seconds"]))

    def test_read_jsonl_coerces_integer_job_id(self):
        path = self.write_jsonl("dispatch/audit.jsonl", [
            {"event": "job-end", "job_id": 12345, "role": "developer",
             "status": "done", "elapsed_seconds": 10, "cost_usd": 1.0}])
        jobs, _ = a.load_jobs(path)
        self.assertEqual(jobs[0]["job_id"], "12345")

    def test_read_jsonl_drops_list_valued_role(self):
        path = self.write_jsonl("dispatch/audit.jsonl", [
            {"event": "job-end", "job_id": "j1", "role": ["developer", "extra"],
             "status": "done", "elapsed_seconds": 10, "cost_usd": 1.0}])
        jobs, _ = a.load_jobs(path)
        self.assertNotIn("role", jobs[0])

    def test_load_jobs_filters_to_job_end_events(self):
        path = self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j1", cost=0.5),
            {"tool": "delegate", "decision": "allow", "ms": 5},
        ])
        jobs, malformed = a.load_jobs(path)
        self.assertEqual(malformed, 0)
        self.assertEqual([j["job_id"] for j in jobs], ["j1"])

    def test_load_broker_calls_requires_role_and_tool(self):
        path = self.write_jsonl("broker/audit.jsonl", [
            broker_call("developer", "npm_ci"),
            {"event": "start", "version": "1.0.0"},
            {"event": "auth-failure", "client": "1.2.3.4", "reason": "bad token"},
        ])
        calls, _ = a.load_broker_calls(path)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool"], "npm_ci")

    def test_load_broker_calls_does_not_double_count_intent_records(self):
        # protocol.py writes an {"event": "intent", "role":..., "tool":...}
        # record before git_push/github_create_pr run, then the normal
        # allow/deny/error record once the call finishes. Both have "role"
        # and "tool", so the intent record must not also be counted as a call.
        path = self.write_jsonl("broker/audit.jsonl", [
            {"ts": ts(datetime.now(timezone.utc)), "role": "developer", "tool": "git_push", "event": "intent"},
            broker_call("developer", "git_push"),
            broker_call("developer", "npm_ci"),
        ])
        calls, _ = a.load_broker_calls(path)
        self.assertEqual(sorted(c["tool"] for c in calls), ["git_push", "npm_ci"])

    def test_load_sessions_skips_malformed_and_non_json_files(self):
        (self.root / "dispatch" / "sessions" / "developer.json").write_text(
            json.dumps({"session_id": "s", "total_cost_usd": 2.0, "context_tokens": 500}))
        (self.root / "dispatch" / "sessions" / "broken.json").write_text("{not json")
        sessions, malformed = a.load_sessions(self.root / "dispatch" / "sessions")
        self.assertEqual(set(sessions), {"developer"})
        self.assertEqual(sessions["developer"]["context_tokens"], 500)
        self.assertEqual(malformed, 1)

    def test_load_sessions_missing_dir_is_empty(self):
        self.assertEqual(a.load_sessions(self.root / "nope"), ({}, 0))

    def test_load_sessions_warns_on_stderr_for_broken_file(self):
        (self.root / "dispatch" / "sessions" / "broken.json").write_text("{not json")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            a.load_sessions(self.root / "dispatch" / "sessions")
        self.assertIn("broken.json", stderr.getvalue())


class AggregationTests(Base):
    def test_summarize_totals(self):
        jobs = [job("developer", "j1", cost=0.5), job("developer", "j2", cost=1.5),
                job("reviewer", "j3", cost=None)]  # unknown cost
        totals = a.summarize_totals(jobs, {"developer": {"context_tokens": 1000},
                                            "reviewer": {"context_tokens": 200}})
        self.assertEqual(totals["jobs"], 3)
        self.assertEqual(totals["jobs_with_known_cost"], 2)
        self.assertEqual(totals["total_cost_usd"], 2.0)
        self.assertEqual(totals["avg_cost_per_job_usd"], 1.0)
        self.assertEqual(totals["total_context_tokens_current"], 1200)

    def test_summarize_totals_with_no_data(self):
        totals = a.summarize_totals([], {})
        self.assertEqual(totals, {"jobs": 0, "jobs_with_known_cost": 0, "total_cost_usd": None,
                                   "avg_cost_per_job_usd": None, "total_context_tokens_current": None})

    def test_context_trend_classification(self):
        self.assertEqual(a.context_trend([100]), (None, None))
        self.assertEqual(a.context_trend([100, 200]), ("growing", 100.0))
        # 100 is a >25% drop below 200 and is the newest reading: nothing to
        # compare it to yet, so this reports a reset, not a (misleading) shrink.
        self.assertEqual(a.context_trend([200, 100]), ("reset", None))
        self.assertEqual(a.context_trend([100, 105]), ("stable", 5.0))
        self.assertEqual(a.context_trend([0, 50]), ("growing", None))
        self.assertEqual(a.context_trend([0, 0]), ("stable", None))

    def test_context_trend_reset_is_newest_reading(self):
        # The drop to 3000 is the very last reading: comparing it against the
        # first-ever reading (1000) would wrongly say "growing +200%".
        self.assertEqual(a.context_trend([1000, 5000, 9000, 9500, 3000]), ("reset", None))

    def test_context_trend_ignores_small_fluctuation_as_reset(self):
        # 19900 is barely (0.5%) below 20000 -- nowhere near the >25% drop
        # that would mark a real session reset/compaction.
        self.assertEqual(a.context_trend([10000, 20000, 19900, 20100]), ("growing", 101.0))

    def test_context_trend_limited_to_since_last_session_reset(self):
        # A near-limit session (90000) resets to a small context (500) then
        # grows a little (600): comparing against the very first job ever
        # (90000) would wrongly call this "shrinking"; it should only look
        # at jobs since the reset.
        trend, change_pct = a.context_trend([90000, 500, 600])
        self.assertEqual(trend, "growing")
        self.assertEqual(change_pct, 20.0)

    def test_per_role_stats_success_rate_cost_and_broker_calls(self):
        now = datetime.now(timezone.utc)
        jobs = [
            job("developer", "j1", status="done", cost=0.5, context=1000, when=now - timedelta(hours=2)),
            job("developer", "j2", status="failed", cost=0.25, context=1200, when=now - timedelta(hours=1)),
            job("developer", "j3", status="done", cost=0.75, context=1600, when=now),
        ]
        calls = [broker_call("developer", "npm_ci", "allow", 40),
                 broker_call("developer", "git_push", "deny", 10),
                 broker_call("developer", "web_fetch", "error", 5)]
        stats = a.per_role_stats(jobs, calls, {"developer": {"context_tokens": 1600}})
        dev = stats["developer"]
        self.assertEqual(dev["jobs"], 3)
        self.assertEqual(dev["done"], 2)
        self.assertEqual(dev["failed"], 1)
        self.assertAlmostEqual(dev["success_rate"], round(2 / 3, 4))
        self.assertEqual(dev["total_cost_usd"], 1.5)
        self.assertEqual(dev["context_tokens_current"], 1600)
        self.assertEqual(dev["context_trend"], "growing")
        self.assertEqual(dev["broker_calls"], 3)
        self.assertEqual(dev["broker_denied"], 1)
        self.assertEqual(dev["broker_errors"], 1)
        self.assertAlmostEqual(dev["broker_avg_ms"], round(55 / 3, 1))

    def test_per_role_stats_falls_back_to_last_job_context_without_session_file(self):
        jobs = [job("researcher", "j1", context=300), job("researcher", "j2", context=900)]
        stats = a.per_role_stats(jobs, [], {})
        self.assertEqual(stats["researcher"]["context_tokens_current"], 900)

    def test_per_role_stats_includes_idle_role_from_session_only(self):
        stats = a.per_role_stats([], [], {"reviewer": {"context_tokens": 50}})
        self.assertEqual(stats["reviewer"]["jobs"], 0)
        self.assertIsNone(stats["reviewer"]["success_rate"])

    def test_task_type_buckets_by_first_word(self):
        self.assertEqual(a.task_type("Add a login page"), "add")
        self.assertEqual(a.task_type("  Fix the flaky test"), "fix")
        self.assertEqual(a.task_type(""), "other")
        self.assertEqual(a.task_type(None), "other")

    def test_cost_drivers_top_jobs_task_type_and_model(self):
        jobs = [
            job("developer", "cheap", cost=0.1, task="Fix a typo", model="claude-haiku-4-5"),
            job("developer", "expensive", cost=5.0, task="Add a big feature", model="claude-sonnet-5"),
            job("reviewer", "mid", cost=1.0, task="Add review notes", model="claude-sonnet-5"),
        ]
        drivers = a.cost_drivers(jobs, top_n=2)
        self.assertEqual([j["job_id"] for j in drivers["top_jobs"]], ["expensive", "mid"])
        types = {t["type"]: t["cost_usd"] for t in drivers["by_task_type"]}
        self.assertEqual(types["add"], 6.0)
        self.assertEqual(types["fix"], 0.1)
        models = {m["model"]: m["cost_usd"] for m in drivers["by_model"]}
        self.assertEqual(models["claude-sonnet-5"], 6.0)
        self.assertEqual(models["claude-haiku-4-5"], 0.1)

    def test_cost_drivers_with_no_costed_jobs(self):
        drivers = a.cost_drivers([job("developer", "j1", cost=None)])
        self.assertEqual(drivers, {"top_jobs": [], "by_task_type": [], "by_model": []})

    def test_detect_anomalies_flags_outlier_cost(self):
        jobs = [job("developer", f"j{i}", cost=0.5) for i in range(5)]
        jobs.append(job("developer", "spike", cost=20.0))
        anomalies = a.detect_anomalies(jobs)
        self.assertTrue(any(x["job_id"] == "spike" for x in anomalies))

    def test_detect_anomalies_needs_at_least_three_jobs(self):
        jobs = [job("developer", "j1", cost=0.1), job("developer", "j2", cost=100.0)]
        self.assertEqual(a.detect_anomalies(jobs), [])

    def test_detect_anomalies_flags_single_spike_among_only_three_jobs(self):
        # Leave-one-out: with only 3 jobs total, the $1000 job should still
        # be flagged against its 2 near-identical $0.10 peers -- the old
        # "mean + 2*SD including the spike itself" approach couldn't fire
        # below 6 jobs.
        jobs = [job("developer", "j1", cost=0.10), job("developer", "j2", cost=0.10),
                job("developer", "spike", cost=1000.0)]
        anomalies = a.detect_anomalies(jobs)
        self.assertTrue(any(x["job_id"] == "spike" for x in anomalies))
        self.assertFalse(any(x["job_id"] in ("j1", "j2") for x in anomalies))

    def test_detect_anomalies_does_not_flag_uniform_jobs(self):
        jobs = [job("developer", f"j{i}", cost=0.5) for i in range(4)]
        self.assertEqual(a.detect_anomalies(jobs), [])

    def test_detect_anomalies_does_not_flag_ordinary_duration_variance(self):
        # mean + 2*SD (including the point itself) used to flag 130 here;
        # median + 3*MAD with a 2x-median floor should not.
        jobs = [job("developer", "j1", elapsed=100), job("developer", "j2", elapsed=110),
                job("developer", "j3", elapsed=130)]
        self.assertEqual(a.detect_anomalies(jobs), [])

    def test_detect_anomalies_no_false_positives_on_log_normal_noise(self):
        rng = random.Random(12345)
        jobs = [job("developer", f"j{i}", elapsed=rng.lognormvariate(math.log(100), 0.3))
                for i in range(20)]
        self.assertEqual(a.detect_anomalies(jobs), [])

    def test_detect_anomalies_flags_10x_spike_in_noisy_data(self):
        rng = random.Random(12345)
        jobs = [job("developer", f"j{i}", elapsed=rng.lognormvariate(math.log(100), 0.3))
                for i in range(20)]
        jobs.append(job("developer", "spike", elapsed=1000.0))
        anomalies = a.detect_anomalies(jobs)
        self.assertEqual([x["job_id"] for x in anomalies], ["spike"])

    def test_detect_anomalies_duration_reason_has_unit_suffix_not_prefix(self):
        jobs = [job("developer", "j1", elapsed=100), job("developer", "j2", elapsed=100),
                job("developer", "spike", elapsed=1000)]
        anomalies = a.detect_anomalies(jobs)
        reason = next(x["reason"] for x in anomalies if x["job_id"] == "spike")
        self.assertIn("1000.0s", reason)
        self.assertNotIn("s1000.00", reason)
        self.assertNotIn("s100.00", reason)

    def test_trends_cost_by_day_and_windows(self):
        now = datetime.now(timezone.utc)
        jobs = [
            job("developer", "recent", cost=1.0, when=now - timedelta(hours=1)),
            job("developer", "last_week", cost=2.0, when=now - timedelta(days=10)),
            job("developer", "last_month", cost=4.0, when=now - timedelta(days=40)),
        ]
        result = a.trends(jobs, now=now)
        self.assertEqual(result["last_7_days_cost_usd"], 1.0)
        self.assertEqual(result["last_30_days_cost_usd"], 3.0)
        total_bucketed = sum(d["cost_usd"] for d in result["cost_by_day"])
        self.assertEqual(total_bucketed, 7.0)

    def test_trends_ignores_jobs_with_unparseable_timestamps(self):
        jobs = [{"event": "job-end", "job_id": "bad-ts", "role": "developer",
                 "ts": "not-a-date", "cost_usd": 5.0}]
        result = a.trends(jobs)
        self.assertEqual(result["cost_by_day"], [])
        self.assertEqual(result["last_7_days_cost_usd"], 0.0)


class BuildReportTests(Base):
    def test_build_report_end_to_end(self):
        self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j1", cost=0.5, model="claude-sonnet-5", context=1000),
            job("reviewer", "j2", cost=0.2, status="failed"),
            "corrupt line",
        ])
        self.write_jsonl("broker/audit.jsonl", [broker_call("developer", "npm_ci")])
        self.write_session("developer", context_tokens=1000, total_cost_usd=0.5)

        report = a.build_report(self.root, top_n=5)
        self.assertEqual(report["totals"]["jobs"], 2)
        self.assertIn("developer", report["roles"])
        self.assertIn("reviewer", report["roles"])
        self.assertTrue(any("malformed" in w for w in report["warnings"]))
        self.assertEqual(len(report["cost_drivers"]["top_jobs"]), 2)

    def test_build_report_warns_on_missing_logs(self):
        report = a.build_report(self.root)
        self.assertEqual(report["totals"]["jobs"], 0)
        self.assertTrue(any("no dispatch audit log yet" in w for w in report["warnings"]))
        self.assertTrue(any("no broker audit log yet" in w for w in report["warnings"]))

    def test_build_report_survives_malformed_identifiers_and_non_finite_numbers(self):
        self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j1", cost=0.5),
            {"event": "job-end", "job_id": "j2", "role": ["developer", "extra"],
             "status": "done", "elapsed_seconds": 5, "cost_usd": 1.0},
            {"event": "job-end", "job_id": 999, "role": "developer",
             "status": "done", "elapsed_seconds": 5, "cost_usd": 0.2},
            {"event": "job-end", "job_id": "j4", "role": "developer",
             "status": "done", "cost_usd": float("nan"), "elapsed_seconds": float("inf")},
        ])
        (self.root / "dispatch" / "sessions" / "developer.json").write_text(
            json.dumps({"session_id": "s", "total_cost_usd": 1.0, "context_tokens": float("nan")}))

        report = a.build_report(self.root)  # must not raise
        self.assertEqual(report["totals"]["jobs"], 4)

        # The originally reported crash sites: non-string job_id/role reaching
        # table-cell formatting, and a list-valued role reaching a set/dict.
        table = a.render_table(report)
        self.assertIn("999", table)
        html_out = a.render_html(report)
        self.assertIn("<table>", html_out)

        out = json.dumps(report)
        self.assertNotIn("NaN", out)
        self.assertNotIn("Infinity", out)

    def test_build_report_handles_huge_integer_cost_without_crashing(self):
        self.write_jsonl("dispatch/audit.jsonl", [
            {"event": "job-end", "job_id": "j1", "role": "developer", "status": "done",
             "elapsed_seconds": 10, "cost_usd": 10**400},
            job("developer", "j2", cost=5.0),
        ])
        report = a.build_report(self.root)  # must not raise (10**400 used to crash _fmt_cost)
        self.assertEqual(report["totals"]["jobs_with_known_cost"], 1)
        self.assertEqual(report["totals"]["total_cost_usd"], 5.0)
        table = a.render_table(report)  # _fmt_cost(10**400) is where this used to blow up
        self.assertIn("$5.00", table)

    def test_build_report_guards_against_sum_overflow_to_infinity(self):
        self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j1", cost=1e308),
            job("developer", "j2", cost=1e308),
        ])
        report = a.build_report(self.root)  # must not raise
        out = json.dumps(report)
        self.assertNotIn("Infinity", out)

        def _reject_constants(token):
            raise ValueError(f"unexpected non-finite constant in JSON output: {token}")

        json.loads(out, parse_constant=_reject_constants)


class RenderTests(Base):
    def setUp(self):
        super().setUp()
        self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j1", cost=1.23, model="claude-sonnet-5"),
            job("reviewer", "j2", cost=0.5, status="failed"),
        ])
        self.write_jsonl("broker/audit.jsonl", [broker_call("developer", "npm_ci")])
        self.report = a.build_report(self.root)

    def test_render_table_contains_key_sections(self):
        out = a.render_table(self.report)
        for heading in ("Summary", "Per role", "Most expensive jobs", "Cost by task type",
                        "Cost by model", "Trends"):
            self.assertIn(heading, out)
        self.assertIn("developer", out)
        self.assertIn("$1.23", out)

    def test_render_table_handles_empty_report(self):
        empty = a.build_report(self.root / "empty")
        out = a.render_table(empty)
        self.assertIn("No jobs recorded yet.", out)

    def test_render_html_contains_tables_and_escapes(self):
        self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j1", cost=1.0, task="<script>alert(1)</script>")])
        report = a.build_report(self.root)
        out = a.render_html(report)
        self.assertIn("<table>", out)
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_render_table_strips_control_characters(self):
        self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j-esc", cost=9.0, task="fix \x1b[31mred\x07 text",
                model="claude\x1bsonnet")])
        report = a.build_report(self.root)
        out = a.render_table(report)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("fix [31mred text", out)

    def test_render_handles_reset_context_trend_in_table_html_and_json(self):
        now = datetime.now(timezone.utc)
        self.write_jsonl("dispatch/audit.jsonl", [
            job("developer", "j1", context=9000, when=now - timedelta(hours=2)),
            job("developer", "j2", context=9500, when=now - timedelta(hours=1)),
            job("developer", "j3", context=3000, when=now),  # >25% drop, newest reading
        ])
        report = a.build_report(self.root)
        self.assertEqual(report["roles"]["developer"]["context_trend"], "reset")
        self.assertIsNone(report["roles"]["developer"]["context_change_pct"])

        table = a.render_table(report)
        self.assertIn("reset", table)

        html_out = a.render_html(report)
        self.assertIn("reset", html_out)

        parsed = json.loads(json.dumps(report))
        self.assertEqual(parsed["roles"]["developer"]["context_trend"], "reset")
        self.assertIsNone(parsed["roles"]["developer"]["context_change_pct"])

    def test_render_html_includes_anomalies_and_broker_calls(self):
        jobs = [job("developer", f"j{i}", cost=0.5) for i in range(4)]
        jobs.append(job("developer", "spike", cost=50.0))
        self.write_jsonl("dispatch/audit.jsonl", jobs)
        self.write_jsonl("broker/audit.jsonl", [
            broker_call("developer", "npm_ci", "allow", 40),
            broker_call("developer", "git_push", "deny", 10)])
        report = a.build_report(self.root)
        out = a.render_html(report)
        self.assertIn("Anomalies", out)
        self.assertIn("spike", out)
        self.assertIn("<h2>Anomalies</h2>", out)
        # Broker call counts (calls, denied, errors) show up per role.
        self.assertIn(">2<", out)  # 2 broker calls for developer
        self.assertIn(">1<", out)  # 1 denied


class MainTests(Base):
    def setUp(self):
        super().setUp()
        self.write_jsonl("dispatch/audit.jsonl", [job("developer", "j1", cost=2.0)])

    def run_main(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = a.main(["--state", str(self.root), *args])
        return code, buf.getvalue()

    def test_main_default_is_table(self):
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("AI Code Factory", out)

    def test_main_json_round_trips(self):
        code, out = self.run_main("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["totals"]["jobs"], 1)

    def test_main_html_non_tty_prints_to_stdout_and_writes_no_file(self):
        # run_main() redirects stdout to a plain io.StringIO, whose isatty()
        # is False -- the same as a pipe or `> file` redirect.
        code, out = self.run_main("--html")
        self.assertEqual(code, 0)
        self.assertIn("<!DOCTYPE html>", out)
        self.assertFalse((self.root / "dispatch" / "analytics.html").exists())

    def test_main_html_tty_writes_file_and_opens_browser(self):
        buf = _TTYStringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch("webbrowser.open") as mock_open:
                code = a.main(["--state", str(self.root), "--html"])
        self.assertEqual(code, 0)

        out_path = self.root / "dispatch" / "analytics.html"
        self.assertTrue(out_path.exists())
        content = out_path.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", content)

        mock_open.assert_called_once_with(out_path.as_uri())
        self.assertIn(str(out_path), buf.getvalue())
        self.assertNotIn("<!DOCTYPE html>", buf.getvalue())  # raw HTML must not also hit stdout

    def test_main_html_tty_still_prints_path_when_browser_open_returns_false(self):
        buf = _TTYStringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch("webbrowser.open", return_value=False):
                code = a.main(["--state", str(self.root), "--html"])
        self.assertEqual(code, 0)
        out_path = self.root / "dispatch" / "analytics.html"
        self.assertTrue(out_path.exists())
        self.assertIn(str(out_path), buf.getvalue())

    def test_main_html_tty_still_prints_path_when_browser_open_raises(self):
        buf = _TTYStringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch("webbrowser.open", side_effect=RuntimeError("no display")):
                code = a.main(["--state", str(self.root), "--html"])
        self.assertEqual(code, 0)
        out_path = self.root / "dispatch" / "analytics.html"
        self.assertTrue(out_path.exists())
        self.assertIn(str(out_path), buf.getvalue())

    def test_main_json_and_html_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.run_main("--json", "--html")

    def test_main_rejects_top_below_one(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.run_main("--top", "0")
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.run_main("--top", "-5")

    def test_main_json_output_is_strict_json_with_malformed_data(self):
        self.write_jsonl("dispatch/audit.jsonl", [
            {"event": "job-end", "job_id": "j-nan", "role": "developer",
             "status": "done", "cost_usd": float("nan"), "elapsed_seconds": float("inf")},
        ])
        code, out = self.run_main("--json")
        self.assertEqual(code, 0)
        self.assertNotIn("NaN", out)
        self.assertNotIn("Infinity", out)

        def _reject_constants(token):
            raise ValueError(f"unexpected non-finite constant in JSON output: {token}")

        json.loads(out, parse_constant=_reject_constants)  # raises if NaN/Infinity slipped through



class LaneSessionsTest(unittest.TestCase):
    def test_lane_sessions_fold_into_their_role(self):
        sessions = {"developer": {"context_tokens": 1000},
                    "developer-lane2": {"context_tokens": 500},
                    "reviewer": {"context_tokens": 200}}
        jobs = [{"role": "developer", "lane": "developer-lane2", "status": "done", "cost_usd": 0.5}]
        stats = a.per_role_stats(jobs, [], sessions)
        self.assertEqual(sorted(stats), ["developer", "reviewer"])
        self.assertEqual(stats["developer"]["context_tokens_current"], 1500)
        self.assertEqual(stats["developer"]["jobs"], 1)


if __name__ == "__main__":
    unittest.main()
