"""Tests for scripts/dashboard.py (factory dashboard): the state snapshot built
from a fixture state folder, the HTTP server's access checks, the docs it
serves at /docs/ (MANUAL), and the page's static files (vendored font
checksums, no HTML injection sinks in the script, help links that resolve).

Run on the host with the system Python:
  python3 -m unittest discover -s scripts -p 'test_dashboard.py' -v
"""

import hashlib
import http.client
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dashboard as d
import dashboard_demo as demo
import dashboard_outbox as ob
from factory import fingerprints as fp
from factory.context import Paths

UI = Path(__file__).resolve().parent / "dashboard-ui"
REPO = Path(__file__).resolve().parent.parent
TOKEN = "test-token-0123456789abcdefghijklmnop"


def hms(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class Fixture:
    """A factory state folder: a developer mid-job with a push held for approval,
    a reviewer that finished with REVISE, and broker calls of every kind."""

    def __init__(self, root: Path):
        # Local noon today: every fixture record falls on the same local day, whenever the tests run.
        t = time.localtime()
        now = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))
        self.now = now
        self.state, self.roles = root / "state", root / "roles"
        for role, lanes in (("developer", 1), ("reviewer", 1)):
            (self.roles / role).mkdir(parents=True)
            (self.roles / role / "role.conf").write_text(f'LANES="{lanes}"\n')
        logs = self.state / "dispatch" / "logs"
        logs.mkdir(parents=True)
        (self.state / "dispatch" / "packets").mkdir()
        # The bridge is up (any live pid will do): with it down no lane can be running.
        (self.state / "dispatch" / "pid").write_text(f"{os.getpid()}\n")
        t0, t1 = hms(now - 120), hms(now - 60)
        (logs / "developer.log").write_text(
            f"{t0}  ▶ job 20261019-000001-aaaaaa from the Coordinator\n"
            f"{t0}     > Push factory/x and open a draft PR\n"
            f"{t0}     model: claude-haiku-4-5\n"
            f'{t1}  🔧 mcp__mcp-gateway__git_push: {{"repo": "o/r", "branch": "factory/x"}}\n')
        (logs / "reviewer.log").write_text(
            f"{t0}  ▶ job 20261019-000002-bbbbbb from the Coordinator\n"
            f"{t0}     > Review factory/x <script>alert(1)</script>\n"
            f"{t1}     VERDICT: REVISE\n"
            f"{t1}  📄 review packet: factory packet 20261019-000002-bbbbbb\n"
            f"{t1}  ■ job 20261019-000002-bbbbbb done after 60s\n")
        (self.state / "dispatch" / "packets" / "20261019-000002-bbbbbb.md").write_text(
            "# reviewer job 20261019-000002-bbbbbb: done\n\n- Lane: reviewer\n- Verdict: REVISE\n")
        (self.state / "dispatch" / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"ts": iso(now - 60), "event": "job-end", "job_id": "20261019-000002-bbbbbb", "role": "reviewer",
             "lane": "reviewer", "status": "done", "elapsed_seconds": 60, "model": "claude-opus-5-5",
             "cost_usd": 0.5, "context_tokens": 53000},
            {"ts": iso(now - 3 * 86400), "event": "job-end", "job_id": "old", "role": "developer",
             "lane": "developer", "status": "done", "elapsed_seconds": 9, "cost_usd": 9.0},
            {"ts": iso(now), "event": "start"},
        ]) + "\nnot json\n")
        broker = self.state / "broker"
        (broker / "approvals" / "pending").mkdir(parents=True)
        (broker / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"ts": iso(now - 50), "role": "developer", "tool": "git_push", "event": "intent", "args": {"repo": "o/r"}},
            {"ts": iso(now - 50), "event": "approval-requested", "id": "c0ffee", "tool": "git_push", "role": "",
             "repo": "o/r", "branch": "factory/x"},
            {"ts": iso(now - 40), "role": "developer", "tool": "web_fetch", "decision": "deny",
             "args": {"url": "https://api.github.com/x"}, "reason": "credentialed host"},
            {"ts": iso(now - 30), "role": "reviewer", "tool": "git_fetch", "decision": "allow", "args": "{'repo': 'o/r'}"},
            {"ts": iso(now - 3 * 86400), "role": "developer", "tool": "web_fetch", "decision": "allow"},
            {"ts": iso(now), "event": "start", "version": "1.0.0"},
        ]) + "\n")
        (broker / "approvals" / "pending" / "c0ffee.json").write_text(json.dumps({
            "id": "c0ffee", "tool": "git_push", "principal": "developer", "repo": "o/r", "branch": "factory/x",
            "base": "main", "commits": 2, "file_count": 3, "files": ["a", "b", "c"], "created": now - 50,
            "expires": now + 1750, "nonce": "n"}))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")

    def tearDown(self):
        self.tmp.cleanup()

    def test_lanes(self):
        snap = self.board.snapshot(self.fx.now)
        lanes = {lane["name"]: lane for lane in snap["lanes"]}
        self.assertEqual(lanes["developer"]["state"], "running")  # its push is on the gate, staged: the call returned
        self.assertEqual(lanes["developer"]["model"], "haiku-4-5")
        self.assertEqual(lanes["developer"]["tail"][-1]["kind"], "tool")
        self.assertEqual(lanes["developer"]["tail"][-1]["text"], "broker git_push(repo=o/r, branch=factory/x)")
        self.assertEqual(lanes["reviewer"]["state"], "revise")  # done, verdict from its packet
        self.assertEqual(lanes["reviewer"]["cost"], 0.5)
        self.assertEqual(lanes["reviewer"]["model"], "opus-5-5")
        # Agent text stays text: the snapshot carries it verbatim for the page to insert with textContent.
        self.assertIn("<script>alert(1)</script>", lanes["reviewer"]["tail"][1]["text"])

    def test_no_lane_runs_without_the_bridge(self):
        (self.fx.state / "dispatch" / "pid").unlink()
        snap = self.board.snapshot(self.fx.now)
        lanes = {lane["name"]: lane for lane in snap["lanes"]}
        self.assertFalse(snap["bridge"])
        self.assertEqual(lanes["developer"]["state"], "interrupted")  # its transcript never got an end line
        self.assertEqual(lanes["reviewer"]["state"], "revise")  # finished jobs keep their state
        self.assertEqual(snap["totals"]["running"], 0)
        self.assertEqual(self.board.running(), {})

    def test_a_job_the_bridge_closed_shows_interrupted(self):
        logs = self.fx.state / "dispatch" / "logs"
        with open(logs / "developer.log", "a") as f:
            f.write(f"{hms(self.fx.now - 30)}  ■ job 20261019-000001-aaaaaa interrupted after 90s\n")
        lane = next(x for x in self.board.snapshot(self.fx.now)["lanes"] if x["name"] == "developer")
        self.assertEqual((lane["state"], lane["elapsed"]), ("interrupted", 90))
        self.assertEqual(self.board.snapshot(self.fx.now)["totals"]["running"], 0)

    def test_desk_placard_fields(self):
        lanes = {lane["name"]: lane for lane in self.board.snapshot(self.fx.now)["lanes"]}
        self.assertEqual((lanes["developer"]["desk"], lanes["developer"]["reviews"]), ("x", ""))
        self.assertEqual((lanes["reviewer"]["desk"], lanes["reviewer"]["reviews"]), ("x", ""))  # names a branch, not a developer ref
        empty = Path(self.tmp.name) / "empty"
        for lane in d.Dashboard(empty, self.fx.roles, "proj").snapshot()["lanes"]:
            self.assertEqual((lane["desk"], lane["reviews"]), ("", ""))  # no job, no placard

    def test_branch_field(self):
        log = self.fx.state / "dispatch" / "logs" / "developer.log"
        t = hms(self.fx.now - 30)
        def branch():
            return next(x for x in self.board.snapshot(self.fx.now)["lanes"] if x["name"] == "developer")["branch"]
        lanes = {lane["name"]: lane for lane in self.board.snapshot(self.fx.now)["lanes"]}
        self.assertEqual((lanes["developer"]["branch"], lanes["reviewer"]["branch"]), ("factory/x", ""))  # the task names it; reviewers get "reviews"
        write_lane(self.fx.state / "dispatch" / "logs", "developer", "20261019-000001-aaaaaa", self.fx.now,
                   f"{t}  ⎇ branch factory-developer/factory/from-log at 1a2b3c4\n")
        self.assertEqual(branch(), "factory/from-log")  # no branch in the task: the log's
        with open(log, "a") as f:  # a later job starts clean: only its own branch counts
            f.write(f"{t}  ▶ job 20261019-000010-dddddd from the Coordinator\n{t}     > push factory/from-task\n")
        self.assertEqual(branch(), "factory/from-task")
        with open(log, "a") as f:
            f.write(f"{t}  ■ job 20261019-000010-dddddd done after 20s\n")
        lane = next(x for x in self.board.snapshot(self.fx.now + 3600)["lanes"] if x["name"] == "developer")
        self.assertEqual((lane["state"], lane["desk"], lane["branch"]), ("idle", "", "factory/from-task"))

    def test_reviewer_reads_the_whole_task(self):
        logs = self.fx.state / "dispatch" / "logs"
        t = hms(self.fx.now - 30)
        (logs / "reviewer.log").write_text(
            f"{t}  ▶ job 20261019-000009-cccccc from the Coordinator\n"
            f"{t}     > Please review.\n"
            f"{t}     >\n"
            f"{t}     > Branch: factory-developer-lane2/factory/rate-limit\n")
        lane = next(x for x in self.board.snapshot(self.fx.now)["lanes"] if x["name"] == "reviewer")
        self.assertEqual((lane["state"], lane["task"]), ("running", "Please review."))
        self.assertEqual((lane["desk"], lane["reviews"]), ("rate limit", "developer-lane2"))

    def test_cards_draft_and_fingerprint(self):
        import approvals
        pending = self.fx.state / "broker" / "approvals" / "pending"
        for cid, extra in (("pr1", {"draft": False}), ("pr2", {}), ("pr3", {"draft": True}),
                           ("mr1", {"tool": "gitlab_create_mr"}), ("mr2", {"tool": "gitlab_create_mr", "draft": False})):
            (pending / f"{cid}.json").write_text(json.dumps(dict({
                "id": cid, "tool": "github_create_pr", "principal": "developer", "repo": "o/r", "head": "factory/y",
                "base": "main", "title": "T", "expires": self.fx.now + 60, "nonce": cid + "n"}, **extra)))
        cards = {c["id"]: c for c in self.board.cards(self.fx.now)}
        self.assertEqual({k: v["draft"] for k, v in cards.items()},
                         {"c0ffee": False, "pr1": False, "pr2": True, "pr3": True, "mr1": True, "mr2": False})
        self.assertTrue(all("fp" not in c for c in cards.values()))
        with_fp = {c["id"]: c for c in self.board.cards(self.fx.now, fp=True)}
        raw = json.loads((pending / "pr1.json").read_text())
        self.assertEqual(with_fp["pr1"]["fp"], approvals.card_fingerprint(raw))
        self.assertNotIn("nonce", json.dumps(with_fp))

    def test_totals_spend_and_gate(self):
        snap = self.board.snapshot(self.fx.now)
        self.assertEqual(snap["totals"], {"cost": 0.5, "jobs": 1, "running": 1})  # the old job is not today's
        self.assertEqual(len(snap["spend"]["hours"]), 24)
        self.assertEqual(snap["spend"]["roles"], [{"role": "reviewer", "cost": 0.5}])
        self.assertEqual(len(snap["gate"]), 1)
        card = snap["gate"][0]
        self.assertEqual((card["id"], card["principal"], card["commits"], card["files"]), ("c0ffee", "developer", 2, ["a", "b", "c"]))
        self.assertGreater(card["seconds_left"], 1700)
        self.assertEqual((card["decided"], card["decided_by"], card["decided_at"]), (None, None, None))
        self.assertTrue(snap["bridge"])

    def test_events(self):
        events = self.board.snapshot(self.fx.now)["events"]
        # Oldest first; no "intent" record, nothing from three days ago.
        self.assertEqual([e["kind"] for e in events], ["JOB", "STAGED", "DENY", "ALLOW"])
        deny = next(e for e in events if e["kind"] == "DENY")
        self.assertTrue(deny["red"])
        self.assertIn("https://api.github.com/x", deny["text"])
        self.assertIn("credentialed host", deny["text"])
        self.assertEqual(events[0]["t"], "11:59:00")

    def test_lane_detail(self):
        detail = self.board.lane_detail("reviewer", self.fx.now)
        self.assertIn("- Verdict: REVISE", detail["packet"])
        self.assertIsNone(self.board.lane_detail("nope", self.fx.now))

    def test_missing_files(self):
        empty = Path(self.tmp.name) / "empty"
        board = d.Dashboard(empty, self.fx.roles, "proj")
        snap = board.snapshot()
        self.assertEqual([lane["state"] for lane in snap["lanes"]], ["idle", "idle"])
        self.assertEqual((snap["events"], snap["gate"], snap["totals"]["jobs"]), ([], [], 0))


