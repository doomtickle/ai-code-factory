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


class BrokerEventTests(unittest.TestCase):
    def test_kinds(self):
        self.assertIsNone(d.broker_event({"tool": "git_push", "event": "intent"}))
        self.assertEqual(d.broker_event({"tool": "git_push", "event": "approval-granted", "approver": "daron",
                                         "repo": "o/r", "branch": "b"})["kind"], "OK")
        ev = d.broker_event({"tool": "npm_fetch", "role": "developer", "decision": "allow", "args": {"lockfile": "x.json"}})
        self.assertEqual((ev["kind"], ev["red"], ev["text"]), ("ALLOW", False, "npm_fetch developer x.json"))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        fx = Fixture(Path(cls.tmp.name))
        cls.server, url = d.serve(d.Dashboard(fx.state, fx.roles, "proj"), 0, TOKEN)
        cls.port = cls.server.server_address[1]
        assert url == f"http://127.0.0.1:{cls.port}/?token={TOKEN}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path, cookie=True, host=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}" if host is None else host}
        if cookie:
            headers["Cookie"] = f"other=1; {d.COOKIE}={TOKEN}"
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
        self.assertIn(f"{d.COOKIE}={TOKEN}", cookie)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)

    def test_refused_without_token(self):
        for path in ("/", "/api/state", "/app.js", "/?token=wrong", "/api/lane?name=developer"):
            self.assertEqual(self.get(path, cookie=False)[0].status, 403, path)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": f"{d.COOKIE}=wrong"})
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

    def test_static_allowlist_only(self):
        for path in d.STATIC:
            self.assertEqual(self.get(path)[0].status, 200, path)
        for path in ("/../dashboard.py", "/fonts/OFL-VT323.txt", "/index.html", "/fonts/../app.js", "//etc/passwd"):
            self.assertEqual(self.get(path)[0].status, 404, path)

    def test_read_only(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.get("/api/state", method=method)[0].status, 405, method)


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
