"""Tests for `factory dashboard --demo` (scripts/dashboard_demo.py and its
wiring in dashboard.py and bin/factory): the fixture builds into a throwaway
folder with times relative to now, the dashboard shows it, the control unit
refuses everything that acts (and still serves the fixture patch through the
real INSPECT path), the same token, cookie, key and CSP rules hold, and the
real factory state folder is never read.

  python3 -m unittest discover -s scripts -p 'test_dashboard_demo.py' -v
"""

import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import dashboard as d
import dashboard_demo as demo
import dashboard_term as dt

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
TOKEN = "demo-token-0123456789abcdefghijklmnopq"


def local_noon():
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))


class Env:
    """A demo state folder (built for local noon today), its Dashboard and DemoControls, both servers."""

    def __init__(self, add_cleanup):
        self.tmp = tempfile.TemporaryDirectory()
        add_cleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.now = local_noon()
        demo.build(self.state, self.now)
        self.board = d.Dashboard(self.state, REPO / "roles", demo.PROJECT, demo=True)
        self.controls = demo.DemoControls(self.board, self.state, None, clock=lambda: self.now)
        self.server, _ = d.serve(self.board, 0, TOKEN, controls=self.controls)
        add_cleanup(d.stop, self.server)
        self.port = self.server.server_address[1]
        self.tport = self.server.term_server.server_address[1]
        self.key = self.server.term_server.cu_key
        self.cookie = f"{d.cookie_name(self.port)}={TOKEN}"

    def post(self, name, body=None, **over):
        headers = {"Host": f"127.0.0.1:{self.tport}", "Origin": f"http://127.0.0.1:{self.tport}",
                   "Cookie": self.cookie, "X-Factory-Control": self.key, "Content-Type": "application/json",
                   "Sec-Fetch-Site": "same-origin"}
        headers.update(over)
        conn = http.client.HTTPConnection("127.0.0.1", self.tport, timeout=10)
        try:
            conn.request("POST", f"/cu/api/{name}", body=json.dumps(body or {}),
                         headers={k: v for k, v in headers.items() if v is not None})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def card(self):
        cards = self.post("state")[1]["cards"]
        return cards[0]


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_every_token_is_replaced(self):
        demo.build(self.state, local_noon())
        files = [p for p in self.state.rglob("*") if p.is_file() and p.suffix != ".patch"]
        self.assertGreaterEqual(len(files), 8)
        for p in files:
            self.assertNotIn("{{", p.read_text(encoding="utf-8"), p)

    def test_the_fixture_itself_stays_a_template(self):
        # The committed fixture holds tokens, never one run's times.
        text = (demo.FIXTURE / "broker" / "approvals" / "pending" / "demo0001push.json").read_text()
        self.assertIn("{{epoch:+", text)
        self.assertNotRegex(text, r'"expires": \d')

    def test_times_follow_now_so_nothing_expires_or_goes_stale(self):
        for now in (local_noon(), local_noon() + 5 * 3600):
            shutil.rmtree(self.state)
            self.state.mkdir()
            demo.build(self.state, now)
            board = d.Dashboard(self.state, REPO / "roles", "x", demo=True)
            snap = board.snapshot(now)
            card = snap["gate"][0]
            self.assertGreater(card["seconds_left"], 3600)
            lanes = {lane["name"]: lane for lane in snap["lanes"]}
            self.assertLess(lanes["developer"]["elapsed"], 900)  # a job started minutes ago, not days
            self.assertEqual(lanes["developer"]["state"], "running")  # its push is staged: the agent moved on

    def test_the_card_names_the_patch_by_its_digest(self):
        demo.build(self.state, local_noon())
        card = json.loads((self.state / "broker" / "approvals" / "pending" / "demo0001push.json").read_text())
        patch = self.state / "broker" / "approvals" / "patches" / (card["patch_sha256"] + ".patch")
        self.assertTrue(patch.is_file())
        import hashlib
        self.assertEqual(hashlib.sha256(patch.read_bytes()).hexdigest(), card["patch_sha256"])

    def test_no_real_names(self):
        demo.build(self.state, local_noon())
        blob = "".join(p.read_text(encoding="utf-8") for p in self.state.rglob("*") if p.is_file())
        self.assertIn("example/app", blob)
        for word in ("doomtickle", "passdesk", "daron", "@gmail"):
            self.assertNotIn(word, blob.lower())

    def test_build_skips_symlinks(self):
        fixture = Path(self.tmp.name) / "fx"
        (fixture / "dispatch").mkdir(parents=True)
        (fixture / "broker" / "approvals" / "patches").mkdir(parents=True)
        (fixture / "dispatch" / "real.log").write_text("ok")
        (fixture / "dispatch" / "link.log").symlink_to("/etc/hostname")
        dest = Path(self.tmp.name) / "out"
        demo.build(dest, local_noon(), fixture)
        self.assertTrue((dest / "dispatch" / "real.log").exists())
        self.assertFalse((dest / "dispatch" / "link.log").exists())


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self.addCleanup)
        self.snap = self.env.board.snapshot(self.env.now)

    def test_lanes(self):
        lanes = {lane["name"]: lane for lane in self.snap["lanes"]}
        self.assertEqual({n: lane["state"] for n, lane in lanes.items()},
                         {"developer": "running", "developer-lane2": "idle", "developer-lane3": "running",
                          "reviewer": "running", "reviewer-lane2": "approved"})
        self.assertEqual({lane["role"] for lane in lanes.values()}, {"developer", "reviewer"})
        self.assertTrue(self.snap["bridge"])
        self.assertTrue(self.snap["demo"])
        self.assertEqual(self.snap["project"], "DEMO")
        self.assertIsNone(self.snap.get("term"))

    def test_desk_placards(self):
        lanes = {lane["name"]: lane for lane in self.snap["lanes"]}
        self.assertEqual({n: (lane["desk"], lane["reviews"]) for n, lane in lanes.items()}, {
            "developer": ("greet empty name", ""), "developer-lane2": ("", ""), "developer-lane3": ("Tidy the packaging metadata, changelog", ""),
            "reviewer": ("greet empty name", "developer"), "reviewer-lane2": ("greet readme", "developer-lane2")})

    def test_a_staged_push_a_deny_and_a_branch(self):
        card = self.snap["gate"][0]
        self.assertEqual((card["tool"], card["repo"], card["branch"], card["role"]),
                         ("git_push", "example/app", "factory/greet-empty-name", "developer"))
        self.assertEqual((card["queued"], card["decided"]), (True, None))  # staged, waiting for a person
        kinds = {e["kind"] for e in self.snap["events"]}
        self.assertTrue({"DENY", "ALLOW", "OK", "STAGED", "WRITTEN", "FAILED", "JOB"} <= kinds, kinds)
        branches = [e["text"] for lane in self.snap["lanes"] for e in lane["tail"] if e["kind"] == "branch"]
        self.assertTrue(branches and all(b.startswith("factory/") for b in branches), branches)

    def test_every_write_state_is_shown(self):
        # Staged (the card above), approved and being written, done and failed.
        writes = {w["id"]: (w["state"], w["tool"], w["summary"]) for w in self.snap["writes"]}
        self.assertEqual(writes, {
            "demo0002issue": ("running", "github_create_issue", ""),
            "demo0000pr": ("made", "github_create_pr", "Opened PR #41: https://github.com/example/app/pull/41"),
            "demo0003push": ("failed", "git_push", "factory/changelog-typo changed in the broker's mirror after the "
                             "push was staged (another push to the same branch?); nothing was pushed")})
        self.assertEqual(self.snap["writes"][0]["state"], "failed")  # failures first

    def test_spend_has_something_to_draw(self):
        self.assertGreater(self.snap["totals"]["cost"], 0)
        self.assertGreaterEqual(len(self.snap["spend"]["roles"]), 2)


