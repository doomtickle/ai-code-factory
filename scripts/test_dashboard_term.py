"""Tests for scripts/dashboard_term.py (Terminal 0, the dashboard's Coordinator
terminal): the WebSocket handshake's accept/refuse matrix, frame encoding and
decoding, the PTY bridge (with a fake command standing in for `sbx run`), the
"open elsewhere" check, both pages' CSPs, the vendored xterm.js, and term.js's
OSC 52 copy prompt (under node, with scripts/test_term_page.js).

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
import shutil
import socket
import struct
import subprocess
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


POWER_ON = json.dumps({"type": "power", "on": True}).encode()


class Collector:
    """A stand-in page: records what a Term sends it."""

    def __init__(self):
        self.frames = []
        self.killed = False

    def text(self, obj):
        self.frames.append(("text", obj))
        return True

    def binary(self, data):
        self.frames.append(("binary", data))
        return True

    def send(self, frame):
        self.frames.append(("raw", frame))
        return True

    def close(self, frame=None):
        self.frames.append(("close", frame))

    def kill(self):
        self.killed = True

    def output(self):
        return b"".join(f for k, f in self.frames if k == "binary").decode("utf-8", "replace")

    def states(self):
        return [f for k, f in self.frames if k == "text"]

    def pings(self):
        return sum(1 for k, f in self.frames if k == "raw" and f[0] & 0x0F == dt.PING)


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
  203   150 sbx run factory-coordinator
  204   150 sbx -D run claude --name factory-coordinator
  205   150 sbx run claude -- --name factory-coordinator
  206   150 sbx --debug run claude factory-coordinator-old
  300   299 python3 dashboard.py --coordinator factory-coordinator
  400   300 sbx run --name factory-coordinator
  401   400 sbx run --name factory-coordinator
  500     1 grep sbx run --name factory-coordinator
  600     1 sbx exec factory-coordinator true
  207   150 sbx run --name factory-coordinator -- --continue
  208   150 sbx run --name factory-coordinator-old -- --continue
"""


RESUME_ON = json.dumps({"type": "power", "on": True, "resume": True}).encode()


class ResumeTests(unittest.TestCase):
    """POWER ON starts the fixed fresh argv, RESUME the fixed `--continue` one; nothing else starts anything."""

    def started(self, payload):
        t = fake_term()
        self.addCleanup(t.shutdown)
        t.attach(Collector())
        t.control(payload)
        return t

    def test_power_on_is_fresh_and_resume_continues(self):
        for payload, want in ((POWER_ON, "argv"), (RESUME_ON, "resume_argv"),
                              (json.dumps({"type": "power", "on": True, "resume": False}).encode(), "argv")):
            t = self.started(payload)
            self.assertTrue(t.running(), payload)
            self.assertEqual(t.proc.args, getattr(t, want), payload)

    def test_a_resume_that_is_not_a_json_boolean_starts_nothing(self):
        for resume in ("yes", "true", 1, 0, None, [], {}, ["--continue"]):
            t = self.started(json.dumps({"type": "power", "on": True, "resume": resume}).encode())
            self.assertFalse(t.running(), resume)

    def test_resume_without_on_starts_nothing(self):
        for msg in ({"type": "power", "resume": True}, {"type": "power", "on": False, "resume": True},
                    {"type": "resume", "on": True}, {"type": "power", "on": "true", "resume": True}):
            self.assertFalse(self.started(json.dumps(msg).encode()).running(), msg)

    def test_resume_makes_the_same_checks_as_power_on(self):
        for kw, reason in (({"exists": lambda: False}, "no-sandbox"), ({"exists": lambda: None}, "no-sbx"),
                           ({"elsewhere": lambda own: [999]}, "elsewhere")):
            t = fake_term(**kw)
            self.addCleanup(t.shutdown)
            page = Collector()
            t.attach(page)
            t.control(RESUME_ON)
            self.assertFalse(t.running(), reason)
            self.assertEqual(page.states()[-1]["reason"], reason)
        t = fake_term()
        self.addCleanup(t.shutdown)
        t.hold("service")
        t.control(RESUME_ON)
        self.assertFalse(t.running())
        self.assertEqual(t.state()["reason"], "service")
        t = fake_term()
        t.shutdown()
        t.control(RESUME_ON)
        self.assertFalse(t.running())

    def test_a_running_session_is_not_replaced_by_a_resume(self):
        t = self.started(POWER_ON)
        proc = t.proc
        t.control(RESUME_ON)
        self.assertIs(t.proc, proc)


