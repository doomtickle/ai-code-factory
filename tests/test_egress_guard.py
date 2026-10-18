"""The egress guard (passdesk/egress_guard.py): a CONNECT-only allowlist
proxy. A local fake upstream stands in for a model API; name resolution is
stubbed so the address checks see private, link-local and v6 addresses
without any network."""

import contextlib
import io
import socket
import socketserver
import threading
import unittest
from pathlib import Path
from unittest import mock

from passdesk import __main__ as cli
from passdesk import egress_guard as guard
from passdesk.core import policy as policy_mod

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
NOT_GLOBAL = ["10.0.0.1", "172.16.5.5", "192.168.1.1", "127.0.0.1", "169.254.169.254", "100.64.0.1",
              "0.0.0.0", "224.0.0.1", "255.255.255.255", "192.0.2.1", "240.0.0.1",
              "::1", "::", "fc00::1", "fd12::1", "fe80::1", "ff02::1", "::ffff:8.8.8.8", "::ffff:10.0.0.1",
              "2002:808:808::1", "2001:0:1::1"]


class Echo(socketserver.StreamRequestHandler):
    """The fake upstream: echoes each line back prefixed, and on EOF sends a
    final line so a half-closed client still gets its reply."""

    def handle(self):
        for line in self.rfile:
            self.wfile.write(b"echo:" + line)
        self.wfile.write(b"bye\n")


_real_getaddrinfo = socket.getaddrinfo
TEST_NAMES = {"model.example", "other.example"}


def addrinfo(*addresses, error=None):
    """A getaddrinfo stand-in: for the test names, these addresses on port
    443 (or `error`); anything else (the tests' own loopback connections)
    goes to the real function."""
    def fake(host, port, *args, **kwargs):
        if host not in TEST_NAMES:
            return _real_getaddrinfo(host, port, *args, **kwargs)
        if error is not None:
            raise error
        out = []
        for a in addresses:
            family = socket.AF_INET6 if ":" in a else socket.AF_INET
            sockaddr = (a, 443, 0, 0) if family == socket.AF_INET6 else (a, 443)
            out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return out
    return fake


class GuardBase(unittest.TestCase):
    allow = frozenset({"model.example", "other.example"})
    max_connections = guard.MAX_CONNECTIONS

    def setUp(self):
        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, args=(0.02,), daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.guard = guard.Guard(("127.0.0.1", 0), self.allow, self.max_connections)
        threading.Thread(target=self.guard.serve_forever, args=(0.02,), daemon=True).start()
        self.addCleanup(self.guard.server_close)
        self.addCleanup(self.guard.shutdown)
        self.log = io.StringIO()
        patch = mock.patch.object(guard.sys, "stderr", self.log)
        patch.start()
        self.addCleanup(patch.stop)

    def upstream_addresses(self):
        return [(socket.AF_INET, self.upstream.server_address)]

    def via_upstream(self):
        """Make `resolve` answer with the fake upstream's loopback address,
        standing in for a checked public one."""
        patch = mock.patch.object(guard, "resolve", return_value=self.upstream_addresses())
        patch.start()
        self.addCleanup(patch.stop)

    def open(self, raw: bytes, timeout=5.0) -> tuple[socket.socket, str]:
        """Send `raw` to the guard; return the socket and the status line."""
        sock = socket.create_connection(self.guard.server_address, timeout=timeout)
        self.addCleanup(sock.close)
        sock.sendall(raw)
        # Unbuffered, and closed right away: a lingering makefile object
        # would keep the fd open after sock.close(), and read-ahead would
        # steal tunnel bytes.
        with sock.makefile("rb", buffering=0) as f:
            status = f.readline().decode().rstrip("\r\n")
            while f.readline() not in (b"\r\n", b"\n", b""):  # headers
                pass
        return sock, status

    def connect(self, target: str, timeout=5.0):
        return self.open(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode(), timeout)


