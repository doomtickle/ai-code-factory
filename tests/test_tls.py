"""TLS on the MCP listener (BROKER_TLS_CERT/BROKER_TLS_KEY) and the
standalone rule that a bind off loopback needs it or BROKER_INSECURE_BIND=1.
Certificates are minted with openssl into a temp dir; the listener tests
skip where openssl isn't installed."""

import http.client
import os
import shutil
import socket
import ssl
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

from passdesk import doctor
from passdesk.core import config, protocol

from .base import Base

OPENSSL = shutil.which("openssl")


def mint(directory: Path, cn: str = "localhost") -> tuple[Path, Path]:
    """A throwaway self-signed certificate and key (the README's recipe)."""
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-days", "2", "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={cn}",
                    "-addext", f"subjectAltName=DNS:{cn},IP:127.0.0.1"],
                   check=True, capture_output=True)
    os.chmod(key, 0o600)
    return cert, key


class TlsSettings(Base):
    def setUp(self):
        super().setUp()
        for name in ("DEPLOYMENT", "BIND_HOST", "TLS_CERT", "TLS_KEY", "INSECURE_BIND"):
            self.addCleanup(setattr, config, name, getattr(config, name))
        config.TLS_CERT = config.TLS_KEY = ""
        config.INSECURE_BIND = False

    def tls_files(self):
        root = Path(self.tmp.name) / "tls"
        root.mkdir()
        config.TLS_CERT, config.TLS_KEY = str(root / "cert.pem"), str(root / "key.pem")


class ValidateTests(TlsSettings):
    def test_standalone_refuses_non_loopback_bind_without_tls(self):
        config.DEPLOYMENT = "standalone"
        for host in ("0.0.0.0", "", "::", "10.0.0.5", "passdesk"):
            config.BIND_HOST = host
            with self.assertRaisesRegex(ValueError, "BROKER_BIND_HOST", msg=host):
                config.validate()

    def test_standalone_loopback_bind_is_fine_without_tls(self):
        config.DEPLOYMENT = "standalone"
        for host in ("127.0.0.1", "::1", "127.1.2.3", "localhost"):
            config.BIND_HOST = host
            config.validate()

    def test_insecure_bind_flag_allows_it(self):
        config.DEPLOYMENT, config.BIND_HOST, config.INSECURE_BIND = "standalone", "0.0.0.0", True
        config.validate()

    def test_tls_allows_it(self):
        config.DEPLOYMENT, config.BIND_HOST = "standalone", "0.0.0.0"
        self.tls_files()
        config.validate()

    def test_sbx_default_bind_keeps_starting_without_tls(self):
        # The factory's deployment: 0.0.0.0, no TLS, no flag. Must not be refused.
        config.DEPLOYMENT, config.BIND_HOST = "sbx", "0.0.0.0"
        config.validate()

    def test_cert_and_key_must_be_set_together(self):
        for deployment in ("sbx", "standalone"):
            config.DEPLOYMENT, config.BIND_HOST = deployment, "127.0.0.1"
            config.TLS_CERT, config.TLS_KEY = "/etc/passdesk/tls/cert.pem", ""
            with self.assertRaisesRegex(ValueError, "set together"):
                config.validate()
            config.TLS_CERT, config.TLS_KEY = "", "/etc/passdesk/tls/key.pem"
            with self.assertRaisesRegex(ValueError, "set together"):
                config.validate()

    def test_tls_files_inside_the_workspace_are_refused(self):
        config.DEPLOYMENT, config.BIND_HOST = "standalone", "0.0.0.0"
        inside = config.WORKSPACE / "tls"
        inside.mkdir()
        config.TLS_CERT, config.TLS_KEY = str(inside / "cert.pem"), str(Path(self.tmp.name) / "key.pem")
        with self.assertRaisesRegex(ValueError, "BROKER_TLS_CERT.*inside the agent workspace"):
            config.validate()
        config.TLS_CERT, config.TLS_KEY = str(Path(self.tmp.name) / "cert.pem"), str(inside / "key.pem")
        with self.assertRaisesRegex(ValueError, "BROKER_TLS_KEY.*inside the agent workspace"):
            config.validate()
        # ... also through a symlink that points into it.
        link = Path(self.tmp.name) / "link"
        link.symlink_to(inside)
        config.TLS_CERT, config.TLS_KEY = str(Path(self.tmp.name) / "cert.pem"), str(link / "key.pem")
        with self.assertRaisesRegex(ValueError, "BROKER_TLS_KEY.*inside the agent workspace"):
            config.validate()

    def test_is_loopback(self):
        for host in ("127.0.0.1", "::1", "127.255.255.254", "localhost"):
            self.assertTrue(config.is_loopback(host), host)
        for host in ("", "0.0.0.0", "::", "10.0.0.1", "169.254.1.1", "no-such-host.invalid", "passdesk"):
            self.assertFalse(config.is_loopback(host), host)

    def test_main_exits_clearly_on_the_bind_rule(self):
        config.DEPLOYMENT, config.BIND_HOST = "standalone", "0.0.0.0"
        with self.assertRaises(SystemExit) as cm:
            protocol.main()
        self.assertIn("BROKER_BIND_HOST", str(cm.exception))
        self.assertIn("BROKER_INSECURE_BIND=1", str(cm.exception))


