"""Egress guard: a CONNECT-only allowlist proxy for an agent's model traffic.

    python3 -m passdesk egress-guard [--bind HOST] [--port PORT] [--max-connections N]

Runs from the Passdesk image (so there is one pin) on both the agents
network and the egress network of a standalone deployment
(docs/standalone.md); the agent container points HTTPS_PROXY at it. It
forwards exactly one thing: `CONNECT host:443` to a host named in
policy.toml's [egress_guard].allow_hosts (exact names, no wildcards), after
resolving the name itself and checking that every address is global
unicast. It then connects to one of those exact addresses (no second
lookup, so no DNS rebinding) and relays bytes both ways without looking at
them: no TLS interception. Every other method, port, host or address is
refused with an HTTP error and one log line naming the target, never the
payload. Header reads, upstream connects and idle tunnels are bounded, and
the number of connections is capped.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import selectors
import socket
import socketserver
import sys
import threading
import time

from .core import policy as policy_mod

PORT = 3128
MAX_LINE = 1024            # bytes: the request line
MAX_HEADER_BYTES = 8192    # bytes: the whole header block
READ_TIMEOUT = 10.0        # seconds to receive the request
CONNECT_TIMEOUT = 10.0     # seconds to reach the upstream
IDLE_TIMEOUT = 600.0       # seconds with no bytes either way before a tunnel closes
MAX_CONNECTIONS = 64
_REQUEST_RE = re.compile(r"CONNECT ([A-Za-z0-9.-]{1,253}):443 HTTP/1\.[01]")
_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message, file=sys.stderr, flush=True)


def normalise_host(host: str) -> str:
    return host.lower().rstrip(".")


def valid_host(host: str) -> bool:
    """A normalised DNS name of at least two labels, and not an IP literal."""
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.fullmatch(label) for label in labels):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def load_allowlist(pol: dict | None = None) -> frozenset[str]:
    """[egress_guard].allow_hosts from the baked-in policy (plus overlay):
    exact host names, normalised. Raises ValueError for anything else."""
    pol = policy_mod.load_policy() if pol is None else pol
    table = pol.get("egress_guard")
    hosts = table.get("allow_hosts", []) if isinstance(table, dict) else []
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ValueError("[egress_guard].allow_hosts must be a list of host names")
    out = set()
    for entry in hosts:
        host = normalise_host(entry)
        if not valid_host(host):
            raise ValueError(f"[egress_guard].allow_hosts entry {entry!r}: exact DNS names only "
                             "(no wildcards, ports, IP literals or scheme)")
        out.add(host)
    return frozenset(out)


def global_unicast(address: str) -> bool:
    """True only for an address on the public internet: not RFC 1918,
    loopback, link-local (169.254.169.254), shared (100.64/10), ULA
    (fc00::/7), multicast, unspecified or reserved, and not an IPv6 address
    that embeds an IPv4 one (v4-mapped, 6to4, Teredo)."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_private or ip.is_loopback or ip.is_link_local
                                 or ip.is_reserved or ip.is_unspecified)


def resolve(host: str) -> list[tuple[int, tuple]]:
    """Every address of host:443 as (family, sockaddr), each checked with
    global_unicast. Raises LookupError naming the first address that fails,
    or OSError when the name doesn't resolve."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    out = []
    for family, _, _, _, sockaddr in infos:
        if not global_unicast(sockaddr[0]):
            raise LookupError(f"{host} resolves to {sockaddr[0]}, which is not a global unicast address")
        out.append((family, sockaddr))
    if not out:
        raise LookupError(f"{host} has no address")
    return out


def connect(addresses: list[tuple[int, tuple]]) -> socket.socket:
    """Connect to the first of the checked addresses that answers: the exact
    sockaddr, never the name again."""
    error: OSError = OSError("no address")
    for family, sockaddr in addresses:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(sockaddr)
            sock.settimeout(None)
            return sock
        except OSError as e:
            sock.close()
            error = e
    raise error


def relay(a: socket.socket, b: socket.socket) -> None:
    """Copy bytes both ways until both sides have closed, a side errors,
    or nothing moves for IDLE_TIMEOUT. A close on one side half-closes the
    other (shutdown(SHUT_WR)), so a response in flight still arrives."""
    with selectors.DefaultSelector() as sel:
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        open_sides = 2
        while open_sides:
            events = sel.select(IDLE_TIMEOUT)
            if not events:
                return
            for key, _ in events:
                src, dst = key.fileobj, key.data
                try:
                    data = src.recv(65536)
                    if data:
                        dst.sendall(data)
                        continue
                    sel.unregister(src)
                    open_sides -= 1
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    return


def _header_end(buf: bytes) -> int:
    """Index just past the header block's blank line, or -1 if not there yet."""
    for terminator in (b"\r\n\r\n", b"\n\n"):
        at = buf.find(terminator)
        if at >= 0:
            return at + len(terminator)
    return -1


