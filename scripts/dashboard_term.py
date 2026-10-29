"""The dashboard's Coordinator terminal (Terminal 0): one PTY, one attachment.

The dashboard owns the only attachment to the Coordinator sandbox and fans it
out to every browser tab. A second `sbx run --name factory-coordinator` while
one is attached would start a second Claude process in the same VM, so:

- The server runs one fixed argv, `sbx run --name <coordinator>`, in a
  pseudo-terminal (no shell; the name is checked against NAME), only when a
  page asks for it ({"type": "power", "on": true}, the POWER ON button):
  attaching a page starts nothing. It never falls back to a shell: when the
  child exits, the pages show the set powered off.
- Before starting it, it refuses if another process on this machine is
  already running `sbx run --name <coordinator>` (`factory chat`), and if `sbx ls -q` doesn't list the sandbox.
- Output goes to every connected page, and the last RING bytes are replayed to
  a page that connects later, as a text frame {"type": "replay"} followed by
  exactly one binary frame (the page mutes its input while it writes that
  frame, so terminal queries in old output don't answer into the
  Coordinator's stdin again); input from any authorized page goes to the PTY;
  the most recent page's size is applied (TIOCSWINSZ).
- Every PING_INTERVAL seconds each page is pinged; one silent for
  PING_TIMEOUT seconds is dropped, so dead sockets don't hold MAX_CLIENTS.

The terminal lives on its own origin: a second port on 127.0.0.1 serving only
the terminal page, its script, the vendored xterm.js and the WebSocket. The
main page embeds it in an iframe, and the terminal page (which carries the
key) is served only to a same-site iframe load (Sec-Fetch-Dest: iframe,
Sec-Fetch-Site: same-site or same-origin), never to a top-level navigation
another local page could open. A WebSocket is accepted only when the Host
names this port, the Origin is exactly this origin, the main dashboard's
cookie matches, and the subprotocol carries this run's terminal key, which
only the terminal page knows (the server writes it into the page it serves;
the main page, which shows agent-written text, can't read a cross-origin
page). The WebSocket is RFC 6455, server side, standard library only.

The same origin serves the control unit (dashboard_controls.py), the one
page that can act: /cu, with its own per-run key, under the same iframe-only
rule, and its API, POST /cu/api/<name>. A POST is read only when the Host and
Origin name this origin exactly, Sec-Fetch-Site (if sent) is same-origin, the
dashboard cookie matches, the X-Factory-Control header holds the control
unit's key (not the terminal's), and the body is declared JSON of at most
MAX_BODY bytes (MAX_CONFIG_BODY for config-prepare: control_refusal); every other method, OPTIONS included, is
refused, and no response carries CORS headers. Without --coordinator the
origin still runs, for the control unit; /term is then a 404.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import base64
import collections
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

UI = Path(__file__).resolve().parent / "dashboard-ui"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")  # fullmatch
RING = 256 * 1024          # bytes of output replayed to a page that connects later
MAX_MESSAGE = 64 * 1024    # largest WebSocket message accepted from a page
MAX_CLIENTS = 8
CLIENT_BACKLOG = 4 * 1024 * 1024  # bytes queued for one page before it is dropped
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PROTOCOL = "factory-term."  # the subprotocol: PROTOCOL + this run's terminal key
KEY_SLOT = "__TERM_KEY__"   # placeholder in term.html the server fills in
HTTP_TIMEOUT = 30          # seconds a request on the terminal port may take, before any upgrade
PING_INTERVAL = 30         # seconds between the server's pings to each page
PING_TIMEOUT = 60          # seconds without any frame from a page before it is dropped
FRAME_DESTS = {"iframe"}                       # Sec-Fetch-Dest allowed for /term and /cu
FRAME_SITES = {"same-site", "same-origin"}     # Sec-Fetch-Site allowed for /term and /cu
CU_KEY_SLOT = "__CU_KEY__"              # placeholders in cu.html the server fills in
CU_PORT_SLOT = "__CU_MAIN_PORT__"
CU_HEADER = "X-Factory-Control"         # the request header carrying the control unit's key
CU_API = "/cu/api/"
MAX_BODY = 8192                         # largest control request body accepted
MAX_CONFIG_BODY = 128 * 1024            # ... except config-prepare's, which carries a notes file (20,000 characters, escaped)
CONFIG_PREPARE = CU_API + "config-prepare"

# The terminal origin's only files: path -> (file under UI, content type).
STATIC = {
    "/term.js": ("term.js", "text/javascript; charset=utf-8"),
    "/term.css": ("term.css", "text/css; charset=utf-8"),
    "/vendor/xterm/xterm.js": ("vendor/xterm/xterm.js", "text/javascript; charset=utf-8"),
    "/vendor/xterm/xterm.css": ("vendor/xterm/xterm.css", "text/css; charset=utf-8"),
    "/vendor/xterm/addon-fit.js": ("vendor/xterm/addon-fit.js", "text/javascript; charset=utf-8"),
    "/fonts/vt323.woff2": ("fonts/vt323.woff2", "font/woff2"),
    # The control unit's (cu.html is served at /cu, like term.html at /term).
    "/cu.js": ("cu.js", "text/javascript; charset=utf-8"),
    "/cu.css": ("cu.css", "text/css; charset=utf-8"),
    # cu.css imports this (same origin, allowed by style-src 'self').
    "/theme.css": ("theme.css", "text/css; charset=utf-8"),
    "/fonts/barlow-condensed-600.woff2": ("fonts/barlow-condensed-600.woff2", "font/woff2"),
    "/fonts/barlow-condensed-700.woff2": ("fonts/barlow-condensed-700.woff2", "font/woff2"),
    "/fonts/dotgothic16.woff2": ("fonts/dotgothic16.woff2", "font/woff2"),
}

# Opcodes (RFC 6455 5.2)
CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def coordinator_argv(name: str) -> list:
    """The one command the terminal runs. Never a shell."""
    if not NAME.fullmatch(name or ""):
        raise ValueError(f"not a sandbox name: {name!r}")
    return ["sbx", "run", "--name", name]


def term_csp(term_port: int, main_port: int) -> str:
    """The terminal page's CSP: its own files, xterm's inline styles, the
    WebSocket to this origin, and framing only by the main dashboard."""
    return ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            f"connect-src ws://127.0.0.1:{term_port} ws://localhost:{term_port}; font-src 'self'; "
            f"frame-ancestors http://127.0.0.1:{main_port} http://localhost:{main_port}; "
            "base-uri 'none'; form-action 'none'")


def cu_csp(term_port: int, main_port: int) -> str:
    """The control unit's CSP: its own files, no inline style or script, fetch()
    only to this origin, and framing only by the main dashboard."""
    return ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
            f"connect-src http://127.0.0.1:{term_port} http://localhost:{term_port}; img-src 'self'; "
            f"frame-ancestors http://127.0.0.1:{main_port} http://localhost:{main_port}; "
            "base-uri 'none'; form-action 'none'")


def control_refusal(headers, *, hosts, origins, cookie: str, token: str, key: str, limit: int = MAX_BODY) -> tuple:
    """(None, length) if a POST to the control unit's API may be read, else
    (status, reason). Every check is on the headers, before any of the body is
    read: Host and Origin name this origin exactly, Sec-Fetch-Site (if the
    browser sent one) is same-origin, exactly one dashboard cookie holds the
    token, the key header holds the control unit's key, and the body is
    declared JSON of at most limit bytes (MAX_BODY; MAX_CONFIG_BODY for config-prepare)."""
    if headers.get("Host", "") not in hosts:
        return 403, "wrong Host header"
    if headers.get("Origin", "") not in origins:
        return 403, "wrong Origin"
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site != "same-origin":
        return 403, "not a same-origin request"
    if not cookie_matches(headers, cookie, token):
        return 403, "no dashboard cookie"
    given = headers.get_all(CU_HEADER) or []
    if len(given) != 1 or not hmac.compare_digest(given[0].encode(), key.encode()):
        return 403, "no control key"
    if not headers.get("Content-Type", "").lower().startswith("application/json"):
        return 415, "send JSON (Content-Type: application/json)"
    if headers.get("Transfer-Encoding") is not None:
        return 400, "send a Content-Length, not a chunked body"
    lengths = headers.get_all("Content-Length") or []
    if len(lengths) != 1 or not lengths[0].strip().isdigit():
        return 400, "Content-Length required"
    length = int(lengths[0])
    if length > limit:
        return 413, f"the body is over {limit} bytes"
    return None, length


def cookie_matches(headers, name: str, token: str) -> bool:
    """Exactly one cookie called name, holding token. A second one (say, set by
    another 127.0.0.1 server) is refused rather than guessed between."""
    values = []
    for header in headers.get_all("Cookie") or []:
        for part in header.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                values.append(value)
    return len(values) == 1 and hmac.compare_digest(values[0].encode(), token.encode())


# --------------------------------------------------------------------------
# Is the Coordinator already attached somewhere else?

def runs_sandbox(argv: list, name: str) -> bool:
    """True if argv is `sbx [global flags] run ...` naming the sandbox: as
    `--name NAME`, `--name=NAME`, or a bare argument NAME before any `--`
    (`sbx run` takes a sandbox name positionally too; sbx has no short form
    of --name). Errs on the side of a match: a second attachment is the harm.
    Same rule as runs_sandbox in factory/sbx.py (what `factory chat` checks)."""
    if not argv or os.path.basename(argv[0]) != "sbx":
        return False
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 1
    if i >= len(argv) or argv[i] != "run":
        return False
    for arg in argv[i + 1:]:
        if arg == "--":
            break
        if arg == name or arg == f"--name={name}":
            return True
    return False


def attached_pids(ps_output: str, name: str, exclude=()) -> list:
    """PIDs in `ps -axo pid=,ppid=,args=` output running `sbx run` on sandbox
    NAME (runs_sandbox), leaving out the pids in exclude and their descendants
    (our own child)."""
    procs = {}
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        procs[int(parts[0])] = (int(parts[1]), parts[2].split())
    ours = set(exclude)
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _) in procs.items():
            if ppid in ours and pid not in ours:
                ours.add(pid)
                changed = True
    out = []
    for pid, (_, argv) in sorted(procs.items()):
        if pid not in ours and runs_sandbox(argv, name):
            out.append(pid)
    return out


def ps_listing() -> str:
    """Every process on this machine, as pid, parent pid and command line."""
    try:
        return subprocess.run(["ps", "-axo", "pid=,ppid=,args="], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def sandbox_listed(name: str):
    """True if `sbx ls -q` lists the sandbox, False if not, None if sbx can't run."""
    try:
        out = subprocess.run(["sbx", "ls", "-q"], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return name in out.split()


# --------------------------------------------------------------------------
# WebSocket framing (RFC 6455), server side

class ProtocolError(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def encode_frame(opcode: int, payload: bytes = b"", fin: bool = True, mask: bytes | None = None) -> bytes:
    """One frame. The server never masks; mask is for tests playing the client."""
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    bit = 0x80 if mask else 0
    if n < 126:
        head.append(bit | n)
    elif n < 1 << 16:
        head.append(bit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", n)
    if mask:
        head += mask
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(head) + payload


def close_frame(code: int, reason: str = "") -> bytes:
    return encode_frame(CLOSE, struct.pack("!H", code) + reason.encode()[:120])


def read_exact(rfile, n: int) -> bytes:
    data = rfile.read(n) if n else b""
    if len(data) != n:
        raise EOFError("connection closed")
    return data


def read_frame(rfile, max_size: int = MAX_MESSAGE) -> tuple:
    """(fin, opcode, payload) of one client frame, unmasked. Client frames must
    be masked; control frames must be short and unfragmented."""
    b0, b1 = read_exact(rfile, 2)
    fin, rsv, opcode = bool(b0 & 0x80), b0 & 0x70, b0 & 0x0F
    masked, n = bool(b1 & 0x80), b1 & 0x7F
    if rsv:
        raise ProtocolError(1002, "reserved bits set")
    if opcode not in (CONT, TEXT, BINARY, CLOSE, PING, PONG):
        raise ProtocolError(1002, "unknown opcode")
    if not masked:
        raise ProtocolError(1002, "client frames must be masked")
    if n == 126:
        n = struct.unpack("!H", read_exact(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", read_exact(rfile, 8))[0]
    if opcode >= CLOSE and (n > 125 or not fin):
        raise ProtocolError(1002, "bad control frame")
    if n > max_size:
        raise ProtocolError(1009, "message too big")
    mask = read_exact(rfile, 4)
    data = read_exact(rfile, n)
    return fin, opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def read_message(rfile, on_control, max_size: int = MAX_MESSAGE) -> tuple:
    """(opcode, payload) of the next whole TEXT or BINARY message, joining
    continuation frames; control frames in between go to on_control(opcode,
    payload), and a CLOSE ends it with (CLOSE, payload)."""
    kind, parts, size = None, [], 0
    while True:
        fin, opcode, payload = read_frame(rfile, max_size)
        if opcode in (PING, PONG):
            on_control(opcode, payload)
            continue
        if opcode == CLOSE:
            return CLOSE, payload
        if opcode == CONT:
            if kind is None:
                raise ProtocolError(1002, "continuation without a message")
        elif kind is not None:
            raise ProtocolError(1002, "new message inside a fragmented one")
        else:
            kind = opcode
        size += len(payload)
        if size > max_size:
            raise ProtocolError(1009, "message too big")
        parts.append(payload)
        if fin:
            data = b"".join(parts)
            if kind == TEXT:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    raise ProtocolError(1007, "text is not UTF-8") from None
            return kind, data


def upgrade_refusal(headers, *, origins, cookie: str, token: str, key: str) -> tuple:
    """(None, protocol) if this WebSocket upgrade may proceed, else (status, reason).
    The Host header is checked before this, for every request."""
    if "websocket" not in headers.get("Upgrade", "").lower():
        return 400, "not a WebSocket upgrade"
    if "upgrade" not in [t.strip().lower() for t in headers.get("Connection", "").split(",")]:
        return 400, "not a WebSocket upgrade"
    if headers.get("Sec-WebSocket-Version", "") != "13":
        return 426, "WebSocket version 13 only"
    try:
        nonce = base64.b64decode(headers.get("Sec-WebSocket-Key", ""), validate=True)
    except ValueError:
        nonce = b""
    if len(nonce) != 16:
        return 400, "bad Sec-WebSocket-Key"
    if headers.get("Origin", "") not in origins:
        return 403, "wrong Origin"
    if not cookie_matches(headers, cookie, token):
        return 403, "no dashboard cookie"
    offered = [p.strip() for h in headers.get_all("Sec-WebSocket-Protocol") or [] for p in h.split(",")]
    ours = [p for p in offered if p.startswith(PROTOCOL)]
    if len(ours) != 1 or not hmac.compare_digest(ours[0][len(PROTOCOL):].encode(), key.encode()):
        return 403, "no terminal key"
    return None, ours[0]


# --------------------------------------------------------------------------
# One connected page

class Client:
    """A page's WebSocket: frames go out through a bounded queue and a writer
    thread, so a slow page can't hold up the PTY or the other pages. A page
    that falls CLIENT_BACKLOG bytes behind is dropped."""

    def __init__(self, sock, backlog: int = CLIENT_BACKLOG):
        self.sock, self.backlog = sock, backlog
        self.last_seen = time.monotonic()  # Term resets it with its own clock
        self.queue: collections.deque = collections.deque()
        self.queued = 0
        self.cond = threading.Condition()
        self.closed = False
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def send(self, frame: bytes) -> bool:
        with self.cond:
            if self.closed:
                return False
            if self.queued + len(frame) > self.backlog:
                self.closed = True
                self.cond.notify()
                self._shutdown()
                return False
            self.queue.append(frame)
            self.queued += len(frame)
            self.cond.notify()
        return True

    def binary(self, data: bytes) -> bool:
        return self.send(encode_frame(BINARY, data))

    def text(self, obj) -> bool:
        return self.send(encode_frame(TEXT, json.dumps(obj).encode()))

    def close(self, frame: bytes | None = None) -> None:
        with self.cond:
            if frame is not None and not self.closed:
                self.queue.append(frame)
            self.closed = True
            self.cond.notify()

    def kill(self) -> None:
        """Cut the connection now (a page that stopped answering)."""
        with self.cond:
            self.closed = True
            self.cond.notify()
        self._shutdown()

    def _shutdown(self) -> None:
        try:
            self.sock.shutdown(2)  # socket.SHUT_RDWR: also ends the reader
        except OSError:
            pass

    def _write(self) -> None:
        while True:
            with self.cond:
                while not self.queue and not self.closed:
                    self.cond.wait()
                if not self.queue:
                    break
                frame = self.queue.popleft()
                self.queued -= len(frame)
            try:
                self.sock.sendall(frame)
            except OSError:
                with self.cond:
                    self.closed = True
                break
        self._shutdown()


# --------------------------------------------------------------------------
# The PTY

class Term:
    """The Coordinator's PTY and the pages attached to it.

    argv is the fixed command (coordinator_argv). name is the sandbox; exists
    and elsewhere are the pre-start checks, and clock, ping_interval and
    ping_timeout the heartbeat's (all injectable for tests)."""

    def __init__(self, argv: list, name: str, exists=None, elsewhere=None,
                 ring: int = RING, max_clients: int = MAX_CLIENTS, env: dict | None = None,
                 clock=time.monotonic, ping_interval: float = PING_INTERVAL,
                 ping_timeout: float = PING_TIMEOUT):
        self.argv, self.name = list(argv), name
        self.exists = exists or (lambda: sandbox_listed(name))
        self.elsewhere = elsewhere or (lambda own: attached_pids(ps_listing(), name, own))
        self.ring, self.max_clients = ring, max_clients
        self.env = dict(os.environ if env is None else env, TERM="xterm-256color", COLORTERM="truecolor")
        self.lock = threading.Lock()        # clients, buffer, state
        self.start_lock = threading.Lock()  # one power-on at a time
        self.write_lock = threading.Lock()
        self.clients: list = []
        self.buffer = bytearray()
        self.proc = None
        self.master = None
        self.reason = None
        self.held = None      # set by hold(): power-on is refused, with this reason, until release()
        self.closing = False  # set by shutdown(): nothing starts after it
        self.stopped = threading.Event()
        self.clock, self.ping_interval, self.ping_timeout = clock, ping_interval, ping_timeout
        self.dropped: list = []  # pages dropped by the heartbeat, cut on its next round
        self.size = (80, 24)  # cols, rows

    # --- state
    def running(self) -> bool:
        return self.proc is not None

    def state(self) -> dict:
        return {"type": "state", "running": self.running(), "reason": self.reason}

    def _broadcast(self, obj) -> None:  # with self.lock held
        for c in list(self.clients):
            if not c.text(obj):
                self.clients.remove(c)

    # --- pages
    def attach(self, client) -> bool:
        """Add a page: it gets the state, then (if there is any output yet) a
        {"type": "replay"} text frame followed by exactly one binary frame
        with the replay buffer, then live output. Starts nothing: the page
        powers the set on with {"type": "power", "on": true}."""
        with self.lock:
            if self.closing or len(self.clients) >= self.max_clients:
                return False
            client.last_seen = self.clock()
            client.text(self.state())
            if self.buffer:
                client.text({"type": "replay"})
                client.binary(bytes(self.buffer))
            self.clients.append(client)
        return True

    def heartbeat(self) -> None:
        """One round: cut the pages dropped last round, drop every page silent
        for ping_timeout (freeing its slot now), and ping the rest."""
        now = self.clock()
        with self.lock:
            cut, self.dropped = self.dropped, []
            stale = [c for c in self.clients if now - c.last_seen > self.ping_timeout]
            for c in stale:
                self.clients.remove(c)
            live = list(self.clients)
            self.dropped = stale
        for c in cut:
            c.kill()
        for c in stale:
            c.close(close_frame(1001, "no answer to ping"))
        for c in live:
            c.send(encode_frame(PING, b"factory"))

    def heartbeat_loop(self) -> None:
        while not self.stopped.wait(self.ping_interval):
            self.heartbeat()

    def detach(self, client: Client) -> None:
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
        client.close()

    # --- the child
    def power_on(self) -> None:
        with self.start_lock:
            if self.closing or self.running():
                return
            if self.held:
                with self.lock:
                    self.reason = self.held
                    self._broadcast(self.state())
                return
            reason = self._preflight()
            if reason:
                with self.lock:
                    self.reason = reason
                    self._broadcast(self.state())
                return
            master, slave = os.openpty()
            try:
                self._winsize(slave)
                proc = subprocess.Popen(
                    self.argv, stdin=slave, stdout=slave, stderr=slave, env=self.env,
                    start_new_session=True, close_fds=True, preexec_fn=_take_tty)
            except (OSError, subprocess.SubprocessError):
                os.close(master)
                os.close(slave)
                with self.lock:
                    self.reason = "error"
                    self._broadcast(self.state())
                return
            os.close(slave)
            with self.lock:
                self.proc, self.master, self.reason = proc, master, None
                self.buffer.clear()
                self._broadcast(self.state())
            threading.Thread(target=self._pump, args=(proc, master), daemon=True).start()

    def _preflight(self):
        exists = self.exists()
        if exists is None:
            return "no-sbx"
        if not exists:
            return "no-sandbox"
        if self.elsewhere([os.getpid()]):
            return "elsewhere"
        return None

    def _pump(self, proc, master: int) -> None:
        """Copy the PTY's output to every page until the child is gone."""
        while True:
            try:
                ready, _, _ = select.select([master], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO: every slave fd is closed
                break
            if not data:
                break
            with self.lock:
                self.buffer += data
                if len(self.buffer) > self.ring:
                    del self.buffer[:len(self.buffer) - self.ring]
                for c in list(self.clients):
                    if not c.binary(data):
                        self.clients.remove(c)
        _reap(proc)
        with self.write_lock, self.lock:
            if self.master == master:
                self.master = None
            os.close(master)
            if self.proc is proc:
                self.proc = None
                self.reason = self.held or "exited"
                self._broadcast(self.state())

    def write(self, data: bytes) -> None:
        with self.write_lock:
            fd = self.master
            if fd is None:
                return
            view = memoryview(data)
            while view:
                try:
                    n = os.write(fd, view)
                except InterruptedError:
                    continue
                except OSError:
                    return
                view = view[n:]

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)
        with self.write_lock:
            if self.master is not None:
                self._winsize(self.master)

    def _winsize(self, fd: int) -> None:
        cols, rows = self.size
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def control(self, payload: bytes) -> None:
        """A page's JSON control message: resize or power."""
        try:
            msg = json.loads(payload.decode("utf-8"))
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "resize":
            cols, rows = msg.get("cols"), msg.get("rows")
            if all(isinstance(v, int) and not isinstance(v, bool) and 2 <= v <= 500 for v in (cols, rows)):
                self.resize(cols, rows)
        elif msg.get("type") == "power" and msg.get("on") is True:
            self.power_on()

    def hold(self, reason: str) -> None:
        """Refuse power-on (the pages show reason) until release(): the control
        unit holds the set while `factory restart` or `factory update` runs,
        since both stop the Coordinator's sandbox."""
        with self.lock:
            self.held = reason
            if not self.running():
                self.reason = reason
            self._broadcast(self.state())

    def release(self) -> None:
        with self.lock:
            if self.held and not self.running() and self.reason == self.held:
                self.reason = None
            self.held = None
            self._broadcast(self.state())

    def hang_up(self, reason: str) -> None:
        """Hang up on the child as shutdown() does (SIGHUP, then SIGTERM, then
        SIGKILL), but keep the pages and the Term usable: the set shows
        powered off with reason, and POWER ON works again (unless held)."""
        with self.start_lock:
            proc = self.proc
            if proc is None:
                return
            _hang_up(proc)
            with self.lock:
                if self.proc is proc:  # the pump hasn't noticed yet
                    self.proc = None
                self.reason = reason
                self._broadcast(self.state())

    def shutdown(self) -> None:
        """Refuse any further power-on or page, hang up on the child (SIGHUP,
        then SIGTERM, then SIGKILL) and drop every page. A KeyboardInterrupt
        (a second Ctrl-C) during the hang-up still ends in SIGKILL and a
        reap, then propagates."""
        self.stopped.set()
        with self.start_lock:
            with self.lock:
                self.closing = True
            proc = self.proc
            if proc is not None:
                _hang_up(proc)
        with self.lock:
            clients, self.clients = self.clients, []
        for c in clients:
            c.close(close_frame(1001, "dashboard stopped"))


def _take_tty() -> None:  # in the child, after setsid(): make the PTY its controlling terminal
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _hang_up(proc) -> None:
    try:
        for sig in (signal.SIGHUP, signal.SIGTERM):
            _killpg(proc, sig)
            try:
                proc.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            _killpg(proc, signal.SIGKILL)
            for _ in range(3):
                try:
                    proc.wait(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    break
                except KeyboardInterrupt:  # yet another Ctrl-C: the reap still happens
                    continue


def _killpg(proc, sig) -> None:
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        pass


def _reap(proc) -> None:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# --------------------------------------------------------------------------
# The terminal origin's HTTP server

def make_handler(term: Term | None, token: str, cookie: str, key: str, port: int, main_port: int,
                 controls=None, cu_key: str = ""):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    csp = term_csp(port, main_port)
    cucsp = cu_csp(port, main_port)

    class Handler(BaseHTTPRequestHandler):
        server_version = "factory-dashboard-term"
        sys_version = ""
        # RFC 6455 needs an HTTP/1.1 101. Every response carries Content-Length,
        # so plain requests may keep the connection alive; timeout bounds the
        # HTTP phase (StreamRequestHandler applies it), cleared after the 101.
        protocol_version = "HTTP/1.1"
        timeout = HTTP_TIMEOUT

        def log_message(self, fmt, *args):
            pass

        def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
                 page_csp: str | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                # Framed by the main dashboard only (frame-ancestors), so no X-Frame-Options.
                self.send_header("Content-Security-Policy", page_csp or csp)
                self.send_header("Cross-Origin-Resource-Policy", "same-site")
            else:
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def refuse(self, code: int, text: str, extra: dict | None = None) -> None:
            # Never keep a refused connection alive (say, a POST whose body was not read).
            self.close_connection = True
            self.send(code, (text + "\n").encode(), "text/plain; charset=utf-8",
                      dict(extra or {}, Connection="close"))

        def do_GET(self) -> None:
            if self.headers.get("Host", "") not in hosts:
                self.refuse(403, "wrong Host header")
                return
            path = urlsplit(self.path).path
            if path == "/term/ws" and self.command == "GET" and term is not None:
                self.websocket()
                return
            if not cookie_matches(self.headers, cookie, token):
                self.refuse(403, "no token: open the link 'factory dashboard' printed")
                return
            if path.startswith(CU_API):
                self.refuse(405, "POST only")
            elif path == "/term" and term is not None:
                # The page carrying the key: only for the main page's iframe,
                # never a top-level navigation another local page could open.
                if not self.framed():
                    self.refuse(403, "the terminal opens inside the dashboard: open the link 'factory dashboard' printed")
                    return
                try:
                    html = (UI / "term.html").read_text(encoding="utf-8")
                except OSError:
                    self.refuse(500, "missing term.html")
                    return
                self.send(200, html.replace(KEY_SLOT, key).encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/cu" and controls is not None:
                # The control unit carries its own key: the same rule as /term.
                if not self.framed():
                    self.refuse(403, "the controls open inside the dashboard: open the link 'factory dashboard' printed")
                    return
                try:
                    html = (UI / "cu.html").read_text(encoding="utf-8")
                except OSError:
                    self.refuse(500, "missing cu.html")
                    return
                html = html.replace(CU_KEY_SLOT, cu_key).replace(CU_PORT_SLOT, str(int(main_port)))
                self.send(200, html.encode("utf-8"), "text/html; charset=utf-8", page_csp=cucsp)
            elif path in STATIC:
                name, ctype = STATIC[path]
                try:
                    body = (UI / name).read_bytes()
                except OSError:
                    self.refuse(500, f"missing {name}")
                    return
                self.send(200, body, ctype)
            else:
                self.refuse(404, "not found")

        do_HEAD = do_GET

        def framed(self) -> bool:
            """A same-site iframe load (the main dashboard framing this page)."""
            return (self.headers.get("Sec-Fetch-Dest", "") in FRAME_DESTS
                    and self.headers.get("Sec-Fetch-Site", "") in FRAME_SITES)

        def refuse_method(self) -> None:
            self.refuse(405, "GET only")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse_method

        def json_reply(self, code: int, obj, close: bool = False) -> None:
            extra = None
            if close:
                self.close_connection = True
                extra = {"Connection": "close"}
            self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                      "application/json; charset=utf-8", extra)

        def do_POST(self) -> None:
            """The control unit's API: POST /cu/api/<name>, a JSON object in and
            out. Nothing else on this origin takes a POST, and no response ever
            carries CORS headers."""
            path = urlsplit(self.path).path
            if controls is None or not path.startswith(CU_API):
                self.refuse(405, "GET only")
                return
            status, detail = control_refusal(self.headers, hosts=hosts, origins=origins, cookie=cookie,
                                             token=token, key=cu_key,
                                             limit=MAX_CONFIG_BODY if path == CONFIG_PREPARE else MAX_BODY)
            if status is not None:
                # The body was never read: end the connection with the reply.
                self.json_reply(status, {"error": detail}, close=True)
                return
            try:
                raw = read_exact(self.rfile, detail)
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (EOFError, OSError, ValueError):
                self.json_reply(400, {"error": "the body is not JSON"}, close=True)
                return
            if not isinstance(body, dict):
                self.json_reply(400, {"error": "send a JSON object"}, close=True)
                return
            code, out = controls.api(path[len(CU_API):], body)
            self.json_reply(code, out)

        def websocket(self) -> None:
            status, detail = upgrade_refusal(self.headers, origins=origins, cookie=cookie, token=token, key=key)
            if status is None:
                with term.lock:
                    if term.closing:
                        status, detail = 503, "the dashboard is stopping"
                    elif len(term.clients) >= term.max_clients:
                        status, detail = 503, "too many terminal pages open"
            if status is not None:
                self.refuse(status, detail, {"Sec-WebSocket-Version": "13"} if status == 426 else None)
                return
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept_key(self.headers["Sec-WebSocket-Key"]))
            self.send_header("Sec-WebSocket-Protocol", detail)
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            self.connection.settimeout(None)  # the heartbeat watches the socket from here on
            client = Client(self.connection)
            if not term.attach(client):
                client.close(close_frame(1001, "the dashboard is stopping") if term.closing
                             else close_frame(1013, "too many terminal pages open"))
                client.thread.join(5)
                return
            try:
                self.serve_socket(client)
            finally:
                term.detach(client)
                client.thread.join(5)  # let the close frame out before the socket closes

        def serve_socket(self, client: Client) -> None:
            def control(opcode, payload):
                client.last_seen = term.clock()  # a PONG (or PING) proves the page is there
                if opcode == PING:
                    client.send(encode_frame(PONG, payload))
            try:
                while True:
                    opcode, payload = read_message(self.rfile, control)
                    client.last_seen = term.clock()
                    if opcode == CLOSE:
                        code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1000
                        client.close(close_frame(code if 1000 <= code < 5000 and code not in (1005, 1006) else 1000))
                        return
                    if opcode == BINARY:
                        term.write(payload)
                    else:
                        term.control(payload)
            except ProtocolError as e:
                client.close(close_frame(e.code, str(e)))
            except (EOFError, OSError, ValueError):
                client.close()

    return Handler


def serve(term: Term | None, token: str, cookie: str, main_port: int, port: int = 0, key: str | None = None,
          controls=None, cu_key: str | None = None) -> tuple:
    """Start the second origin on 127.0.0.1 in a background thread: (server, key).
    It serves the terminal when term is given (else /term is a 404) and the
    control unit when controls is given (else /cu is a 404), each with its
    own key; server.cu_key is the control unit's."""
    key = key or secrets.token_hex(24)
    cu_key = cu_key or secrets.token_hex(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    server.daemon_threads = True
    real_port = server.server_address[1]
    server.cu_key = cu_key
    server.RequestHandlerClass = make_handler(term, token, cookie, key, real_port, main_port, controls, cu_key)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if term is not None:
        threading.Thread(target=term.heartbeat_loop, daemon=True).start()
    return server, key


def wait_for(cond, timeout: float = 5.0, step: float = 0.02) -> bool:
    """Poll cond() until it is true or timeout passes (used by the tests)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())