class TunnelTests(GuardBase):
    def test_allowlisted_connect_tunnels_both_ways(self):
        self.via_upstream()
        sock, status = self.connect("model.example:443")
        self.assertEqual(status, "HTTP/1.1 200 Connection established")
        sock.sendall(b"hello\n")
        self.assertEqual(sock.recv(100), b"echo:hello\n")
        sock.shutdown(socket.SHUT_WR)  # half-close: the upstream's final line still arrives
        self.assertEqual(sock.recv(100), b"bye\n")
        self.assertEqual(sock.recv(100), b"")
        self.assertIn("allow model.example:443 -> 127.0.0.1", self.log.getvalue())

    def test_bytes_sent_right_after_the_headers_are_not_swallowed(self):
        self.via_upstream()
        sock, status = self.open(b"CONNECT model.example:443 HTTP/1.1\r\n\r\nearly\n")
        self.assertEqual(status, "HTTP/1.1 200 Connection established")
        self.assertEqual(sock.recv(100), b"echo:early\n")

    def test_host_is_normalised_before_matching(self):
        self.via_upstream()
        for target in ("Model.Example:443", "model.example.:443", "MODEL.EXAMPLE.:443"):
            _, status = self.connect(target)
            self.assertEqual(status, "HTTP/1.1 200 Connection established", target)

    def test_http_1_0_connect_is_accepted(self):
        self.via_upstream()
        _, status = self.open(b"CONNECT model.example:443 HTTP/1.0\r\n\r\n")
        self.assertEqual(status, "HTTP/1.1 200 Connection established")

    def test_connects_to_the_checked_address_with_no_second_lookup(self):
        self.via_upstream()
        with mock.patch.object(guard.socket, "getaddrinfo", addrinfo(error=AssertionError("re-resolved the name"))):
            sock, status = self.connect("model.example:443")
            self.assertEqual(status, "HTTP/1.1 200 Connection established")
            sock.sendall(b"x\n")
            self.assertEqual(sock.recv(100), b"echo:x\n")

    def test_upstream_connection_refused_is_502(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()  # nothing listens here now
        with mock.patch.object(guard, "resolve", return_value=[(socket.AF_INET, ("127.0.0.1", port))]):
            _, status = self.connect("model.example:443")
        self.assertEqual(status, "HTTP/1.1 502 Refused")
        self.assertIn("deny 502 model.example: cannot connect", self.log.getvalue())

    def test_payload_never_reaches_the_log(self):
        self.via_upstream()
        marker = b"SECRET-PAYLOAD-7f3a"
        sock, _ = self.connect("model.example:443")
        sock.sendall(marker + b"\n")
        self.assertEqual(sock.recv(100), b"echo:" + marker + b"\n")
        self.assertNotIn(marker.decode(), self.log.getvalue())


class RequestRefusalTests(GuardBase):
    def test_host_not_in_allowlist_is_403(self):
        with mock.patch.object(guard.socket, "getaddrinfo", addrinfo(PUBLIC_V4)):
            for target in ("github.com:443", "sub.model.example:443", "modelxexample:443", "model.example.evil:443"):
                _, status = self.connect(target)
                self.assertEqual(status, "HTTP/1.1 403 Refused", target)
        self.assertIn("deny 403 github.com:443 is not in the allowlist", self.log.getvalue())

    def test_only_port_443(self):
        self.via_upstream()
        for target in ("model.example:80", "model.example:8443", "model.example:4430", "model.example", "model.example:"):
            _, status = self.connect(target)
            self.assertEqual(status, "HTTP/1.1 400 Refused", target)

    def test_ip_literal_targets_are_refused(self):
        self.via_upstream()
        for target in ("1.2.3.4:443", "127.0.0.1:443", "[::1]:443", "[2606:4700:4700::1111]:443",
                       "[::ffff:1.2.3.4]:443", "0x7f.1:443", "2130706433:443"):
            _, status = self.connect(target)
            self.assertIn(status, ("HTTP/1.1 400 Refused", "HTTP/1.1 403 Refused"), target)
        self.assertNotIn("allow ", self.log.getvalue())

    def test_other_methods_are_405(self):
        self.via_upstream()
        for raw in (b"GET http://model.example/ HTTP/1.1\r\nHost: model.example\r\n\r\n",
                    b"GET / HTTP/1.1\r\n\r\n", b"POST model.example:443 HTTP/1.1\r\n\r\n",
                    b"connect model.example:443 HTTP/1.1\r\n\r\n"):
            _, status = self.open(raw)
            self.assertEqual(status, "HTTP/1.1 405 Refused", raw)

    def test_malformed_request_lines_are_400(self):
        self.via_upstream()
        for raw in (b"\r\n\r\n", b"CONNECT model.example:443\r\n\r\n", b"CONNECT model.example:443 HTTP/2\r\n\r\n",
                    b"CONNECT  model.example:443 HTTP/1.1\r\n\r\n", b"CONNECT model.example:443 HTTP/1.1 extra\r\n\r\n",
                    b"CONNECT http://model.example:443 HTTP/1.1\r\n\r\n", b"CONNECT model.example:443\x00 HTTP/1.1\r\n\r\n"):
            _, status = self.open(raw)
            self.assertEqual(status, "HTTP/1.1 400 Refused", raw)

    def test_overlong_request_line_is_400(self):
        self.via_upstream()
        _, status = self.open(b"CONNECT " + b"a" * guard.MAX_LINE + b":443 HTTP/1.1\r\n\r\n")
        self.assertEqual(status, "HTTP/1.1 400 Refused")

    def test_overlong_headers_are_400(self):
        self.via_upstream()
        headers = b"".join(b"X-Pad-%d: %s\r\n" % (i, b"p" * 500) for i in range(20))
        _, status = self.open(b"CONNECT model.example:443 HTTP/1.1\r\n" + headers + b"\r\n")
        self.assertEqual(status, "HTTP/1.1 400 Refused")

    def test_slow_client_is_cut_off(self):
        with mock.patch.object(guard, "READ_TIMEOUT", 0.3):
            _, status = self.open(b"CONNECT model.example:443 HTTP/1.1\r\n")  # never finishes the headers
        self.assertEqual(status, "HTTP/1.1 408 Refused")

    def test_dripping_client_is_cut_off_at_the_deadline(self):
        # One byte every 0.1 s, forever: the deadline is on the whole read,
        # not reset by each byte that arrives.
        with mock.patch.object(guard, "READ_TIMEOUT", 0.5):
            sock = socket.create_connection(self.guard.server_address, timeout=5)
            self.addCleanup(sock.close)
            sock.settimeout(0.1)
            data = b""
            for byte in b"CONNECT model.example:443 HTTP/1.1\r\nX-Slow: " + b"y" * 100:
                try:
                    sock.sendall(bytes([byte]))
                    data = sock.recv(1, socket.MSG_PEEK)  # nothing until the 408
                except TimeoutError:
                    continue
                break
            sock.settimeout(5)
            data = sock.recv(200)
        self.assertTrue(data.startswith(b"HTTP/1.1 408"), data)

    def test_client_closing_early_is_400(self):
        sock = socket.create_connection(self.guard.server_address, timeout=5)
        sock.sendall(b"CONNECT model.example:443 HTTP/1.1\r\n")
        sock.shutdown(socket.SHUT_WR)
        with sock:
            self.assertTrue(sock.recv(200).startswith(b"HTTP/1.1 400"))

    def test_dns_failure_is_502(self):
        with mock.patch.object(guard.socket, "getaddrinfo", addrinfo(error=socket.gaierror("no such name"))):
            _, status = self.connect("model.example:443")
        self.assertEqual(status, "HTTP/1.1 502 Refused")
        self.assertIn("deny 502 model.example: cannot resolve", self.log.getvalue())


class AddressCheckTests(GuardBase):
    def test_every_non_global_resolution_is_refused(self):
        for address in NOT_GLOBAL:
            with mock.patch.object(guard.socket, "getaddrinfo", addrinfo(address)):
                _, status = self.connect("model.example:443")
            self.assertEqual(status, "HTTP/1.1 403 Refused", address)
            self.assertIn(f"model.example resolves to {address}", self.log.getvalue(), address)
        self.assertNotIn("allow ", self.log.getvalue())

    def test_one_bad_address_among_good_ones_refuses_the_whole_name(self):
        for addresses in ((PUBLIC_V4, "10.0.0.1"), ("169.254.169.254", PUBLIC_V4), (PUBLIC_V6, "fc00::1")):
            with mock.patch.object(guard.socket, "getaddrinfo", addrinfo(*addresses)):
                _, status = self.connect("model.example:443")
            self.assertEqual(status, "HTTP/1.1 403 Refused", addresses)

    def test_empty_resolution_is_refused(self):
        with mock.patch.object(guard.socket, "getaddrinfo", addrinfo()):
            _, status = self.connect("model.example:443")
        self.assertEqual(status, "HTTP/1.1 403 Refused")

    def test_global_unicast(self):
        for address in (PUBLIC_V4, PUBLIC_V6, "8.8.8.8", "2606:4700:4700::1111", "1.1.1.1"):
            self.assertTrue(guard.global_unicast(address), address)
        for address in NOT_GLOBAL:
            self.assertFalse(guard.global_unicast(address), address)

    def test_resolve_returns_every_checked_address_in_order(self):
        with mock.patch.object(guard.socket, "getaddrinfo", addrinfo(PUBLIC_V4, PUBLIC_V6)):
            self.assertEqual(guard.resolve("model.example"),
                             [(socket.AF_INET, (PUBLIC_V4, 443)), (socket.AF_INET6, (PUBLIC_V6, 443, 0, 0))])

    def test_connect_tries_the_next_address_when_one_fails(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        sock = guard.connect([(socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET, self.upstream.server_address)])
        with sock:
            self.assertEqual(sock.getpeername(), self.upstream.server_address)


class ConcurrencyTests(GuardBase):
    max_connections = 1

    def test_cap_refuses_with_503_and_frees_on_close(self):
        self.via_upstream()
        first, status = self.connect("model.example:443")
        self.assertEqual(status, "HTTP/1.1 200 Connection established")
        _, status = self.connect("model.example:443")
        self.assertEqual(status, "HTTP/1.1 503 Refused")
        self.assertIn("deny 503 too many connections", self.log.getvalue())
        first.close()
        for _ in range(50):  # the slot frees as the handler thread winds down
            _, status = self.connect("model.example:443")
            if status.startswith("HTTP/1.1 200"):
                break
            threading.Event().wait(0.05)
        self.assertEqual(status, "HTTP/1.1 200 Connection established")


class AllowlistTests(unittest.TestCase):
    def test_repo_policy_defaults(self):
        self.assertEqual(guard.load_allowlist(policy_mod.load_policy(Path(__file__).parents[1] / "policy.toml")),
                         {"api.anthropic.com", "platform.claude.com", "claude.ai", "claude.com",
                          "chatgpt.com", "auth.openai.com", "api.openai.com"})

    def test_entries_are_normalised(self):
        self.assertEqual(guard.load_allowlist({"egress_guard": {"allow_hosts": ["API.Anthropic.com.", "api.anthropic.com"]}}),
                         {"api.anthropic.com"})

    def test_missing_table_is_empty(self):
        self.assertEqual(guard.load_allowlist({}), frozenset())
        self.assertEqual(guard.load_allowlist({"egress_guard": {}}), frozenset())

    def test_bad_entries_are_refused(self):
        for bad in ("*.cursor.sh", "api.anthropic.com:443", "https://api.anthropic.com", "1.2.3.4", "[::1]",
                    "localhost", "api", "", "api anthropic.com", "-api.anthropic.com", "api.anthropic.com/v1"):
            with self.assertRaisesRegex(ValueError, "exact DNS names only", msg=bad):
                guard.load_allowlist({"egress_guard": {"allow_hosts": [bad]}})
        for bad_list in ("api.anthropic.com", ["api.anthropic.com", 1], {"host": "x"}):
            with self.assertRaisesRegex(ValueError, "must be a list"):
                guard.load_allowlist({"egress_guard": {"allow_hosts": bad_list}})

    def test_serve_refuses_to_start_on_bad_or_empty_allowlist(self):
        err = io.StringIO()
        with mock.patch.object(guard, "load_allowlist", return_value=frozenset()), contextlib.redirect_stderr(err):
            self.assertEqual(guard.serve("127.0.0.1", 0), 2)
        self.assertIn("is empty", err.getvalue())
        with mock.patch.object(guard, "load_allowlist", side_effect=ValueError("bad entry")), contextlib.redirect_stderr(err):
            self.assertEqual(guard.serve("127.0.0.1", 0), 2)
        self.assertIn("bad entry", err.getvalue())


class CliTests(unittest.TestCase):
    def test_subcommand_passes_its_options_to_serve(self):
        with mock.patch.object(guard, "serve", return_value=0) as serve:
            code = cli.main(["egress-guard", "--bind", "127.0.0.1", "--port", "3129", "--max-connections", "5"])
        self.assertEqual(code, 0)
        serve.assert_called_once_with("127.0.0.1", 3129, 5)

    def test_defaults(self):
        with mock.patch.object(guard, "serve", return_value=0) as serve:
            cli.main(["egress-guard"])
        serve.assert_called_once_with("0.0.0.0", guard.PORT, guard.MAX_CONNECTIONS)


if __name__ == "__main__":
    unittest.main()