class ElsewhereTests(unittest.TestCase):
    def test_finds_other_attachments(self):
        self.assertEqual(dt.attached_pids(PS, "factory-coordinator", [300]), [200, 202, 203, 204, 207])

    def test_forms(self):
        name = "factory-coordinator"
        for line, want in (("sbx run --name factory-coordinator", True),
                           ("sbx run --name=factory-coordinator", True),
                           ("sbx run factory-coordinator", True),            # positional sandbox name
                           ("sbx run claude --name factory-coordinator", True),
                           ("sbx --debug run --name factory-coordinator", True),  # global flag first
                           ("/opt/bin/sbx run -e X=1 factory-coordinator", True),
                           ("sbx run --name factory-coordinator -- --continue", True),  # the RESUME form
                           ("sbx run factory-coordinator -- --continue", True),
                           ("sbx run -- --continue", False),
                           ("sbx run claude -- factory-coordinator", False),  # an agent argument
                           ("sbx run factory-coordinator-old", False),
                           ("sbx exec factory-coordinator true", False),
                           ("sbx ls factory-coordinator", False),
                           ("grep sbx run factory-coordinator", False),
                           ("sbx", False), ("", False)):
            self.assertEqual(dt.runs_sandbox(line.split(), name), want, line)

    def test_nothing_else(self):
        self.assertEqual(dt.attached_pids(PS, "factory-coordinator", [300, 150]), [])
        self.assertEqual(dt.attached_pids("", "factory-coordinator"), [])
        self.assertEqual(dt.attached_pids("garbage\n  x y z\n", "factory-coordinator"), [])

    def test_refused_when_elsewhere(self):
        t = fake_term(elsewhere=lambda own: dt.attached_pids(PS, "factory-coordinator", own))
        page = Collector()
        t.attach(page)
        t.control(POWER_ON)
        self.assertFalse(t.running())
        self.assertEqual(page.states()[-1], {"type": "state", "running": False, "reason": "elsewhere"})

    def test_missing_sandbox_and_sbx(self):
        for exists, reason in ((lambda: False, "no-sandbox"), (lambda: None, "no-sbx")):
            t = fake_term(exists=exists)
            page = Collector()
            t.attach(page)
            t.control(POWER_ON)
            self.assertFalse(t.running())
            self.assertEqual(page.states()[-1]["reason"], reason)

    def test_argv_is_fixed(self):
        self.assertEqual(dt.coordinator_argv("factory-coordinator"), ["sbx", "run", "--name", "factory-coordinator"])
        self.assertEqual(dt.coordinator_argv("factory-coordinator", resume=True),
                         ["sbx", "run", "--name", "factory-coordinator", "--", "--continue"])
        for bad in ("", "-x", "a b", "a;b", "A", "../x", "x\n"):
            for resume in (False, True):
                with self.assertRaises(ValueError):
                    dt.coordinator_argv(bad, resume)

    def test_resume_argv_defaults_to_continue_on_the_fixed_argv(self):
        t = fake_term()
        self.assertEqual(t.resume_argv, t.argv + ["--", "--continue"])
        u = dt.Term(dt.coordinator_argv("x"), "x", resume_argv=dt.coordinator_argv("x", resume=True))
        self.assertEqual((u.argv, u.resume_argv), (["sbx", "run", "--name", "x"], ["sbx", "run", "--name", "x", "--", "--continue"]))


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
        t.attach(a)  # attaching starts nothing
        self.assertFalse(t.running())
        self.assertEqual(a.frames, [("text", {"type": "state", "running": False, "reason": None})])
        t.control(POWER_ON)  # the page's POWER ON does
        self.assertTrue(t.running())
        self.assertEqual(a.states()[1], {"type": "state", "running": True, "reason": None})
        self.assertTrue(dt.wait_for(lambda: "READY 100x30 tty=True" in a.output()), a.output())

        b = Collector()  # a late page gets the state, the replay marker, then one replay frame
        t.attach(b)
        self.assertEqual(b.frames[0], ("text", {"type": "state", "running": True, "reason": None}))
        self.assertEqual(b.frames[1], ("text", {"type": "replay"}))
        self.assertEqual(b.frames[2][0], "binary")
        self.assertIn("READY 100x30", b.frames[2][1].decode())
        self.assertEqual(len(b.frames), 3)

        t.write(b"hello\r")
        for page in (a, b):
            self.assertTrue(dt.wait_for(lambda: "ECHO:hello" in page.output()), page.output())

        t.control(json.dumps({"type": "resize", "cols": 120, "rows": 40}).encode())
        self.assertTrue(dt.wait_for(lambda: "WINCH 120x40" in a.output()), a.output())
        for bad in ({"type": "resize", "cols": 1, "rows": 40}, {"type": "resize", "cols": 501, "rows": 40},
                    {"type": "resize", "cols": "80", "rows": 24}, {"type": "resize", "cols": True, "rows": 24},
                    {"type": "resize", "cols": 80, "rows": False}, {"type": "resize", "cols": 80, "rows": 0},
                    {"type": "resize", "cols": 80, "rows": 501}, {"type": "resize", "cols": -80, "rows": 24},
                    {"type": "resize", "cols": 80.0, "rows": 24}, {"type": "resize", "cols": 80, "rows": None},
                    {"type": "resize", "cols": 80}, {"type": "resize", "cols": 10 ** 30, "rows": 24}):
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
        self.assertEqual(c.states(), [exited, {"type": "replay"}])

        t.control(json.dumps({"type": "power", "on": True}).encode())
        self.assertTrue(t.running())
        for page in (a, b, c):
            self.assertTrue(dt.wait_for(lambda: page.states()[-1]["running"]), page.states())
        self.assertTrue(dt.wait_for(lambda: c.output().count("READY 120x40") == 1), c.output())

    def test_ring_buffer_keeps_the_tail(self):
        t = fake_term(ring=64)
        self.addCleanup(t.shutdown)
        t.attach(Collector())
        t.control(POWER_ON)
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
        self.assertTrue(t.attach(Collector()))
        self.assertTrue(t.attach(Collector()))
        self.assertFalse(t.attach(Collector()))
        self.assertFalse(t.running())

    def test_attach_starts_nothing_and_early_page_gets_no_replay(self):
        t = self.term
        early = Collector()
        t.attach(early)
        self.assertFalse(t.running())
        self.assertIsNone(t.proc)
        t.control(POWER_ON)
        self.assertTrue(dt.wait_for(lambda: "READY" in early.output()), early.output())
        self.assertNotIn({"type": "replay"}, early.states())

    def test_shutdown_hangs_up(self):
        t = self.term
        t.attach(Collector())
        t.control(POWER_ON)
        proc = t.proc
        t.shutdown()
        self.assertIsNotNone(proc.poll())

    def test_closing_refuses_power_on_and_pages(self):
        t = self.term
        t.shutdown()
        self.assertTrue(t.closing)
        t.control(POWER_ON)
        t.power_on()
        self.assertFalse(t.running())
        self.assertFalse(t.attach(Collector()))

    def test_second_interrupt_during_hang_up_still_kills(self):
        # A child that ignores SIGHUP and SIGTERM; a Ctrl-C lands in the first wait.
        stubborn = ("import signal, time\n"
                    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "print('READY', flush=True)\n"
                    "time.sleep(60)\n")
        t = dt.Term([sys.executable, "-u", "-c", stubborn], "factory-coordinator",
                    exists=lambda: True, elsewhere=lambda own: [])
        self.addCleanup(t.shutdown)
        page = Collector()
        t.attach(page)
        t.control(POWER_ON)
        self.assertTrue(dt.wait_for(lambda: "READY" in page.output()), page.output())
        proc = t.proc
        real_wait = proc.wait
        calls = []

        def wait(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real_wait(timeout)
        proc.wait = wait
        with self.assertRaises(KeyboardInterrupt):
            t.shutdown()
        self.assertIsNotNone(proc.poll(), "the child must be SIGKILLed and reaped")
        self.assertEqual(proc.returncode, -9)


class HeartbeatTests(unittest.TestCase):
    def test_silent_page_dropped_answering_page_kept(self):
        now = [1000.0]
        t = fake_term(clock=lambda: now[0], ping_timeout=60)
        self.addCleanup(t.shutdown)
        quiet, chatty = Collector(), Collector()
        t.attach(quiet)
        t.attach(chatty)
        t.heartbeat()
        self.assertEqual((quiet.pings(), chatty.pings()), (1, 1))
        now[0] += 45
        chatty.last_seen = now[0]  # its PONG arrived
        t.heartbeat()
        self.assertEqual(t.clients, [quiet, chatty])
        now[0] += 30  # quiet: 75 s without a frame
        t.heartbeat()
        self.assertEqual(t.clients, [chatty])  # its slot is free at once
        self.assertTrue(any(k == "close" and f for k, f in quiet.frames))
        self.assertFalse(quiet.killed)
        t.heartbeat()  # the next round cuts its socket, in case the close frame never got out
        self.assertTrue(quiet.killed)
        self.assertFalse(chatty.killed)


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

    def __init__(self, **term_kw):
        self.tmp = tempfile.TemporaryDirectory()
        fx = Fixture(Path(self.tmp.name))
        self.term = fake_term(**term_kw)
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
        # As the browser loads the terminal page into the main page's iframe.
        h = {"Host": f"127.0.0.1:{port}", "Cookie": self.cookie,
             "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-site"}
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
        self.f.close()  # the socket's fd closes only once its makefile is closed too
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
        self.assertIsNone(srv.term.proc, "a refused upgrade must not power on the Coordinator")

    def test_session(self):
        srv = self.srv
        a = self.page()
        self.assertTrue(a.status.startswith("HTTP/1.1 101 "), a.status)  # RFC 6455 4.2.2
        self.assertEqual(a.headers["sec-websocket-protocol"], f"{dt.PROTOCOL}{srv.key}")
        self.assertEqual(a.headers["sec-websocket-accept"], dt.accept_key(base64.b64encode(b"0123456789abcdef").decode()))
        self.assertTrue(a.pump_until(lambda: a.states))
        self.assertEqual(a.states[0], {"type": "state", "running": False, "reason": None})  # attach starts nothing
        self.assertIsNone(srv.term.proc)
        a.send(dt.TEXT, json.dumps({"type": "resize", "cols": 90, "rows": 20}).encode())
        a.send(dt.TEXT, POWER_ON)
        self.assertTrue(a.pump_until(lambda: b"READY" in a.out))
        self.assertEqual(a.states[1], {"type": "state", "running": True, "reason": None})
        self.assertNotIn({"type": "replay"}, a.states)  # an early page has nothing to replay

        b = self.page(Origin=f"http://localhost:{srv.tport}", Host=f"localhost:{srv.tport}")
        self.assertIn(" 101 ", b.status)
        # A late page: state, the replay marker, then exactly one binary frame with the buffer.
        self.assertEqual(read_server_frame(b.f), (dt.TEXT, json.dumps({"type": "state", "running": True, "reason": None}).encode()))
        self.assertEqual(read_server_frame(b.f), (dt.TEXT, b'{"type": "replay"}'))
        op, replay = read_server_frame(b.f)
        self.assertEqual(op, dt.BINARY)
        self.assertIn(b"READY", replay)
        self.assertTrue(a.out.startswith(replay[:len(a.out)]) or replay.startswith(a.out[:len(replay)]))

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
        self.assertEqual(resp.version, 11)
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

    def test_term_page_only_for_a_same_site_iframe(self):
        srv = self.srv
        cases = {
            "no Sec-Fetch headers": ({"Sec-Fetch-Dest": None, "Sec-Fetch-Site": None}, 403),
            "no Sec-Fetch-Dest": ({"Sec-Fetch-Dest": None}, 403),
            "no Sec-Fetch-Site": ({"Sec-Fetch-Site": None}, 403),
            "top-level navigation": ({"Sec-Fetch-Dest": "document"}, 403),
            "window.open from a same-site page": ({"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "same-site"}, 403),
            "cross-site iframe": ({"Sec-Fetch-Site": "cross-site"}, 403),
            "typed in the address bar": ({"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "none"}, 403),
            "embed": ({"Sec-Fetch-Dest": "embed"}, 403),
            "same-site iframe": ({}, 200),
            "same-origin iframe": ({"Sec-Fetch-Site": "same-origin"}, 200),
        }
        for name, (over, code) in cases.items():
            resp, body = srv.get("/term", headers=over)
            self.assertEqual(resp.status, code, name)
            if code != 200:
                self.assertNotIn(srv.key, body.decode(), name)
        # Its static files don't carry the key and stay as they are.
        self.assertEqual(srv.get("/term.js", headers={"Sec-Fetch-Dest": "script", "Sec-Fetch-Site": "same-origin"})[0].status, 200)

    def test_head_term_sends_no_body(self):
        srv = self.srv
        resp, body = srv.get("/term", method="HEAD")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"")
        # On the wire, too: headers only, and no key in them.
        s = socket.create_connection(("127.0.0.1", srv.tport), timeout=5)
        try:
            s.sendall((f"HEAD /term HTTP/1.1\r\nHost: 127.0.0.1:{srv.tport}\r\nCookie: {srv.cookie}\r\n"
                       "Sec-Fetch-Dest: iframe\r\nSec-Fetch-Site: same-site\r\nConnection: close\r\n\r\n").encode())
            raw = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            s.close()
        head, _, rest = raw.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200 "), head)
        self.assertEqual(rest, b"")
        self.assertNotIn(srv.key.encode(), raw)

    def test_keep_alive(self):
        # HTTP/1.1: two requests on one connection, neither hanging.
        srv = self.srv
        conn = http.client.HTTPConnection("127.0.0.1", srv.tport, timeout=5)
        try:
            for path in ("/term.js", "/term.css"):
                conn.request("GET", path, headers={"Host": f"127.0.0.1:{srv.tport}", "Cookie": srv.cookie})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200, path)
                resp.read()
            conn.request("GET", "/nope", headers={"Host": f"127.0.0.1:{srv.tport}", "Cookie": srv.cookie})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 404)
            self.assertEqual(resp.getheader("Connection"), "close")  # refusals end the connection
            resp.read()
        finally:
            conn.close()

    def test_main_page_never_sees_the_key(self):
        srv = self.srv
        resp, body = srv.get("/api/state", port=srv.port)
        self.assertEqual(resp.status, 200)
        state = json.loads(body)
        self.assertEqual(state["term"]["port"], srv.tport)
        self.assertNotIn(srv.key, body.decode())
        resp, body = srv.get("/", port=srv.port)
        self.assertNotIn(srv.key, body.decode())


class WireLimitTests(unittest.TestCase):
    def server(self, **kw):
        srv = Server(**kw)
        self.addCleanup(srv.close)
        return srv

    def test_client_cap_on_the_wire(self):
        srv = self.server(max_clients=2)
        pages = []
        for _ in range(2):
            p = WsPage(srv)
            self.addCleanup(p.close)
            self.assertIn(" 101 ", p.status)
            self.assertTrue(p.pump_until(lambda: p.states))  # attached
            pages.append(p)
        s, f, status, _ = srv.ws()
        s.close()
        self.assertTrue(status.startswith("HTTP/1.1 503 "), status)
        pages[0].close()  # a slot frees when a page leaves
        self.assertTrue(dt.wait_for(lambda: len(srv.term.clients) == 1))
        p = WsPage(srv)
        self.addCleanup(p.close)
        self.assertIn(" 101 ", p.status)

    def test_silent_page_dropped_on_the_wire(self):
        srv = self.server(ping_interval=0.1, ping_timeout=0.4)
        quiet, chatty = WsPage(srv), WsPage(srv)
        for p in (quiet, chatty):
            self.addCleanup(p.close)
        self.assertTrue(dt.wait_for(lambda: len(srv.term.clients) == 2))
        # chatty answers every PING with a PONG; quiet reads but never answers.
        end = dt.time.monotonic() + 1.5
        while dt.time.monotonic() < end:
            op, data = read_server_frame(chatty.f)
            if op == dt.PING:
                chatty.send(dt.PONG, data)
        self.assertEqual(len(srv.term.clients), 1)
        frames = []
        while True:  # quiet: its state, some PINGs, then a close frame, then EOF
            try:
                op, data = read_server_frame(quiet.f)
            except ValueError:  # EOF: fewer than two bytes
                break
            frames.append((op, data))
            if op == dt.CLOSE:
                break
        self.assertEqual(frames[-1][0], dt.CLOSE)
        self.assertEqual(struct.unpack("!H", frames[-1][1][:2])[0], 1001)
        self.assertIn(dt.PING, [op for op, _ in frames])

    def test_http_phase_times_out(self):
        srv = self.server()
        handler = srv.main.term_server.RequestHandlerClass
        self.assertEqual(handler.timeout, dt.HTTP_TIMEOUT)
        self.assertEqual(handler.protocol_version, "HTTP/1.1")
        handler.timeout = 0.3  # a connection that never sends a request is closed
        s = socket.create_connection(("127.0.0.1", srv.tport), timeout=5)
        self.addCleanup(s.close)
        self.assertEqual(s.recv(10), b"")


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
            self.assertNotIn(b"sourceMappingURL", data, name)  # the trailers are stripped (vendor README)
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)
        self.assertEqual({p.name for p in vendor.glob("LICENSE-*.txt")}, {"LICENSE-xterm.txt", "LICENSE-addon-fit.txt"})

    def test_term_page(self):
        js = (UI / "term.js").read_text()
        self.assertIn("window.top === window", js)  # refuses to run outside the dashboard
        self.assertIn('"replay"', js)               # mutes input while the replay is written
        css = (UI / "term.css").read_text()
        self.assertIn("font-variant-ligatures: none", css)  # VT323's ff/fi/fl ligatures break xterm's grid
        # FitAddon subtracts only .xterm's own padding: padding on #term lets the grid overflow the frame
        self.assertIsNone(re.search(r"^#term\s*\{[^}]*padding", css, re.M), "padding on #term, not .xterm")
        self.assertRegex(css, r"#term \.xterm \{[^}]*padding:")
        self.assertIn("Math.min(22, Math.floor(window.innerWidth / 30))", js)  # 22px cap: larger type clipped on wide screens
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                     "ClipboardAddon", "WebLinksAddon", "ImageAddon", "window.open",
                     "readText", "clipboard.read", "clipboard-read"):
            self.assertNotIn(sink, js, f"term.js uses {sink}")
        self.assertIn("allowProposedApi: false", js)
        # One OSC 52 handler, ours; the only clipboard write is the COPY key's (TermScriptTests).
        self.assertEqual(js.count("registerOscHandler("), 1)
        self.assertIn("registerOscHandler(52,", js)
        self.assertEqual(js.count(".writeText("), 2)  # the COPY key and the copy-key fallback
        self.assertIn("macOptionClickForcesSelection: true", js)
        html = (UI / "term.html").read_text()
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "inline script")
        self.assertIsNone(re.search(r"\son[a-z]+=", html), "inline event handler")
        self.assertEqual(html.count(dt.KEY_SLOT), 1)
        self.assertIsNone(re.search(r"https?://", html + js + (UI / "term.css").read_text()), "network URL")

    def test_main_page_frames_with_sandbox(self):
        js = (UI / "app.js").read_text()
        self.assertIn('setAttribute("sandbox", "allow-scripts allow-same-origin")', js)
        self.assertNotIn("term-key", js)
        # Clipboard write for Terminal 0's frame only (its COPY key); never read, never the control unit.
        self.assertEqual(js.count('setAttribute("allow", '), 1)
        self.assertIn('termFrame.setAttribute("allow", "clipboard-write");', js)
        self.assertNotIn("clipboard-read", js)
        cu = js[js.index("function renderCu()"):]
        cu = cu[:cu.index("\n  }\n")]
        self.assertNotIn("allow\"", cu)