def write_lane(logs: Path, name: str, job_id: str, now: float, extra: str = "") -> None:
    t = hms(now - 60)
    (logs / f"{name}.log").write_text(f"{t}  ▶ job {job_id} from the Coordinator\n{t}     > a task\n" + extra)


class HeldTests(unittest.TestCase):
    """A card names the role (the broker token's principal), not the lane."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        t = time.localtime()
        self.now = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))
        self.state, self.roles = root / "state", root / "roles"
        for role, lanes in (("developer", 3), ("reviewer", 1)):
            (self.roles / role).mkdir(parents=True)
            (self.roles / role / "role.conf").write_text(f'LANES="{lanes}"\n')
        self.logs = self.state / "dispatch" / "logs"
        self.logs.mkdir(parents=True)
        (self.state / "dispatch" / "pid").write_text(f"{os.getpid()}\n")
        self.pending = self.state / "broker" / "approvals" / "pending"
        self.pending.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, cid, principal, **extra):
        rec = {"id": cid, "tool": "git_push", "principal": principal, "expires": self.now + 600}
        rec.update(extra)
        (self.pending / f"{cid}.json").write_text(json.dumps(rec))

    def snap(self):
        snap = d.Dashboard(self.state, self.roles, "proj").snapshot(self.now)
        return {lane["name"]: lane for lane in snap["lanes"]}, snap["gate"]

    def test_two_running_lanes_one_card(self):
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        write_lane(self.logs, "developer-lane2", "20261019-000002-bbbbbb", self.now)
        self.card("c1", "developer")
        lanes, gate = self.snap()
        # A card names the role, not the lane, and holds no lane: both keep running.
        self.assertEqual((lanes["developer"]["state"], lanes["developer-lane2"]["state"]), ("running", "running"))
        self.assertNotIn("held_note", lanes["developer-lane2"])
        self.assertEqual(lanes["developer-lane3"]["state"], "idle")
        self.assertEqual((gate[0]["role"], gate[0]["lanes"]), ("developer", ["developer", "developer-lane2"]))

    def test_one_running_lane_of_several(self):
        # The card lists lane2, the only developer lane running, though it says "developer".
        write_lane(self.logs, "developer-lane2", "20261019-000002-bbbbbb", self.now)
        self.card("c1", "developer")
        lanes, gate = self.snap()
        self.assertEqual((lanes["developer"]["state"], lanes["developer-lane2"]["state"]), ("idle", "running"))
        self.assertEqual(gate[0]["lanes"], ["developer-lane2"])

    def test_decided_and_expired_cards_show_in_the_gate(self):
        import approvals
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        self.card("declined", "developer", nonce="n")
        self.card("expired", "developer", expires=self.now - 5)
        card = next(c for c in approvals.read_cards(self.state / "broker") if c["id"] == "declined")
        approvals.write_decision(self.state / "broker", card, "decline", "tester", "no")
        lanes, gate = self.snap()
        self.assertEqual(lanes["developer"]["state"], "running")
        self.assertEqual({c["id"]: (c["decided"], c["seconds_left"] < 0) for c in gate},
                         {"declined": ("decline", False), "expired": (None, True)})
        # A live, undecided card holds no lane, in the snapshot or the lane detail (Monitor).
        self.card("live", "developer")
        self.assertEqual(self.snap()[0]["developer"]["state"], "running")
        detail = d.Dashboard(self.state, self.roles, "proj").lane_detail("developer", self.now)
        self.assertEqual(detail["state"], "running")
        self.assertNotIn("held_note", detail)

    def queue(self, cid, nonce="n"):
        folder = self.state / "broker" / "approvals" / "queued"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{cid}.json").write_text(json.dumps({"id": cid, "nonce": nonce, "tool": "git_push"}))

    def test_queued_card_is_marked(self):
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        self.card("q1", "developer", nonce="n")
        self.queue("q1")
        lanes, gate = self.snap()
        self.assertEqual(lanes["developer"]["state"], "running")  # the agent's call already returned
        self.assertEqual([(c["id"], c["queued"], c["decided"]) for c in gate], [("q1", True, None)])
        # A live card beside it is not marked queued.
        self.card("live", "developer", nonce="m")
        lanes, gate = self.snap()
        self.assertEqual({c["id"]: c["queued"] for c in gate}, {"q1": True, "live": False})

    def test_queued_file_for_another_card_marks_nothing(self):
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        self.card("q1", "developer", nonce="new")
        self.queue("q1", nonce="old")  # an earlier card with the same id
        lanes, gate = self.snap()
        self.assertFalse(gate[0]["queued"])

    def test_card_for_role_with_no_running_lane(self):
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        self.card("c1", "reviewer")
        self.card("c2", "")
        lanes, gate = self.snap()
        self.assertEqual((lanes["developer"]["state"], lanes["reviewer"]["state"]), ("running", "idle"))
        self.assertEqual([(c["role"], c["lanes"]) for c in gate], [("reviewer", []), ("", [])])

    def test_malformed_records(self):
        # One bad record must not break /api/state: each bad field becomes unknown, the rest still shows.
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        self.card("c1", "developer", expires="soon", commits="two", file_count=None, files="a")
        self.card("c2", "reviewer", expires=None)
        (self.pending / "c3.json").write_text('{"id": "c3", "expires": NaN}')
        (self.state / "dispatch" / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"ts": iso(self.now - 30), "event": "job-end", "job_id": "a", "role": "reviewer", "status": "done",
             "cost_usd": "1.25", "elapsed_seconds": "a minute", "model": 5},
            {"ts": iso(self.now - 20), "event": "job-end", "job_id": "b", "role": None, "status": None,
             "cost_usd": None, "elapsed_seconds": None, "lane": None},
            {"ts": iso(self.now - 10), "event": "job-end", "job_id": "c", "role": "developer", "status": "done",
             "cost_usd": 0.75, "elapsed_seconds": 90, "context_tokens": "lots"},
            {"ts": 12345, "event": "job-end", "job_id": "d", "cost_usd": 1},
            {"ts": iso(self.now - 5), "event": "job-end", "job_id": "e", "cost_usd": True},
        ]) + "\n")
        board = d.Dashboard(self.state, self.roles, "proj")
        snap = board.snapshot(self.now)
        json.dumps(snap)  # serializable
        self.assertEqual([c["seconds_left"] for c in snap["gate"]], [None, None, None])
        self.assertEqual((snap["gate"][0]["commits"], snap["gate"][0]["files"]), (None, []))
        self.assertEqual(snap["totals"]["cost"], 0.75)
        self.assertEqual(snap["totals"]["jobs"], 4)  # "d" has no usable timestamp
        self.assertEqual(snap["spend"]["roles"][0], {"role": "developer", "cost": 0.75})
        texts = [e["text"] for e in snap["events"]]
        self.assertIn("reviewer done 0:00", texts)  # no "$" for a cost that isn't a number
        self.assertIn("developer done 1:30 $0.75", texts)
        self.assertEqual(snap["events"][-1]["t"], hms(self.now - 5))
        self.assertIsNotNone(board.lane_detail("developer", self.now))


class BrokerEventTests(unittest.TestCase):
    def test_kinds(self):
        self.assertIsNone(d.broker_event({"tool": "git_push", "event": "intent"}))
        self.assertEqual(d.broker_event({"tool": "git_push", "event": "approval-granted", "approver": "daron",
                                         "repo": "o/r", "branch": "b"})["kind"], "OK")
        ev = d.broker_event({"tool": "npm_fetch", "role": "developer", "decision": "allow", "args": {"lockfile": "x.json"}})
        self.assertEqual((ev["kind"], ev["red"], ev["text"]), ("ALLOW", False, "npm_fetch developer x.json"))

    def test_audit_keeps_only_printable_records(self):
        # "intent" records would crowd real calls out of the 500 kept on a busy day.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            recs = []
            for i in range(600):
                recs.append({"ts": "2026-10-19T06:00:00Z", "tool": "web_fetch", "event": "intent"})
                recs.append({"ts": "2026-10-19T06:00:00Z", "tool": "web_fetch", "decision": "allow", "n": i})
            path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
            audit = d.BrokerAudit(path)
            audit.feed()
            self.assertEqual(len(audit.calls), 500)
            self.assertTrue(all(r.get("decision") == "allow" for r in audit.calls))
            self.assertEqual(audit.calls[0]["n"], 100)


class StagedEventTests(unittest.TestCase):
    def test_staging_events_are_printed(self):
        # approval-requested is the staging; approval-queued is in older logs.
        for event in ("approval-requested", "approval-queued"):
            ev = d.broker_event({"ts": "2026-10-19T06:00:00Z", "event": event, "id": "c1", "tool": "git_push",
                                 "repo": "o/r", "branch": "factory/x"})
            self.assertEqual((ev["kind"], ev["red"], ev["text"]), ("STAGED", True, "git_push o/r factory/x"))
        ev = d.broker_event({"event": "approval-write-done", "id": "c1", "tool": "github_create_pr", "repo": "o/r",
                             "result": "\n\nhttps://github.com/o/r/pull/9\nsecond line"})
        self.assertEqual((ev["kind"], ev["red"]), ("WRITTEN", False))
        self.assertTrue(ev["text"].endswith("· https://github.com/o/r/pull/9"), ev["text"])
        self.assertNotIn("second line", ev["text"])
        ev = d.broker_event({"event": "approval-write-failed", "id": "c1", "tool": "git_push", "repo": "o/r",
                             "reason": "denied: branch moved"})
        self.assertEqual((ev["kind"], ev["red"]), ("FAILED", True))
        self.assertTrue(ev["text"].endswith("· denied: branch moved"), ev["text"])

    def test_staged_call_record_prints_nothing(self):
        # The call itself is audited with decision "staged"; its approval-requested event is the line.
        for decision in ("staged", "queued"):
            self.assertIsNone(d.broker_event({"tool": "git_push", "role": "developer", "decision": decision}))

    def test_hostile_reason_and_result_print_as_one_clean_line(self):
        evil = "bad\x1b[2J\r\n‮evil​\x00" + "x" * 5000
        for event, field in (("approval-write-failed", "reason"), ("approval-write-done", "result")):
            ev = d.broker_event({"event": event, "tool": "git_push", field: evil})
            self.assertEqual(len(ev["text"].splitlines()), 1)
            for ch in ev["text"]:
                self.assertGreaterEqual(ch, " ")
                self.assertNotIn(ch, "‮​\x7f")
            self.assertLess(len(ev["text"]), 400)

    def test_non_string_fields_do_not_break_it(self):
        for rec in ({"event": "approval-write-failed", "tool": "t", "reason": {"a": 1}},
                    {"event": "approval-write-done", "tool": "t", "result": 5},
                    {"event": "approval-write-done", "tool": "t", "result": ["x"]}):
            self.assertEqual(len(d.broker_event(rec)["text"].splitlines()), 1)


class WritesTests(unittest.TestCase):
    """The snapshot's deferred-write list: display only, every field one clean line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))
        self.done = self.fx.state / "broker" / "approvals" / "done"
        self.done.mkdir(parents=True)
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, cid, write, age=60, **over):
        rec = {"id": cid, "tool": "git_push", "principal": "developer", "repo": "o/r", "branch": "factory/x",
               "nonce": "n", "decision": "approve", "approver": "daron", "queued": True, "write": write,
               "result": "pushed 2 commits\nmore", "finished": self.fx.now - age}
        rec.update(over)
        (self.done / f"{cid}.json").write_text(json.dumps(rec))

    def test_snapshot_lists_failures_first(self):
        self.put("made1", "done", age=30)
        self.put("bad1", "failed", age=300, result="denied: branch moved")
        self.put("run1", "running", age=5)
        self.put("declined1", "skipped")
        self.put("plain1", "done", queued=False)  # approved while the agent waited: not a deferred write
        snap = self.board.snapshot(self.fx.now)
        json.dumps(snap)
        self.assertEqual([(w["id"], w["state"]) for w in snap["writes"]],
                         [("bad1", "failed"), ("run1", "running"), ("made1", "made")])
        self.assertEqual(snap["writes"][0]["summary"], "denied: branch moved")
        self.assertEqual(snap["writes"][2]["summary"], "pushed 2 commits")

    def test_no_done_folder(self):
        self.done.rmdir()
        self.assertEqual(self.board.snapshot(self.fx.now)["writes"], [])

    def test_hostile_contents_come_out_clean(self):
        evil = "\x1b]0;pwned\x07\r\n‮<img src=x onerror=alert(1)>​\x00" + "y" * 5000
        self.put("evil1", "failed", tool=evil, principal=evil, repo=evil, branch=evil, title=evil, result=evil,
                 approver=evil)
        w = self.board.snapshot(self.fx.now)["writes"][0]
        for key in ("tool", "role", "repo", "target", "title", "summary", "approver"):
            value = w[key]
            self.assertEqual(len(value.splitlines()), 1, key)
            for ch in value:
                self.assertGreaterEqual(ch, " ", key)
                self.assertNotIn(ch, "‮​\x7f", key)
            self.assertLessEqual(len(value), 200, key)
        # Text stays text: the page draws it with textContent, so markup is only ever characters.
        self.assertIn("<img", w["summary"])

    def test_bad_files_are_skipped(self):
        self.put("good1", "done")
        (self.done / "junk.json").write_text("{not json")
        (self.done / "list.json").write_text("[1, 2]")
        (self.done / "wrongid.json").write_text(json.dumps({"id": "other", "queued": True, "write": "done"}))
        (self.done / "weird.json").write_text(json.dumps({"id": "weird", "queued": True, "write": ["done"],
                                                           "finished": "soon", "result": 7}))
        writes = self.board.snapshot(self.fx.now)["writes"]
        self.assertEqual([w["id"] for w in writes], ["good1"])

    def test_read_is_cached_between_polls(self):
        self.put("made1", "done")
        with mock.patch.object(d.approvals, "read_writes", wraps=d.approvals.read_writes) as spy:
            self.board.writes(self.fx.now)
            self.board.writes(self.fx.now + 1)
            self.assertEqual(spy.call_count, 1)
            self.put("bad1", "failed")
            self.assertEqual(len(self.board.writes(self.fx.now + d.WRITES_TTL + 1)), 2)
            self.assertEqual(spy.call_count, 2)

    def test_cached_list_cannot_be_changed_by_a_caller(self):
        self.put("made1", "done")
        first = self.board.writes(self.fx.now)
        first[0]["summary"] = "tampered"
        self.assertEqual(self.board.writes(self.fx.now + 1)[0]["summary"], "pushed 2 commits")


