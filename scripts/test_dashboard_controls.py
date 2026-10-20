"""Tests for the dashboard's control unit (scripts/dashboard_controls.py and
its routes in dashboard_term.py): every refusal of POST /cu/api/* (Host,
Origin, Sec-Fetch-Site, cookie, key, content type, size, body, methods),
approve/decline (exact id, gone, changed, expired, reason rules, the decision
file), the prepare -> run confirm step (single use, expiry, bound to one
action), one action at a time, the action's log, the Coordinator terminal's
hang-up and hold around an action, the reload digest, and the /cu page's
iframe gating and CSP.

No real factory command runs here: the control unit is given a small fake
`factory` script, and the terminal a fake Coordinator.

  python3 -m unittest discover -s scripts -p 'test_dashboard_controls.py' -v
"""

import http.client
import json
import os
import re
import shutil
import socket
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import approvals
import dashboard as d
import dashboard_controls as dc
import dashboard_term as dt
from test_dashboard import TOKEN, Fixture
from test_dashboard_term import fake_term

UI = Path(__file__).resolve().parent / "dashboard-ui"

# The fake factory: records its argv, environment and stdin, prints the plan
# for `update --plan`, waits while $DIR/block exists, appends to the files
# listed in $DIR/touch (one per line), then exits with the code in $DIR/exit.
FAKE_FACTORY = r"""#!/bin/sh
DIR="__DIR__"
echo "$*" >> "$DIR/argv"
if [ "$1 $2" = "update --plan" ]; then
  printf 'commit\tabc1234\tFix the thing\n'
  printf 'recreate\tfactory-broker\tnothing (it keeps no history)\n'
  printf 'maybe\tfactory-developer\tits kit changes (a guess)\n'
  printf 'note\tsomething to know\n'
  printf 'garbage line\n'
  [ ! -f "$DIR/plan-fails" ] || { echo "plan went wrong" >&2; exit 3; }
  exit 0
fi
echo "no_attach=$FACTORY_NO_ATTACH"
if read -r line; then echo "stdin=$line"; else echo "stdin=eof"; fi
while [ -f "$DIR/block" ]; do sleep 0.02; done
if [ -f "$DIR/touch" ]; then
  while IFS= read -r f; do echo "// changed" >> "$f"; done < "$DIR/touch"
fi
echo "step one ✓"
exit "$(cat "$DIR/exit" 2>/dev/null || echo 0)"
"""


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    """A fixture state folder, a fake factory, a Controls and (optionally) both servers."""

    def __init__(self, add_cleanup, term=None, serve=True):
        self.tmp = tempfile.TemporaryDirectory()
        add_cleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.fx = Fixture(root)
        self.dir = root / "fake"
        self.dir.mkdir()
        self.factory = root / "factory"
        self.factory.write_text(FAKE_FACTORY.replace("__DIR__", str(self.dir)))
        self.factory.chmod(0o755)
        # A copy of the dashboard's files, for the reload digest.
        self.scripts = root / "scripts"
        (self.scripts / "dashboard-ui" / "vendor").mkdir(parents=True)
        for name in ("dashboard.py", "dashboard_term.py", "dashboard_controls.py", "approvals.py", "watch.py"):
            (self.scripts / name).write_text(f"# {name}\n")
        (self.scripts / "dashboard-ui" / "app.js").write_text("// app\n")
        (self.scripts / "dashboard-ui" / "vendor" / "x.js").write_text("// vendored\n")
        self.clock = Clock(self.fx.now)
        self.term = term
        self.board = d.Dashboard(self.fx.state, self.fx.roles, "proj")
        self.controls = dc.Controls(self.board, self.fx.state, term, self.factory, clock=self.clock,
                                    approver="tester", scripts=self.scripts)
        add_cleanup(lambda: self.controls.wait(10))
        self.server = None
        if serve:
            self.server, _ = d.serve(self.board, 0, TOKEN, term=term, controls=self.controls)
            add_cleanup(d.stop, self.server, term)
            self.port = self.server.server_address[1]
            self.tport = self.server.term_server.server_address[1]
            self.key = self.server.term_server.cu_key
            self.cookie = f"{d.cookie_name(self.port)}={TOKEN}"

    def bind(self, project="/home/me/proj"):
        (self.fx.state / "broker" / "project").write_text(project + "\n")

    def argv(self):
        try:
            return (self.dir / "argv").read_text().splitlines()
        except OSError:
            return []

    # --- HTTP
    def headers(self, **over):
        h = {"Host": f"127.0.0.1:{self.tport}", "Origin": f"http://127.0.0.1:{self.tport}",
             "Cookie": self.cookie, "X-Factory-Control": self.key, "Content-Type": "application/json",
             "Sec-Fetch-Site": "same-origin"}
        h.update(over)
        return {k: v for k, v in h.items() if v is not None}

    def post(self, name, body=None, raw=None, method="POST", **over):
        """(status, parsed JSON or text, response)."""
        data = raw if raw is not None else json.dumps({} if body is None else body).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.tport, timeout=10)
        try:
            conn.request(method, f"/cu/api/{name}", body=data, headers=self.headers(**over))
            resp = conn.getresponse()
            text = resp.read()
        finally:
            conn.close()
        try:
            out = json.loads(text)
        except ValueError:
            out = text.decode()
        return resp.status, out, resp

    def raw(self, request: bytes) -> bytes:
        s = socket.create_connection(("127.0.0.1", self.tport), timeout=10)
        try:
            s.sendall(request)
            out = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        finally:
            s.close()
        return out

    def get(self, path, port=None, **over):
        port = port or self.tport
        h = {"Host": f"127.0.0.1:{port}", "Cookie": self.cookie, "Sec-Fetch-Dest": "iframe",
             "Sec-Fetch-Site": "same-site"}
        h.update(over)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers={k: v for k, v in h.items() if v is not None})
            resp = conn.getresponse()
            return resp, resp.read()
        finally:
            conn.close()