class Handler(socketserver.StreamRequestHandler):
    def refuse(self, code: int, reason: str) -> None:
        log(f"deny {code} {reason} client={self.client_address[0]}")
        try:
            self.wfile.write(f"HTTP/1.1 {code} Refused\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError:
            pass

    def handle(self) -> None:
        if not self.server.slots.acquire(blocking=False):
            return self.refuse(503, "too many connections")
        try:
            self.serve()
        finally:
            self.server.slots.release()

    def read_request(self) -> tuple[str, bytes] | None:
        """The request line, plus any bytes the client sent after its header
        block (relayed once the tunnel is up, not swallowed). One deadline
        for the whole read, whatever the pace, and one size cap; None after
        a refusal when it doesn't fit or doesn't arrive."""
        deadline = time.monotonic() + READ_TIMEOUT
        buf = b""
        while (end := _header_end(buf)) < 0:
            if len(buf) > MAX_HEADER_BYTES:
                self.refuse(400, "headers too long")
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.refuse(408, "timed out reading the request")
                return None
            self.connection.settimeout(remaining)
            try:
                chunk = self.connection.recv(MAX_HEADER_BYTES + 1 - len(buf))
            except OSError:
                self.refuse(408, "timed out reading the request")
                return None
            if not chunk:
                self.refuse(400, "connection closed before the request completed")
                return None
            buf += chunk
        line = buf[:end].split(b"\n", 1)[0].rstrip(b"\r")
        if len(line) > MAX_LINE:
            self.refuse(400, "request line too long")
            return None
        return line.decode("ascii", "replace"), buf[end:]

    def serve(self) -> None:
        read = self.read_request()
        if read is None:
            return
        request, early = read
        match = _REQUEST_RE.fullmatch(request)
        if match is None:
            method = request.split(" ", 1)[0]
            return self.refuse(405 if method and method != "CONNECT" else 400,
                               f"only CONNECT to port 443 is accepted, got {request[:120]!r}")
        host = normalise_host(match.group(1))
        if not valid_host(host) or host not in self.server.allow:
            return self.refuse(403, f"{host}:443 is not in the allowlist")
        try:
            addresses = resolve(host)
        except LookupError as e:
            return self.refuse(403, str(e))
        except OSError as e:
            return self.refuse(502, f"{host}: cannot resolve: {e}")
        try:
            upstream = connect(addresses)
        except OSError as e:
            return self.refuse(502, f"{host}: cannot connect: {e}")
        with upstream:
            self.connection.settimeout(None)
            log(f"allow {host}:443 -> {upstream.getpeername()[0]} client={self.client_address[0]}")
            try:
                if early:
                    upstream.sendall(early)
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            except OSError:
                return
            relay(self.connection, upstream)


class Guard(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], allow: frozenset[str], max_connections: int = MAX_CONNECTIONS):
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.allow = allow
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, Handler)


def serve(bind: str = "0.0.0.0", port: int = PORT, max_connections: int = MAX_CONNECTIONS) -> int:
    try:
        allow = load_allowlist()
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not allow:
        print("error: [egress_guard].allow_hosts is empty: nothing to forward (README, \"Egress guard\")",
              file=sys.stderr)
        return 2
    guard = Guard((bind, port), allow, max_connections)
    log(f"egress guard listening on {bind}:{port}, forwarding CONNECT to port 443 on: {', '.join(sorted(allow))}")
    guard.serve_forever()
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("egress-guard", help="run the CONNECT allowlist proxy for an agent's model traffic")
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: every interface)")
    parser.add_argument("--port", type=int, default=PORT, help=f"port to listen on (default: {PORT})")
    parser.add_argument("--max-connections", type=int, default=MAX_CONNECTIONS,
                        help=f"concurrent connections before new ones get 503 (default: {MAX_CONNECTIONS})")
    parser.set_defaults(func=lambda args: serve(args.bind, args.port, args.max_connections))