@unittest.skipUnless(OPENSSL, "openssl not installed")
class ListenerTests(TlsSettings):
    """A real TLS listener on 127.0.0.1, port 0."""

    def setUp(self):
        super().setUp()
        config.DEPLOYMENT, config.BIND_HOST = "standalone", "127.0.0.1"
        self.cert, self.key = mint(Path(self.tmp.name))
        config.TLS_CERT, config.TLS_KEY = str(self.cert), str(self.key)
        self.server = protocol.make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # Handshake failures print one line to stderr; keep the test output clean.
        quiet = mock.patch.object(protocol.Server, "handle_error", lambda *a: None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def https(self, method="POST", path="/mcp", body=b"{}", headers=None, server_hostname="localhost"):
        ctx = ssl.create_default_context(cafile=str(self.cert))
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=5)
        try:
            conn.request(method, path, body=body, headers={"Content-Length": str(len(body)), **(headers or {})})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_tls_client_reaches_the_mcp_handler(self):
        status, _ = self.https()
        self.assertEqual(status, 401)  # no token: the handler answered, over TLS

    def test_certificate_is_verified_against_the_configured_file(self):
        # A client that trusts only the system store rejects the self-signed cert.
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=5)
        with self.assertRaises(ssl.SSLCertVerificationError):
            conn.request("POST", "/mcp", body=b"{}")
        conn.close()

    def test_plain_http_client_gets_no_response(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        with self.assertRaises((http.client.BadStatusLine, http.client.RemoteDisconnected, ConnectionError, OSError)):
            conn.request("POST", "/mcp", body=b"{}")
            conn.getresponse()
        conn.close()
        # ... and the listener is still serving afterwards.
        self.assertEqual(self.https()[0], 401)

    def test_stalled_client_does_not_block_others(self):
        # A client that connects and never starts its handshake. With the
        # handshake done per connection (Handler.setup), not in accept(),
        # the next client's request still completes immediately.
        stalled = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(stalled.close)
        with mock.patch.object(protocol.Handler, "handshake_timeout", 30):
            self.assertEqual(self.https()[0], 401)

    def test_minimum_version_is_tls12(self):
        self.assertEqual(protocol.tls_context().minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_without_tls_make_server_is_plain(self):
        config.TLS_CERT = config.TLS_KEY = ""
        server = protocol.make_server("127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.assertNotIsInstance(server.socket, ssl.SSLSocket)
        self.assertIsNone(protocol.tls_context())


@unittest.skipUnless(OPENSSL, "openssl not installed")
class StartupTests(TlsSettings):
    def test_main_exits_clearly_when_tls_files_do_not_load(self):
        config.DEPLOYMENT, config.BIND_HOST = "standalone", "127.0.0.1"
        self.addCleanup(setattr, config, "PORT", config.PORT)
        config.PORT = 0  # never the real port; and a bad certificate must exit before binding anyway
        cert, key = mint(Path(self.tmp.name))
        bogus = Path(self.tmp.name) / "bogus.pem"
        bogus.write_text("not a key\n")
        config.TLS_CERT, config.TLS_KEY = str(cert), str(bogus)
        with self.assertRaises(SystemExit) as cm:
            protocol.main()
        self.assertIn("cannot start the listener", str(cm.exception))
        self.assertNotIn("not a key", str(cm.exception))
        config.TLS_CERT, config.TLS_KEY = str(cert), str(Path(self.tmp.name) / "missing.pem")
        with self.assertRaises(SystemExit) as cm:
            protocol.main()
        self.assertIn("missing.pem", str(cm.exception))


class DoctorTests(TlsSettings):
    def setUp(self):
        super().setUp()
        config.DEPLOYMENT, config.BIND_HOST = "standalone", "0.0.0.0"

    def test_quiet_on_loopback_without_tls(self):
        config.BIND_HOST = "127.0.0.1"
        report = doctor.Report()
        doctor.check_tls(report)
        self.assertEqual(report.items, [])

    def test_off_loopback_without_tls_fails_in_standalone(self):
        report = doctor.Report()
        doctor.check_tls(report)
        self.assertTrue(any("refuses to start" in m for m in report.messages(doctor.FAIL)))

    def test_off_loopback_with_insecure_bind_warns(self):
        config.INSECURE_BIND = True
        report = doctor.Report()
        doctor.check_tls(report)
        self.assertEqual(report.messages(doctor.FAIL), [])
        self.assertTrue(any("BROKER_INSECURE_BIND=1" in m for m in report.messages(doctor.WARN)))

    def test_sbx_off_loopback_without_tls_is_quiet(self):
        # sbx's default; the doctor's usual sbx report must not change.
        config.DEPLOYMENT = "sbx"
        report = doctor.Report()
        doctor.check_tls(report)
        self.assertEqual(report.items, [])

    def test_one_of_the_pair_fails(self):
        config.TLS_CERT = "/etc/passdesk/tls/cert.pem"
        report = doctor.Report()
        doctor.check_tls(report)
        self.assertTrue(any("set together" in m for m in report.messages(doctor.FAIL)))

    def test_missing_files_fail(self):
        self.tls_files()
        report = doctor.Report()
        doctor.check_tls(report)
        fails = report.messages(doctor.FAIL)
        self.assertEqual(len(fails), 2)
        self.assertTrue(all("No such file" in m for m in fails), fails)

    @unittest.skipUnless(OPENSSL, "openssl not installed")
    def test_good_pair_is_ok_and_loose_key_mode_warns(self):
        cert, key = mint(Path(self.tmp.name))
        config.TLS_CERT, config.TLS_KEY = str(cert), str(key)
        report = doctor.Report()
        doctor.check_tls(report)
        self.assertEqual(report.messages(doctor.FAIL), [])
        self.assertEqual(report.messages(doctor.WARN), [])
        self.assertTrue(any("TLS on" in m for m in report.messages(doctor.OK)))
        os.chmod(key, 0o644)
        report = doctor.Report()
        doctor.check_tls(report)
        self.assertTrue(any("readable by others" in m for m in report.messages(doctor.WARN)))

    @unittest.skipUnless(OPENSSL, "openssl not installed")
    def test_mismatched_pair_fails_without_key_material(self):
        a, b = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"
        a.mkdir()
        b.mkdir()
        cert, _ = mint(a)
        _, key = mint(b)
        config.TLS_CERT, config.TLS_KEY = str(cert), str(key)
        report = doctor.Report()
        doctor.check_tls(report)
        fails = report.messages(doctor.FAIL)
        self.assertTrue(any("do not load" in m for m in fails), fails)
        key_body = key.read_text().splitlines()[1]
        self.assertFalse(any(key_body in t for t in fails))

    def test_live_default_url_is_https_with_tls(self):
        seen = []

        def http(url, headers=None, data=None, timeout=10, cafile=None):
            seen.append((url, cafile))
            return 401, b""
        self.tls_files()
        config.BIND_HOST = "127.0.0.1"
        with mock.patch.object(doctor, "check_policy", return_value=None), \
                mock.patch.object(doctor, "check_state", return_value=set()):
            doctor.run(live=True, http=http)
        self.assertEqual(seen, [(f"https://127.0.0.1:{config.PORT}/mcp", config.TLS_CERT)])
        seen.clear()
        config.TLS_CERT = config.TLS_KEY = ""
        with mock.patch.object(doctor, "check_policy", return_value=None), \
                mock.patch.object(doctor, "check_state", return_value=set()):
            doctor.run(live=True, http=lambda url, headers=None, data=None, timeout=10: (seen.append(url), 401, b"")[1:])
        self.assertEqual(seen, [f"http://127.0.0.1:{config.PORT}/mcp"])


if __name__ == "__main__":
    unittest.main()