@unittest.skipUnless(shutil.which("node"), "needs node")
class Osc52Tests(unittest.TestCase):
    """term.js's pure copy helpers, loaded under node (they return before touching the DOM)."""

    def js(self, expr):
        code = f"const T = require({str(UI / 'term.js')!r}); process.stdout.write(JSON.stringify({expr}));"
        return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True, timeout=30).stdout)

    def parse(self, data):
        return self.js(f"T.osc52Text({json.dumps(data)})")

    @staticmethod
    def b64(text):
        return base64.b64encode(text.encode()).decode()

    def test_writes(self):
        for target in ("c", "p", "s", "0", "", "cpq7", "anything"):  # the target is ignored
            self.assertEqual(self.parse(f"{target};{self.b64('git status')}"), "git status")
        self.assertEqual(self.parse("c;" + self.b64("naïve ✓ 日本 🙂")), "naïve ✓ 日本 🙂")
        self.assertEqual(self.parse("c;" + self.b64("a\r\nb\rc\td")), "a\nb\nc\td")
        self.assertEqual(self.parse("c;" + self.b64("ab").rstrip("=")), "ab")  # unpadded
        self.assertEqual(self.parse("c;a;b".replace("a;b", self.b64("x;y"))), "x;y")

    def test_refused(self):
        for data in ("c;?", "?", ";?", "c;", "c", "", "c;" + self.b64("x") + "?", "c;@@@@", "c;YQ=x", "c;Y",
                     "c; " + self.b64("x"), "c;" + self.b64("x") + "\n",
                     "c;" + base64.b64encode(b"\xff\xfe").decode(),         # not UTF-8
                     "c;" + base64.b64encode(b"\xed\xa0\x80").decode(),     # a lone surrogate
                     "c;" + self.b64("echo \x1b[31m"), "c;" + self.b64("a\x00b"), "c;" + self.b64("a\x85b"),
                     "c;" + self.b64("a\x7f")):
            self.assertIsNone(self.parse(data), repr(data))
        self.assertIsNone(self.js("T.osc52Text(null)"))

    def test_cap(self):
        cap = self.js("T.COPY_MAX")
        self.assertEqual(cap, 64 * 1024)
        self.assertEqual(self.js(f"T.osc52Text('c;' + Buffer.from('x'.repeat({cap})).toString('base64')).length"), cap)
        self.assertIsNone(self.js(f"T.osc52Text('c;' + Buffer.from('x'.repeat({cap + 1})).toString('base64'))"))
        # The cap is bytes: 3-byte characters fit a third as many.
        self.assertIsNone(self.js(f"T.osc52Text('c;' + Buffer.from('✓'.repeat({cap // 3 + 1})).toString('base64'))"))
        self.assertIsNone(self.js(f"T.osc52Text('c;' + 'A'.repeat({cap * 2}))"))

    def test_oversize(self):
        cap = self.js("T.COPY_MAX")
        size = lambda data: self.js(f"T.osc52Oversize({data})")
        self.assertEqual(size(f"'c;' + Buffer.from('x'.repeat({cap})).toString('base64')"), 0)  # fits
        self.assertEqual(size(f"'c;' + Buffer.from('x'.repeat({cap + 1})).toString('base64')"), cap + 1)
        self.assertEqual(size(f"'c;' + Buffer.from('x'.repeat({cap + 2})).toString('base64')"), cap + 2)
        self.assertEqual(size(f"';' + 'A'.repeat({cap * 2})"), cap * 2 * 3 // 4)
        for bad in ("'c;?'", "'c'", "null", f"'c;' + '@'.repeat({cap * 2})", f"'c;' + 'A'.repeat({cap * 2 + 1})"):
            self.assertEqual(size(bad), 0, bad)

    def test_preview_and_trim(self):
        self.assertEqual(self.js("T.copyPreview('a\\nb\\tc\\u202ed\\u200b')"), "a⏎ b  c⟨U+202E⟩d⟨U+200B⟩")
        self.assertTrue(self.js("T.hasHidden('x\\u2066y')"))
        self.assertFalse(self.js("T.hasHidden('plain text')"))
        cut = self.js("T.copyPreview('z'.repeat(1000))")
        self.assertEqual(len(cut), self.js("T.PREVIEW_MAX"))
        self.assertTrue(cut.endswith("…"))
        self.assertEqual(self.js("T.trimSelection('  a b   \\nc\\u00a0 \\n\\n d')"), "  a b\nc\n\n d")

    def test_copy_keys(self):
        def key(mac, **ev):
            ev = {"type": "keydown", "key": "c", "code": "KeyC", "ctrlKey": False, "shiftKey": False,
                  "altKey": False, "metaKey": False, **ev}
            return self.js(f"T.isCopyKey({json.dumps(ev)}, {json.dumps(mac)})")
        self.assertTrue(key(False, ctrlKey=True, shiftKey=True, key="C"))
        self.assertTrue(key(False, ctrlKey=True, key="Insert", code="Insert"))
        self.assertTrue(key(False, ctrlKey=True))  # asked only with a selection; else Ctrl+C is the Coordinator's
        self.assertFalse(key(False, ctrlKey=True, shiftKey=True, key="Insert", code="Insert"))
        self.assertFalse(key(True, ctrlKey=True))  # always the Coordinator's on a Mac
        self.assertFalse(key(False, metaKey=True))
        self.assertFalse(key(False, ctrlKey=True, shiftKey=True, altKey=True))
        self.assertFalse(key(False, ctrlKey=True, shiftKey=True, type="keyup"))
        self.assertTrue(key(True, metaKey=True))
        self.assertFalse(key(True, ctrlKey=True, shiftKey=True))
        self.assertFalse(key(True, metaKey=True, shiftKey=True))


@unittest.skipUnless(shutil.which("node"), "needs node")
class TermScriptTests(unittest.TestCase):
    """term.js itself, under node with a fake DOM and Terminal (scripts/test_term_page.js)."""

    def test_page_script(self):
        here = Path(__file__).resolve().parent
        for platform in ("other", "mac"):
            proc = subprocess.run(["node", str(here / "test_term_page.js"), str(UI / "term.js"), platform],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1]),
                             ["auto power-on resumes, POWER ON is fresh",
                              "copy only on click, newest wins, after the hold", "queries and bad payloads refused",
                              "too large: a note, nothing to copy", "dismiss discards",
                              "replay raises no prompt", "preview is visible text", "fallback when writeText is refused",
                              "selection copied trimmed", "copy keys"], platform)


if __name__ == "__main__":
    unittest.main()