class TickerTests(unittest.TestCase):
    def test_appends_audit_records_the_printer_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            demo.build(state, local_noon())
            log = state / "broker" / "audit.jsonl"
            before = len(log.read_text().splitlines())
            stop = demo.start_ticker(state, interval=0.01, first=0.01)
            try:
                deadline = time.monotonic() + 5
                while len(log.read_text().splitlines()) < before + 4 and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                stop.set()
            new = [json.loads(line) for line in log.read_text().splitlines()[before:]]
            self.assertGreaterEqual(len(new), 4)
            self.assertEqual(new[0]["decision"], "deny")  # the first crate the floor sees goes in the bin
            self.assertTrue(all(d.broker_event(r) is not None for r in new))
            time.sleep(0.1)
            settled = log.read_text()
            time.sleep(0.1)
            self.assertEqual(log.read_text(), settled)  # set() stops it

    def test_stops_quietly_when_the_folder_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "demo"
            demo.build(state, local_noon())
            real = threading.Thread
            threads = []
            with mock.patch.object(demo.threading, "Thread",
                                   lambda **kw: threads.append(real(**kw)) or threads[-1]):
                stop = demo.start_ticker(state, interval=0.01, first=0.3)
            shutil.rmtree(state)
            threads[0].join(5)
            self.assertFalse(threads[0].is_alive())
            self.assertFalse(state.exists())  # not recreated
            stop.set()


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self.addCleanup)
        self.decisions = self.env.state / "broker" / "approvals" / "decisions"

    def test_every_acting_endpoint_is_refused(self):
        card = self.env.card()
        popen = mock.Mock(side_effect=AssertionError("a command was started"))
        with mock.patch("subprocess.Popen", popen), mock.patch("os.kill", side_effect=AssertionError("kill")):
            for verdict in ("approve", "decline"):
                status, out = self.env.post("decide", {"id": card["id"], "fp": card["fp"], "verdict": verdict,
                                                       "reason": "no"})
                self.assertEqual((status, out["error"]), (403, demo.REFUSED))
            for action in ("restart", "update", "doctor"):
                self.assertEqual(self.env.post("prepare", {"action": action})[0], 403)
                self.assertEqual(self.env.post("run", {"action": action, "confirm": "x"})[0], 403)
        popen.assert_not_called()
        self.assertFalse(self.decisions.exists())
        self.assertFalse((self.env.state / "dashboard").exists())  # no action log folder either
        self.assertEqual(self.env.post("state")[1]["action"]["phase"], "idle")

    def test_the_card_stays_staged_and_undecided(self):
        card = self.env.card()
        self.env.post("decide", {"id": card["id"], "fp": card["fp"], "verdict": "approve"})
        again = self.env.card()
        self.assertEqual((again["decided"], again["queued"]), (None, True))
        self.assertEqual(self.env.board.snapshot(self.env.now)["lanes"][0]["state"], "running")

    def test_an_unknown_control_is_a_404(self):
        self.assertEqual(self.env.post("shell", {"cmd": "id"})[0], 404)

    def test_controls_never_resolve_a_command(self):
        for action in dc_actions():
            with self.assertRaises(RuntimeError):
                self.env.controls.argv(action)

    def test_the_key_and_origin_rules_still_apply(self):
        self.assertEqual(self.env.post("state", **{"X-Factory-Control": "wrong"})[0], 403)
        self.assertEqual(self.env.post("state", Origin="http://127.0.0.1:1")[0], 403)
        self.assertEqual(self.env.post("state", Cookie=None)[0], 403)

    def test_inspect_reads_the_fixture_patch_through_the_real_path(self):
        card = self.env.card()
        self.assertTrue(card["patch"])
        status, out = self.env.post("patch", {"id": card["id"], "fp": card["fp"]})
        self.assertEqual(status, 200, out)
        names = json.dumps(out)
        self.assertIn("src/greet.py", names)
        self.assertIn("tests/test_greet.py", names)
        self.assertEqual(self.env.post("patch", {"id": card["id"], "fp": "0" * 16})[1]["error"], "changed")

    def test_inspect_fails_closed_if_the_patch_is_swapped(self):
        card = self.env.card()
        (patch,) = (self.env.state / "broker" / "approvals" / "patches").glob("*.patch")
        patch.write_text("not the card's patch\n")
        status, out = self.env.post("patch", {"id": card["id"], "fp": card["fp"]})
        self.assertEqual((status, out["error"]), (409, "patch does not match its card"))


