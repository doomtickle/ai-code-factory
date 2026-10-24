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
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dashboard as d

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
        self.assertEqual(lanes["developer"]["state"], "held")  # running, and its push is on the gate
        self.assertEqual(lanes["developer"]["model"], "haiku-4-5")
        self.assertEqual(lanes["developer"]["tail"][-1]["kind"], "tool")
        self.assertEqual(lanes["developer"]["tail"][-1]["text"], "broker git_push(repo=o/r, branch=factory/x)")
        self.assertEqual(lanes["reviewer"]["state"], "revise")  # done, verdict from its packet
        self.assertEqual(lanes["reviewer"]["cost"], 0.5)
        self.assertEqual(lanes["reviewer"]["model"], "opus-5-5")
        # Agent text stays text: the snapshot carries it verbatim for the page to insert with textContent.
        self.assertIn("<script>alert(1)</script>", lanes["reviewer"]["tail"][1]["text"])

    def test_desk_placard_fields(self):
        lanes = {lane["name"]: lane for lane in self.board.snapshot(self.fx.now)["lanes"]}
        self.assertEqual((lanes["developer"]["desk"], lanes["developer"]["reviews"]), ("x", ""))
        self.assertEqual((lanes["reviewer"]["desk"], lanes["reviewer"]["reviews"]), ("x", ""))  # names a branch, not a developer ref
        empty = Path(self.tmp.name) / "empty"
        for lane in d.Dashboard(empty, self.fx.roles, "proj").snapshot()["lanes"]:
            self.assertEqual((lane["desk"], lane["reviews"]), ("", ""))  # no job, no placard

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
        self.assertFalse(snap["bridge"])

    def test_events(self):
        events = self.board.snapshot(self.fx.now)["events"]
        # Oldest first; no "intent" record, nothing from three days ago.
        self.assertEqual([e["kind"] for e in events], ["JOB", "HELD", "DENY", "ALLOW"])
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


