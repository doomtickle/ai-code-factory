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
import dashboard_term
import dashboard_demo as demo
import dashboard_jobs as jobs_mod
import dashboard_setup
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
        self.assertEqual(lanes["developer"]["desk"], "x")
        self.assertEqual(lanes["reviewer"]["desk"], "x")  # names a branch, not a developer ref
        empty = Path(self.tmp.name) / "empty"
        for lane in d.Dashboard(empty, self.fx.roles, "proj").snapshot()["lanes"]:
            self.assertEqual(lane["desk"], "")  # no job, no placard

    def test_branch_field(self):
        log = self.fx.state / "dispatch" / "logs" / "developer.log"
        t = hms(self.fx.now - 30)
        def branch():
            return next(x for x in self.board.snapshot(self.fx.now)["lanes"] if x["name"] == "developer")["branch"]
        lanes = {lane["name"]: lane for lane in self.board.snapshot(self.fx.now)["lanes"]}
        self.assertEqual((lanes["developer"]["branch"], lanes["reviewer"]["branch"]), ("factory/x", ""))  # the task names it
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
        self.assertEqual(lane["desk"], "rate limit")

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

    def test_events_carry_the_job_and_card_they_are_about(self):
        events = self.board.snapshot(self.fx.now)["events"]
        job = next(e for e in events if e["kind"] == "JOB")
        self.assertEqual((job["job_id"], job["lane"]), ("20261019-000002-bbbbbb", "reviewer"))
        self.assertEqual(next(e for e in events if e["kind"] == "STAGED")["card_id"], "c0ffee")
        for kind in ("DENY", "ALLOW"):
            self.assertNotIn("card_id", next(e for e in events if e["kind"] == kind))
            self.assertNotIn("job_id", next(e for e in events if e["kind"] == kind))

    def test_job_event_ids_are_dropped_unless_well_formed(self):
        # A job id or lane that is not the bridge's own pattern never reaches the page as an id.
        t = self.fx.now
        for rec in ({"job_id": "../../x", "lane": "reviewer"}, {"job_id": "20261019-000002-bbbbbb", "lane": "Bad Lane"},
                    {"job_id": "20261019-000002-bbbbbb", "lane": None}):
            (self.fx.state / "dispatch" / "audit.jsonl").write_text(json.dumps(dict(
                {"ts": iso(t - 60), "event": "job-end", "role": "reviewer", "status": "done", "cost_usd": 0.5}, **rec)) + "\n")
            board = d.Dashboard(self.fx.state, self.fx.roles, "proj")
            job = next(e for e in board.snapshot(t)["events"] if e["kind"] == "JOB")
            self.assertNotIn("job_id", job, rec)
            self.assertNotIn("lane", job, rec)

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


