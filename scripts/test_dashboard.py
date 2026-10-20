"""Tests for scripts/dashboard.py (factory dashboard): the state snapshot built
from a fixture state folder, the HTTP server's access checks, and the page's
static files (vendored font checksums, no HTML injection sinks in the script).

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

    def test_cards_draft_and_fingerprint(self):
        import approvals
        pending = self.fx.state / "broker" / "approvals" / "pending"
        for cid, extra in (("pr1", {"draft": False}), ("pr2", {}), ("pr3", {"draft": True})):
            (pending / f"{cid}.json").write_text(json.dumps(dict({
                "id": cid, "tool": "github_create_pr", "principal": "developer", "repo": "o/r", "head": "factory/y",
                "base": "main", "title": "T", "expires": self.fx.now + 60, "nonce": cid + "n"}, **extra)))
        cards = {c["id"]: c for c in self.board.cards(self.fx.now)}
        self.assertEqual({k: v["draft"] for k, v in cards.items()},
                         {"c0ffee": False, "pr1": False, "pr2": True, "pr3": True})
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
        cls.server, url = d.serve(d.Dashboard(fx.state, fx.roles, "proj"), 0, TOKEN)
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
        for path in ("/../dashboard.py", "/fonts/OFL-VT323.txt", "/index.html", "/fonts/../app.js", "//etc/passwd"):
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
        self.assertNotIn(self.server.term_server.cu_key, json.dumps(state))


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
        js = (UI / "app.js").read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, js, f"app.js uses {sink}")

    def test_page_obeys_its_csp(self):
        html = (UI / "index.html").read_text()
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "inline script")
        self.assertIsNone(re.search(r"\sstyle=", html), "inline style attribute")
        self.assertIsNone(re.search(r"\son[a-z]+=", html), "inline event handler")
        self.assertNotIn("<style", html)
        self.assertIsNone(re.search(r"https?://", html + (UI / "app.css").read_text()), "network URL")


if __name__ == "__main__":
    unittest.main()