class DeskLabelTests(unittest.TestCase):
    def test_branch_topic_wins(self):
        for text, want in (
            ("Repo: ai-code-factory. Branch from main; commit to `factory/desk-placards`.", "desk placards"),
            ("Review factory-developer-lane2/factory/rate_limit.v2 now", "rate limit v2"),
            ("push factory/a-very-long-branch-name-indeed", "a very long branch"),
            ("Fix x, then factory/first and factory/second", "first"),
            ("Put the bundle in .factory/outgoing/ then push factory/fix-x", "fix x"),
            ("See https://example.com/o/factory/pull/1 and push factory/fix-x", "fix x"),
            ("Edit a/factory/b then factory-developer/factory/fix-y", "fix y"),
            ("Review origin/factory/fix-x", "fix x"),
            ("Diff main...factory/fix-x", "fix x"),
            ("git diff origin/main...factory-developer/factory/fix-y", "fix y"),
        ):
            self.assertEqual(d.desk_label(text), want)

    def test_branch_pattern_ignores_lookalikes(self):
        self.assertEqual(d.desk_label("Edit ai-code-factory/scripts today"), "Edit")
        self.assertEqual(d.desk_label("use factory/<topic> branches"), "use factory/ topic")  # a placeholder is no branch

    def test_fallback_first_words(self):
        for text, want in (
            ("Reject empty names in greet(), test it, and push", "Reject empty names"),
            ("Repo: passdesk-ops. Fix the **dashboard** crash", "Fix the dashboard"),
            ("Task: Fix the login crash", "Fix the login"),
            ("goal:make tests pass", "make tests pass"),
            ("Repo: ai-code-factory.git Fix x", "Fix x"), ("Repository: a.b. Do it", "Do it"),
            ("Repo: x;Fix y", "Fix y"), ("Project: demo; Tidy up", "Tidy up"),
            ("\n\n  Tidy the `docs`  \nsecond line", "Tidy the docs"),
            ("Supercalifragilisticexpialidocious", "Supercalifragilis…"),
            ("", ""), ("   \n ", ""), ("Repo: x.", ""),
        ):
            self.assertEqual(d.desk_label(text), want)

    def test_one_clean_line(self):
        label = d.desk_label("\x1b[31m\u202eevil\x00 <b>thing</b>\ttab")
        self.assertTrue(label.isprintable() and len(label) <= d.DESK_MAX, repr(label))
        self.assertNotRegex(label, r"[<>\u202e]")
        self.assertLessEqual(len(d.desk_label("word " * 50)), d.DESK_MAX)
        self.assertEqual(d.desk_label(None), "")

    def test_reviewed_lane(self):
        for text, want in (
            ("Review factory-developer/factory/x", "developer"),
            ("Review factory-developer-lane3/factory/x and factory-developer/factory/y", "developer-lane3"),
            ("Review factory/x", ""), ("factory-reviewer/factory/x", ""),
            ("factory-developer-lane/factory/x", ""), ("xfactory-developer/factory/x", ""), ("", ""),
        ):
            self.assertEqual(d.reviewed_lane(text), want, text)


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
        # We can't tell which developer lane is waiting: both show held, and say why.
        self.assertEqual((lanes["developer"]["state"], lanes["developer-lane2"]["state"]), ("held", "held"))
        self.assertIn("one of 2 running developer lanes", lanes["developer-lane2"]["held_note"])
        self.assertEqual(lanes["developer-lane3"]["state"], "idle")
        self.assertEqual((gate[0]["role"], gate[0]["lanes"]), ("developer", ["developer", "developer-lane2"]))

    def test_one_running_lane_of_several(self):
        # The hold belongs to lane2, the only developer lane running, though the card says "developer".
        write_lane(self.logs, "developer-lane2", "20261019-000002-bbbbbb", self.now)
        self.card("c1", "developer")
        lanes, gate = self.snap()
        self.assertEqual((lanes["developer"]["state"], lanes["developer-lane2"]["state"]), ("idle", "held"))
        self.assertEqual(lanes["developer-lane2"]["held_note"], "")
        self.assertEqual(gate[0]["lanes"], ["developer-lane2"])

    def test_decided_and_expired_cards_hold_nothing(self):
        import approvals
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        self.card("declined", "developer", nonce="n")
        self.card("expired", "developer", expires=self.now - 5)
        card = next(c for c in approvals.read_cards(self.state / "broker") if c["id"] == "declined")
        approvals.write_decision(self.state / "broker", card, "decline", "tester", "no")
        lanes, gate = self.snap()
        self.assertEqual(lanes["developer"]["state"], "running")
        self.assertEqual(lanes["developer"]["held_note"], "")
        self.assertEqual({c["id"]: (c["decided"], c["seconds_left"] < 0) for c in gate},
                         {"declined": ("decline", False), "expired": (None, True)})
        # A live, undecided card beside them still holds the lane.
        self.card("live", "developer")
        self.assertEqual(self.snap()[0]["developer"]["state"], "held")
        # So does the lane detail (Monitor).
        detail = d.Dashboard(self.state, self.roles, "proj").lane_detail("developer", self.now)
        self.assertEqual(detail["state"], "held")

    def queue(self, cid, nonce="n"):
        folder = self.state / "broker" / "approvals" / "queued"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{cid}.json").write_text(json.dumps({"id": cid, "nonce": nonce, "tool": "git_push"}))

    def test_queued_card_holds_nothing_and_is_marked(self):
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        self.card("q1", "developer", nonce="n")
        self.queue("q1")
        lanes, gate = self.snap()
        self.assertEqual(lanes["developer"]["state"], "running")  # the agent's call already returned
        self.assertEqual(lanes["developer"]["held_note"], "")
        self.assertEqual([(c["id"], c["queued"], c["decided"]) for c in gate], [("q1", True, None)])
        # A live card beside it still holds the lane, and is not marked queued.
        self.card("live", "developer", nonce="m")
        lanes, gate = self.snap()
        self.assertEqual(lanes["developer"]["state"], "held")
        self.assertEqual({c["id"]: c["queued"] for c in gate}, {"q1": True, "live": False})

    def test_queued_file_for_another_card_marks_nothing(self):
        write_lane(self.logs, "developer", "20261019-000001-aaaaaa", self.now)
        self.card("q1", "developer", nonce="new")
        self.queue("q1", nonce="old")  # an earlier card with the same id
        lanes, gate = self.snap()
        self.assertEqual(lanes["developer"]["state"], "held")
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


