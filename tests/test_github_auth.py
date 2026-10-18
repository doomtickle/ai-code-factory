"""The gateway's own GitHub token (standalone deployments): how git and the
API get it, and that it never reaches argv, disk, errors, the audit log or
an approval card. With no token, behaviour is exactly the proxy mode's."""

import base64
import http.server
import io
import json
import os
import subprocess
import threading
import urllib.request
from pathlib import Path
from unittest import mock

from passdesk.core import approvals, config, credentials, filters, policy, protocol, workspace
from passdesk.core.errors import Denied
from passdesk.tools import git

from .base import POLICY, Base, _FakeHTTPResponse, fake_http_error

# Matches none of filters.SECRET_PATTERNS, so only the live-value filter can catch it.
TOKEN = "TESTONLY_" + "q7" * 20
B64 = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
PR_ARGS = {"repo": "acme/widgets", "head": "factory/x", "title": "t", "body": "b"}


def leaks(text: str) -> bool:
    return TOKEN in text or B64 in text


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Stands in for github.com (git smart HTTP) and api.github.com: records
    every request's path and Authorization header, and answers per path."""

    def _answer(self):
        self.server.seen.append((self.headers.get("Host"), self.path, self.headers.get("Authorization")))
        if "/moved/" in self.path:
            # Redirect to the same server under another host name.
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/elsewhere/")
            self.end_headers()
            return
        body = f"denied; you sent {self.headers.get('Authorization')}".encode()
        self.send_response(401 if "/echo/" in self.path else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


class GitHubTokenTests(Base):
    def setUp(self):
        super().setUp()
        for name in ("CREDENTIAL_PROVIDERS", "DEPLOYMENT"):
            self.addCleanup(setattr, config, name, getattr(config, name))
        config.CREDENTIAL_PROVIDERS = "file,env-file"  # the standalone default
        config.DEPLOYMENT = "standalone"
        self.write_secret(credentials.SECRETS_DIR / "github", TOKEN + "\n")
        config.MIRRORS = Path(self.tmp.name) / "mirrors"
        # Make the trace switches that would print headers look switched on.
        env = mock.patch.dict(os.environ, {"GIT_TRACE": "1", "GIT_TRACE_CURL": "1", "GIT_TRACE_PACKET": "1",
                                           "GIT_TRACE2_EVENT": "1", "GIT_CURL_VERBOSE": "1",
                                           "GIT_TRACE_REDACT": "0", "GIT_CONFIG_PARAMETERS": "'x.y'='z'",
                                           "GIT_CONFIG_KEY_7": "http.extraHeader", "GIT_CONFIG_VALUE_7": "X: y"})
        env.start()
        self.addCleanup(env.stop)

    def server(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        srv.seen = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, srv.server_address[1]

    def mirror_at(self, url: str):
        """Point ensure_mirror at `url` and scope the token to it, as for github.com."""
        orig = git.ensure_mirror

        def ensure(repo):
            path = orig(repo)
            git.git("remote", "set-url", "origin", url, cwd=path)
            return path
        p = mock.patch.object(git, "ensure_mirror", ensure)
        p.start()
        self.addCleanup(p.stop)

    def capture_runs(self):
        """Record every git child's argv, environment and raw output."""
        runs = []
        real = workspace.run

        def spy(cmd, cwd=None, timeout=600, extra_env=None):
            cp = real(cmd, cwd=cwd, timeout=timeout, extra_env=extra_env)
            runs.append((cmd, workspace._env(extra_env), cp))
            return cp
        p = mock.patch.object(git, "run", spy)
        p.start()
        self.addCleanup(p.stop)
        return runs

    def scope(self, url: str):
        p = mock.patch.object(git, "GITHUB_GIT_URL", url)
        p.start()
        self.addCleanup(p.stop)

    # --- git -------------------------------------------------------------------------

    def test_auth_env_carries_a_scoped_header_and_nothing_unsafe(self):
        env = workspace._env(git.git_auth_env())
        count = int(env["GIT_CONFIG_COUNT"])
        pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
        self.assertEqual(pairs, {"http.https://github.com/.extraHeader": f"Authorization: Basic {B64}",
                                 "http.followRedirects": "false", "credential.helper": ""})
        for name in ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_TRACE_PACKET", "GIT_TRACE2_EVENT", "GIT_CURL_VERBOSE",
                     "GIT_TRACE_REDACT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_KEY_7", "GIT_CONFIG_VALUE_7"):
            self.assertNotIn(name, env)
        # The hardening that keeps system/global config and prompts out is untouched.
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_fetch_sends_the_header_only_through_the_environment(self):
        srv, port = self.server()
        url = f"http://127.0.0.1:{port}/"
        self.scope(url)
        self.mirror_at(url + "acme/widgets.git")
        runs = self.capture_runs()
        with self.assertRaises(RuntimeError) as cm:
            git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.assertEqual([a for _, _, a in srv.seen], [f"Basic {B64}"])
        self.assertFalse(leaks(str(cm.exception)))
        for cmd, env, cp in runs:
            self.assertFalse(any(leaks(arg) for arg in cmd), cmd)  # argv is what /proc/<pid>/cmdline shows
            self.assertFalse(leaks(cp.stderr + cp.stdout), "git traced the header despite the forced-off switches")
        network = [env for cmd, env, _ in runs if "fetch" in cmd]
        self.assertEqual(len(network), 1)
        self.assertIn(B64, network[0]["GIT_CONFIG_VALUE_0"])
        local = [env for cmd, env, _ in runs if "fetch" not in cmd]
        self.assertTrue(local)
        self.assertFalse(any("GIT_CONFIG_COUNT" in env for env in local), "only network git gets the token")

    def test_token_is_never_written_to_disk(self):
        srv, port = self.server()
        url = f"http://127.0.0.1:{port}/"
        self.scope(url)
        self.mirror_at(url + "acme/widgets.git")
        with self.assertRaises(RuntimeError):
            git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.assertTrue(srv.seen)
        for root in (config.MIRRORS, config.STATE, config.WORKSPACE):
            for path in root.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    self.assertNotIn(TOKEN.encode(), data, path)
                    self.assertNotIn(B64.encode(), data, path)

    def test_header_is_not_sent_to_another_host(self):
        srv, port = self.server()
        self.scope(f"http://127.0.0.1:{port}/")
        self.mirror_at(f"http://localhost:{port}/acme/widgets.git")  # same server, different host
        with self.assertRaises(RuntimeError):
            git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.assertTrue(srv.seen)
        self.assertEqual({a for _, _, a in srv.seen}, {None})

    def test_git_refuses_redirects(self):
        srv, port = self.server()
        url = f"http://127.0.0.1:{port}/"
        self.scope(url)
        self.mirror_at(url + "moved/widgets.git")
        with self.assertRaises(RuntimeError) as cm:
            git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.assertIn("302", str(cm.exception))
        self.assertEqual(len(srv.seen), 1, "git followed the redirect")
        self.assertFalse(leaks(str(cm.exception)))

    def test_git_error_output_is_redacted_in_errors_and_audit(self):
        def fake_run(cmd, cwd=None, timeout=600, extra_env=None):
            if "fetch" not in cmd:  # the mirror's local set-up succeeds
                return subprocess.CompletedProcess(cmd, 0, "", "")
            stderr = f"fatal: header Authorization: Basic {B64} token={TOKEN}\n"
            return subprocess.CompletedProcess(cmd, 128, "", stderr)
        self.write_secret(config.STATE / "roles.json", json.dumps({"developer": ["git_fetch"]}))
        with mock.patch.object(git, "run", side_effect=fake_run), \
                mock.patch.object(policy, "load_policy", return_value=POLICY), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            text, is_error = protocol.call_tool("git_fetch", {"repo": "acme/widgets", "branch": "main"}, "developer")
        self.assertTrue(is_error)
        self.assertIn("[REDACTED:live-credential]", text)
        self.assertFalse(leaks(text))
        self.assertFalse(leaks(err.getvalue()))
        self.assertFalse(leaks((config.STATE / "audit.jsonl").read_text()))

    def test_push_rejection_output_is_redacted(self):
        git.github_token()  # what the push's own git calls do first
        out = git.sanitize(f"remote: bad credentials {TOKEN}\n! [remote rejected] Basic {B64}")
        self.assertFalse(leaks(out))

    # --- github_create_pr ------------------------------------------------------------

    def test_pr_sends_a_bearer_token_and_never_follows_redirects(self):
        sent = []

        def fake_open(req, timeout):
            sent.append(req)
            return _FakeHTTPResponse(b'{"number": 7, "html_url": "https://github.com/acme/widgets/pull/7"}')
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake_open), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("followed redirects")):
            out = git.tool_github_create_pr(PR_ARGS, POLICY)
        self.assertIn("Opened PR #7", out)
        self.assertEqual(sent[0].get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(sent[0].full_url, "https://api.github.com/repos/acme/widgets/pulls")

    def test_pr_redirect_is_refused_and_nothing_reaches_the_target(self):
        srv, port = self.server()
        with mock.patch.object(git, "GITHUB_API", f"http://127.0.0.1:{port}/moved"):
            out = git.tool_github_create_pr(PR_ARGS, POLICY)
        self.assertIn("HTTP 302", out)
        self.assertEqual([(p, a) for _, p, a in srv.seen], [("/moved/repos/acme/widgets/pulls", f"Bearer {TOKEN}")])
        self.assertFalse(leaks(out))

    def test_pr_error_echoing_the_token_is_redacted(self):
        srv, port = self.server()
        with mock.patch.object(git, "GITHUB_API", f"http://127.0.0.1:{port}/echo"):
            out = git.tool_github_create_pr(PR_ARGS, POLICY)
        self.assertIn("HTTP 401", out)
        self.assertIn("[REDACTED:live-credential]", out)
        self.assertFalse(leaks(out))
        self.assertIn("Contents and Pull requests", out)
        self.assertNotIn("sbx secret set", out)

    def test_token_in_pr_text_is_refused_before_the_approval_card(self):
        git.github_token()
        cards = []
        approvals.set_hook(cards.append)
        self.addCleanup(approvals.set_hook, None)
        for field in ("title", "body"):
            with self.assertRaises(Denied) as cm:
                git.tool_github_create_pr({**PR_ARGS, field: f"see {TOKEN}"}, POLICY)
            self.assertIn("live-credential", str(cm.exception))
            self.assertFalse(leaks(str(cm.exception)))
        self.assertEqual(cards, [])

    def test_approval_cards_redact_the_token(self):
        git.github_token()
        cards = []
        approvals.set_hook(cards.append)
        self.addCleanup(approvals.set_hook, None)
        approvals.require({"tool": "git_push", "shortstat": f"{TOKEN} {B64}", "files": [TOKEN]})
        self.assertFalse(leaks(json.dumps(cards)))

    # --- the credential itself -------------------------------------------------------

    def test_a_value_that_is_not_a_github_token_is_refused_without_echoing_it(self):
        for bad in ("short_tok", "has-a-dash-" + "x" * 20, "x" * 256, "spaces in " + "x" * 20):
            self.write_secret(credentials.SECRETS_DIR / "github", bad)
            with self.assertRaises(Denied) as cm:
                git.git_auth_env()
            self.assertNotIn(bad, str(cm.exception))
            with mock.patch.object(git.NO_REDIRECT_OPENER, "open") as opened, \
                    self.assertRaises(Denied):
                git.tool_github_create_pr(PR_ARGS, POLICY)
            opened.assert_not_called()

    def test_rotated_token_is_read_fresh(self):
        self.assertIn(B64, git.git_auth_env()["GIT_CONFIG_VALUE_0"])
        new = "TESTONLY_" + "r8" * 20
        self.write_secret(credentials.SECRETS_DIR / "github", new)
        self.assertIn(base64.b64encode(f"x-access-token:{new}".encode()).decode(),
                      git.git_auth_env()["GIT_CONFIG_VALUE_0"])
        self.assertNotIn(TOKEN, filters.redact(TOKEN + new))  # the old value is still redacted


class ProxyModeTests(Base):
    """No token held by the gateway: exactly what happened before."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, config, "CREDENTIAL_PROVIDERS", config.CREDENTIAL_PROVIDERS)
        config.CREDENTIAL_PROVIDERS = "proxy,file,env-file"  # the sbx default

    def test_sbx_default_ignores_a_github_file(self):
        self.write_secret(credentials.SECRETS_DIR / "github", TOKEN)
        self.assertIsNone(git.git_auth_env())

    def test_git_environment_is_unchanged(self):
        with mock.patch.dict(os.environ, {"GIT_TRACE": "1"}):
            with mock.patch.object(git, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                git.git("fetch", "origin", cwd=Path(self.tmp.name), auth=True)
            self.assertIsNone(run.call_args.kwargs["extra_env"])
            self.assertEqual(workspace._env(None)["GIT_TRACE"], "1")  # not touched in proxy mode

    def test_pr_uses_the_plain_opener_without_authorization(self):
        sent = []

        def fake_urlopen(req, timeout):
            sent.append(req)
            return _FakeHTTPResponse(b'{"number": 1, "html_url": "u"}')
        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=AssertionError("changed opener")):
            git.tool_github_create_pr(PR_ARGS, POLICY)
        self.assertIsNone(sent[0].get_header("Authorization"))

    def test_pr_auth_failure_hint_is_unchanged(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_http_error(401, b"Bad credentials")):
            out = git.tool_github_create_pr(PR_ARGS, POLICY)
        self.assertIn("Hint: the GitHub credential is supplied by the deployment (with sbx: sbx secret set github",
                      out)


class LiveSecretFilterTests(Base):
    def test_remembered_values_are_redacted_and_found(self):
        filters.remember_secret("abcdefgh1234", "abcdefgh1234-and-more")
        self.assertEqual(filters.redact("x abcdefgh1234-and-more y abcdefgh1234"),
                         "x [REDACTED:live-credential] y [REDACTED:live-credential]")
        self.assertEqual(filters.find_secrets("..abcdefgh1234.."), ["live-credential"])
        self.assertNotIn("abcdefgh1234", filters.sanitize("abcdefgh1234" * 3, 10))

    def test_short_values_are_ignored(self):
        filters.remember_secret("abc")
        self.assertEqual(filters.redact("abc"), "abc")

    def test_only_the_newest_values_are_kept(self):
        values = [f"value-{i:04d}" for i in range(filters._LIVE_MAX + 5)]
        filters.remember_secret(*values)
        filters.remember_secret(values[-1])  # remembering again doesn't duplicate
        self.assertEqual(filters.redact(values[0]), values[0])
        self.assertEqual(filters.redact(values[-1]), "[REDACTED:live-credential]")
        self.assertEqual(len(filters._live_sorted), filters._LIVE_MAX)

