"""Tests for scripts/dashboard_term.py (Terminal 0, the dashboard's Coordinator
terminal): the WebSocket handshake's accept/refuse matrix, frame encoding and
decoding, the PTY bridge (with a fake command standing in for `sbx run`), the
"open elsewhere" check, both pages' CSPs, and the vendored xterm.js.

No sbx runs here: every Term gets a small Python program as its command.

  python3 -m unittest discover -s scripts -p 'test_dashboard_term.py' -v
"""

import base64
import hashlib
import http.client
import io
import json
import os
import re
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import dashboard as d
import dashboard_term as dt
from test_dashboard import TOKEN, Fixture

UI = Path(__file__).resolve().parent / "dashboard-ui"
KEY = "k" * 48

# The fake Coordinator: reports its window size at start and on SIGWINCH,
# echoes lines, exits on "exit".
FAKE = r"""
import fcntl, os, signal, struct, sys, termios
def size():
    rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))[:2]
    return f"{cols}x{rows}"
signal.signal(signal.SIGWINCH, lambda *a: print("WINCH " + size(), flush=True))
print("READY " + size() + " tty=" + str(os.isatty(0)), flush=True)
for line in sys.stdin:
    line = line.strip()
    if line == "exit":
        break
    print("ECHO:" + line, flush=True)
"""


def fake_term(**kw):
    kw.setdefault("exists", lambda: True)
    kw.setdefault("elsewhere", lambda own: [])
    return dt.Term([sys.executable, "-u", "-c", FAKE], "factory-coordinator", **kw)


class Collector:
    """A stand-in page: records what a Term sends it."""

    def __init__(self):
        self.frames = []

    def text(self, obj):
        self.frames.append(("text", obj))
        return True

    def binary(self, data):
        self.frames.append(("binary", data))
        return True

    def close(self, frame=None):
        pass

    def output(self):
        return b"".join(f for k, f in self.frames if k == "binary").decode("utf-8", "replace")

    def states(self):
        return [f for k, f in self.frames if k == "text"]


# --------------------------------------------------------------------------
# Frames

def client_frame(opcode, payload=b"", fin=True):
    return dt.encode_frame(opcode, payload, fin, mask=b"\x01\x02\x03\x04")