class QueuedEventTests(unittest.TestCase):
    def test_queue_events_are_printed(self):
        ev = d.broker_event({"ts": "2026-10-19T06:00:00Z", "event": "approval-queued", "id": "c1", "tool": "git_push",
                             "repo": "o/r", "branch": "factory/x"})
        self.assertEqual((ev["kind"], ev["red"], ev["text"]), ("QUEUED", True, "git_push o/r factory/x"))
        ev = d.broker_event({"event": "approval-write-done", "id": "c1", "tool": "github_create_pr", "repo": "o/r",
                             "result": "\n\nhttps://github.com/o/r/pull/9\nsecond line"})
        self.assertEqual((ev["kind"], ev["red"]), ("WRITTEN", False))
        self.assertTrue(ev["text"].endswith("· https://github.com/o/r/pull/9"), ev["text"])
        self.assertNotIn("second line", ev["text"])
        ev = d.broker_event({"event": "approval-write-failed", "id": "c1", "tool": "git_push", "repo": "o/r",
                             "reason": "denied: branch moved"})
        self.assertEqual((ev["kind"], ev["red"]), ("FAILED", True))
        self.assertTrue(ev["text"].endswith("· denied: branch moved"), ev["text"])

    def test_queued_call_record_prints_nothing(self):
        # The call itself is audited with decision "queued"; its approval-queued event is the line.
        self.assertIsNone(d.broker_event({"tool": "git_push", "role": "developer", "decision": "queued"}))

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
        for name in ("app.js", "floor.js", "sound.js"):
            js = (UI / name).read_text()
            for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(sink, js, f"{name} uses {sink}")

    def test_floor_only_displays(self):
        # The factory floor draws from the state app.js hands it: it makes no request
        # of its own, so it can reach nothing the main page can't (scripts/CLAUDE.md).
        js = (UI / "floor.js").read_text()
        for call in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "postMessage", ".src =", "href"):
            self.assertNotIn(call, js, f"floor.js uses {call}")
        html = (UI / "index.html").read_text()
        self.assertIn('<script src="/floor.js" defer></script>', html)
        self.assertIn('<link rel="stylesheet" href="/floor.css">', html)

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
        # The redesigned floor: no foreman's booth, broker gatehouse or dock; a Coordinator
        # console, a bay per role and rails between them, all drawn from lane state.
        js = (UI / "floor.js").read_text()
        css = (UI / "floor.css").read_text()
        for gone in ("hue-f", "hue-g", "DOCK_X", "DOCK_Y", "POLICY · AUDIT", "TO DOCK", "OUTBOUND ▸ BROKER", "docked(", "boothNote", '"FOREMAN"'):
            self.assertNotIn(gone, js, gone)
        for gone in (".booth", ".gatehouse", ".dock", ".foreman", "hue-f", "hue-g", "roll-dock"):
            self.assertNotIn(gone, css, gone)
        for label in ('"COORDINATOR"', '"DEVELOPERS"', '"REVIEWERS"', '"OUTBOUND ▸ OUT"', '"DISPATCH"', '"HAND-OFF"'):
            self.assertIn(label, js, label)
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
        for name in ("passed", "confiscated", "docked"):
            self.assertIn(name, js)
        self.assertIn("document.addEventListener(`factory-floor:${name}`, () => cue(name));", js)
        self.assertIn("let on = false,", js)                                  # off until the key is pressed
        self.assertIn('if (localStorage.getItem(STORE) === "1") setOn(true, false);', js)
        self.assertRegex(js, r"try \{ if \(localStorage\.getItem\(STORE\)")      # storage may be blocked
        self.assertIn("if (!AC) { key.disabled = true; return; }", js)       # no Web Audio: harmless
        # The context is only created in wake(), which runs from a click or a key/pointer press.
        self.assertEqual(js.count("new AC()"), 1)
        self.assertEqual(js.count("wake()"), 3)   # its definition, the key's click, the first-gesture hook
        self.assertIn("GAP = 0.25", js)

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