def write_card(state: Path, cid: str, now: float, **over):
    card = {"id": cid, "tool": "github_create_pr", "principal": "developer", "repo": "o/r", "head": "factory/y",
            "base": "main", "title": "Add it", "draft": True, "created": now - 10, "expires": now + 600,
            "nonce": "nonce-" + cid}
    card.update(over)
    (state / "broker" / "approvals" / "pending" / f"{cid}.json").write_text(json.dumps(card))
    return card


# --------------------------------------------------------------------------
# The API's front door

class RefusalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env(cls.addClassCleanup)

    def test_accepted_when_everything_holds(self):
        e = self.env
        code, out, resp = e.post("state")
        self.assertEqual(code, 200, out)
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        # Variants that are fine: localhost, no Sec-Fetch-Site (an older browser), a charset.
        self.assertEqual(e.post("state", Host=f"localhost:{e.tport}", Origin=f"http://localhost:{e.tport}")[0], 200)
        self.assertEqual(e.post("state", **{"Sec-Fetch-Site": None})[0], 200)
        self.assertEqual(e.post("state", **{"Content-Type": "application/json; charset=utf-8"})[0], 200)

    def test_header_refusals(self):
        e = self.env
        other = d.cookie_name(e.port + 1)
        cases = {
            "foreign Host": ({"Host": "evil.example"}, 403),
            "main port Host": ({"Host": f"127.0.0.1:{e.port}"}, 403),
            "no Origin": ({"Origin": None}, 403),
            "main page's Origin": ({"Origin": f"http://127.0.0.1:{e.port}"}, 403),
            "null Origin": ({"Origin": "null"}, 403),
            "foreign Origin": ({"Origin": "http://evil.example"}, 403),
            "Origin with a path": ({"Origin": f"http://127.0.0.1:{e.tport}/"}, 403),
            "same-site fetch": ({"Sec-Fetch-Site": "same-site"}, 403),
            "cross-site fetch": ({"Sec-Fetch-Site": "cross-site"}, 403),
            "typed or bookmarked": ({"Sec-Fetch-Site": "none"}, 403),
            "no cookie": ({"Cookie": None}, 403),
            "wrong cookie": ({"Cookie": f"{d.cookie_name(e.port)}=wrong"}, 403),
            "another dashboard's cookie": ({"Cookie": f"{other}={TOKEN}"}, 403),
            "cookie twice": ({"Cookie": f"{e.cookie}; {d.cookie_name(e.port)}=x"}, 403),
            "no key": ({"X-Factory-Control": None}, 403),
            "wrong key": ({"X-Factory-Control": "0" * 48}, 403),
            "key with extra": ({"X-Factory-Control": e.key + "0"}, 403),
            "text/plain": ({"Content-Type": "text/plain"}, 415),
            "form post": ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
            "no content type": ({"Content-Type": None}, 415),
        }
        for name, (over, code) in cases.items():
            status, out, resp = e.post("state", **over)
            self.assertEqual(status, code, name)
            self.assertIn("error", out, name)
            self.assertEqual(resp.getheader("Connection"), "close", name)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"), name)

    def test_body_refusals(self):
        e = self.env
        self.assertEqual(e.post("state", raw=b"x" * (dt.MAX_BODY + 1))[0], 413)
        self.assertEqual(e.post("state", raw=b" " * dt.MAX_BODY + b"{}")[0], 413)
        ok = b"{" + b" " * (dt.MAX_BODY - 2) + b"}"
        self.assertEqual(e.post("state", raw=ok)[0], 200)  # exactly the limit is fine
        for body in (b"[]", b"1", b'"x"', b"null", b"not json", b"{", b"", b"\xff\xfe"):
            status, out, _ = e.post("state", raw=body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", out)

    def test_length_and_chunked(self):
        e = self.env
        base = (f"POST /cu/api/state HTTP/1.1\r\nHost: 127.0.0.1:{e.tport}\r\n"
                f"Origin: http://127.0.0.1:{e.tport}\r\nCookie: {e.cookie}\r\nX-Factory-Control: {e.key}\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n")
        cases = {
            "no Content-Length": ("", b"{}", b" 400 "),
            "chunked": ("Transfer-Encoding: chunked\r\n", b"2\r\n{}\r\n0\r\n\r\n", b" 400 "),
            "chunked and a length": ("Transfer-Encoding: chunked\r\nContent-Length: 2\r\n", b"{}", b" 400 "),
            "two lengths": ("Content-Length: 2\r\nContent-Length: 2\r\n", b"{}", b" 400 "),
            "a negative length": ("Content-Length: -1\r\n", b"{}", b" 400 "),
            "a huge length, no body sent": (f"Content-Length: {10 ** 9}\r\n", b"", b" 413 "),
            "two keys": (f"X-Factory-Control: {e.key}\r\nContent-Length: 2\r\n", b"{}", b" 403 "),
            "fine": ("Content-Length: 2\r\n", b"{}", b" 200 "),
        }
        for name, (extra, body, want) in cases.items():
            out = e.raw((base + extra + "\r\n").encode() + body)
            self.assertIn(want, out.split(b"\r\n", 1)[0], name)
            self.assertNotIn(b"Access-Control", out, name)

    def test_methods(self):
        e = self.env
        for method in ("GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            status, _, resp = e.post("state", method=method)
            self.assertEqual(status, 405, method)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"), method)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Methods"), method)
        # A CORS preflight, as a browser would send it: refused, no CORS headers.
        out = e.raw((f"OPTIONS /cu/api/decide HTTP/1.1\r\nHost: 127.0.0.1:{e.tport}\r\n"
                     "Origin: http://evil.example\r\nAccess-Control-Request-Method: POST\r\n"
                     "Access-Control-Request-Headers: x-factory-control, content-type\r\n"
                     "Connection: close\r\n\r\n").encode())
        self.assertIn(b" 405 ", out.split(b"\r\n", 1)[0])
        self.assertNotIn(b"Access-Control", out)
        # POST anywhere else on the second origin: refused.
        for path in ("/cu", "/term", "/cu/api", "/cu.js"):
            conn = http.client.HTTPConnection("127.0.0.1", e.tport, timeout=10)
            conn.request("POST", path, body=b"{}", headers=e.headers())
            self.assertEqual(conn.getresponse().status, 405, path)
            conn.close()

    def test_unknown_control(self):
        e = self.env
        self.assertEqual(e.post("nope")[0], 404)
        self.assertEqual(e.post("../state")[0], 404)
        self.assertEqual(e.post("state", raw=b"{}", Cookie=None)[0], 403)  # auth comes first