class SaidTests(unittest.TestCase):
    """The Coordinator's spoken lines, checked on the way to the main page (doomtickle/passdesk-ops#216)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        (self.state / "dispatch").mkdir(parents=True)
        self.path = self.state / "dispatch" / "said.json"
        self.board = d.Dashboard(self.state, Path(tmp.name) / "roles", "proj")

    def write(self, *lines):
        self.path.write_text(json.dumps({"said": list(lines)}))

    def test_the_reader_and_the_bridge_agree_on_the_limit(self):
        bridge = (Path(d.__file__).resolve().parent.parent / "dispatch" / "server.py").read_text()
        self.assertEqual(int(re.search(r"^MAX_SAY_CHARS = (\d+)", bridge, re.M).group(1)), d.SAID_MAX_TEXT)

    def test_absent_is_empty(self):
        self.assertEqual(self.board.said(), [])
        self.assertEqual(self.board.snapshot()["said"], [])

    def test_a_good_line_reaches_the_state_with_only_its_id_and_text(self):
        self.write({"id": "ab12cd34ef56", "t": 1.5, "text": "On it.", "extra": "no"})
        self.assertEqual(self.board.snapshot()["said"], [{"id": "ab12cd34ef56", "text": "On it."}])

    def test_bad_lines_are_dropped_and_good_ones_kept(self):
        good = {"id": "ab12cd34ef56", "text": "Fine."}
        self.write({"id": "../x", "text": "bad id"}, {"id": 5, "text": "bad id"}, {"id": "ab12cd34ef57", "text": 7},
                   {"id": "ab12cd34ef58", "text": ""}, {"id": "ab12cd34ef59", "text": "x" * (d.SAID_MAX_TEXT + 1)}, "nope", good)
        self.assertEqual(self.board.said(), [good])

    def test_only_the_newest_eight(self):
        self.write(*[{"id": "%012x" % n, "text": "Line."} for n in range(20)])
        self.assertEqual([l["id"] for l in self.board.said()], ["%012x" % n for n in range(12, 20)])

    def test_a_broken_file_is_empty(self):
        for raw in ("{not json", "[]", '{"said": 3}', '{"other": []}', "null"):
            self.path.write_text(raw)
            self.assertEqual(self.board.said(), [], raw)

    def test_a_symlink_is_not_followed(self):
        outside = self.state / "outside.json"
        outside.write_text(json.dumps({"said": [{"id": "ab12cd34ef56", "text": "Followed."}]}))
        self.path.symlink_to(outside)
        self.assertEqual(self.board.said(), [])

    def test_a_file_cut_at_the_size_limit_is_empty(self):
        self.path.write_text('{"said": [' + " " * d.SAID_MAX_BYTES + "]}")
        self.assertEqual(self.board.said(), [])


class ShownTests(unittest.TestCase):
    """The Coordinator's posted block (dispatch/server.py tool_show), checked on the way to the main page."""

    GOOD = {"id": "ab12cd34ef56", "t": 1.5, "text": "1. Merge x\n   git merge x"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        (self.state / "dispatch").mkdir(parents=True)
        self.path = self.state / "dispatch" / "shown.json"
        self.board = d.Dashboard(self.state, Path(tmp.name) / "roles", "proj")

    def write(self, rec):
        self.path.write_text(json.dumps(rec, ensure_ascii=False))

    def test_absent_is_none_and_the_state_carries_it(self):
        self.assertIsNone(self.board.shown())
        self.assertIsNone(self.board.snapshot()["shown"])

    def test_a_good_block_reaches_the_state_with_only_id_time_and_text(self):
        self.write({**self.GOOD, "extra": "no"})
        self.assertEqual(self.board.snapshot()["shown"], self.GOOD)

    def test_a_missing_or_odd_time_becomes_none_and_the_block_is_kept(self):
        for t in (None, "soon", True, float("inf")):
            self.write({"id": "ab12cd34ef56", "t": t, "text": "x"})
            self.assertEqual(self.board.shown(), {"id": "ab12cd34ef56", "t": None, "text": "x"}, repr(t))

    def test_bad_files_are_ignored(self):
        bad = [{"id": "../x", "text": "bad id"}, {"id": "AB12CD34EF56", "text": "upper"}, {"id": 5, "text": "n"},
               {"id": "ab12cd34ef56", "text": 7}, {"id": "ab12cd34ef56", "text": ""}, {"id": "ab12cd34ef56", "text": " \n "},
               {"id": "ab12cd34ef56", "text": "x" * (d.SHOWN_MAX_TEXT + 1)},
               {"id": "ab12cd34ef56", "text": "\n".join(["a"] * (d.SHOWN_MAX_LINES + 1))}, {"shown": self.GOOD}, [self.GOOD], "nope", None, 3]
        for rec in bad:
            self.write(rec)
            self.assertIsNone(self.board.shown(), repr(rec))
        # JSON escapes decode to lone surrogates, which cannot be encoded as UTF-8 for /api/state.
        for esc in ("ok \\ud800 high", "\\udfff", "\\ud83d\\ude00 \\ud83d", "\\ude00\\ud83d"):
            self.path.write_text('{"id": "ab12cd34ef56", "text": "%s"}' % esc)
            self.assertIsNone(self.board.shown(), esc)
        self.path.write_text('{"id": "ab12cd34ef56", "text": "\\ud83d\\ude00 pair"}')
        self.assertEqual(self.board.shown()["text"], "\U0001f600 pair")
        for raw in ("{not json", "", "\x00"):
            self.path.write_text(raw)
            self.assertIsNone(self.board.shown(), repr(raw))

    def test_blocks_at_the_limits_are_kept(self):
        self.write({"id": "ab12cd34ef56", "text": "x" * d.SHOWN_MAX_TEXT})
        self.assertIsNotNone(self.board.shown())
        self.write({"id": "ab12cd34ef56", "text": "\n".join(["a"] * d.SHOWN_MAX_LINES)})
        self.assertIsNotNone(self.board.shown())

    def test_hostile_text_is_neutralised_control_removed_and_hidden_characters_shown(self):
        hostile = "ls\x1b[2J\x07 \u202e-rf\x00 ~\u200b\ufeff\u2028\x85 ok\r\nnext\ttab\rend\u2066"
        self.write({"id": "ab12cd34ef56", "text": hostile})
        text = self.board.shown()["text"]
        self.assertEqual(text, "ls[2J ⟨U+202E⟩-rf ~⟨U+200B⟩⟨U+FEFF⟩ ok\nnext  tab\nend⟨U+2066⟩")
        self.assertNotRegex(text, "[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]")

    def test_newlines_are_kept_and_blank_edges_trimmed(self):
        self.write({"id": "ab12cd34ef56", "text": "\n\na\n\nb\n"})
        self.assertEqual(self.board.shown()["text"], "a\n\nb")

    def test_a_block_that_is_only_removed_characters_is_none(self):
        self.write({"id": "ab12cd34ef56", "text": "\x00\x07\n\x1b"})
        self.assertIsNone(self.board.shown())

    def test_a_symlink_is_not_followed(self):
        outside = self.state / "outside.json"
        outside.write_text(json.dumps(self.GOOD))
        self.path.symlink_to(outside)
        self.assertIsNone(self.board.shown())

    def test_a_file_cut_at_the_size_limit_is_none(self):
        self.path.write_text('{"id": "ab12cd34ef56", "text": "' + " " * d.SHOWN_MAX_BYTES + '"}')
        self.assertIsNone(self.board.shown())


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
        self.assertEqual(ev["card_id"], "c1")
        ev = d.broker_event({"event": "approval-write-failed", "id": "c1", "tool": "git_push", "repo": "o/r",
                             "reason": "denied: branch moved"})
        self.assertEqual((ev["kind"], ev["red"]), ("FAILED", True))
        self.assertTrue(ev["text"].endswith("· denied: branch moved"), ev["text"])

    def test_card_id_only_when_it_is_a_card_id(self):
        for cid in ("../x", "a b", "", "x" * 65, 7, None, ["c1"]):
            ev = d.broker_event({"event": "approval-requested", "id": cid, "tool": "git_push", "repo": "o/r"})
            self.assertNotIn("card_id", ev, cid)
        ev = d.broker_event({"event": "approval-requested", "id": "x" * 64, "tool": "git_push", "repo": "o/r"})
        self.assertEqual(ev["card_id"], "x" * 64)
        # A denied call is not about a card, whatever else it carries.
        ev = d.broker_event({"tool": "web_fetch", "role": "developer", "decision": "deny", "id": "c1", "reason": "no"})
        self.assertNotIn("card_id", ev)

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
        resp, body = self.get("/api/lane?name=reviewer&job=20261019-000002-bbbbbb")
        self.assertEqual(resp.status, 200)
        detail = json.loads(body)
        self.assertIn("reviewer job 20261019-000002-bbbbbb", detail["packet"])
        self.assertEqual((detail["job"], detail["jobs"]), ("20261019-000002-bbbbbb", ["20261019-000002-bbbbbb"]))
        # A job that did not run on that lane (another lane's, or none at all) is a 404 that says so.
        for name, job in (("developer", "20261019-000002-bbbbbb"), ("reviewer", "20261019-000077-abcdef")):
            resp, body = self.get(f"/api/lane?name={name}&job={job}")
            self.assertEqual(resp.status, 404, (name, job))
            self.assertIn("did not run on this lane", body.decode())
        for bad in ("../x", "20261019-000002-bbbbbb%2f..", "x", "20261019-000002-BBBBBB"):
            self.assertEqual(self.get(f"/api/lane?name=developer&job={bad}")[0].status, 400, bad)
        self.assertEqual(self.get("/api/lane?name=nobody&job=20261019-000002-bbbbbb")[0].status, 404)
        self.assertEqual(self.get("/api/lane?name=reviewer&job=20261019-000002-bbbbbb", cookie=False)[0].status, 403)

    def test_token_becomes_cookie(self):
        resp, _ = self.get(f"/?token={TOKEN}", cookie=False)
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/")
        cookie = resp.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith(f"factory_dashboard_{self.port}={TOKEN};"), cookie)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)

    def test_refused_without_token(self):
        for path in ("/", "/api/state", "/classic", "/?token=wrong", "/api/lane?name=developer"):
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
        self.assertIn(b'id="plate-name"', body)
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
        # its CSS carries the same tokens as the console, inlined from theme.css at build time.
        theme = (UI / "theme.css").read_text(encoding="utf-8")
        _resp, css = self.get("/docs/docs.css")
        self.assertIn(theme, css.decode())

    def test_root_is_the_thoughtfield(self):
        resp, body = self.get("/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, (UI / "field.html").read_bytes())
        self.assertEqual(self.get("/field")[1], body)

    def test_classic_redirects_to_the_thoughtfield(self):
        resp, _ = self.get("/classic")
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/"))
        self.assertEqual(self.get("/classic", cookie=False)[0].status, 403)
        self.assertEqual(self.get("/classic", host="evil.example")[0].status, 403)

    def test_removed_classic_files_are_gone(self):
        for path in ("/app.js", "/app.css", "/floor.js", "/floor.css", "/sound.js", "/radio.js",
                     "/lanelabel.js", "/look.js", "/theme.css", "/index.html", "/term.css", "/cu.css"):
            self.assertEqual(self.get(path)[0].status, 404, path)

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

    def test_a_deleted_or_emptied_notes_file_is_restart_by_comparing_with_the_snapshot(self):
        self.record()
        self.snapshot_start()
        snap = self.fx.state / "dispatch" / "instructions"
        snap.mkdir()
        (snap / "reviewer.md").write_bytes(b"BASE\n\n# This project (proj)\nshared\n")
        (snap / "developer.md").write_bytes(b"BASE\n\n# This project (proj)\nshared\n\n# This project (proj), for the developer\nown\n")
        shared = self.config("projects/proj.md", "shared\n")
        own = self.config("projects/proj.developer.md", "own\n")
        self.assertIsNone(self.service()["due"])  # the same text, however old or new the files are
        own.unlink()
        self.assertEqual(self.service(), {"due": "restart", "lines": ["project notes proj.developer.md changed"]})
        self.config("projects/proj.developer.md", "own\n")
        shared.write_text("")
        self.assertEqual(self.service()["lines"], ["project notes proj.md changed"])
        shared.write_text("shared\n")
        self.config("projects/proj.reviewer.md", "new\n")
        self.assertEqual(self.service()["lines"], ["project notes proj.reviewer.md changed"])

    def test_a_snapshot_that_is_a_symlink_is_not_followed_by_the_lamp(self):
        self.record()
        self.snapshot_start()
        snap = self.fx.state / "dispatch" / "instructions"
        snap.mkdir()
        elsewhere = self.fx.state / "elsewhere.md"
        elsewhere.write_bytes(b"BASE\n\n# This project (proj)\nshared\n")
        for role in ("developer", "reviewer"):
            (snap / f"{role}.md").symlink_to(elsewhere)
        self.config("projects/proj.md", "shared\n")
        old = time.time() - 500
        os.utime(Path(self.paths.config) / "projects" / "proj.md", (old, old))
        self.assertIsNone(self.service()["due"])  # read as no snapshot: the old file is not newer than the start

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


class LaneJobsTests(unittest.TestCase):
    """The PACKET tab's job list (doomtickle/passdesk-ops#191): /api/lane returns the lane's past jobs,
    newest first and capped, the packet of the one asked for, and what the monitor shows about it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))
        self.now = self.fx.now
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")
        self.dispatch = self.fx.state / "dispatch"

    def tearDown(self):
        self.tmp.cleanup()

    def job_id(self, n):
        return f"20261019-{n // 3600:02d}{n // 60 % 60:02d}{n % 60:02d}-{n:06x}"

    def write_jobs(self, records):
        (self.dispatch / "jobs.json").write_text(json.dumps({"version": 1, "jobs": records}))

    def rec(self, n, lane="reviewer", status="done", **extra):
        return dict({"id": self.job_id(n), "role": "reviewer", "lane": lane, "status": status, "task": f"Review number {n}",
                     "started": self.now - 1000 + n, "finished": self.now - 940 + n, "model": "claude-opus-5-5",
                     "cost_usd": 0.25}, **extra)

    def packet(self, n, text=None):
        (self.dispatch / "packets" / f"{self.job_id(n)}.md").write_text(text or f"# reviewer job {self.job_id(n)}: done\n")

    def test_a_lane_with_no_job_has_an_empty_list(self):
        (self.fx.roles / "idle").mkdir()
        (self.fx.roles / "idle" / "role.conf").write_text('LANES="1"\n')
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")   # discovers the lane made above
        detail = self.board.lane_detail("idle", self.now)
        self.assertEqual(detail["jobs"], [])
        self.assertIsNone(detail["sel"])
        self.assertFalse(detail["pending"])
        self.assertEqual(detail["packet"], "(no job on this lane yet)")
        with self.assertRaises(d.NoSuchJob):
            self.board.lane_detail("idle", self.now, job=self.job_id(1))

    def test_a_lane_with_one_job_selects_it(self):
        detail = self.board.lane_detail("reviewer", self.now)
        self.assertEqual((detail["job"], detail["jobs"]), ("20261019-000002-bbbbbb", ["20261019-000002-bbbbbb"]))
        self.assertIn("- Verdict: REVISE", detail["packet"])
        self.assertFalse(detail["pending"])
        sel = detail["sel"]
        self.assertEqual((sel["id"], sel["state"], sel["status"], sel["model"], sel["cost"], sel["elapsed"]),
                         ("20261019-000002-bbbbbb", "revise", "done", "opus-5-5", 0.5, 60))

    def test_many_jobs_come_newest_first_and_capped(self):
        self.write_jobs([self.rec(n) for n in range(1, 61)] + [self.rec(99, lane="developer")])
        for n in (1, 60, 55):
            self.packet(n)
        detail = self.board.lane_detail("reviewer", self.now)
        ids = detail["jobs"]
        self.assertEqual(ids[0], self.job_id(60))
        self.assertEqual(len(ids), jobs_mod.LIST_MAX)
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertNotIn(self.job_id(99), ids)            # another lane's job
        self.assertEqual(detail["job"], self.job_id(60))   # no job asked for: the newest
        self.assertEqual(detail["sel"]["task"], "Review number 60")
        # A job past the cap is still served, and comes last on the list.
        old = self.board.lane_detail("reviewer", self.now, job=self.job_id(1))
        self.assertEqual((old["job"], old["jobs"][-1], len(old["jobs"])), (self.job_id(1), self.job_id(1), jobs_mod.LIST_MAX + 1))
        self.assertIn(self.job_id(1), old["packet"])
        mid = self.board.lane_detail("reviewer", self.now, job=self.job_id(55))
        self.assertEqual((mid["job"], len(mid["jobs"])), (self.job_id(55), jobs_mod.LIST_MAX))
        # Another lane's job is refused, not served.
        with self.assertRaises(d.NoSuchJob):
            self.board.lane_detail("reviewer", self.now, job=self.job_id(99))

    def test_jobs_come_from_the_index_the_audit_and_the_transcript(self):
        self.write_jobs([self.rec(5), dict(self.rec(6), status="running", finished=None, cost_usd=None)])
        with open(self.dispatch / "audit.jsonl", "a") as f:
            f.write(json.dumps(
                {"ts": "2026-10-19T00:00:00Z", "event": "job-end", "job_id": self.job_id(4), "lane": "reviewer", "status": "failed",
                 "elapsed_seconds": 7, "cost_usd": 0.1}) + "\n" + json.dumps(
                {"ts": "2026-10-19T00:00:00Z", "event": "job-end", "job_id": self.job_id(3), "lane": "developer", "status": "done"}) + "\n")
        ids = self.board.lane_detail("reviewer", self.now)["jobs"]
        # The index, the audit's (this lane's only), and the transcript's job, newest id first.
        self.assertEqual(ids, [self.job_id(6), self.job_id(5), self.job_id(4), "20261019-000002-bbbbbb"])
        failed = self.board.lane_detail("reviewer", self.now, job=self.job_id(4))
        self.assertEqual((failed["sel"]["state"], failed["sel"]["cost"], failed["sel"]["elapsed"]), ("failed", 0.1, 7))

    def test_a_missing_packet_file_says_so(self):
        self.write_jobs([self.rec(5), self.rec(6)])
        self.packet(5)
        detail = self.board.lane_detail("reviewer", self.now, job=self.job_id(6))
        self.assertIn(f"no packet for job {self.job_id(6)}", detail["packet"])
        self.assertFalse(detail["pending"])   # a finished job is not pending: the file is just not there
        self.assertEqual(detail["sel"]["state"], "done")
        self.assertIn("# reviewer job", self.board.lane_detail("reviewer", self.now, job=self.job_id(5))["packet"])

    def test_a_symlinked_or_oversized_packet_is_not_followed_or_read_whole(self):
        self.write_jobs([self.rec(5), self.rec(6)])
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("TOP SECRET")
        (self.dispatch / "packets" / f"{self.job_id(5)}.md").symlink_to(secret)
        self.assertNotIn("TOP SECRET", self.board.lane_detail("reviewer", self.now, job=self.job_id(5))["packet"])
        self.packet(6, "x" * (d.PACKET_MAX + 10))
        self.assertEqual(len(self.board.lane_detail("reviewer", self.now, job=self.job_id(6))["packet"]), d.PACKET_MAX)

    def test_a_running_job_is_pending_with_its_task_model_elapsed_and_cost(self):
        running = dict(self.rec(5), status="running", finished=None, cost_usd=0.31, started=self.now - 90, model="claude-sonnet-5-5")
        self.write_jobs([running, self.rec(4)])
        self.packet(4)
        (self.dispatch / "logs" / "reviewer.log").write_text(
            f"{hms(self.now - 90)}  \u25b6 job {self.job_id(5)} from the Coordinator\n{hms(self.now - 90)}     > Review number 5\n")
        detail = self.board.lane_detail("reviewer", self.now)
        self.assertEqual(detail["job"], self.job_id(5))
        self.assertTrue(detail["pending"])
        sel = detail["sel"]
        self.assertEqual((sel["state"], sel["task"], sel["model"], sel["cost"], sel["elapsed"]),
                         ("running", "Review number 5", "sonnet-5-5", 0.31, 90))
        self.assertRegex(sel["when"], r"^\d\d:\d\d$")
        # Stepping back to the finished job is not pending; its transcript tail is the same live one.
        back = self.board.lane_detail("reviewer", self.now, job=self.job_id(4))
        self.assertFalse(back["pending"])
        self.assertEqual(back["tail"], detail["tail"])
        # With the bridge down nothing is running: interrupted, and no pending view.
        (self.dispatch / "pid").unlink()
        down = self.board.lane_detail("reviewer", self.now)
        self.assertEqual((down["sel"]["state"], down["pending"]), ("interrupted", False))

    def test_agent_text_stays_text_and_one_line(self):
        task = "<img src=x onerror=alert(1)>\n  second   line"
        self.write_jobs([self.rec(5, task=task)])
        self.packet(5, "<script>alert(1)</script>")
        detail = self.board.lane_detail("reviewer", self.now, job=self.job_id(5))
        self.assertEqual(detail["packet"], "<script>alert(1)</script>")
        self.assertEqual(detail["sel"]["task"], "<img src=x onerror=alert(1)> second line")

    def test_a_bad_index_adds_no_jobs(self):
        for text in ("not json", json.dumps({"version": 2, "jobs": [self.rec(5)]}), json.dumps({"version": 1, "jobs": "x"}),
                     json.dumps({"version": 1, "jobs": [5, None, dict(self.rec(5), id="../x"), dict(self.rec(6), started="x", model=7, cost_usd=True)]})):
            (self.dispatch / "jobs.json").write_text(text)
            ids = self.board.lane_detail("reviewer", self.now)["jobs"]
            self.assertEqual(ids[-1], "20261019-000002-bbbbbb", text)
            self.assertNotIn("../x", ids)
        sel = self.board.lane_detail("reviewer", self.now, job=self.job_id(6))["sel"]
        self.assertEqual((sel["model"], sel["cost"], sel["elapsed"]), ("", None, None))

    def test_the_demo_lane_has_two_packets_and_a_pending_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo.build(Path(tmp))
            board = d.Dashboard(Path(tmp), d.DOCS_ROOT / "roles", demo.PROJECT, demo=True)
            detail = board.lane_detail("developer")
            self.assertGreaterEqual(len(detail["jobs"]), 3)
            self.assertTrue(detail["pending"])                     # the running job writes its packet when it ends
            self.assertEqual(detail["sel"]["state"], "running")
            with_packets = [j for j in detail["jobs"] if not board.lane_detail("developer", job=j)["packet"].startswith("(no packet")]
            self.assertGreaterEqual(len(with_packets), 2)
            for job in with_packets:
                self.assertIn(f"job {job}", board.lane_detail("developer", job=job)["packet"].split("\n")[0])

    def test_the_lane_packet_endpoint_takes_only_a_job_of_that_lane(self):
        detail = self.board.lane_detail("reviewer", self.now, job="20261019-000002-bbbbbb")
        self.assertIn("reviewer job 20261019-000002-bbbbbb", detail["packet"])
        for lane, job in (("developer", "20261019-000002-bbbbbb"), ("reviewer", "20261019-000077-abcdef"),
                          ("reviewer", "../../broker/audit"), ("reviewer", "20261019-000002-bbbbbb/../x"),
                          ("reviewer", "x"), ("reviewer", "20261019-000002-BBBBBB")):
            with self.assertRaises(d.NoSuchJob, msg=(lane, job)):
                self.board.lane_detail(lane, self.now, job=job)
        self.assertIsNone(self.board.lane_detail("nobody", self.now, job="20261019-000002-bbbbbb"))

    def test_read_regular_refuses_links_fifos_and_missing_files(self):
        real = Path(self.tmp.name) / "real"
        real.write_bytes(b"abcdef")
        link, fifo = Path(self.tmp.name) / "link", Path(self.tmp.name) / "fifo"
        link.symlink_to(real)
        os.mkfifo(fifo)
        self.assertEqual(jobs_mod.read_regular(real, 3), b"abc")
        for path in (link, fifo, Path(self.tmp.name) / "nope"):
            self.assertIsNone(jobs_mod.read_regular(path, 10), path.name)

    def test_a_symlinked_job_index_is_not_read(self):
        real = self.dispatch / "jobs.json"
        other = Path(self.tmp.name) / "elsewhere.json"
        other.write_text(json.dumps({"version": 1, "jobs": [{"id": "20261019-000077-abcdef", "lane": "developer", "status": "done"}]}))
        real.unlink(missing_ok=True)
        real.symlink_to(other)
        self.assertEqual(jobs_mod._index(self.fx.state, "developer"), {})

    # The DIFF tab: /api/diff serves the sidecar the dispatch bridge wrote (packets/<job>.diff.json).
    PATCH = ("diff --git a/f.txt b/f.txt\nindex 1111111..2222222 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+2\n three\n"
             "diff --git a/g.txt b/g.txt\nnew file mode 100644\nindex 0000000..3333333\n--- /dev/null\n+++ b/g.txt\n@@ -0,0 +1 @@\n+new\n")

    def diff_file(self, n, **over):
        branch = dict({"ref": "factory-developer/factory/x", "repo": ".", "commit": "8d02e47", "base": "a" * 40, "tip": "8d02e47" + "b" * 33,
                       "patch": self.PATCH, "truncated": False, "error": ""}, **over)
        doc = {"version": 1, "job": self.job_id(n), "max_bytes": 1000000, "max_lines": 20000, "branches": [branch]}
        (self.dispatch / "packets" / f"{self.job_id(n)}.diff.json").write_text(json.dumps(doc))

    def test_a_job_diff_comes_back_as_parsed_files(self):
        self.diff_file(5)
        out = self.board.job_diff(self.job_id(5))
        self.assertEqual((out["state"], out["message"], len(out["branches"])), ("ok", "", 1))
        b = out["branches"][0]
        self.assertEqual((b["ref"], b["commit"], b["tip"], b["truncated"], b["error"]), ("factory-developer/factory/x", "8d02e47", "8d02e47bbbbb", False, ""))
        self.assertEqual([(f["path"], f["status"], f["adds"], f["dels"]) for f in b["files"]], [("f.txt", "M", 1, 1), ("g.txt", "A", 1, 0)])
        self.assertEqual((b["adds"], b["dels"]), (2, 1))
        self.assertEqual(b["files"][0]["hunks"][0]["lines"], [[" ", "one"], ["-", "two"], ["+", "2"], [" ", "three"]])

    def test_a_job_with_no_diff_file_says_so(self):
        out = self.board.job_diff(self.job_id(5))
        self.assertEqual((out["state"], out["message"], out["branches"]), ("none", "NO DIFF FOR THIS JOB", []))

    def test_a_symlinked_oversized_or_unreadable_diff_is_not_shown(self):
        packets = self.dispatch / "packets"
        secret = Path(self.tmp.name) / "secret.json"
        secret.write_text(json.dumps({"version": 1, "job": self.job_id(5), "branches": [{"ref": "TOP SECRET", "patch": self.PATCH}]}))
        (packets / f"{self.job_id(5)}.diff.json").symlink_to(secret)
        out = self.board.job_diff(self.job_id(5))
        self.assertEqual((out["state"], out["branches"]), ("none", []))
        self.assertNotIn("TOP SECRET", json.dumps(out))
        (packets / f"{self.job_id(6)}.diff.json").write_text(" " * (jobs_mod.DIFF_FILE_MAX + 1))
        self.assertEqual(self.board.job_diff(self.job_id(6))["state"], "bad")
        self.assertIn("TOO LARGE", self.board.job_diff(self.job_id(6))["message"])
        bad = {7: "not json", 8: json.dumps([1]), 9: json.dumps({"version": 2, "job": self.job_id(9), "branches": []}),
               10: json.dumps({"version": 1, "job": self.job_id(7), "branches": []}),   # another job's file
               11: json.dumps({"version": 1, "job": self.job_id(11), "branches": "x"})}
        for n, text in bad.items():
            (packets / f"{self.job_id(n)}.diff.json").write_text(text)
            out = self.board.job_diff(self.job_id(n))
            self.assertEqual((out["state"], out["branches"]), ("bad", []), n)
            self.assertIn("CANNOT BE READ", out["message"])
        os.mkfifo(packets / f"{self.job_id(12)}.diff.json")
        self.assertEqual(self.board.job_diff(self.job_id(12))["state"], "none")

    def test_a_diff_is_cut_to_the_caps_whatever_the_file_says(self):
        long = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1,3000 @@\n" + "".join(f"+{i:06d} {'x' * 400}\n" for i in range(3000))
        self.assertGreater(len(long), jobs_mod.DIFF_BYTES_MAX)
        self.diff_file(5, patch=long, truncated=False)
        b = self.board.job_diff(self.job_id(5))["branches"][0]
        self.assertTrue(b["truncated"])
        shown = sum(len(h["lines"]) for f in b["files"] for h in f["hunks"])
        self.assertLess(shown, 3000)
        many = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1,30000 @@\n" + "+x\n" * 30000
        self.diff_file(6, patch=many)
        b = self.board.job_diff(self.job_id(6))["branches"][0]
        self.assertTrue(b["truncated"])
        self.assertLessEqual(sum(len(h["lines"]) for f in b["files"] for h in f["hunks"]), jobs_mod.DIFF_LINES_MAX)
        # The bridge's own flag survives, and so does a branch the bridge could not diff.
        self.diff_file(7, truncated=True)
        self.assertTrue(self.board.job_diff(self.job_id(7))["branches"][0]["truncated"])
        self.diff_file(8, patch="", error="could not resolve the branch")
        b = self.board.job_diff(self.job_id(8))["branches"][0]
        self.assertEqual((b["files"], b["error"]), ([], "could not resolve the branch"))

    def test_diff_text_comes_back_with_hostile_characters_made_visible(self):
        patch = ("diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+ok\u202eevil\x1b[31m\u200b\n")
        self.diff_file(5, patch=patch, ref="refs/\u202e<img src=x onerror=alert(1)>", commit="zz<script>", tip="not hex", error="a\nb\x07")
        out = self.board.job_diff(self.job_id(5))
        b = out["branches"][0]
        text = json.dumps(out, ensure_ascii=False)
        for raw in ("\u202e", "\x1b", "\u200b", "\x07"):
            self.assertNotIn(raw, text, repr(raw))
        self.assertIn("\u27e8U+202E\u27e9", text)
        self.assertEqual((b["commit"], b["tip"]), ("", ""))   # only hex is kept
        self.assertEqual(b["error"], "a b\u27e8U+0007\u27e9")
        self.assertIn("bidi", b["files"][0]["flags"])

    def test_no_more_than_the_first_branches_are_returned(self):
        doc = {"version": 1, "job": self.job_id(5), "branches": [{"ref": f"r{i}", "patch": ""} for i in range(jobs_mod.DIFF_BRANCHES_MAX + 5)]
               + ["x", None]}
        (self.dispatch / "packets" / f"{self.job_id(5)}.diff.json").write_text(json.dumps(doc))
        self.assertEqual(len(self.board.job_diff(self.job_id(5))["branches"]), jobs_mod.DIFF_BRANCHES_MAX)

    def test_the_demo_has_diffs_for_its_two_finished_jobs_with_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo.build(Path(tmp))
            board = d.Dashboard(Path(tmp), d.DOCS_ROOT / "roles", demo.PROJECT, demo=True)
            full = board.job_diff("20261023-100000-c3d4e5")
            self.assertEqual(full["state"], "ok")
            files = full["branches"][0]["files"]
            self.assertEqual([f["status"] for f in files], ["B", "M", "R", "M"])
            self.assertTrue(all(not b["truncated"] for b in full["branches"]))
            readme = board.job_diff("20261023-094500-a1b2c3")
            self.assertEqual((readme["state"], readme["branches"][0]["files"][0]["path"]), ("ok", "README.md"))
            self.assertEqual(board.job_diff("20261023-080000-f0a1b2")["state"], "none")



class PromptTests(unittest.TestCase):
    """The Thoughtfield's greeting suggests one example prompt (/api/state `prompt`)."""

    def test_the_state_carries_the_example_prompt_and_no_briefing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            snap = d.Dashboard(fx.state, fx.roles, "proj").snapshot(fx.now)
        self.assertEqual(snap["prompt"], dashboard_setup.example_prompt(fx.now))
        self.assertNotIn("briefing", snap)
        self.assertNotIn("hours", snap["spend"])

    def test_the_example_prompt_is_from_the_fixed_list_and_steady_within_a_day(self):
        self.assertIn("slugify", dashboard_setup.PROMPTS[0])
        day = 20_000 * 86400
        self.assertEqual(dashboard_setup.example_prompt(day), dashboard_setup.example_prompt(day + 86399))
        seen = {dashboard_setup.example_prompt(day + i * 86400) for i in range(len(dashboard_setup.PROMPTS))}
        self.assertEqual(seen, set(dashboard_setup.PROMPTS))


class PageTests(unittest.TestCase):
    def test_fonts_match_checksums(self):
        sums = (UI / "fonts" / "SHA256SUMS").read_text().split("\n")
        pinned = dict(reversed(line.split()) for line in sums if line.strip())
        served = {Path(name).name for name, ctype in [*d.STATIC.values(), *dashboard_term.STATIC.values()] if ctype == "font/woff2"}
        self.assertEqual(set(pinned), served)
        for name, digest in pinned.items():
            self.assertEqual(hashlib.sha256((UI / "fonts" / name).read_bytes()).hexdigest(), digest, name)
        licenses = {p.name for p in (UI / "fonts").glob("OFL-*.txt")}
        self.assertEqual(licenses, {"OFL-VT323.txt", "OFL-BarlowCondensed.txt", "OFL-DotGothic16.txt", "OFL-Nunito.txt", "OFL-JetBrainsMono.txt"})

    def test_no_html_sinks(self):
        for name in ("field-hud.js", "field-order.js", "field-sky.js", "field-face.js",
                     "field-monitor.js", "field-shown.js", "voice.js", "voice-lab.js", "cu-field.js"):
            js = (UI / name).read_text()
            for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(sink, js, f"{name} uses {sink}")

    def test_the_dashboard_serves_the_diff_endpoint_on_the_main_port_by_get(self):
        src = Path(d.__file__).read_text()
        self.assertIn('"/api/diff"', src)
        self.assertIn("JOB_ID.fullmatch(job)", src[src.index('"/api/diff"'):src.index('"/api/diff"') + 600])

    def test_settings_entry_is_wired_and_speaks_only_its_fixed_messages(self):
        # The header's SETTINGS key asks the control unit's frame to open its configuration screen: one fixed
        # object to the frame's exact origin, no request, no key.
        js = (UI / "field-hud.js").read_text()
        self.assertRegex((UI / "field.html").read_text(), r'<button class="ic" id="settings-key" type="button" disabled aria-keyshortcuts=","')
        self.assertIn('postMessage({ type: "cu:open", screen: "settings" }, cuOrigin);', js)
        self.assertIn('postMessage({ type: "cu:select", id }, cuOrigin);', js)
        self.assertIn("e.source === cuFrame.contentWindow && e.origin === cuOrigin", js)
        # The unit side accepts only the two fixed shapes, from the main origin and window.
        cu = (UI / "cu.js").read_text()
        self.assertIn('d.type === "cu:open" && names.includes("screen") && d.screen === "settings"', cu)
        self.assertIn("e.source !== window.parent || e.origin !== mainOrigin", cu)

    def test_page_obeys_its_csp(self):
        html = (UI / "field.html").read_text()
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "inline script")
        self.assertIsNone(re.search(r"\sstyle=", html), "inline style attribute")
        self.assertIsNone(re.search(r"\son[a-z]+=", html), "inline event handler")
        self.assertNotIn("<style", html)
        self.assertIsNone(re.search(r"https?://", html + (UI / "field.css").read_text()), "network URL")


if __name__ == "__main__":
    unittest.main()