class TailTests(unittest.TestCase):
    def test_truncated_log_is_reread(self):
        # A lane log truncated and rewritten (a sandbox recreated, say) is read again from the top.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t = time.localtime()
            now = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))
            (root / "roles" / "developer").mkdir(parents=True)
            (root / "roles" / "developer" / "role.conf").write_text('LANES="1"\n')
            logs = root / "state" / "dispatch" / "logs"
            logs.mkdir(parents=True)
            write_lane(logs, "developer", "20261019-000001-aaaaaa", now,
                       "".join(f"{hms(now - 50)}     old line {i} padding padding padding\n" for i in range(20)))
            board = d.Dashboard(root / "state", root / "roles", "proj")
            self.assertEqual(board.lane_detail("developer", now)["job"], "20261019-000001-aaaaaa")
            write_lane(logs, "developer", "20261019-000009-zzzzzz", now, f"{hms(now - 40)}     fresh\n")
            detail = board.lane_detail("developer", now)
            self.assertEqual(detail["job"], "20261019-000009-zzzzzz")
            self.assertEqual([e["text"] for e in detail["tail"]], ["20261019-000009-zzzzzz", "a task", "fresh"])  # nothing old survives


class OpenBrowserTests(unittest.TestCase):
    """Which opener runs, with what argv. Nothing is actually launched."""
    URL = "http://127.0.0.1:1234/?token=abc-_123"

    def run_open(self, wsl, on_path, popen_fails=(), browser=True):
        calls = []

        def popen(argv, **kw):
            calls.append(argv)
            self.assertEqual((kw.get("stdout"), kw.get("stderr")), (d.subprocess.DEVNULL, d.subprocess.DEVNULL))
            self.assertNotIn("shell", kw)
            if argv[0] in popen_fails:
                raise OSError("exec format error")
            return mock.Mock()

        with mock.patch.object(d, "is_wsl", return_value=wsl), \
                mock.patch.object(d.shutil, "which", side_effect=lambda name: f"/x/{name}" if name in on_path else None), \
                mock.patch.object(d.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(d.webbrowser, "open", return_value=browser) as wb:
            result = d.open_browser(self.URL)
        return result, calls, [c.args for c in wb.call_args_list]

    def test_wsl_uses_rundll32_protocol_handler(self):
        result, calls, wb = self.run_open(True, {"rundll32.exe", "wslview", "explorer.exe"})
        self.assertEqual(result, "rundll32.exe")
        self.assertEqual(calls, [["rundll32.exe", "url.dll,FileProtocolHandler", self.URL]])
        self.assertEqual(wb, [])

    def test_wsl_falls_back_to_wslview(self):
        for on_path, fails in (({"wslview"}, ()), ({"rundll32.exe", "wslview"}, ("rundll32.exe",))):
            result, calls, wb = self.run_open(True, on_path, fails)
            self.assertEqual(result, "wslview")
            self.assertEqual(calls[-1], ["wslview", self.URL])
            self.assertEqual(wb, [])

    def test_wsl_falls_back_to_webbrowser(self):
        result, calls, wb = self.run_open(True, {"rundll32.exe", "wslview"}, ("rundll32.exe", "wslview"))
        self.assertEqual((result, wb), ("webbrowser", [(self.URL,)]))
        self.assertEqual([c[0] for c in calls], ["rundll32.exe", "wslview"])
        result, calls, wb = self.run_open(True, set(), browser=False)
        self.assertEqual((result, calls, wb), (None, [], [(self.URL,)]))

    def test_not_wsl_goes_straight_to_webbrowser(self):
        result, calls, wb = self.run_open(False, {"rundll32.exe", "wslview"})
        self.assertEqual((result, calls, wb), ("webbrowser", [], [(self.URL,)]))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        fx = Fixture(Path(cls.tmp.name))
        # A lane with more transcript than /api/lane sends.
        logs = fx.state / "dispatch" / "logs"
        (fx.roles / "tester").mkdir()
        (fx.roles / "tester" / "role.conf").write_text('LANES="1"\n')
        write_lane(logs, "tester", "20261019-000003-cccccc", fx.now,
                   "".join(f"{hms(fx.now - 30)}     line {i}\n" for i in range(d.FULL + 100)))
        cls.server, url = d.serve(d.Dashboard(fx.state, fx.roles, "proj"), 0, TOKEN, manual=d.load_docs())
        cls.port = cls.server.server_address[1]
        cls.cookie = d.cookie_name(cls.port)
        assert url == f"http://127.0.0.1:{cls.port}/?token={TOKEN}"

    @classmethod
    def tearDownClass(cls):
        d.stop(cls.server)  # both origins: the second one (the control unit's) always runs
        cls.tmp.cleanup()

    def get(self, path, cookie=True, host=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}" if host is None else host}
        if cookie:
            headers["Cookie"] = f"other=1; {d.COOKIE}=stale; {self.cookie}={TOKEN}"
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_lane_packet_by_job_id(self):
        resp, body = self.get("/api/lane?name=developer&job=20261019-000002-bbbbbb")
        self.assertEqual(resp.status, 200)
        self.assertIn("reviewer job 20261019-000002-bbbbbb", json.loads(body)["packet"])
        resp, body = self.get("/api/lane?name=developer&job=20261019-000077-abcdef")
        self.assertEqual(resp.status, 200)
        self.assertIn("no packet for job", json.loads(body)["packet"])
        for bad in ("../x", "20261019-000002-bbbbbb%2f..", "x", "20261019-000002-BBBBBB"):
            self.assertEqual(self.get(f"/api/lane?name=developer&job={bad}")[0].status, 400, bad)
        self.assertEqual(self.get("/api/lane?name=nobody&job=20261019-000002-bbbbbb")[0].status, 404)
        self.assertEqual(self.get("/api/lane?name=developer&job=20261019-000002-bbbbbb", cookie=False)[0].status, 403)

    def test_state_carries_the_outbox(self):
        resp, body = self.get("/api/state")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body)["outbox"], [])

    def test_token_becomes_cookie(self):
        resp, _ = self.get(f"/?token={TOKEN}", cookie=False)
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/")
        cookie = resp.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith(f"factory_dashboard_{self.port}={TOKEN};"), cookie)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)

    def test_refused_without_token(self):
        for path in ("/", "/api/state", "/app.js", "/?token=wrong", "/api/lane?name=developer"):
            self.assertEqual(self.get(path, cookie=False)[0].status, 403, path)
        other = d.cookie_name(self.port + 1)
        for cookie in (f"{self.cookie}=wrong",
                       f"{d.COOKIE}={TOKEN}",                          # the unported name
                       f"{other}={TOKEN}",                             # another dashboard's port
                       f"{self.cookie}={TOKEN}; {self.cookie}=x",      # twice: refuse, don't guess
                       f"{self.cookie}={TOKEN}; {self.cookie}={TOKEN}",
                       f"{self.cookie}={TOKEN}x"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/api/state", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": cookie})
            self.assertEqual(conn.getresponse().status, 403, cookie)
            conn.close()
        # Two Cookie headers, one cookie each, count as twice too.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", "/api/state", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("Cookie", f"{self.cookie}={TOKEN}")
        conn.putheader("Cookie", f"{self.cookie}=x")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_foreign_host_refused(self):
        # A DNS-rebinding page reaches 127.0.0.1 under its own name; even with the cookie it gets nothing.
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1:1", ""):
            self.assertEqual(self.get("/api/state", host=host)[0].status, 403, host)
        self.assertEqual(self.get("/api/state", host=f"localhost:{self.port}")[0].status, 200)

    def test_page_and_state(self):
        resp, body = self.get("/")
        self.assertEqual(resp.status, 200)
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertIn(b"FC-1984", body)
        resp, body = self.get("/api/state")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(json.loads(body)["project"], "proj")
        resp, body = self.get("/api/lane?name=reviewer")
        self.assertIn("Verdict: REVISE", json.loads(body)["packet"])
        self.assertEqual(self.get("/api/lane?name=..%2Fx")[0].status, 404)

    def test_lane_detail_caps_at_full(self):
        lane = json.loads(self.get("/api/lane?name=tester")[1])
        self.assertEqual(len(lane["tail"]), d.FULL)
        self.assertEqual(lane["tail"][-1]["text"], f"line {d.FULL + 99}")
        state = json.loads(self.get("/api/state")[1])
        self.assertEqual(len(next(x for x in state["lanes"] if x["name"] == "tester")["tail"]), d.TAIL)

    def test_static_allowlist_only(self):
        for path in d.STATIC:
            self.assertEqual(self.get(path)[0].status, 200, path)
        for path in ("/../dashboard.py", "/fonts/OFL-VT323.txt", "/index.html", "/fonts/../app.js", "//etc/passwd",
                     "/docs/index.html", "/docs/../app.js", "/docs/README.md", "/docs/docs.css/"):
            self.assertEqual(self.get(path)[0].status, 404, path)

    def test_docs(self):
        resp, body = self.get("/docs/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
        # The main page's CSP minus frame-src: the docs frame nothing.
        self.assertEqual(resp.getheader("Content-Security-Policy"), d.CSP)
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertIn(b"Use the dashboard", body)
        for path, (_name, ctype) in d.DOCS.items():
            resp, _ = self.get(path)
            self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, ctype), path)
            self.assertEqual(self.get(path, cookie=False)[0].status, 403, path)
        resp, _ = self.get("/docs")
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/docs/"))
        self.assertEqual(self.get("/docs/", host="evil.example")[0].status, 403)

    def test_docs_css_shares_the_theme(self):
        # The manual can't drift from the console it documents (doomtickle/passdesk-ops#131):
        # its CSS carries the same tokens as app.css, inlined from theme.css at build time.
        theme = (UI / "theme.css").read_text(encoding="utf-8")
        _resp, css = self.get("/docs/docs.css")
        self.assertIn(theme, css.decode())

    def test_theme_css_served(self):
        resp, body = self.get("/theme.css")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/css; charset=utf-8")
        self.assertIn(b":root", body)
        self.assertIn(b"--stripe-1", body)
        self.assertEqual(self.get("/theme.css", cookie=False)[0].status, 403)

    def test_favicon_is_empty(self):
        self.assertEqual(self.get("/favicon.ico")[0].status, 204)
        self.assertEqual(self.get("/favicon.ico", cookie=False)[0].status, 403)

    def test_read_only(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            for path in ("/api/state", "/cu/api/state", "/cu/api/decide"):
                resp, _ = self.get(path, method=method)
                self.assertEqual(resp.status, 405, (method, path))
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

    def test_state_points_at_the_control_unit_but_holds_no_controls(self):
        state = json.loads(self.get("/api/state")[1])
        self.assertEqual(state["cu"], {"port": self.server.term_server.server_address[1]})
        self.assertIsNone(state["term"])  # started without --coordinator
        for card in state["gate"]:
            self.assertNotIn("fp", card)  # the fingerprint is for the control unit only
            self.assertNotIn("nonce", card)
            self.assertIs(card["queued"], False)
        self.assertEqual(state["writes"], [])
        self.assertNotIn(self.server.term_server.cu_key, json.dumps(state))


class ServiceTests(unittest.TestCase):
    """The SERVICE DUE lamp: fixture fingerprints and config files, no sbx and no command run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.fx = Fixture(base)
        self.paths = Paths(str(base), str(base / "config"), str(self.fx.state))
        (base / "kits" / "coordinator").mkdir(parents=True)
        (base / "kits" / "coordinator" / "spec.yaml").write_text("name: coordinator\n")
        (base / "kits" / "broker").mkdir(parents=True)
        (base / "kits" / "broker" / "spec.yaml").write_text("name: broker\n")
        (self.fx.roles / "developer" / "kit").mkdir()
        (self.fx.roles / "developer" / "kit" / "spec.yaml").write_text("name: developer\n")
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj", paths=self.paths)
        self.names = [fp.sbx.BROKER_SANDBOX, fp.COORDINATOR_SANDBOX, "factory-developer", "factory-reviewer"]

    def tearDown(self):
        self.tmp.cleanup()

    def record(self):
        for name in self.names:
            fp.record_sandbox_fingerprint(self.paths, name)

    def service(self, now=None):
        board = d.Dashboard(self.fx.state, self.fx.roles, "proj", paths=self.paths)  # no cache between calls
        return board.snapshot(now or self.fx.now)["service"]

    def snapshot_start(self, notes_age=0):
        """What `factory start` leaves in dispatch/ (enabled-roles is written last)."""
        snap = self.fx.state / "dispatch"
        (snap / "role-overrides").mkdir(exist_ok=True)
        (snap / "enabled-roles").write_text("")
        past = time.time() - notes_age
        os.utime(snap / "enabled-roles", (past, past))

    def config(self, rel, text):
        path = Path(self.paths.config) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_nothing_due_when_nothing_changed(self):
        self.record()
        self.snapshot_start()
        self.assertEqual(self.service(), {"due": None, "lines": []})

    def test_no_recorded_fingerprints_is_not_due(self):
        self.assertEqual(self.service(), {"due": None, "lines": []})

    def test_changed_kit_is_update_and_names_the_sandbox_and_why(self):
        self.record()
        (self.fx.roles / "developer" / "kit" / "spec.yaml").write_text("name: developer\nchanged: yes\n")
        self.assertEqual(self.service(), {"due": "update", "lines": ["factory-developer: kit changed"]})

    def test_broker_reasons_and_order(self):
        self.record()
        self.config("broker-kit/hosts.yaml", "hosts: [example.org]\n")
        (Path(self.paths.state) / "images").mkdir()
        (Path(self.paths.state) / "images" / f"factory-broker-{fp.sbx.TEMPLATE_TAG}.id").write_text("sha256:new\n")
        (Path(self.paths.root) / "kits" / "coordinator" / "spec.yaml").write_text("name: other\n")
        self.assertEqual(self.service(), {"due": "update", "lines": [
            "factory-broker: image rebuilt (policy.toml or broker code), broker-kit/ changed",
            "factory-coordinator: kit changed"]})

    def test_a_sandbox_recorded_before_reasons_were_kept_gets_a_general_one(self):
        self.record()
        os.unlink(fp.parts_file(self.paths, "factory-reviewer"))
        (self.fx.roles / "reviewer" / "role.conf").write_text('LANES="1"\nBROKER_TOOLS="web_fetch"\n')
        self.assertEqual(self.service()["lines"], ["factory-reviewer: made from an older image, kit or setting"])

    def test_update_wins_over_restart_and_hides_its_lines(self):
        self.record()
        self.snapshot_start()
        (self.fx.roles / "developer" / "kit" / "spec.yaml").write_text("name: x\n")
        self.config("enabled-roles", "reviewer\n")
        self.assertEqual(self.service(), {"due": "update", "lines": ["factory-developer: kit changed"]})

    def test_new_override_is_restart(self):
        self.record()
        self.snapshot_start()
        self.config("roles/developer.conf", 'MODEL="claude-haiku-4-5"\n')
        self.assertEqual(self.service(), {"due": "restart", "lines": ["role override developer.conf changed"]})

    def test_override_matching_its_snapshot_is_quiet_and_an_edit_or_removal_is_not(self):
        self.record()
        self.snapshot_start()
        self.config("roles/developer.conf", 'MODEL="a"\n')
        (self.fx.state / "dispatch" / "role-overrides" / "developer.conf").write_text('MODEL="a"\n')
        self.assertIsNone(self.service()["due"])
        self.config("roles/developer.conf", 'MODEL="b"\n')
        self.assertEqual(self.service()["lines"], ["role override developer.conf changed"])
        os.unlink(Path(self.paths.config) / "roles" / "developer.conf")
        self.assertEqual(self.service()["lines"], ["role override developer.conf changed"])

    def test_override_of_a_disabled_optional_role_is_not_due(self):
        (self.fx.roles / "researcher").mkdir()
        (self.fx.roles / "researcher" / "role.conf").write_text('LANES="1"\nOPTIONAL="1"\n')
        self.record()
        self.snapshot_start()  # what `factory start` writes: no researcher override, enabled-roles empty
        self.config("roles/researcher.conf", 'MODEL="a"\n')
        self.config("projects/proj.researcher.md", "- notes\n")
        self.assertEqual(self.service(), {"due": None, "lines": []})
        self.config("enabled-roles", "researcher\n")  # switching it on is what makes the override count
        self.assertEqual(self.service()["lines"], [
            "role override researcher.conf changed", "enabled-roles changed",
            "project notes proj.researcher.md changed"])

    def test_enabled_roles_compared_by_content(self):
        self.record()
        self.snapshot_start()
        self.config("enabled-roles", "# nothing\n")
        self.assertIsNone(self.service()["due"])
        self.config("enabled-roles", "researcher\n")
        self.assertEqual(self.service(), {"due": "restart", "lines": ["enabled-roles changed"]})

    def test_project_notes_newer_than_the_snapshot_are_restart_and_older_are_not(self):
        self.record()
        self.snapshot_start(notes_age=100)
        notes = self.config("projects/proj.md", "- tests: npm test\n")
        old = time.time() - 500
        os.utime(notes, (old, old))
        self.assertIsNone(self.service()["due"])
        self.config("projects/proj.developer.md", "- style\n")
        self.config("projects/other.md", "not this project\n")
        self.assertEqual(self.service(), {"due": "restart", "lines": ["project notes proj.developer.md changed"]})

    def test_nothing_to_compare_before_the_first_start(self):
        self.record()
        self.config("enabled-roles", "researcher\n")
        self.config("roles/developer.conf", 'MODEL="b"\n')
        self.assertEqual(self.service(), {"due": None, "lines": []})

    def test_odd_file_names_are_made_printable(self):
        self.record()
        self.snapshot_start()
        (self.fx.state / "dispatch" / "role-overrides" / "ev\x1b[31mil.conf").write_text('MODEL="b"\n')
        self.assertEqual(self.service()["lines"], ["role override ev?[31mil.conf changed"])

    def test_a_failure_reads_as_nothing_due(self):
        self.record()
        with mock.patch.object(d.ds.fingerprints, "recorded_sandboxes", side_effect=RuntimeError):
            self.assertEqual(self.service(), {"due": None, "lines": []})

    def test_no_paths_never_due_and_the_result_is_cached(self):
        self.assertEqual(d.Dashboard(self.fx.state, self.fx.roles, "proj").snapshot(self.fx.now)["service"],
                         {"due": None, "lines": []})
        self.record()
        self.assertIsNone(self.board.snapshot(self.fx.now)["service"]["due"])
        (self.fx.roles / "developer" / "kit" / "spec.yaml").write_text("name: x\n")
        self.assertIsNone(self.board.snapshot(self.fx.now + 1)["service"]["due"])
        self.assertEqual(self.board.snapshot(self.fx.now + d.SERVICE_TTL)["service"]["due"], "update")

    def test_the_demo_has_the_lamp_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo.build(Path(tmp))
            board = d.Dashboard(Path(tmp), d.DOCS_ROOT / "roles", demo.PROJECT, demo=True)
            self.assertEqual(board.snapshot()["service"], {"due": None, "lines": []})

    def test_the_page_has_the_lamp_and_the_unit_keys_follow_it(self):
        html = (UI / "index.html").read_text()
        self.assertIn('id="lamp-service"', html)
        self.assertIn("state.service", (UI / "app.js").read_text())
        cu = (UI / "cu.js").read_text()
        self.assertIn('due === "restart"', cu)
        self.assertIn('due === "update"', cu)


class DocsAbsentTests(unittest.TestCase):
    """Without the docs the dashboard still runs; /docs/ says why."""

    def test_no_docs_and_failed_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            failed = d.load_docs(Path(tmp) / "no-checkout")
            self.assertIsInstance(failed, str)
            for manual, code in ((None, 404), (failed, 503)):
                server, _ = d.serve(d.Dashboard(fx.state, fx.roles, "proj"), 0, TOKEN, manual=manual)
                try:
                    port = server.server_address[1]
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/docs/", headers={"Host": f"127.0.0.1:{port}",
                                                          "Cookie": f"{d.cookie_name(port)}={TOKEN}"})
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, code)
                    self.assertIn(b"factory docs", resp.read())
                    conn.close()
                finally:
                    d.stop(server)


class OutboxTests(unittest.TestCase):
    """The OUTBOX module (doomtickle/passdesk-ops#189): imported factory/* branches, joined with
    the reviewer's verdict, the push and the pull request, over a real throwaway git repo."""

    DEV = "20261019-000001-aaaaaa"
    REV = "20261019-000009-cccccc"
    REV2 = "20261019-000010-dddddd"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Dev",
                        GIT_AUTHOR_EMAIL="dev@example.com", GIT_COMMITTER_NAME="Dev", GIT_COMMITTER_EMAIL="dev@example.com")
        self.git("init", "-q", "-b", "main")
        self.base = self.commit("base")
        self.git("checkout", "-q", "-b", "side")
        self.commit("one")
        self.tip = self.commit("two")
        self.git("checkout", "-q", "main")
        (self.fx.state / "dispatch" / "project").write_text(f"{self.proj}\n")
        self.ref("factory-developer/factory/foo", self.tip)
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")
        self.now = self.fx.now
        self.write_jobs([self.dev_job(self.DEV, "factory-developer/factory/foo", self.tip)])

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        cp = subprocess.run(["git", "-C", str(self.proj), *args], env=self.env, capture_output=True, check=True)
        return cp.stdout.decode().strip()

    def commit(self, msg):
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def ref(self, name, oid):
        self.git("update-ref", f"refs/remotes/{name}", oid)

    def dev_job(self, job, ref, commit, ago=3000, lane="developer"):
        return {"id": job, "role": "developer", "lane": lane, "status": "done", "started": self.now - ago - 600,
                "finished": self.now - ago, "branches": [{"repo": ".", "ref": ref, "commit": commit[:7]}]}

    def rev_job(self, job, started_ago, lane="reviewer"):
        return {"id": job, "role": "reviewer", "lane": lane, "status": "done", "started": self.now - started_ago,
                "finished": self.now - started_ago + 60, "branches": None}

    def write_jobs(self, jobs):
        (self.fx.state / "dispatch" / "jobs.json").write_text(json.dumps({"version": 1, "jobs": jobs}))

    def packet(self, job, verdict, task):
        text = f"# reviewer job {job}: done\n\n- Lane: reviewer\n- Verdict: {verdict}\n\n## Task\n\n{task}\n\n## Report\n\nok\n"
        (self.fx.state / "dispatch" / "packets" / f"{job}.md").write_text(text)

    def audit(self, *records):
        with open(self.fx.state / "broker" / "audit.jsonl", "a") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def rows(self, step=0):
        # Each call is past the cache time, so it recomputes.
        self.now_calls = getattr(self, "now_calls", 0) + 1
        return self.board.snapshot(self.now + self.now_calls * 100 + step)["outbox"]

    def test_an_imported_branch_is_a_row(self):
        (row,) = self.rows()
        self.assertEqual((row["repo"], row["branch"], row["status"], row["ahead"]),
                         ("proj", "factory-developer/factory/foo", "OPEN", 2))
        self.assertEqual((row["verdict"], row["push"], row["pr"], row["job"], row["lane"]), ("", "", None, self.DEV, "developer"))
        self.assertEqual(row["commit"], self.tip[:7])

    def test_newest_first_and_capped(self):
        self.ref("factory-developer/factory/bar", self.tip)
        self.write_jobs([self.dev_job(self.DEV, "factory-developer/factory/foo", self.tip, ago=3000),
                         self.dev_job("20261019-000002-eeeeee", "factory-developer/factory/bar", self.tip, ago=100)])
        self.assertEqual([r["branch"] for r in self.rows()],
                         ["factory-developer/factory/bar", "factory-developer/factory/foo"])
        jobs = []
        for i in range(ob.MAX_ROWS + 5):
            self.ref(f"factory-developer/factory/b{i}", self.tip)
            jobs.append(self.dev_job(f"20261019-{i:06d}-aaaaaa", f"factory-developer/factory/b{i}", self.tip, ago=10 + i))
        self.write_jobs(jobs)
        self.assertEqual(len(self.rows()), ob.MAX_ROWS)

    def test_the_verdict_joins_by_exact_branch(self):
        self.ref("factory-developer/factory/foo-bar", self.tip)
        self.write_jobs([self.dev_job(self.DEV, "factory-developer/factory/foo", self.tip),
                         self.dev_job("20261019-000002-eeeeee", "factory-developer/factory/foo-bar", self.tip),
                         self.rev_job(self.REV, 1000), self.rev_job(self.REV2, 500)])
        self.packet(self.REV, "REVISE", "Review factory-developer/factory/foo.")
        self.packet(self.REV2, "APPROVED", "Review factory-developer/factory/foo-bar, then summarise.")
        by = {r["branch"]: r for r in self.rows()}
        self.assertEqual(by["factory-developer/factory/foo"]["verdict"], "REVISE")   # not foo-bar's APPROVED
        self.assertEqual(by["factory-developer/factory/foo-bar"]["verdict"], "APPROVED")
        self.assertFalse(by["factory-developer/factory/foo"]["stale"])

    def test_the_newest_review_wins_and_an_older_one_marks_stale(self):
        self.write_jobs([self.dev_job(self.DEV, "factory-developer/factory/foo", self.tip, ago=3000),
                         self.rev_job(self.REV, 4000), self.rev_job(self.REV2, 1000)])
        self.packet(self.REV, "APPROVED", "Review factory-developer/factory/foo")
        (row,) = self.rows()
        self.assertEqual((row["verdict"], row["stale"]), ("APPROVED", True))   # it began before the job ended
        self.packet(self.REV2, "REVISE", "Re-review refs/remotes/factory-developer/factory/foo.")
        (row,) = self.rows()
        self.assertEqual((row["verdict"], row["stale"]), ("REVISE", False))

    def test_only_the_packet_header_gives_a_verdict(self):
        self.write_jobs([self.dev_job(self.DEV, "factory-developer/factory/foo", self.tip), self.rev_job(self.REV, 1000)])
        self.packet(self.REV, "REVISE", "Review factory-developer/factory/foo\n\n- Verdict: APPROVED")
        self.assertEqual(self.rows()[0]["verdict"], "REVISE")
        # No verdict line, no packet, or a verdict that is neither word: none.
        (self.fx.state / "dispatch" / "packets" / f"{self.REV}.md").write_text(
            "# x\n\n- Verdict: MAYBE\n\n## Task\n\nReview factory-developer/factory/foo\n")
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")
        self.assertEqual(self.rows()[0]["verdict"], "")
        (self.fx.state / "dispatch" / "packets" / f"{self.REV}.md").unlink()
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")
        self.assertEqual(self.rows()[0]["verdict"], "")

    def test_a_pending_push_is_staged_only_while_its_card_lives(self):
        self.write_jobs([self.dev_job(self.DEV, "factory-developer/factory/x", self.tip)])
        self.ref("factory-developer/factory/x", self.tip)
        (row,) = self.rows()   # the fixture's c0ffee card (factory/x) is pending
        self.assertEqual(row["push"], "STAGED")
        (self.fx.state / "broker" / "approvals" / "pending" / "c0ffee.json").unlink()
        self.assertEqual(self.rows()[0]["push"], "")   # expired or gone: nothing to approve any more

    def test_push_done_failed_and_written(self):
        base = {"event": "approval-write-done", "branch": "factory/foo", "repo": "o/r"}
        self.audit({**base, "ts": iso(self.now - 20), "tool": "git_push", "id": "p1", "result": "Pushed"})
        self.assertEqual(self.rows()[0]["push"], "PUSHED")
        self.audit({**base, "ts": iso(self.now - 10), "tool": "git_push", "id": "p2", "event": "approval-write-failed"})
        self.assertEqual(self.rows()[0]["push"], "FAILED")
        # A PR the gateway opened with no push on record: the branch got there some other way.
        self.fx_state_audit_reset()
        self.audit({**base, "ts": iso(self.now - 5), "tool": "github_create_pr", "id": "p3",
                    "result": "Opened PR #7: https://github.com/o/r/pull/7"})
        row = self.rows()[0]
        self.assertEqual((row["push"], row["pr"]["label"]), ("WRITTEN", "PR #7"))

    def test_a_write_no_hold_stopped_is_an_ordinary_record(self):
        # Holds are off by default: the call is logged once, decision "allow", args kept by summarize_args.
        push = {"ts": iso(self.now - 20), "tool": "git_push", "decision": "allow", "result": "Pushed factory/foo",
                "args": {"repo": "o/r", "branch": "factory/foo"}}
        self.audit(push)
        self.assertEqual(self.rows()[0]["push"], "PUSHED")
        self.audit({**push, "ts": iso(self.now - 10), "decision": "deny", "reason": "no", "result": None})
        self.assertEqual(self.rows()[0]["push"], "FAILED")
        self.audit({**push, "ts": iso(self.now - 5), "decision": "error"})
        self.assertEqual(self.rows()[0]["push"], "FAILED")
        # A staged call and an intent record say nothing; the approval events do.
        self.fx_state_audit_reset()
        self.audit({**push, "decision": "staged"}, {**push, "event": "intent"})
        self.assertEqual(self.rows()[0]["push"], "")

    def test_an_unheld_pr_and_mr_give_a_validated_link(self):
        pr = {"ts": iso(self.now - 5), "tool": "github_create_pr", "decision": "allow",
              "args": {"repo": "o/r", "head": "factory/foo", "base": "main"},
              "result": "Opened PR #41: https://github.com/o/r/pull/41"}
        self.audit(pr)
        row = self.rows()[0]
        self.assertEqual(row["pr"], {"label": "PR #41", "url": "https://github.com/o/r/pull/41", "number": 41})
        self.assertEqual(row["push"], "WRITTEN")
        for result in ("Opened PR #41: https://evil.example/o/r/pull/41", "Opened PR #41: https://github.com/o/r/pull/42",
                       "Opened PR #41: https://user@github.com/o/r/pull/41", "Opened PR #41: https://github.com/o/r/pull/41?x=1"):
            self.fx_state_audit_reset()
            self.audit({**pr, "result": result})
            self.assertEqual(self.rows()[0]["pr"], {"label": "PR #41", "url": "", "number": 41}, result)
        # A refused PR opened nothing.
        self.fx_state_audit_reset()
        self.audit({**pr, "decision": "deny", "result": None})
        self.assertIsNone(self.rows()[0]["pr"])
        # An MR links only for the configured GitLab host.
        self.fx_state_audit_reset()
        self.audit({"ts": iso(self.now - 5), "tool": "gitlab_create_mr", "decision": "allow",
                    "args": {"repo": "gl.example.com/g/p", "head": "factory/foo"},
                    "result": "Opened MR !5: https://gl.example.com/g/p/-/merge_requests/5"})
        with mock.patch.object(ob, "gitlab_host", return_value="gl.example.com"):
            self.assertEqual(self.rows()[0]["pr"], {"label": "MR !5", "url": "https://gl.example.com/g/p/-/merge_requests/5", "number": 5})

    def fx_state_audit_reset(self):
        (self.fx.state / "broker" / "audit.jsonl").write_text("")
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")

    def test_a_deferred_write_counts_like_an_audit_line(self):
        self.board.outbox.git.begin()
        writes = [{"state": "made", "target": "factory/foo", "tool": "git_push", "summary": "pushed", "at": self.now - 5, "id": "w1"}]
        (row,) = self.board.outbox.rows(self.now + 100, [], set(), writes)
        self.assertEqual(row["push"], "PUSHED")
        writes[0]["state"] = "failed"
        (row,) = self.board.outbox.rows(self.now + 200, [], set(), writes)
        self.assertEqual(row["push"], "FAILED")

    def test_a_pr_link_comes_only_from_the_audit(self):
        self.audit({"ts": iso(self.now - 5), "tool": "github_create_pr", "event": "approval-write-done", "id": "p9",
                    "branch": "factory/foo", "result": "Opened PR #41: https://github.com/o/r/pull/41"})
        pr = self.rows()[0]["pr"]
        self.assertEqual(pr, {"label": "PR #41", "url": "https://github.com/o/r/pull/41", "number": 41})

    def test_a_pr_url_that_is_not_the_pr_is_text(self):
        for result in ("Opened PR #41: https://evil.example/o/r/pull/41",
                       "Opened PR #41: https://github.com.evil.example/o/r/pull/41",
                       "Opened PR #41: https://user@github.com/o/r/pull/41",
                       "Opened PR #41: https://github.com@evil.example/o/r/pull/41",
                       "Opened PR #41: http://github.com/o/r/pull/41",
                       "Opened PR #41: javascript:alert(1)",
                       "Opened PR #41: https://github.com/o/r/pull/42",        # another number
                       "Opened PR #41: https://github.com/o/r/pull/41?x=1",
                       "Opened PR #41: https://github.com/o/r/issues/41",
                       "Opened PR #41"):
            self.fx_state_audit_reset()
            self.audit({"ts": iso(self.now - 5), "tool": "github_create_pr", "event": "approval-write-done", "id": "p9",
                        "branch": "factory/foo", "result": result})
            self.assertEqual(self.rows()[0]["pr"], {"label": "PR #41", "url": "", "number": 41}, result)

    def test_pr_url_checks(self):
        gh, gl = "github_create_pr", "gitlab_create_mr"
        self.assertEqual(ob.pr_url("https://github.com/o/r/pull/3", gh, ""), "https://github.com/o/r/pull/3")
        self.assertEqual(ob.pr_url("https://gitlab.example.com/g/sub/p/-/merge_requests/3", gl, "gitlab.example.com"),
                         "https://gitlab.example.com/g/sub/p/-/merge_requests/3")
        for url, tool, host in (("https://gitlab.example.com/g/p/-/merge_requests/3", gl, ""),          # no host configured
                                ("https://gitlab.other.com/g/p/-/merge_requests/3", gl, "gitlab.example.com"),
                                ("https://gitlab.example.com/g/p/-/merge_requests/3", gh, "gitlab.example.com"),
                                ("https://github.com/o/r/pull/3", gl, "github.com"),
                                ("https://GitHub.com/o/r/pull/3", gh, ""),
                                ("https://github.com:443/o/r/pull/3", gh, ""),
                                ("https://github.com./o/r/pull/3", gh, ""),
                                ("https://github.com/o/r/pull/3#x", gh, ""),
                                ("https://github.com/o/r/pull/3 ", gh, ""),
                                ("https://github.com/o/r/pull/3\n", gh, ""),
                                ("https://github.com/o/‮r/pull/3", gh, ""),
                                ("https://gitlab.example.com/g/../p/-/merge_requests/3", gl, "gitlab.example.com"),
                                ("", gh, ""), (None, gh, "")):
            self.assertEqual(ob.pr_url(url, tool, host), "", url)

    def test_the_gitlab_host_comes_from_policy(self):
        with mock.patch.object(ob.policy, "policy_value_source", return_value=('"https://GitLab.Example.com/"', "f")):
            self.assertEqual(ob.gitlab_host(object()), "gitlab.example.com")
        with mock.patch.object(ob.policy, "policy_value_source", return_value=('"http://gitlab.example.com"', "f")):
            self.assertEqual(ob.gitlab_host(object()), "")
        with mock.patch.object(ob.policy, "policy_value_source", side_effect=OSError):
            self.assertEqual(ob.gitlab_host(object()), "")
        self.assertEqual(ob.gitlab_host(None), "")

    def test_merged_and_gone_rows_retire_themselves(self):
        self.ref("factory-developer/factory/done", self.base)   # contained in HEAD
        self.write_jobs([self.dev_job(self.DEV, "factory-developer/factory/foo", self.tip),
                         self.dev_job("20261019-000002-eeeeee", "factory-developer/factory/done", self.base, ago=100),
                         self.dev_job("20261019-000003-ffffff", "factory-developer/factory/lost", self.base, ago=200)])
        by = {r["branch"]: r for r in self.rows()}
        self.assertEqual(by["factory-developer/factory/done"]["status"], "MERGED")
        self.assertEqual(by["factory-developer/factory/done"]["ahead"], 0)
        self.assertEqual(by["factory-developer/factory/lost"]["status"], "GONE")
        self.assertEqual(by["factory-developer/factory/foo"]["status"], "OPEN")
        day = ob.RETIRE_SECONDS
        self.write_jobs([self.dev_job(self.DEV, "factory-developer/factory/foo", self.tip, ago=day + 10),
                         self.dev_job("20261019-000002-eeeeee", "factory-developer/factory/done", self.base, ago=day + 100),
                         self.dev_job("20261019-000003-ffffff", "factory-developer/factory/lost", self.base, ago=day + 200)])
        # Finished work that is still open stays however old it is.
        self.assertEqual([r["branch"] for r in self.rows()], ["factory-developer/factory/foo"])

    def test_merging_the_branch_turns_the_row_merged(self):
        self.assertEqual(self.rows()[0]["status"], "OPEN")
        self.git("merge", "-q", "--no-edit", "--no-ff", self.tip)
        self.assertEqual(self.rows()[0]["status"], "MERGED")

    def test_hostile_branch_names_are_shown_clean_and_never_reach_git(self):
        hostile = ["factory-developer/factory/--upload-pack=touch /tmp/pwn", "-C/tmp", "factory--x/factory/y",
                   "factory-developer/factory/-x", "factory-developer/factory/a\nb", "factory-developer/factory/$(touch pwn)",
                   "factory-developer/factory/x;sh", "factory-developer/factory/../../etc", "refs/heads/main",
                   "factory-developer/factory/a‮b", "factory-developer/factory/" + "a" * 400, "", 7]
        self.write_jobs([self.dev_job(f"20261019-{i:06d}-aaaaaa", ref, self.tip, ago=10 + i) for i, ref in enumerate(hostile)])
        seen = []
        real = subprocess.run

        def spy(argv, **kw):
            seen.append(list(argv))
            return real(argv, **kw)
        with mock.patch.object(ob.subprocess, "run", spy):
            rows = self.rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["status"], "UNCHECKED", row)
            self.assertIsNone(row["ahead"])
            self.assertNotIn("\n", row["branch"])
            self.assertNotIn("‮", row["branch"])
            self.assertLessEqual(len(row["branch"]), 160)
        for argv in seen:
            self.assertEqual(argv[:6], ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C"], argv)
            self.assertIn(argv[7], ("for-each-ref", "rev-list"))
            joined = "\0".join(argv)
            for bad in ("upload-pack", "touch", "$(", ";sh", "\n", "etc"):
                self.assertNotIn(bad, joined)
        self.assertFalse(any(a[7] == "rev-list" for a in seen))   # nothing was valid enough to count

    def test_git_runs_only_the_fixed_read_only_forms(self):
        seen = []
        real = subprocess.run

        def spy(argv, **kw):
            seen.append((list(argv), kw))
            return real(argv, **kw)
        with mock.patch.object(ob.subprocess, "run", spy):
            self.rows()
        self.assertEqual(len(seen), 2)
        (refs, kw1), (count, kw2) = seen
        self.assertEqual(refs[7:], ["for-each-ref", "--format=%(objectname) %(refname)", "refs/remotes/factory-developer/factory/"])
        self.assertEqual(count[7:9], ["rev-list", "--count"])
        self.assertRegex(count[9], r"^HEAD\.\.[0-9a-f]{40}$")
        for kw in (kw1, kw2):
            self.assertFalse(kw.get("shell"))
            self.assertEqual(kw["env"]["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(kw["stdin"], subprocess.DEVNULL)
            self.assertFalse([k for k in kw["env"] if k.startswith("GIT_") and k not in ("GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")])

    def test_git_hooks_and_the_environment_are_inert(self):
        hook = self.proj / ".git" / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {self.tmp.name}/hook-ran\n")
        hook.chmod(0o755)
        self.git("config", "core.fsmonitor", f"touch {self.tmp.name}/fsmonitor-ran")
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent"}):
            (row,) = self.rows()
        self.assertEqual(row["status"], "OPEN")
        self.assertFalse((Path(self.tmp.name) / "hook-ran").exists())
        self.assertFalse((Path(self.tmp.name) / "fsmonitor-ran").exists())

    def test_the_result_is_cached_for_a_few_seconds(self):
        calls = []
        real = ob.subprocess.run

        def spy(argv, **kw):
            calls.append(argv)
            return real(argv, **kw)
        with mock.patch.object(ob.subprocess, "run", spy):
            t = self.now + 1000
            self.board.snapshot(t)
            n = len(calls)
            self.assertGreater(n, 0)
            self.board.snapshot(t + ob.TTL - 1)
            self.assertEqual(len(calls), n)
            self.board.snapshot(t + ob.TTL)
            self.assertGreater(len(calls), n)

    def test_no_project_no_index_and_a_bad_index_give_no_rows_not_an_error(self):
        (self.fx.state / "dispatch" / "project").unlink()
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["UNCHECKED"])   # listed, not checked
        (self.fx.state / "dispatch" / "jobs.json").write_text("not json")
        self.assertEqual(self.rows(), [])
        (self.fx.state / "dispatch" / "jobs.json").write_text(json.dumps({"version": 2, "jobs": []}))
        self.assertEqual(self.rows(), [])
        (self.fx.state / "dispatch" / "jobs.json").unlink()
        self.assertEqual(self.rows(), [])

    def test_a_symlinked_index_or_packet_is_not_read(self):
        real = self.fx.state / "dispatch" / "jobs.json"
        other = Path(self.tmp.name) / "elsewhere.json"
        other.write_text(real.read_text())
        real.unlink()
        real.symlink_to(other)
        self.assertEqual(self.rows(), [])
        self.assertIsNone(ob.read_regular(Path(self.tmp.name) / "nope", 10))

    def test_the_lane_packet_endpoint_takes_only_a_job_id(self):
        detail = self.board.lane_detail("developer", self.now, job="20261019-000002-bbbbbb")
        self.assertIn("reviewer job 20261019-000002-bbbbbb", detail["packet"])
        detail = self.board.lane_detail("developer", self.now, job="20261019-000077-abcdef")
        self.assertIn("no packet for job", detail["packet"])
        for bad in ("../../broker/audit", "20261019-000002-bbbbbb/../x", "x", "20261019-000002-BBBBBB"):
            self.assertIn("no packet", self.board.lane_detail("developer", self.now, job=bad)["packet"])
        self.assertIsNone(self.board.lane_detail("nobody", self.now, job="20261019-000002-bbbbbb"))

    def test_the_demo_has_two_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo.build(Path(tmp))
            board = d.Dashboard(Path(tmp), d.DOCS_ROOT / "roles", demo.PROJECT, demo=True)
            with mock.patch.object(ob.subprocess, "run", side_effect=AssertionError("the demo runs no git")):
                rows = board.snapshot()["outbox"]
            self.assertEqual(len(rows), 2)
            by = {r["branch"]: r for r in rows}
            ok = by["factory-developer/factory/greet-readme"]
            self.assertEqual((ok["verdict"], ok["push"], ok["status"]), ("APPROVED", "PUSHED", "OPEN"))
            self.assertEqual(ok["pr"]["url"], "https://github.com/example/app/pull/41")
            fix = by["factory-developer-lane2/factory/greet-unicode-name"]
            self.assertEqual((fix["verdict"], fix["push"], fix["pr"]), ("REVISE", "", None))
            for row in rows:   # the press needs the lane to exist, and the packet to be the job's own
                self.assertIn(row["lane"], [lane["name"] for lane in board.snapshot()["lanes"]])
                self.assertIn(f"job {row['job']}", board.lane_detail(row["lane"], job=row["job"])["packet"].split("\n")[0])


class PageTests(unittest.TestCase):
    def test_fonts_match_checksums(self):
        sums = (UI / "fonts" / "SHA256SUMS").read_text().split("\n")
        pinned = dict(reversed(line.split()) for line in sums if line.strip())
        served = {Path(name).name for name, ctype in d.STATIC.values() if ctype == "font/woff2"}
        self.assertEqual(set(pinned), served)
        for name, digest in pinned.items():
            self.assertEqual(hashlib.sha256((UI / "fonts" / name).read_bytes()).hexdigest(), digest, name)
        licenses = {p.name for p in (UI / "fonts").glob("OFL-*.txt")}
        self.assertEqual(licenses, {"OFL-VT323.txt", "OFL-BarlowCondensed.txt", "OFL-DotGothic16.txt"})

    def test_no_html_sinks(self):
        for name in ("app.js", "floor.js", "sound.js", "radio.js", "lanelabel.js"):
            js = (UI / name).read_text()
            for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(sink, js, f"{name} uses {sink}")

    def test_outbox_draws_text_and_links_only_validated_urls(self):
        js = (UI / "app.js").read_text()
        html = (UI / "index.html").read_text()
        self.assertIn('href="/docs/#guide-dashboard:the-outbox"', html)
        self.assertIn('id="outbox-list"', html)
        body = js[js.index("function obRow"):js.index("function renderOutbox")]
        # The only href is set from the server's URL after an https-only check; classes come from fixed maps.
        self.assertEqual(body.count(".href ="), 1)
        self.assertLess(body.index("SAFE_URL.test(r.pr.url)"), body.index(".href ="))
        self.assertIn('a.rel = "noopener noreferrer"', body)
        self.assertIn('const SAFE_URL = /^https:', js)
        for fixed in ("OB_STATUS[r.status]", "OB_VERDICT[r.verdict]", "OB_PUSH[r.push]"):
            self.assertIn(fixed, body)
        self.assertNotIn("className = r.", body)
        self.assertNotIn("setAttribute", body.replace('row.setAttribute("role", "listitem")', ""))
        # A row opens that job's packet, and the monitor fetches it by the job id the server validates.
        self.assertIn("openMon(r.lane, r.job, obKey(r))", body)
        self.assertIn("&job=${encodeURIComponent(monJob)}", js)

    def test_floor_only_displays(self):
        # The factory floor draws from the state app.js hands it: it makes no request
        # of its own, so it can reach nothing the main page can't (scripts/CLAUDE.md).
        js = (UI / "floor.js").read_text()
        for call in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "postMessage", ".src =", "href"):
            self.assertNotIn(call, js, f"floor.js uses {call}")
        html = (UI / "index.html").read_text()
        self.assertIn('<script src="/floor.js" defer></script>', html)
        self.assertIn('<link rel="stylesheet" href="/floor.css">', html)

    def test_floor_overlays_never_take_clicks(self):
        # The glass drawn over the stations (and the crates in flight) once swallowed every
        # click on a station; test_floor_browser.py clicks them in a real browser.
        css = (UI / "floor.css").read_text()
        self.assertIn(".fl .over, .fl .fx { pointer-events: none; }", css)
        js = (UI / "floor.js").read_text()
        self.assertNotIn("style=", js)
        self.assertIn("motionOff.matches", js.split("function placard")[1].split("document.fonts")[0])

    def test_floor_crates_are_inspectable_as_text(self):
        # Parked and binned crates name their write by an SVG <title> and aria-label set
        # as text (doomtickle/passdesk-ops#162); the bin pile is bounded and page-local.
        js = (UI / "floor.js").read_text()
        self.assertIn("title.textContent = label", js)
        self.assertIn('setAttribute("aria-label", label)', js)
        self.assertIn('setAttribute("tabindex", "0")', js)
        self.assertIn("MAX_BINNED = 5", js)
        self.assertRegex(js, r"if \(binned\.length > MAX_BINNED\) binned\.shift\(\);")
        self.assertIn('new Set(["DENY", "NO", "FAILED"])', js)
        self.assertIn("else if (primed && BAD.has(ev.kind)) binCrate(line(ev));", js)
        self.assertIn("[c.tool, c.role, c.repo, c.branch]", js)
        # A capped DENY (no free crate for the trip to the arch) is binned directly.
        self.assertIn('return travel("crate", [[x, y], [x, oy], [ARCH_X - 4, oy]], then);', js)
        self.assertIn('if (toArch(role, () => confiscate(ARCH_X - 4, line(ev)))) return true;\n        binCrate(line(ev));', js)

    def test_floor_is_a_line_from_coordinator_to_out(self):
        # The floor (doomtickle/passdesk-ops#163): a panel with an amber tube, one loop track
        # inside it from the Coordinator's dispatch console past the developers and reviewers
        # to the gate and the OUT portal; no foreman's booth, gatehouse, dock, bays or rails.
        js = (UI / "floor.js").read_text()
        css = (UI / "floor.css").read_text()
        for gone in ("hue-f", "hue-g", "DOCK_X", "DOCK_Y", "POLICY · AUDIT", "TO DOCK", "OUTBOUND ▸ BROKER", "docked(", "boothNote", '"FOREMAN"',
                     '"HAND-OFF"', '"OUTBOUND ▸ OUT"', "function belt(", "RAIL_A", "RAIL_B", "RAIL_C"):
            self.assertNotIn(gone, js, gone)
        for gone in (".booth", ".gatehouse", ".dock", ".foreman", "hue-f", "hue-g", "roll-dock", ".belt", ".roll", ".concrete", ".wall {", ".hat"):
            self.assertNotIn(gone, css, gone)
        for label in ('"COORDINATOR"', '"DEVELOPERS ▲"', '"REVIEWERS ▼"', '"DISPATCH"', '"GATE"', '"OUT"', '"DEREZ"', '"LOIS WORKS"'):
            self.assertIn(label, js, label)
        # One track, drawn once, carries every trip; the glass clips crates leaving by the portal.
        self.assertEqual(js.count("trackPath()"), 1)
        self.assertIn('parts.fx = S("g", { "clip-path": "url(#fl-glass)" }, "fx");', js)
        for path in ("const dispatchPath = st => [CHUTE, [LX + RAD, CHUTE[1]], [LX + RAD, TY], [st.x, TY], st.inTray];", "const handoffPath = (dev, rev) => [dev.outTray, [dev.x, TY], [RX - RAD, TY]",
                     "const dropPath = rev => [rev.inTray, rev.outTray, [rev.x, BY], [OUT_X, BY], [OUT_X, GLASS_B + 12]];"):
            self.assertIn(path, js, path)
        # The people stay: a program per station (and the User at dispatch while Terminal 0 is attached), its disc carrying the state.
        self.assertIn('con.append(...program(CON_X, CON_Y, "hue-user", "user"));', js)
        self.assertIn("g.append(...program(cx, feet, hueCls));", js)
        self.assertIn(".fl .console .user { display: none; }", css)
        self.assertIn(".fl .console.in .user { display: inline; }", css)
        for rule in (".fl .s-running .disc", ".fl .s-held .disc", ".fl .s-done .disc", ".fl .s-fail .disc", ".fl .s-idle .disc"):
            self.assertIn(rule, css, rule)
        # Every colour is a theme token (plus the plate's screws and a few fixed darks), and the tube follows Terminal 0's CRT dial.
        self.assertIn("var(--crt, .3)", css)
        self.assertIn('document.documentElement.style.setProperty("--crt", f(t));', (UI / "app.js").read_text())
        # The console plate keeps the old booth's three states.
        self.assertIn('!state.bridge ? "BRIDGE DOWN" : attached ? "ON DUTY" : "BACK SOON"', js)
        self.assertIn("const attached = !!(state.term && state.term.running);", js)
        self.assertIn(".fl .console.in .plate-t { fill: var(--led-green); }", css)
        self.assertIn(".fl .console.lit:not(.in) .plate-t { fill: var(--led-amber); }", css)
        self.assertIn(".fl .console:not(.lit) .plate-t { fill: var(--led-red); }", css)
        # Keycaps and lane order stay the index in state.lanes, whatever bay a lane is in.
        self.assertIn("lanes.forEach((lane, i) => groups[bayOf(lane)].push([lane, i]));", js)
        self.assertIn("if (i < 9) g.append(R(2, 2, 9, 9, \"kc\")", js)
        # Each trip is decided from the previous poll's lane, and nothing is played from history.
        flow = js[js.index("function flow(animate)"):js.index("// New events and branches")]
        self.assertIn("if (!primed || !was) continue;", flow)
        self.assertIn('travel("ticket", dispatchPath(st))', flow)
        self.assertIn('if (animate) travel("crate branch", handoffPath(dev, st));', flow)
        self.assertIn('travel("crate ok", dropPath(st)', flow)
        self.assertIn('travel("crate fix", handoffPath(dev, st).reverse(), repark(home))', flow)
        # Only the trips need animation: clearing and re-parking a crate happens whatever `animate` says.
        self.assertEqual(flow.count("animate &&"), 2)
        self.assertIn("if (st && dev && pair && pair !== was.pair) {", flow)
        self.assertIn("if (st && dev && lane.state === \"revise\" && was.state !== \"revise\") {", flow)
        self.assertIn("if (!animate || !travel(", flow)
        self.assertIn("later.forEach(f => f());", flow)
        # A lane a reviewer already names at page load is not parked: that hand-off is history.
        self.assertIn('if (!primed && lane.state === "done" && !beingReviewed(lane.name)) park(lane.name);', js)
        self.assertIn('l.role === "reviewer" && l.reviews === name', js)
        self.assertNotIn("announce(", flow)
        self.assertIn("const animate = primed && on && svg && !motionOff.matches;", js)
        # Parked branch crates draw whether or not motion is allowed; docked says whether one was drawn.
        self.assertIn("const drawn = park(lane.name);", js)
        self.assertIn("animated: drawn }", js)
        self.assertIn("if (running && (was.state !== \"running\" || was.task !== task)) unpark(lane.name);", flow)
        self.assertIn("if (crates >= MAX_CRATES) return false;", js)

    def test_floor_events_are_display_only_hooks(self):
        # Floor events (doomtickle/passdesk-ops#162): three CustomEvents on document for
        # a future sound layer or shift ticket. Fired from play(), not from the animation,
        # once per event, never for first-poll history; nothing can veto the floor.
        js = (UI / "floor.js").read_text()
        for name in ("passed", "confiscated", "docked"):
            self.assertEqual(js.count(f'announce("factory-floor:{name}"'), 1, name)
        self.assertIn(
            "document.dispatchEvent(new CustomEvent(name, { bubbles: false, cancelable: false, detail: Object.freeze(detail) }));", js)
        self.assertEqual(js.count("dispatchEvent(new CustomEvent(name"), 1)
        # Fired from play() (after the not-animated bin path), never inside an animation callback.
        play = js[js.index("function play()"):js.index("// A new bay shape")]
        self.assertEqual(play.count("announce("), 3)
        self.assertEqual(js.count("announce("), 4)   # its definition and the three calls in play()
        self.assertIn("if (!primed) continue;", play)
        self.assertIn("if (fresh || !primed) continue;", play)
        # The detail holds strings and booleans only.
        self.assertRegex(play, r'\{ kind: String\(ev\.kind\), role: [^\n]*, line: clip\(line\(ev\), 300\), animated: moved \}')
        self.assertRegex(play, r'\{ lane: String\(lane\.name\), role: String\(lane\.role \|\| ""\), branch: clip\(String\(e\.text \|\| ""\), 200\), animated: ')
        # Every bin path reaches the confiscated event through play(): the animated crate,
        # the capped fallback (no free crate) and the not-animated path.
        self.assertIn('GOOD = new Set(["ALLOW", "OK", "WRITTEN"])', js)
        self.assertIn('else if (BAD.has(ev.kind)) announce("factory-floor:confiscated", detail);', play)
        self.assertIn("if (animate) moved = broker(ev);", play)                       # animated, and the capped fallback inside broker()
        self.assertIn("else if (primed && BAD.has(ev.kind)) binCrate(line(ev));", play)  # not animated
        self.assertIn("if (!moved) binCrate(label);", js)                              # confiscate()'s capped fallback
        self.assertIn("binCrate(line(ev));   // too many crates moving", js)          # DENY's capped fallback
        # The event fires whatever the floor's state: play() has no `on` gate of its own.
        self.assertNotRegex(play, r"if \(on\b")

    def test_sound_layer_only_listens(self):
        # The sound layer (doomtickle/passdesk-ops#162) synthesises tones from the three
        # floor events: no request, no audio file, nothing read from an event, off by default.
        js = (UI / "sound.js").read_text()
        for call in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "postMessage", ".src", "href",
                     "new Audio", "<audio", "decodeAudioData", "createMediaElementSource", "textContent", "e.detail", "dispatchEvent"):
            self.assertNotIn(call, js, f"sound.js uses {call}")
        for name in ("passed", "confiscated", "docked", "held", "done"):
            self.assertIn(name, js)
        self.assertIn("document.addEventListener(`factory-floor:${name}`, () => cue(name));", js)
        self.assertIn("document.addEventListener(`factory-attention:${name}`, () => cue(name));", js)   # #192: held write, finished job
        self.assertIn('const ATTENTION = ["held", "done"];', js)
        self.assertIn("let on = false,", js)                                  # off until the key is pressed
        self.assertIn('if (localStorage.getItem(STORE) === "1") setOn(true, false);', js)
        self.assertRegex(js, r"try \{ if \(localStorage\.getItem\(STORE\)")      # storage may be blocked
        self.assertIn("if (!AC) { key.disabled = true; return; }", js)       # no Web Audio: harmless
        # The context is only created in wake(), which runs from a click or a key/pointer press.
        self.assertEqual(js.count("new AC()"), 1)
        self.assertEqual(js.count("wake()"), 3)   # its definition, the key's click, the first-gesture hook
        self.assertIn("GAP = 0.25", js)

    def test_attention_signals_are_wired(self):
        # #192: the badge, cues and notifications come from the state the page polls; only counts,
        # a fixed tool list and the lane-name pattern reach the title, favicon or a notification.
        js = (UI / "app.js").read_text()
        html = (UI / "index.html").read_text()
        body = js[js.index("// ---------- attention"):js.index("// ---------- polling")]
        for call in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "postMessage", "innerHTML", "location"):
            self.assertNotIn(call, body, f"the attention block uses {call}")
        self.assertIn("renderTerm(); attention();", js)
        self.assertIn('const TOOLS = new Set(["git_push", "github_create_pr", "gitlab_create_mr", "github_create_issue"]);', body)
        self.assertIn("const LANE_NAME = /^[a-z][a-z0-9-]{1,40}$/;", body)
        # The one thing agent-controlled that reaches a notification is a name that passed those two checks.
        self.assertEqual(re.findall(r"new Notification\(([^;]*)\);", body), ['"LOIS", { body, tag: "factory-attention", silent: true }'])
        self.assertIn("TOOLS.has(x.tool) ? x.tool : \"write\"", body)
        self.assertIn('LANE_NAME.test(name) ? name : "lane"', body)
        # Permission is asked in one place, the key's click handler, never on load.
        self.assertEqual(js.count("requestPermission("), 1)
        self.assertLess(body.index('notifyKey.addEventListener("click"'), body.index("requestPermission("))
        self.assertIn('if (localStorage.getItem(NOTIFY_STORE) === "1" && Notification.permission === "granted") setNotify(true, false);', body)
        self.assertIn('if (typeof Notification === "undefined") notifyKey.disabled = true;', body)
        self.assertIn('if (e.key === "n" || e.key === "N") { notifyKey.click(); return; }', js)
        self.assertLess(js.index('e.key === "p") setTab'), js.index("notifyKey.click(); return;"))
        self.assertRegex(html, r'<button class="tabkey notify-key" type="button" id="notify-key" aria-pressed="false">Notify</button>')
        # The favicon is a data: image drawn on a canvas, so the page's CSP allows img-src data: and nothing wider.
        self.assertIn("img-src 'self' data:;", d.CSP)
        self.assertNotIn("img-src *", d.CSP)

    def test_sound_key_is_wired(self):
        html = (UI / "index.html").read_text()
        self.assertIn('<script src="/sound.js" defer></script>', html)
        self.assertRegex(html, r'<button class="tabkey sound-key" type="button" id="sound-key" aria-pressed="false">Sound</button>')
        self.assertEqual(d.STATIC["/sound.js"], ("sound.js", "text/javascript; charset=utf-8"))
        self.assertTrue((UI / "sound.js").is_file())
        app = (UI / "app.js").read_text()
        self.assertIn('if (e.key === "s" || e.key === "S") { $("sound-key").click(); return; }', app)
        # S sits after the monitor's own keys, so it never fires inside the modal.
        self.assertLess(app.index('e.key === "p") setTab'), app.index('$("sound-key").click()'))

    def test_radio_layer_only_listens(self):
        # The factory radio (a Konami-code easter egg beside the sound layer) is synthesised
        # from key names alone: no request, no audio file, nothing read from the page or state.
        js = (UI / "radio.js").read_text()
        for call in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "postMessage", ".src", "href",
                     "new Audio", "<audio", "decodeAudioData", "createMediaElementSource", "textContent", "e.detail", "dispatchEvent",
                     "preventDefault", "stopPropagation", "stopImmediatePropagation", "createElement", "location", "cookie"):
            self.assertNotIn(call, js, f"radio.js uses {call}")
        self.assertEqual(re.findall(r"getElementById\(([^)]*)\)", js), ['"radio-key"'])   # the badge is the only page element it touches
        self.assertEqual(sorted(set(re.findall(r'addEventListener\("([a-z]+)"', js))), ["click", "keydown"])
        self.assertIn('const CODE = ["arrowup", "arrowup", "arrowdown", "arrowdown", "arrowleft", "arrowright", "arrowleft", "arrowright", "b", "a"];', js)
        self.assertIn("if (e.repeat", js)                                           # holding a key doesn't advance the code
        self.assertIn("t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)", js)   # typing in a field doesn't count
        self.assertIn("let on = false,", js)                                        # off until the code is typed
        self.assertIn('const STORE = "fc1984-radio";', js)
        self.assertIn('if (localStorage.getItem(STORE) === "1") setOn(true, false);', js)
        self.assertRegex(js, r"try \{ if \(localStorage\.getItem\(STORE\)")        # storage may be blocked
        self.assertIn("const BPM = 72;", js)
        self.assertIn("LEVEL = 0.10;", js)
        self.assertIn("createConvolver", js)
        self.assertIn("setTimeout(tick, TICK)", js)                                 # look-ahead scheduler
        # Its own context, created only in wake(), which runs from a key press or the first gesture.
        self.assertEqual(js.count("new AC()"), 1)
        self.assertEqual(js.count("wake()"), 3)   # its definition, the code, the first-gesture hook
        self.assertEqual(js.count("clearTimeout(timer)"), 1)

    def test_radio_key_is_wired(self):
        html = (UI / "index.html").read_text()
        self.assertLess(html.index('<script src="/sound.js" defer></script>'), html.index('<script src="/radio.js" defer></script>'))
        self.assertRegex(html, r'<button class="tabkey radio-key" type="button" id="radio-key" aria-pressed="false" hidden>Radio</button>')
        self.assertEqual(d.STATIC["/radio.js"], ("radio.js", "text/javascript; charset=utf-8"))
        self.assertTrue((UI / "radio.js").is_file())
        css = (UI / "floor.css").read_text()
        self.assertIn(".lanes-h .radio-key", css)
        # The main page's key handler is untouched: the code rides on its own listener.
        app = (UI / "app.js").read_text()
        self.assertNotIn("radio", app)

    def test_lane_labels_are_one_shared_script(self):
        # #194: the "DEV 2" tag comes from one file both the floor and the modules read, loaded
        # (as a plain file, no inline script) before them, and drawn with textContent only.
        html = (UI / "index.html").read_text()
        self.assertEqual(d.STATIC["/lanelabel.js"], ("lanelabel.js", "text/javascript; charset=utf-8"))
        self.assertTrue((UI / "lanelabel.js").is_file())
        tag = '<script src="/lanelabel.js" defer></script>'
        self.assertEqual(html.count(tag), 1)
        self.assertLess(html.index(tag), html.index('<script src="/app.js" defer></script>'))
        self.assertLess(html.index(tag), html.index('<script src="/floor.js" defer></script>'))
        floor, app = (UI / "floor.js").read_text(), (UI / "app.js").read_text()
        self.assertNotIn("devTag", floor)
        self.assertIn("FactoryLabels.reviewTag(", floor + app)
        self.assertIn("reviewTag(reviews)", floor)
        self.assertIn("FactoryLabels.reviewTag(lane.reviews)", app)
        body = app[app.index("function laneRef"):app.index("function makeChip")]
        for call in ("fetch(", "innerHTML"):
            self.assertNotIn(call, body)

    def test_page_obeys_its_csp(self):
        html = (UI / "index.html").read_text()
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "inline script")
        self.assertIsNone(re.search(r"\sstyle=", html), "inline style attribute")
        self.assertIsNone(re.search(r"\son[a-z]+=", html), "inline event handler")
        self.assertNotIn("<style", html)
        self.assertIsNone(re.search(r"https?://", html + (UI / "app.css").read_text() + (UI / "floor.css").read_text()), "network URL")

    def test_help_links_resolve(self):
        # Every MANUAL and ? link opens the docs tab, and every ? names a heading of
        # the dashboard guide: renaming a heading there must not break one silently.
        html = (UI / "index.html").read_text()
        links = re.findall(r'<a class="[^"]*\b(?:help|manual)\b[^"]*" href="([^"]*)" target="([^"]*)"', html)
        self.assertEqual(len(links), len(re.findall(r'class="[^"]*\b(?:help|manual)\b', html)))
        self.assertGreaterEqual(len(links), 6)
        self.assertEqual({t for _h, t in links}, {"factory-docs"})
        page = d.load_docs(REPO)["index.html"].decode()
        for href, _t in links:
            self.assertTrue(href.startswith("/docs/"), href)
            anchor = href.partition("#")[2]
            if anchor:
                self.assertTrue(anchor.startswith("guide-dashboard:"), href)
                self.assertIn(f'id="{anchor}"', page, href)


if __name__ == "__main__":
    unittest.main()