# --------------------------------------------------------------------------
# State and decisions

class DecideTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self.addCleanup)
        self.pending = self.env.fx.state / "broker" / "approvals" / "pending"
        self.decisions = self.env.fx.state / "broker" / "approvals" / "decisions"

    def state(self):
        code, out, _ = self.env.post("state")
        self.assertEqual(code, 200, out)
        return out

    def test_state(self):
        e = self.env
        write_card(e.fx.state, "pr1", e.fx.now)
        out = self.state()
        self.assertEqual(out["now"], e.fx.now)
        self.assertEqual((out["project"], out["bound"]), ("", False))
        e.bind()
        out = self.state()
        self.assertEqual((out["project"], out["bound"]), ("/home/me/proj", True))
        cards = {c["id"]: c for c in out["cards"]}
        self.assertEqual(set(cards), {"c0ffee", "pr1"})
        self.assertEqual(set(cards["pr1"]), set(dc.CARD_KEYS))
        self.assertEqual(cards["c0ffee"]["lanes"], ["developer"])
        self.assertEqual(cards["pr1"]["branch"], "factory/y")
        self.assertTrue(cards["pr1"]["draft"])
        self.assertEqual(cards["pr1"]["seconds_left"], 600)
        raw = json.loads((self.pending / "pr1.json").read_text())
        self.assertEqual(cards["pr1"]["fp"], approvals.card_fingerprint(raw))
        self.assertNotIn("nonce-pr1", json.dumps(out))  # the nonce never leaves the server
        self.assertEqual(out["action"], {"name": None, "phase": "idle", "started": None, "ended": None,
                                         "exit": None, "reload": "none"})

    def decide(self, **body):
        return self.env.post("decide", body)

    def fp(self, cid):
        return approvals.card_fingerprint(json.loads((self.pending / f"{cid}.json").read_text()))

    def test_approve(self):
        e = self.env
        write_card(e.fx.state, "pr1", e.fx.now)
        code, out, _ = self.decide(id="pr1", fp=self.fp("pr1"), verdict="approve", reason="")
        self.assertEqual(code, 200, out)
        self.assertEqual((out["ok"], out["id"], out["verdict"]), (True, "pr1", "approve"))
        self.assertIn('draft PR on o/r: factory/y -> main "Add it"', out["text"])
        rec = json.loads((self.decisions / "pr1.json").read_text())
        self.assertEqual({k: rec[k] for k in ("id", "nonce", "decision", "approver", "reason")},
                         {"id": "pr1", "nonce": "nonce-pr1", "decision": "approve", "approver": "tester",
                          "reason": ""})

    def test_decline_reason_rules(self):
        e = self.env
        write_card(e.fx.state, "pr1", e.fx.now)
        fp = self.fp("pr1")
        for reason in ("", "   ", "\x00\x1b\t\n"):
            code, out, _ = self.decide(id="pr1", fp=fp, verdict="decline", reason=reason)
            self.assertEqual(code, 400, repr(reason))
        self.assertEqual(self.decide(id="pr1", fp=fp, verdict="decline")[0], 400)  # no reason at all
        self.assertEqual(self.decide(id="pr1", fp=fp, verdict="decline", reason="x" * 501)[0], 400)
        self.assertEqual(self.decide(id="pr1", fp=fp, verdict="decline", reason=5)[0], 400)
        self.assertFalse(self.decisions.exists())
        code, out, _ = self.decide(id="pr1", fp=fp, verdict="decline",
                                   reason="  too\x1b[31m big\x07\u0085, split it  ")
        self.assertEqual(code, 200, out)
        rec = json.loads((self.decisions / "pr1.json").read_text())
        self.assertEqual((rec["decision"], rec["reason"]), ("decline", "too[31m big, split it"))
        self.assertEqual(self.decide(id="pr1", fp=fp, verdict="decline", reason="y" * 500)[0], 200)

    def test_gone_changed_expired(self):
        e = self.env
        write_card(e.fx.state, "pr1", e.fx.now)
        fp = self.fp("pr1")
        self.assertEqual(self.decide(id="nope", fp=fp, verdict="approve")[1], {"error": "gone"})
        self.assertEqual(self.decide(id="pr", fp=fp, verdict="approve")[1], {"error": "gone"})  # no prefix match
        self.assertEqual(self.decide(id="pr1 ", fp=fp, verdict="approve")[1], {"error": "gone"})
        # The card expired and the agent's retry made a new one, same id, new nonce.
        write_card(e.fx.state, "pr1", e.fx.now, nonce="fresh")
        code, out, _ = self.decide(id="pr1", fp=fp, verdict="approve")
        self.assertEqual((code, out), (409, {"error": "changed"}))
        self.assertEqual(self.decide(id="pr1", fp=self.fp("pr1"), verdict="approve")[0], 200)
        write_card(e.fx.state, "old", e.fx.now, expires=e.fx.now - 1)
        code, out, _ = self.decide(id="old", fp=self.fp("old"), verdict="approve")
        self.assertEqual((code, out), (409, {"error": "expired"}))
        write_card(e.fx.state, "odd", e.fx.now, expires="soon")
        self.assertEqual(self.decide(id="odd", fp=self.fp("odd"), verdict="approve")[1], {"error": "expired"})
        self.assertFalse((self.decisions / "old.json").exists())

    def test_bad_requests(self):
        e = self.env
        write_card(e.fx.state, "pr1", e.fx.now)
        fp = self.fp("pr1")
        for body in ({"fp": fp, "verdict": "approve"}, {"id": "pr1", "verdict": "approve"},
                     {"id": "pr1", "fp": fp}, {"id": "pr1", "fp": fp, "verdict": "APPROVE"},
                     {"id": ["pr1"], "fp": fp, "verdict": "approve"}, {"id": "pr1", "fp": 1, "verdict": "approve"}):
            self.assertEqual(self.env.post("decide", body)[0], 400, body)
        self.assertFalse(self.decisions.exists())