def dc_actions():
    import dashboard_controls as dc
    return dc.ACTIONS


class MainPortTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self.addCleanup)

    def request(self, method, path, cookie=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.env.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.env.port}"}
        if cookie:
            headers["Cookie"] = self.env.cookie
        try:
            conn.request(method, path, headers=headers)
            resp = conn.getresponse()
            return resp, resp.read()
        finally:
            conn.close()

    def test_get_only_with_cookie_host_and_csp(self):
        resp, body = self.request("GET", "/api/state")
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(body)["demo"])
        self.assertEqual(self.request("GET", "/api/state", cookie=False)[0].status, 403)
        self.assertEqual(self.request("GET", "/api/state", host="evil.example")[0].status, 403)
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.request(method, "/api/state")[0].status, 405, method)
        resp, _ = self.request("GET", "/")
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(self.env.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.env.server.term_server.server_address[0], "127.0.0.1")

    def test_there_is_no_terminal(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.env.tport, timeout=10)
        try:
            conn.request("GET", "/term", headers={"Host": f"127.0.0.1:{self.env.tport}", "Cookie": self.env.cookie,
                                                  "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-site"})
            self.assertEqual(conn.getresponse().status, 404)
        finally:
            conn.close()


class MainTests(unittest.TestCase):
    """main(): the flags it refuses, what it builds, what it removes."""

    def test_demo_refuses_what_could_act_or_read_real_state(self):
        for flags in (["--coordinator", "factory-coordinator"], ["--state", "/tmp/x"], ["--factory", "/bin/true"],
                      ["--project", "real"]):
            with self.subTest(flags=flags), mock.patch.object(dt, "Term") as term, \
                    mock.patch.object(d, "_serve") as serve, mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as cm:
                    d.main(["--demo", "--no-open", *flags])
                self.assertEqual(cm.exception.code, 2)
                term.assert_not_called()
                serve.assert_not_called()

    def test_without_demo_state_and_roles_stay_required(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as cm:
            d.main(["--no-open"])
        self.assertEqual(cm.exception.code, 2)

    def test_demo_serves_a_temp_copy_and_removes_it(self):
        seen = {}

        def fake_serve(args, term):
            seen.update(state=args.state, demo=args.demo, term=term, project=args.project,
                        lanes=len(d.Dashboard(args.state, args.roles, "p", demo=True).watch.lanes),
                        pending=sorted(p.name for p in (args.state / "broker" / "approvals" / "pending").iterdir()))
            return 0

        with mock.patch.object(d, "_serve", fake_serve):
            self.assertEqual(d.main(["--demo", "--no-open"]), 0)
        self.assertTrue(seen["demo"])
        self.assertIsNone(seen["term"])
        self.assertEqual((seen["project"], seen["lanes"], seen["pending"]), ("DEMO", 5, ["demo0001push.json"]))
        state = seen["state"]
        self.assertTrue(state.name.startswith("factory-demo-"))
        self.assertFalse(str(state).startswith(str(REPO)))  # never inside the checkout
        self.assertFalse(state.exists())  # removed on the way out

    def test_a_failing_build_leaves_nothing_behind(self):
        made = []
        real = tempfile.mkdtemp

        def mk(*a, **k):
            made.append(real(*a, **k))
            return made[-1]

        with mock.patch("tempfile.mkdtemp", mk), mock.patch.object(demo, "build", side_effect=OSError("disk")), \
                mock.patch("sys.stderr"):
            self.assertEqual(d.main(["--demo", "--no-open"]), 1)
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))