class FrameTests(unittest.TestCase):
    def roundtrip(self, payload):
        fin, op, data = dt.read_frame(io.BytesIO(client_frame(dt.BINARY, payload)), max_size=1 << 20)
        self.assertTrue(fin)
        self.assertEqual(op, dt.BINARY)
        self.assertEqual(data, payload)

    def test_lengths(self):
        for n in (0, 1, 125, 126, 127, 65535, 65536, 70000):
            self.roundtrip(os.urandom(n))

    def test_server_encoding(self):
        self.assertEqual(dt.encode_frame(dt.TEXT, b"hi"), b"\x81\x02hi")
        self.assertEqual(dt.encode_frame(dt.BINARY, b"x" * 200)[:4], b"\x82\x7e\x00\xc8")
        self.assertEqual(dt.encode_frame(dt.BINARY, b"x" * 70000)[:10], b"\x82\x7f" + struct.pack("!Q", 70000))
        self.assertEqual(dt.close_frame(1000), b"\x88\x02\x03\xe8")

    def test_masking_is_applied(self):
        raw = client_frame(dt.TEXT, b"abcd")
        self.assertEqual(raw[1] & 0x80, 0x80)
        self.assertEqual(raw[2:6], b"\x01\x02\x03\x04")
        self.assertEqual(raw[6:], bytes([0x61 ^ 1, 0x62 ^ 2, 0x63 ^ 3, 0x64 ^ 4]))

    def test_unmasked_refused(self):
        with self.assertRaises(dt.ProtocolError) as e:
            dt.read_frame(io.BytesIO(dt.encode_frame(dt.TEXT, b"hi")))
        self.assertEqual(e.exception.code, 1002)

    def test_fragmentation_and_interleaved_ping(self):
        pings = []
        raw = (client_frame(dt.TEXT, b"hel", fin=False) + client_frame(dt.PING, b"p")
               + client_frame(dt.CONT, b"lo", fin=False) + client_frame(dt.CONT, b"!"))
        op, data = dt.read_message(io.BytesIO(raw), lambda o, p: pings.append((o, p)))
        self.assertEqual((op, data), (dt.TEXT, b"hello!"))
        self.assertEqual(pings, [(dt.PING, b"p")])

    def test_close(self):
        op, data = dt.read_message(io.BytesIO(client_frame(dt.CLOSE, struct.pack("!H", 1000))), lambda o, p: None)
        self.assertEqual(op, dt.CLOSE)
        self.assertEqual(data, b"\x03\xe8")

    def test_protocol_errors(self):
        cases = [
            (client_frame(dt.CONT, b"x"), 1002),                                 # continuation first
            (client_frame(dt.TEXT, b"a", fin=False) + client_frame(dt.TEXT, b"b"), 1002),  # new message mid-way
            (client_frame(dt.PING, b"x" * 126), 1002),                           # long control frame
            (client_frame(dt.PING, b"x", fin=False), 1002),                      # fragmented control frame
            (bytes([0x83, 0x80]) + b"\0\0\0\0", 1002),                          # unknown opcode
            (bytes([0xC2, 0x80]) + b"\0\0\0\0", 1002),                          # RSV1 set
            (client_frame(dt.TEXT, b"\xff\xfe"), 1007),                          # not UTF-8
            (client_frame(dt.BINARY, b"x" * (dt.MAX_MESSAGE + 1)), 1009),       # one big frame
            (client_frame(dt.BINARY, b"x" * dt.MAX_MESSAGE, fin=False) + client_frame(dt.CONT, b"x"), 1009),
        ]
        for raw, code in cases:
            with self.assertRaises(dt.ProtocolError, msg=raw[:4]) as e:
                dt.read_message(io.BytesIO(raw), lambda o, p: None)
            self.assertEqual(e.exception.code, code, raw[:4])

    def test_truncated(self):
        with self.assertRaises(EOFError):
            dt.read_frame(io.BytesIO(client_frame(dt.BINARY, b"abcdef")[:-2]))

    def test_accept_key(self):  # RFC 6455 section 1.3's example
        self.assertEqual(dt.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


# --------------------------------------------------------------------------
# "Open elsewhere"

PS = """\
    1     0 /sbin/init
  100     1 /usr/bin/sbx daemon start
  200   150 sbx run --name factory-coordinator
  201   150 sbx run --name factory-coordinator-old
  202   150 /usr/local/bin/sbx run --name=factory-coordinator --resume
  300   299 python3 dashboard.py --coordinator factory-coordinator
  400   300 sbx run --name factory-coordinator
  401   400 sbx run --name factory-coordinator
  500     1 grep sbx run --name factory-coordinator
  600     1 sbx exec factory-coordinator true
"""


class ElsewhereTests(unittest.TestCase):
    def test_finds_other_attachments(self):
        self.assertEqual(dt.attached_pids(PS, "factory-coordinator", [300]), [200, 202])

    def test_nothing_else(self):
        self.assertEqual(dt.attached_pids(PS, "factory-coordinator", [300, 150]), [])
        self.assertEqual(dt.attached_pids("", "factory-coordinator"), [])
        self.assertEqual(dt.attached_pids("garbage\n  x y z\n", "factory-coordinator"), [])

    def test_refused_when_elsewhere(self):
        t = fake_term(elsewhere=lambda own: dt.attached_pids(PS, "factory-coordinator", own))
        page = Collector()
        t.attach(page)
        self.assertFalse(t.running())
        self.assertEqual(page.states()[-1], {"type": "state", "running": False, "reason": "elsewhere"})

    def test_missing_sandbox_and_sbx(self):
        for exists, reason in ((lambda: False, "no-sandbox"), (lambda: None, "no-sbx")):
            t = fake_term(exists=exists)
            page = Collector()
            t.attach(page)
            self.assertFalse(t.running())
            self.assertEqual(page.states()[-1]["reason"], reason)

    def test_argv_is_fixed(self):
        self.assertEqual(dt.coordinator_argv("factory-coordinator"), ["sbx", "run", "--name", "factory-coordinator"])
        for bad in ("", "-x", "a b", "a;b", "A", "../x", "x\n"):
            with self.assertRaises(ValueError):
                dt.coordinator_argv(bad)


# --------------------------------------------------------------------------
# The PTY bridge

class PtyTests(unittest.TestCase):
    def setUp(self):
        self.term = fake_term()
        self.addCleanup(self.term.shutdown)

    def test_broadcast_replay_input_resize_exit_restart(self):
        t = self.term
        a = Collector()
        t.resize(100, 30)
        t.attach(a)  # the first page powers it on
        self.assertTrue(t.running())
        self.assertEqual(a.states()[0], {"type": "state", "running": True, "reason": None})
        self.assertTrue(dt.wait_for(lambda: "READY 100x30 tty=True" in a.output()), a.output())

        b = Collector()  # a late page gets the state, then the replay
        t.attach(b)
        self.assertEqual(b.frames[0], ("text", {"type": "state", "running": True, "reason": None}))
        self.assertIn("READY 100x30", b.output())

        t.write(b"hello\r")
        for page in (a, b):
            self.assertTrue(dt.wait_for(lambda: "ECHO:hello" in page.output()), page.output())

        t.control(json.dumps({"type": "resize", "cols": 120, "rows": 40}).encode())
        self.assertTrue(dt.wait_for(lambda: "WINCH 120x40" in a.output()), a.output())
        for bad in ({"type": "resize", "cols": 1, "rows": 40}, {"type": "resize", "cols": 501, "rows": 40},
                    {"type": "resize", "cols": "80", "rows": 24}, {"type": "resize", "cols": True, "rows": 24}):
            t.control(json.dumps(bad).encode())
            self.assertEqual(t.size, (120, 40))
        t.control(b"not json")
        t.control(b"[1, 2]")

        t.write(b"exit\r")
        exited = {"type": "state", "running": False, "reason": "exited"}
        for page in (a, b):
            self.assertTrue(dt.wait_for(lambda: exited in page.states()), page.states())
        self.assertFalse(t.running())
        t.write(b"ignored\r")  # nothing to write to: dropped, no error

        c = Collector()  # a page arriving after the exit: powered off, not restarted
        t.attach(c)
        self.assertFalse(t.running())
        self.assertEqual(c.states(), [exited])

        t.control(json.dumps({"type": "power", "on": True}).encode())
        self.assertTrue(t.running())
        for page in (a, b, c):
            self.assertTrue(dt.wait_for(lambda: page.states()[-1]["running"]), page.states())
        self.assertTrue(dt.wait_for(lambda: c.output().count("READY 120x40") == 1), c.output())

    def test_ring_buffer_keeps_the_tail(self):
        t = fake_term(ring=64)
        self.addCleanup(t.shutdown)
        t.attach(Collector())
        for i in range(20):
            t.write(f"line{i}\r".encode())
        late = Collector()
        self.assertTrue(dt.wait_for(lambda: "ECHO:line19" in bytes(t.buffer).decode("utf-8", "replace")))
        t.attach(late)
        replay = late.output()
        self.assertLessEqual(len(replay.encode()), 64)
        self.assertIn("ECHO:line19", replay)

    def test_client_cap(self):
        t = fake_term(max_clients=2)
        self.addCleanup(t.shutdown)
        self.assertTrue(t.attach(Collector(), autostart=False))
        self.assertTrue(t.attach(Collector(), autostart=False))
        self.assertFalse(t.attach(Collector(), autostart=False))
        self.assertFalse(t.started)

    def test_shutdown_hangs_up(self):
        t = self.term
        t.attach(Collector())
        proc = t.proc
        t.shutdown()
        self.assertIsNotNone(proc.poll())


class SlowClientTests(unittest.TestCase):
    def test_overflow_drops_only_that_client(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        c = dt.Client(a, backlog=1000)
        # Nobody reads b: the socket buffer fills, then the queue, then it is dropped.
        sent = 0
        while c.send(dt.encode_frame(dt.BINARY, b"x" * 500)):
            sent += 1
            self.assertLess(sent, 100000)
        self.assertTrue(c.closed)


# --------------------------------------------------------------------------
# The terminal origin over HTTP and WebSocket

class Server:
    """The main dashboard and its terminal origin, with a fake Coordinator."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        fx = Fixture(Path(self.tmp.name))
        self.term = fake_term()
        self.main, _ = d.serve(d.Dashboard(fx.state, fx.roles, "proj"), 0, TOKEN, term=self.term)
        self.port = self.main.server_address[1]
        self.tport = self.main.term_server.server_address[1]
        self.cookie = f"{d.cookie_name(self.port)}={TOKEN}"
        # The terminal key, as only the terminal page knows it.
        self.key = re.search(r'name="term-key" content="([0-9a-f]+)"', self.get("/term")[1].decode()).group(1)

    def close(self):
        d.stop(self.main, self.term)
        self.tmp.cleanup()

    def get(self, path, port=None, headers=None, method="GET"):
        port = port or self.tport
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": f"127.0.0.1:{port}", "Cookie": self.cookie}
        h.update(headers or {})
        conn.request(method, path, headers={k: v for k, v in h.items() if v is not None})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def upgrade_headers(self, **over):
        h = {"Host": f"127.0.0.1:{self.tport}", "Upgrade": "websocket", "Connection": "keep-alive, Upgrade",
             "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": base64.b64encode(b"0123456789abcdef").decode(),
             "Origin": f"http://127.0.0.1:{self.tport}", "Cookie": self.cookie,
             "Sec-WebSocket-Protocol": f"chat, {dt.PROTOCOL}{self.key}"}
        h.update(over)
        return {k: v for k, v in h.items() if v is not None}

    def ws(self, **over):
        """Open a WebSocket: (socket, file, status line, response headers)."""
        s = socket.create_connection(("127.0.0.1", self.tport), timeout=5)
        req = "GET /term/ws HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in self.upgrade_headers(**over).items()) + "\r\n"
        s.sendall(req.encode())
        f = s.makefile("rb")
        status = f.readline().decode()
        headers = {}
        while True:
            line = f.readline().decode().strip()
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        return s, f, status, headers


def read_server_frame(f):
    b0, b1 = f.read(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", f.read(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", f.read(8))[0]
    assert not b1 & 0x80, "server frames are never masked"
    return b0 & 0x0F, f.read(n)


class WsPage:
    def __init__(self, srv, **over):
        self.sock, self.f, self.status, self.headers = srv.ws(**over)
        self.out, self.states = b"", []

    def pump_until(self, cond, limit=200):
        for _ in range(limit):
            if cond():
                return True
            op, data = read_server_frame(self.f)
            if op == dt.BINARY:
                self.out += data
            elif op == dt.TEXT:
                self.states.append(json.loads(data))
            elif op in (dt.CLOSE, dt.PONG):
                self.states.append({"op": op, "data": data})
        return cond()

    def send(self, opcode, payload):
        self.sock.sendall(client_frame(opcode, payload))

    def close(self):
        self.sock.close()


class WebSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def page(self, **over):
        p = WsPage(self.srv, **over)
        self.addCleanup(p.close)
        return p

    def test_refusals(self):
        srv = self.srv
        other = base64.b64encode(b"short").decode()
        cases = {
            "bad Origin": ({"Origin": f"http://127.0.0.1:{srv.port}"}, "403"),
            "evil Origin": ({"Origin": "http://evil.example"}, "403"),
            "null Origin": ({"Origin": "null"}, "403"),
            "no Origin": ({"Origin": None}, "403"),
            "no key": ({"Sec-WebSocket-Protocol": None}, "403"),
            "wrong key": ({"Sec-WebSocket-Protocol": f"{dt.PROTOCOL}{'0' * 48}"}, "403"),
            "key twice": ({"Sec-WebSocket-Protocol": f"{dt.PROTOCOL}{srv.key}, {dt.PROTOCOL}x"}, "403"),
            "key without prefix": ({"Sec-WebSocket-Protocol": srv.key}, "403"),
            "wrong Host": ({"Host": "evil.example"}, "403"),
            "main port Host": ({"Host": f"127.0.0.1:{srv.port}"}, "403"),
            "no cookie": ({"Cookie": None}, "403"),
            "wrong cookie": ({"Cookie": f"{d.cookie_name(srv.port)}=nope"}, "403"),
            "two cookies": ({"Cookie": f"{srv.cookie}; {srv.cookie}"}, "403"),
            "version 8": ({"Sec-WebSocket-Version": "8"}, "426"),
            "bad nonce": ({"Sec-WebSocket-Key": other}, "400"),
            "no upgrade": ({"Upgrade": None}, "400"),
        }
        for name, (over, code) in cases.items():
            s, f, status, _ = srv.ws(**over)
            s.close()
            self.assertIn(f" {code} ", status, name)
        self.assertFalse(srv.term.started, "a refused upgrade must not power on the Coordinator")

    def test_session(self):
        srv = self.srv
        a = self.page()
        self.assertIn(" 101 ", a.status)
        self.assertEqual(a.headers["sec-websocket-protocol"], f"{dt.PROTOCOL}{srv.key}")
        self.assertEqual(a.headers["sec-websocket-accept"], dt.accept_key(base64.b64encode(b"0123456789abcdef").decode()))
        a.send(dt.TEXT, json.dumps({"type": "resize", "cols": 90, "rows": 20}).encode())
        self.assertTrue(a.pump_until(lambda: b"READY" in a.out))
        self.assertEqual(a.states[0], {"type": "state", "running": True, "reason": None})

        b = self.page(Origin=f"http://localhost:{srv.tport}", Host=f"localhost:{srv.tport}")
        self.assertIn(" 101 ", b.status)
        self.assertTrue(b.pump_until(lambda: b"READY" in b.out))  # the replay

        b.send(dt.BINARY, b"from b\r")
        self.assertTrue(a.pump_until(lambda: b"ECHO:from b" in a.out))
        self.assertTrue(b.pump_until(lambda: b"ECHO:from b" in b.out))
        self.assertTrue(dt.wait_for(lambda: srv.term.size == (90, 20)))

        a.send(dt.PING, b"hi")
        self.assertTrue(a.pump_until(lambda: {"op": dt.PONG, "data": b"hi"} in a.states))

        # A protocol error closes that page only.
        b.sock.sendall(dt.encode_frame(dt.BINARY, b"unmasked"))
        self.assertTrue(b.pump_until(lambda: any(s.get("op") == dt.CLOSE for s in b.states)))
        close = next(s for s in b.states if s.get("op") == dt.CLOSE)
        self.assertEqual(struct.unpack("!H", close["data"][:2])[0], 1002)

        a.send(dt.BINARY, b"still here\r")
        self.assertTrue(a.pump_until(lambda: b"ECHO:still here" in a.out))
        a.send(dt.CLOSE, struct.pack("!H", 1000))
        self.assertTrue(a.pump_until(lambda: any(s.get("op") == dt.CLOSE for s in a.states)))

    def test_http_on_the_terminal_origin(self):
        srv = self.srv
        resp, body = srv.get("/term")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Security-Policy"), dt.term_csp(srv.tport, srv.port))
        self.assertIsNone(resp.getheader("X-Frame-Options"))
        self.assertNotIn(dt.KEY_SLOT, body.decode())
        for path in dt.STATIC:
            self.assertEqual(srv.get(path)[0].status, 200, path)
        self.assertEqual(srv.get("/term", headers={"Cookie": None})[0].status, 403)
        self.assertEqual(srv.get("/term.js", headers={"Cookie": None})[0].status, 403)
        self.assertEqual(srv.get("/term", headers={"Host": f"127.0.0.1:{srv.port}"})[0].status, 403)
        self.assertEqual(srv.get("/", )[0].status, 404)       # the main page is not served here
        self.assertEqual(srv.get("/api/state")[0].status, 404)
        self.assertEqual(srv.get("/term.html")[0].status, 404)
        self.assertEqual(srv.get("/../dashboard.py")[0].status, 404)
        self.assertEqual(srv.get("/term", method="POST")[0].status, 405)

    def test_main_page_never_sees_the_key(self):
        srv = self.srv
        resp, body = srv.get("/api/state", port=srv.port)
        self.assertEqual(resp.status, 200)
        state = json.loads(body)
        self.assertEqual(state["term"]["port"], srv.tport)
        self.assertNotIn(srv.key, body.decode())
        resp, body = srv.get("/", port=srv.port)
        self.assertNotIn(srv.key, body.decode())


class CspTests(unittest.TestCase):
    def test_main_csp(self):
        self.assertEqual(d.page_csp(None), d.CSP)
        csp = d.page_csp(4321)
        self.assertEqual(csp, d.CSP + "; frame-src http://127.0.0.1:4321 http://localhost:4321")
        self.assertNotIn("unsafe", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_term_csp_exact(self):
        self.assertEqual(dt.term_csp(5000, 4000), (
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src ws://127.0.0.1:5000 ws://localhost:5000; font-src 'self'; "
            "frame-ancestors http://127.0.0.1:4000 http://localhost:4000; base-uri 'none'; form-action 'none'"))

    def test_main_page_served_with_frame_src(self):
        srv = Server()
        try:
            resp, _ = srv.get("/", port=srv.port)
            self.assertEqual(resp.getheader("Content-Security-Policy"), d.page_csp(srv.tport))
        finally:
            srv.close()


class VendorTests(unittest.TestCase):
    def test_xterm_matches_checksums(self):
        vendor = UI / "vendor" / "xterm"
        sums = dict(reversed(line.split()) for line in (vendor / "SHA256SUMS").read_text().splitlines() if line.strip())
        self.assertEqual(set(sums), {"xterm.js", "xterm.css", "addon-fit.js"})
        served = {Path(name).name for name, _ in dt.STATIC.values() if name.startswith("vendor/")}
        self.assertEqual(served, set(sums))
        for name, digest in sums.items():
            data = (vendor / name).read_bytes()
            self.assertNotIn(b"\r\n", data, name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)
        self.assertEqual({p.name for p in vendor.glob("LICENSE-*.txt")}, {"LICENSE-xterm.txt", "LICENSE-addon-fit.txt"})

    def test_term_page(self):
        js = (UI / "term.js").read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                     "ClipboardAddon", "WebLinksAddon", "ImageAddon", "window.open"):
            self.assertNotIn(sink, js, f"term.js uses {sink}")
        self.assertIn("allowProposedApi: false", js)
        html = (UI / "term.html").read_text()
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "inline script")
        self.assertIsNone(re.search(r"\son[a-z]+=", html), "inline event handler")
        self.assertEqual(html.count(dt.KEY_SLOT), 1)
        self.assertIsNone(re.search(r"https?://", html + js + (UI / "term.css").read_text()), "network URL")

    def test_main_page_frames_with_sandbox(self):
        js = (UI / "app.js").read_text()
        self.assertIn('setAttribute("sandbox", "allow-scripts allow-same-origin")', js)
        self.assertNotIn("term-key", js)


if __name__ == "__main__":
    unittest.main()