# --------------------------------------------------------------------------
# Restart and update

class ActionTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self.addCleanup)

    def prepare(self, action):
        return self.env.post("prepare", {"action": action})

    def run_action(self, action, token):
        return self.env.post("run", {"action": action, "confirm": token})

    def test_restart_needs_a_bound_project(self):
        code, out, _ = self.prepare("restart")
        self.assertEqual(code, 409, out)
        self.assertEqual(self.env.argv(), [])

    def test_prepare_restart_runs_nothing(self):
        e = self.env
        e.bind()
        code, out, _ = self.prepare("restart")
        self.assertEqual(code, 200, out)
        self.assertEqual(out["action"], "restart")
        self.assertEqual(out["expires_in"], 120)
        self.assertEqual(out["project"], "/home/me/proj")
        self.assertEqual(out["jobs"], [{"lane": "developer", "task": "Push factory/x and open a draft PR"}])
        self.assertEqual([h["id"] for h in out["held"]], ["c0ffee"])
        self.assertIn("push factory/x -> o/r", out["held"][0]["text"])
        self.assertEqual((out["commits"], out["recreate"], out["maybe"], out["notes"], out["plan_error"]),
                         ([], [], [], [], None))
        self.assertGreaterEqual(len(out["confirm"]), 32)
        self.assertEqual(e.argv(), [])  # prepare ran nothing
        self.assertEqual(self.prepare("reboot")[0], 400)
        self.assertEqual(e.post("prepare", {})[0], 400)

    def test_prepare_update_runs_the_plan(self):
        e = self.env
        code, out, _ = self.prepare("update")  # no project needed
        self.assertEqual(code, 200, out)
        self.assertEqual(e.argv(), ["update --plan"])
        self.assertEqual(out["commits"], [{"sha": "abc1234", "subject": "Fix the thing"}])
        self.assertEqual(out["recreate"], [{"sandbox": "factory-broker", "loses": "nothing (it keeps no history)"}])
        self.assertEqual(out["maybe"], [{"sandbox": "factory-developer", "why": "its kit changes (a guess)"}])
        self.assertEqual(out["notes"], ["something to know"])
        self.assertIsNone(out["plan_error"])
        (e.dir / "plan-fails").write_text("")
        code, out, _ = self.prepare("update")
        self.assertEqual(code, 200)
        self.assertEqual(out["plan_error"], "plan went wrong")
        self.assertEqual(len(out["commits"]), 1)  # what it printed before failing still shows

    def test_plan_timeout_and_missing_factory(self):
        e = self.env
        with mock.patch.object(dc, "PLAN_TIMEOUT", 0.2):
            e.factory.write_text("#!/bin/sh\nsleep 5\n")
            plan, err = e.controls.plan()
            self.assertIn("took over", err)
        e.controls.factory = str(e.dir / "missing")
        plan, err = e.controls.plan()
        self.assertIn("could not run", err)

    def test_confirm_token_rules(self):
        e = self.env
        e.bind()
        (e.dir / "block").write_text("")
        self.addCleanup(lambda: (e.dir / "block").exists() and (e.dir / "block").unlink())
        token = self.prepare("restart")[1]["confirm"]
        # No token, a wrong one, the right one for the other action: refused (403), nothing runs.
        self.assertEqual(e.post("run", {"action": "restart"})[0], 403)
        self.assertEqual(self.run_action("restart", "x" * len(token))[0], 403)
        self.assertEqual(self.run_action("restart", 5)[0], 403)
        self.assertEqual(e.argv(), [])
        token = self.prepare("restart")[1]["confirm"]
        self.assertEqual(self.run_action("update", token)[0], 403)
        self.assertEqual(self.run_action("restart", token)[0], 403)  # the attempt used it up
        # Expired.
        token = self.prepare("restart")[1]["confirm"]
        e.clock.t += 121
        self.assertEqual(self.run_action("restart", token)[0], 403)
        self.assertEqual(e.argv(), [])
        # A new prepare replaces the old token.
        old = self.prepare("restart")[1]["confirm"]
        new = self.prepare("restart")[1]["confirm"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.run_action("restart", old)[0], 403)
        new = self.prepare("restart")[1]["confirm"]
        code, out, _ = self.run_action("restart", new)
        self.assertEqual((code, out), (202, {"started": True}))
        self.assertEqual(self.run_action("restart", new)[0], 403)  # single use
        # One at a time: no prepare while it runs, and no run either.
        self.assertTrue(dt.wait_for(lambda: e.controls.proc is not None))
        self.assertEqual(self.prepare("update")[0], 409)
        self.assertEqual(self.prepare("restart")[0], 409)
        e.controls.confirm = ("t" * 32, "update", e.clock.t + 100)
        self.assertEqual(self.run_action("update", "t" * 32)[0], 409)
        state = e.post("state")[1]["action"]
        self.assertEqual((state["name"], state["phase"], state["exit"]), ("restart", "running", None))
        # Its own session: Ctrl-C on the dashboard doesn't reach it.
        proc = e.controls.proc
        self.assertEqual(os.getsid(proc.pid), proc.pid)
        (e.dir / "block").unlink()
        self.assertTrue(e.controls.wait())
        self.assertEqual(e.argv(), ["restart"])
        state = e.post("state")[1]["action"]
        self.assertEqual((state["phase"], state["exit"], state["reload"]), ("done", 0, "none"))
        self.assertEqual(state["started"], e.clock.t)
        self.assertEqual(state["ended"], e.clock.t)

    def test_log(self):
        e = self.env
        self.assertEqual(e.post("log", {"offset": 0})[1],
                         {"text": "", "offset": 0, "phase": "idle", "exit": None, "reload": "none"})
        (e.dir / "exit").write_text("4")
        token = self.prepare("update")[1]["confirm"]
        self.assertEqual(self.run_action("update", token)[0], 202)
        self.assertTrue(e.controls.wait())
        code, out, _ = e.post("log", {"offset": 0})
        self.assertEqual(code, 200)
        self.assertEqual(out["text"], "no_attach=1\nstdin=eof\nstep one ✓\n")
        self.assertEqual(out["offset"], len(out["text"].encode()))
        self.assertEqual((out["phase"], out["exit"], out["reload"]), ("failed", 4, "none"))
        self.assertEqual(e.argv(), ["update --plan", "update --yes"])
        more = e.post("log", {"offset": out["offset"]})[1]
        self.assertEqual((more["text"], more["offset"]), ("", out["offset"]))
        self.assertEqual(e.post("log", {"offset": 3})[1]["text"], out["text"][3:])
        for bad in (-1, "0", 1.5, True, None):
            self.assertEqual(e.post("log", {"offset": bad})[0], 400, bad)
        # The log file: under the state folder, private.
        path = e.controls.log_path
        self.assertEqual(path.parent, e.fx.state / "dashboard" / "actions")
        self.assertRegex(path.name, r"^\d{8}-\d{6}-update\.log$")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_log_chunks_and_utf8(self):
        e = self.env
        path = e.fx.state / "x.log"
        data = ("é" * (dc.LOG_CHUNK // 2 + 10)).encode()
        path.write_bytes(data)
        e.controls.log_path = path
        out = e.post("log", {"offset": 1})[1]  # mid-character: replaced, not an error
        self.assertTrue(out["text"].startswith("�"))
        got, offset = "", 0
        while True:
            out = e.post("log", {"offset": offset})[1]
            self.assertLessEqual(len(out["text"].encode()), dc.LOG_CHUNK)
            if not out["text"]:
                break
            got += out["text"]
            offset = out["offset"]
        self.assertEqual(got, data.decode())  # chunk edges never split a character
        self.assertEqual(dc.utf8_cut(b"ab\xc3"), b"ab")
        self.assertEqual(dc.utf8_cut(b"ab\xe2\x9c"), b"ab")
        self.assertEqual(dc.utf8_cut(b"ab\xe2\x9c\x93"), b"ab\xe2\x9c\x93")
        self.assertEqual(dc.utf8_cut(b"\xf0\x9f\x98"), b"")
        self.assertEqual(dc.utf8_cut(b""), b"")

    def test_reload_digest(self):
        e = self.env
        ui, vendor = e.scripts / "dashboard-ui" / "app.js", e.scripts / "dashboard-ui" / "vendor" / "x.js"

        def update(touch, code=0, action="update"):
            (e.dir / "touch").write_text("".join(f"{p}\n" for p in touch))
            (e.dir / "exit").write_text(str(code))
            if action == "restart":
                e.bind()
            token = self.prepare(action)[1]["confirm"]
            self.assertEqual(self.run_action(action, token)[0], 202)
            self.assertTrue(e.controls.wait())
            return e.post("state")[1]["action"]["reload"]

        self.assertEqual(update([]), "none")
        self.assertEqual(update([vendor]), "none")      # vendored files are left out
        self.assertEqual(update([ui], code=1), "none")  # a failed update: nothing to reload for
        self.assertEqual(update([], action="restart"), "none")
        e.controls.digests = dc.dashboard_digests(e.scripts)
        self.assertEqual(update([ui]), "page")
        e.controls.digests = dc.dashboard_digests(e.scripts)
        self.assertEqual(update([e.scripts / "watch.py", ui]), "server")
        e.controls.digests = dc.dashboard_digests(e.scripts)
        self.assertEqual(update([e.scripts / "dashboard_term.py"]), "server")

    def test_factory_that_cannot_start(self):
        e = self.env
        token = self.prepare("update")[1]["confirm"]
        e.controls.factory = str(e.dir / "missing")
        self.assertEqual(self.run_action("update", token)[0], 202)
        self.assertTrue(e.controls.wait())
        out = e.post("log", {"offset": 0})[1]
        self.assertEqual((out["phase"], out["exit"]), ("failed", None))
        self.assertIn("could not start", out["text"])

    def test_argv_is_fixed(self):
        c = self.env.controls
        self.assertEqual(c.argv("restart"), [str(self.env.factory), "restart"])
        self.assertEqual(c.argv("update"), [str(self.env.factory), "update", "--yes"])
        self.assertEqual(dc.FACTORY, Path(dc.__file__).resolve().parent.parent / "bin" / "factory")


class ReviewFixTests(unittest.TestCase):
    """Fixes from the security review: each action's own log, bounded log
    offsets, the plan's process group, serialised plans, the reload digest
    reported once, and ambiguous or unsafe card ids."""

    def setUp(self):
        self.env = Env(self.addCleanup)

    def go(self, action):
        e = self.env
        if action == "restart":
            e.bind()
        token = e.post("prepare", {"action": action})[1]["confirm"]
        self.assertEqual(e.post("run", {"action": action, "confirm": token})[0], 202)

    def test_a_new_action_never_shows_the_last_ones_log(self):
        e = self.env
        self.go("restart")
        self.assertTrue(e.controls.wait())
        first = e.post("log", {"offset": 0})[1]
        self.assertIn("step one", first["text"])
        first_path = e.controls.log_path
        (e.dir / "block").write_text("")
        self.addCleanup(lambda: (e.dir / "block").exists() and (e.dir / "block").unlink())
        self.go("restart")
        # Right after the 202: already the new log, whatever it holds yet.
        self.assertNotEqual(e.controls.log_path, first_path)
        now = e.post("log", {"offset": 0})[1]
        self.assertNotIn("step one", now["text"])
        self.assertEqual(now["phase"], "running")
        self.assertTrue(dt.wait_for(lambda: e.controls.log_path.stat().st_size > 0))
        # The page still holds the old log's offset, past the new log's end: from the top.
        self.assertGreater(first["offset"], e.controls.log_path.stat().st_size)
        again = e.post("log", {"offset": first["offset"]})[1]
        self.assertTrue(again["text"].startswith("no_attach=1"), again)
        self.assertEqual(again["offset"], len(again["text"].encode()))
        (e.dir / "block").unlink()
        self.assertTrue(e.controls.wait())

    def test_log_offset_bounds(self):
        e = self.env
        self.go("update")
        self.assertTrue(e.controls.wait())
        for bad in (10 ** 30, 2 ** 53 + 1, -(10 ** 30)):
            self.assertEqual(e.post("log", {"offset": bad})[0], 400, bad)
        code, out, _ = e.post("log", {"offset": 2 ** 53})
        self.assertEqual(code, 200)
        self.assertTrue(out["text"].startswith("no_attach=1"))  # past the end: from the top
        # A seek that fails anyway answers, rather than killing the handler.
        with mock.patch.object(dc, "MAX_OFFSET", 10 ** 30):
            code, out, _ = e.post("log", {"offset": 10 ** 30})
            self.assertEqual(code, 200)
        self.assertEqual(e.post("state")[0], 200)

    def test_plan_timeout_kills_its_process_group(self):
        e = self.env
        pidfile = e.dir / "child.pid"
        e.factory.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > '{pidfile}'\nwait\n")
        with mock.patch.object(dc, "PLAN_TIMEOUT", 0.5):
            _, err = e.controls.plan()
        self.assertIn("took over", err)
        pid = int(pidfile.read_text())

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            # A zombie that init hasn't reaped yet counts as gone.
            try:
                return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
            except OSError:
                return False
        self.assertTrue(dt.wait_for(gone), "the plan's child (a hanging git fetch, say) survived")

    def test_plans_run_one_at_a_time(self):
        e = self.env
        trace = e.dir / "trace"
        e.factory.write_text(f"#!/bin/sh\necho start >> '{trace}'\nsleep 0.3\necho end >> '{trace}'\n")
        import threading
        threads = [threading.Thread(target=e.controls.api, args=("prepare", {"action": "update"})) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(trace.read_text().split(), ["start", "end"] * 3)

    def test_page_reload_reported_once(self):
        e = self.env
        (e.dir / "touch").write_text(f"{e.scripts / 'dashboard-ui' / 'app.js'}\n")
        self.go("update")
        self.assertTrue(e.controls.wait())
        self.assertEqual(e.controls.reload, "page")
        (e.dir / "touch").write_text("")
        self.go("update")
        self.assertTrue(e.controls.wait())
        self.assertEqual(e.controls.reload, "none")

    def test_two_cards_with_one_id(self):
        e = self.env
        card = write_card(e.fx.state, "dup", e.fx.now)
        pending = e.fx.state / "broker" / "approvals" / "pending"
        (pending / "dup-copy.json").write_text(json.dumps(dict(card, nonce="other")))
        code, out, _ = e.post("decide", {"id": "dup", "fp": approvals.card_fingerprint(card), "verdict": "approve"})
        self.assertEqual((code, out), (409, {"error": "ambiguous"}))
        self.assertFalse((e.fx.state / "broker" / "approvals" / "decisions").exists())

    def test_unsafe_card_ids(self):
        e = self.env
        pending = e.fx.state / "broker" / "approvals" / "pending"
        for name, cid in (("dots", "../../evil"), ("long", "a" * 65), ("space", "a b")):
            card = {"id": cid, "tool": "git_push", "expires": e.fx.now + 60, "nonce": "n"}
            (pending / f"{name}.json").write_text(json.dumps(card))
            code, out, _ = e.post("decide", {"id": cid, "fp": approvals.card_fingerprint(card), "verdict": "approve"})
            self.assertEqual(code, 400, cid)
        (pending / "int.json").write_text(json.dumps({"id": 12345, "tool": "git_push", "expires": e.fx.now + 60}))
        self.assertEqual(e.post("state")[0], 200)  # a non-text id is skipped, not a crash
        self.assertFalse((e.fx.state / "broker" / "approvals" / "decisions").exists())
        self.assertFalse((e.fx.state / "evil.json").exists())


class TermAroundActionTests(unittest.TestCase):
    """Terminal 0 hangs up before an action, refuses POWER ON during it, and
    attaches again after one that succeeded."""

    def setUp(self):
        self.term = fake_term()
        self.addCleanup(self.term.shutdown)
        self.env = Env(self.addCleanup, term=self.term, serve=False)
        self.env.bind()
        self.c = self.env.controls

    def start(self, action="restart", block=True):
        if block:
            (self.env.dir / "block").write_text("")
        token = self.c.api("prepare", {"action": action})[1]["confirm"]
        self.assertEqual(self.c.api("run", {"action": action, "confirm": token})[0], 202)
        self.assertTrue(dt.wait_for(lambda: self.c.proc is not None or self.c.phase != "running"))

    def test_hang_up_hold_and_repower(self):
        t = self.term
        t.power_on()
        self.assertTrue(t.running())
        self.start()
        self.assertFalse(t.running())
        self.assertEqual((t.held, t.reason), ("service", "service"))
        t.power_on()  # a page's POWER ON while it runs: refused
        self.assertFalse(t.running())
        self.assertEqual(t.state(), {"type": "state", "running": False, "reason": "service"})
        (self.env.dir / "block").unlink()
        self.assertTrue(self.c.wait())
        self.assertIsNone(t.held)
        self.assertTrue(dt.wait_for(t.running))  # attached before, and it succeeded: attached again

    def test_no_repower_after_a_failure_or_when_not_attached(self):
        t = self.term
        t.power_on()
        (self.env.dir / "exit").write_text("1")
        self.start(block=False)
        self.assertTrue(self.c.wait())
        self.assertFalse(t.running())
        self.assertIsNone(t.held)
        self.assertIsNone(t.reason)  # released: plain powered off, POWER ON works again
        t.power_on()
        self.assertTrue(t.running())
        t.hang_up("service")
        (self.env.dir / "exit").write_text("0")
        self.start(block=False)  # not attached when it started
        self.assertTrue(self.c.wait())
        time.sleep(0.2)
        self.assertFalse(t.running())

    def test_hang_up_keeps_the_term_usable(self):
        t = self.term
        t.hang_up("service")  # nothing attached: nothing to do
        t.power_on()
        t.hang_up("service")
        self.assertFalse(t.running())
        self.assertEqual(t.reason, "service")
        self.assertTrue(dt.wait_for(lambda: t.master is None))
        self.assertEqual(t.reason, "service")  # the pump doesn't overwrite it with "exited"
        t.power_on()
        self.assertTrue(t.running())


# --------------------------------------------------------------------------
# The /cu page

class PageTests(unittest.TestCase):
    def setUp(self):
        self.ui = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ui)
        (self.ui / "cu.html").write_text(
            '<meta name="cu-key" content="__CU_KEY__"><meta name="cu-main-port" content="__CU_MAIN_PORT__">')
        patcher = mock.patch.object(dt, "UI", self.ui)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = Env(self.addCleanup)

    def test_served_only_to_a_same_site_iframe(self):
        e = self.env
        cases = {
            "no Sec-Fetch headers": ({"Sec-Fetch-Dest": None, "Sec-Fetch-Site": None}, 403),
            "top-level navigation": ({"Sec-Fetch-Dest": "document"}, 403),
            "window.open": ({"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "same-site"}, 403),
            "cross-site iframe": ({"Sec-Fetch-Site": "cross-site"}, 403),
            "address bar": ({"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "none"}, 403),
            "no cookie": ({"Cookie": None}, 403),
            "foreign Host": ({"Host": "evil.example"}, 403),
            "same-site iframe": ({}, 200),
            "same-origin iframe": ({"Sec-Fetch-Site": "same-origin"}, 200),
        }
        for name, (over, code) in cases.items():
            resp, body = e.get("/cu", **over)
            self.assertEqual(resp.status, code, name)
            if code != 200:
                self.assertNotIn(e.key, body.decode(), name)
        resp, body = e.get("/cu")
        html = body.decode()
        self.assertIn(f'content="{e.key}"', html)
        self.assertIn(f'name="cu-main-port" content="{e.port}"', html)
        self.assertNotIn("__CU_", html)
        self.assertEqual(resp.getheader("Content-Security-Policy"), dt.cu_csp(e.tport, e.port))
        self.assertIsNone(resp.getheader("X-Frame-Options"))
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_csp_exact(self):
        self.assertEqual(dt.cu_csp(5000, 4000), (
            "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "connect-src http://127.0.0.1:5000 http://localhost:5000; img-src 'self'; "
            "frame-ancestors http://127.0.0.1:4000 http://localhost:4000; base-uri 'none'; form-action 'none'"))
        self.assertNotIn("unsafe", dt.cu_csp(5000, 4000))

    def test_the_key_stays_on_its_page(self):
        e = self.env
        for path in ("/api/state", "/"):
            resp, body = e.get(path, port=e.port)
            self.assertNotIn(e.key, body.decode(), path)
        state = json.loads(e.get("/api/state", port=e.port)[1])
        self.assertEqual(state["cu"], {"port": e.tport})
        self.assertNotIn("fp", json.dumps(state["gate"]))
        # Main page's CSP may frame the second origin.
        resp, _ = e.get("/", port=e.port)
        self.assertEqual(resp.getheader("Content-Security-Policy"), d.page_csp(e.tport))

    def test_without_a_coordinator(self):
        e = self.env  # started without a Term
        self.assertEqual(e.get("/term")[0].status, 404)
        self.assertEqual(e.get("/cu")[0].status, 200)
        self.assertEqual(e.post("state")[0], 200)
        self.assertIsNone(json.loads(e.get("/api/state", port=e.port)[1])["term"])

    def test_without_controls(self):
        main, _ = d.serve(self.env.board, 0, TOKEN)
        self.addCleanup(d.stop, main)
        tport = main.term_server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", tport, timeout=10)
        conn.request("GET", "/cu", headers={"Host": f"127.0.0.1:{tport}",
                                            "Cookie": f"{d.cookie_name(main.server_address[1])}={TOKEN}",
                                            "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-site"})
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()


class KeySeparationTests(unittest.TestCase):
    def test_terminal_key_is_not_the_control_key(self):
        term = fake_term()
        self.addCleanup(term.shutdown)
        e = Env(self.addCleanup, term=term)
        term_key = re.search(r'name="term-key" content="([0-9a-f]+)"', e.get("/term")[1].decode()).group(1)
        self.assertNotEqual(term_key, e.key)
        self.assertEqual(e.post("state", **{"X-Factory-Control": term_key})[0], 403)
        self.assertEqual(e.post("state")[0], 200)


class RealPageTests(unittest.TestCase):
    """The control unit's real files (scripts/dashboard-ui/cu.*), once they exist."""

    def setUp(self):
        if not (UI / "cu.html").exists():
            self.skipTest("no cu.html yet")

    def test_page_obeys_its_csp(self):
        html = (UI / "cu.html").read_text()
        self.assertEqual(html.count(dt.CU_KEY_SLOT), 1)
        self.assertEqual(html.count(dt.CU_PORT_SLOT), 1)
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "inline script")
        self.assertIsNone(re.search(r"\sstyle=", html), "inline style attribute")
        self.assertIsNone(re.search(r"\son[a-z]+=", html), "inline event handler")
        self.assertNotIn("<style", html)
        js = (UI / "cu.js").read_text() if (UI / "cu.js").exists() else ""
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, js, f"cu.js uses {sink}")
        css = (UI / "cu.css").read_text() if (UI / "cu.css").exists() else ""
        self.assertIsNone(re.search(r"https?://", html + css), "network URL")


if __name__ == "__main__":
    unittest.main()