class ProcessTests(unittest.TestCase):
    """The real command, as a person runs it: bin/factory dashboard --demo, with a
    real state folder in the environment that it must not read."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        state = self.home / ".local" / "state" / "ai-code-factory"
        (state / "dispatch" / "logs").mkdir(parents=True)
        (state / "broker" / "approvals" / "pending").mkdir(parents=True)
        (state / "dispatch" / "logs" / "developer.log").write_text(
            "12:00:00  ▶ job REAL-STATE-JOB from the Coordinator\n12:00:00     > REAL-STATE-TASK\n")
        (state / "broker" / "approvals" / "pending" / "realcard.json").write_text(
            json.dumps({"id": "realcard", "tool": "git_push", "principal": "developer", "repo": "real/repo",
                        "expires": time.time() + 3600, "nonce": "n"}))
        self.real_state = state
        self.before = self.listing(state)
        (self.home / ".config").mkdir()
        self.env = dict(os.environ, HOME=str(self.home), XDG_STATE_HOME=str(self.home / ".local" / "state"),
                        XDG_CONFIG_HOME=str(self.home / ".config"), PYTHONDONTWRITEBYTECODE="1",
                        TMPDIR=str(root))
        self.env.pop("FACTORY_STATE", None)

    @staticmethod
    def listing(folder):
        return sorted((str(p.relative_to(folder)), p.stat().st_mtime_ns, p.stat().st_size)
                      for p in folder.rglob("*"))

    def start(self, *argv):
        proc = subprocess.Popen([str(REPO / "bin" / "factory"), "dashboard", *argv], env=self.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def test_refuses_the_coordinator_terminal(self):
        proc = self.start("--demo", "--no-open", "--coordinator", "factory-coordinator")
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--coordinator", err)
        self.assertIn("--demo", err)

    def test_serves_the_fixture_and_never_the_real_state(self):
        proc = self.start("--demo", "--no-open")
        line = proc.stdout.readline()
        m = re.match(r"Factory dashboard: http://127\.0\.0\.1:(\d+)/\?token=(\S+)$", line.strip())
        self.assertIsNotNone(m, line)
        port, token = int(m.group(1)), m.group(2)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", f"/?token={token}", headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 303)
            cookie = resp.getheader("Set-Cookie").split(";")[0]
            conn.request("GET", "/api/state", headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie})
            body = conn.getresponse().read().decode()
            conn.close()
            snap = json.loads(body)
            self.assertEqual([c["id"] for c in snap["gate"]], ["demo0001push"])
            self.assertEqual(snap["project"], "DEMO")
            self.assertNotIn("REAL-STATE", body)
            self.assertNotIn("real/repo", body)
            self.assertNotIn("realcard", body)
            self.assertEqual(self.listing(self.real_state), self.before)
            self.assertFalse((self.real_state / "dashboard").exists())
            temps = [p for p in Path(self.tmp.name).glob("factory-demo-*")]
            self.assertEqual(len(temps), 1)
        finally:
            proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=30)
        self.assertEqual(list(Path(self.tmp.name).glob("factory-demo-*")), [])  # deleted on exit
        self.assertEqual(self.listing(self.real_state), self.before)

    def test_works_without_the_broker_files(self):
        # bin/factory must not stop at roles.sh when broker/ has no tools.txt.
        clone = Path(self.tmp.name) / "clone"
        shutil.copytree(REPO, clone, ignore=shutil.ignore_patterns(".git", "broker", "node_modules"))
        (clone / "broker").mkdir()
        proc = subprocess.Popen([str(clone / "bin" / "factory"), "dashboard", "--demo", "--no-open"], env=self.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        line = proc.stdout.readline()
        self.assertTrue(line.startswith("Factory dashboard: http://127.0.0.1:"), line)
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=30)


if __name__ == "__main__":
    unittest.main()
