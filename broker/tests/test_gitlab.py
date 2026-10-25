"""GitLab (doomtickle/passdesk-ops#120): host-qualified repo names on the
instance [gitlab].url names, the `gitlab` file credential (scoped to that URL
prefix, never sent to GitHub, never GitHub's sent to it), gitlab_create_mr,
web_fetch's refusal of the host, and the doctor's checks."""

import base64
import http.server
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from passdesk import doctor
from passdesk.core import approvals, config, credentials
from passdesk.core.errors import Denied
from passdesk.tools import git, web

from .base import POLICY, Base, _FakeHTTPResponse

# Matches none of filters.SECRET_PATTERNS, so only the live-value filter can catch them.
GL_TOKEN = "TESTONLY-gl." + "k3" * 12
GH_TOKEN = "TESTONLY_" + "q7" * 20
GL_B64 = base64.b64encode(f"oauth2:{GL_TOKEN}".encode()).decode()
GH_B64 = base64.b64encode(f"x-access-token:{GH_TOKEN}".encode()).decode()
GL = "gitlab.acme.net"
PROJECT = f"{GL}/platform/api"
GL_POLICY = {**POLICY, "git": {**POLICY["git"], "repos": ["acme/widgets", PROJECT, f"{GL}/a/b/c/svc"]},
             "gitlab": {"url": f"https://{GL}/"}}
MR_ARGS = {"repo": PROJECT, "head": "factory/x", "title": "Add the thing", "body": "b"}


def leaks(text: str, *tokens: str) -> bool:
    return any(t in text for t in (tokens or (GL_TOKEN, GL_B64, GH_TOKEN, GH_B64)))


class GitLabBase(Base):
    def setUp(self):
        super().setUp()
        for name in ("CREDENTIAL_PROVIDERS", "MIRRORS"):
            self.addCleanup(setattr, config, name, getattr(config, name))
        config.CREDENTIAL_PROVIDERS = "proxy,file,env-file"  # the sbx default: GitHub is proxy-injected
        config.MIRRORS = Path(self.tmp.name) / "mirrors"
        self.write_secret(credentials.secrets_dir() / "gitlab", GL_TOKEN + "\n")

    def cards(self):
        seen = []
        approvals.set_hook(seen.append)
        self.addCleanup(approvals.set_hook, None)
        return seen


class NamesTests(GitLabBase):
    def test_gitlab_base(self):
        self.assertIsNone(git.gitlab_base(POLICY))
        self.assertIsNone(git.gitlab_base({**POLICY, "gitlab": {"url": ""}}))
        for url, want in ((f"https://{GL}/", f"https://{GL}/"), (f"https://{GL.upper()}/", f"https://{GL}/"),
                          (f"https://{GL}:8443/", f"https://{GL}:8443/"), (f"https://{GL}:443/", f"https://{GL}/"),
                          ("https://acme.net/gitlab/", "https://acme.net/gitlab/")):
            self.assertEqual(git.gitlab_base({"gitlab": {"url": url}}), want, url)
        # An IPv4 address is a host like any other (its certificate must still verify).
        self.assertEqual(git.gitlab_base({"gitlab": {"url": "https://10.0.0.5/"}}), "https://10.0.0.5/")
        for bad in (f"http://{GL}/", f"https://{GL}", f"https://me:pw@{GL}/", f"https://{GL}/?x=1/",
                    f"https://{GL}/#f/", "https:///", "https://github.com/", "https://api.github.com/",
                    f"https://{GL}/ x/", 7, ["https://x/"], "https://[::1/",
                    f"https://{GL}./", "https://.acme.net/", "https://a..b/", "https://-a.net/", "https://a-.net/",
                    f"https://{GL}:0/", f"https://{GL}:70000/", "https://[::1]/", "https://a_b.net/"):
            with self.assertRaises(Denied, msg=bad):
                git.gitlab_base({"gitlab": {"url": bad}})

    def test_repo_names(self):
        self.assertEqual(git.check_repo(PROJECT, GL_POLICY), PROJECT)
        self.assertEqual(git.check_repo(f"{GL}/a/b/c/svc", GL_POLICY), f"{GL}/a/b/c/svc")
        self.assertEqual(git.check_repo(PROJECT.upper(), GL_POLICY), PROJECT.upper())  # as GitHub: case-insensitive
        self.assertEqual(git.check_repo("acme/widgets", GL_POLICY), "acme/widgets")
        r = git.remote_of(f"{GL}/a/b/c/svc", GL_POLICY)
        self.assertEqual((r.forge, r.path, r.git_url), ("gitlab", "a/b/c/svc", f"https://{GL}/a/b/c/svc.git"))
        self.assertEqual(git.remote_of("acme/widgets", GL_POLICY).git_url, "https://github.com/acme/widgets.git")
        for bad in (f"{GL}/proj", f"{GL}/g/../p", f"{GL}/g/p.git", f"{GL}/g/.p", f"{GL}/g/-p", f"{GL}//g/p",
                    f"{GL}/g/p/", f"{GL}/g p/x", f"{GL}/g/p?x"):
            with self.assertRaises(Denied, msg=bad):
                git.check_repo(bad, {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": [bad]}})

    def test_gitlab_names_need_the_configured_instance(self):
        with self.assertRaises(Denied) as cm:
            git.check_repo(PROJECT, {**GL_POLICY, "gitlab": {"url": ""}})
        self.assertIn("[gitlab].url is not set", str(cm.exception))
        other = "gitlab.evil.example/platform/api"
        with self.assertRaises(Denied) as cm:
            git.check_repo(other, {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": [other]}})
        self.assertIn("not on the configured GitLab", str(cm.exception))
        with self.assertRaises(Denied):  # listed or not, a port must match too
            port = f"{GL}:8443/platform/api"
            git.check_repo(port, {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": [port]}})
        with self.assertRaises(Denied) as cm:
            git.check_repo(f"{GL}/platform/other", GL_POLICY)
        self.assertIn("not in the broker allowlist", str(cm.exception))

    def test_bundles_never_collide(self):
        pol = {**GL_POLICY, "git": {"repos": [f"{GL}/a__b/c", f"{GL}/a/b__c"]}}
        stems = {git.bundle_stem(r, git.remote_of(r, pol)) for r in pol["git"]["repos"]}
        self.assertEqual(len(stems), 2)
        self.assertEqual(git.bundle_stem("acme/widgets", git.remote_of("acme/widgets", POLICY)), "acme__widgets")

    def test_mirrors_never_collide(self):
        names = {git.mirror_name(git.remote_of(r, {**GL_POLICY, "git": {"repos": [r]}}))
                 for r in (f"{GL}/g/a__b", f"{GL}/g__a/b", f"{GL}/g/a/b", f"{GL}/G/A/B")}
        self.assertEqual(len(names), 3)  # G/A/B is g/a/b: GitLab paths are case-insensitive
        self.assertEqual(git.mirror_name(git.remote_of("acme/widgets", POLICY)), "acme__widgets.git")
        self.assertTrue(all(n.startswith("gitlab__gitlab.acme.net__") for n in names))


class CredentialTests(GitLabBase):
    def pairs(self, remote):
        env = git.git_auth_env(remote)
        return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}

    def test_auth_env_is_scoped_to_the_gitlab_prefix(self):
        remote = git.remote_of(PROJECT, GL_POLICY)
        self.assertEqual(self.pairs(remote), {f"http.https://{GL}/.extraHeader": f"Authorization: Basic {GL_B64}",
                                              "http.followRedirects": "false", "credential.helper": ""})

    def test_neither_forge_gets_the_others_token(self):
        self.write_secret(credentials.secrets_dir() / "github", GH_TOKEN)
        config.CREDENTIAL_PROVIDERS = "file,env-file"  # standalone: the gateway holds both
        gl = json.dumps(self.pairs(git.remote_of(PROJECT, GL_POLICY)))
        gh = json.dumps(self.pairs(git.remote_of("acme/widgets", GL_POLICY)))
        self.assertTrue(leaks(gl, GL_B64) and not leaks(gl, GH_TOKEN, GH_B64))
        self.assertTrue(leaks(gh, GH_B64) and not leaks(gh, GL_TOKEN, GL_B64))
        self.assertNotIn(GL, gh)

    def test_no_gitlab_token_means_no_change(self):
        (credentials.secrets_dir() / "gitlab").unlink()
        self.assertIsNone(git.git_auth_env(git.remote_of(PROJECT, GL_POLICY)))

    def test_a_value_that_is_not_a_token_is_refused_without_echoing_it(self):
        for bad in ("short", "x" * 256, "has spaces in it " + "x" * 20, "slash/" + "x" * 20):
            self.write_secret(credentials.secrets_dir() / "gitlab", bad)
            with self.assertRaises(Denied) as cm:
                git.gitlab_token()
            self.assertNotIn(bad, str(cm.exception))

    def test_token_is_redacted_once_read(self):
        git.gitlab_token()
        out = git.sanitize(f"remote: HTTP Basic: Access denied {GL_TOKEN} {GL_B64}")
        self.assertFalse(leaks(out))


class _Recorder(http.server.BaseHTTPRequestHandler):
    def _answer(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if "/moved/" in self.path:
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/elsewhere/")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


class GitOverHttpTests(GitLabBase):
    """git itself against a stand-in GitLab: the header goes only there."""

    def setUp(self):
        super().setUp()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.srv.seen = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        port = self.srv.server_address[1]
        self.host = f"127.0.0.1:{port}"
        # gitlab_base insists on https; the stand-in speaks http.
        p = mock.patch.object(git, "gitlab_base", lambda policy: f"http://{self.host}/")
        p.start()
        self.addCleanup(p.stop)
        self.policy = {**POLICY, "git": {**POLICY["git"], "repos": [f"{self.host}/platform/api",
                                                                   f"{self.host}/moved/api"]}}
        self.write_secret(credentials.secrets_dir() / "github", GH_TOKEN)
        config.CREDENTIAL_PROVIDERS = "file,env-file"

    def test_fetch_sends_the_gitlab_header_and_only_it(self):
        with self.assertRaises(RuntimeError) as cm:
            git.tool_git_fetch({"repo": f"{self.host}/platform/api", "branch": "main"}, self.policy)
        self.assertEqual(self.srv.seen[0][0].split("?")[0], "/platform/api.git/info/refs")
        self.assertEqual({a for _, a in self.srv.seen}, {f"Basic {GL_B64}"})
        self.assertFalse(leaks(str(cm.exception)))

    def test_git_refuses_redirects(self):
        with self.assertRaises(RuntimeError) as cm:
            git.tool_git_fetch({"repo": f"{self.host}/moved/api", "branch": "main"}, self.policy)
        self.assertIn("302", str(cm.exception))
        self.assertEqual(len(self.srv.seen), 1, "git followed the redirect")

    def test_token_is_never_written_to_disk(self):
        with self.assertRaises(RuntimeError):
            git.tool_git_fetch({"repo": f"{self.host}/platform/api", "branch": "main"}, self.policy)
        for root in (config.MIRRORS, config.STATE, config.WORKSPACE):
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(GL_TOKEN.encode(), path.read_bytes(), path)
                    self.assertNotIn(GL_B64.encode(), path.read_bytes(), path)


class MergeRequestTests(GitLabBase):
    def open_mr(self, args=None, answer=b'{"iid": 12, "web_url": "https://gitlab.acme.net/platform/api/-/merge_requests/12"}'):
        sent = []

        def fake_open(req, timeout):
            sent.append(req)
            return _FakeHTTPResponse(answer)
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake_open), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("followed redirects")):
            out = git.tool_gitlab_create_mr(args or MR_ARGS, GL_POLICY)
        return out, sent

    def test_opens_a_draft_mr_with_a_bearer_token(self):
        cards = self.cards()
        out, sent = self.open_mr()
        self.assertEqual(out, "Opened MR !12: https://gitlab.acme.net/platform/api/-/merge_requests/12")
        req = sent[0]
        self.assertEqual(req.full_url, f"https://{GL}/api/v4/projects/platform%2Fapi/merge_requests")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {GL_TOKEN}")
        self.assertEqual(json.loads(req.data), {"source_branch": "factory/x", "target_branch": "main",
                                                "title": "Draft: Add the thing", "description": "b"})
        self.assertEqual([{k: v for k, v in c.items() if k != "principal"} for c in cards],
                         [{"tool": "gitlab_create_mr", "repo": PROJECT, "head": "factory/x", "base": "main",
                           "title": "Draft: Add the thing", "body": "b",
                           "body_sha256": __import__("hashlib").sha256(b"b").hexdigest(), "body_chars": 1,
                           "draft": True}])

    def test_a_value_a_plugin_hook_returns_adds_nothing_to_the_result(self):
        approvals.set_hook(lambda card: {"id": "0123456789abcdef", "approver": "daron"})
        self.addCleanup(approvals.set_hook, None)
        out, _ = self.open_mr()
        self.assertEqual(out, "Opened MR !12: https://gitlab.acme.net/platform/api/-/merge_requests/12")

    def test_bodies_differing_past_the_excerpt_get_different_cards(self):
        cards = self.cards()
        self.open_mr({**MR_ARGS, "body": "x" * 3000})
        self.open_mr({**MR_ARGS, "body": "x" * 2999 + "y"})
        self.assertEqual(cards[0]["body"], cards[1]["body"])
        self.assertNotEqual(cards[0]["body_sha256"], cards[1]["body_sha256"])
        self.assertEqual(cards[0]["body_chars"], 3000)

    def test_draft_prefix_once_and_not_for_ready_mrs(self):
        _, sent = self.open_mr({**MR_ARGS, "title": "Draft: already"})
        self.assertEqual(json.loads(sent[0].data)["title"], "Draft: already")
        _, sent = self.open_mr({**MR_ARGS, "title": "Draft - not a GitLab marker"})
        self.assertEqual(json.loads(sent[0].data)["title"], "Draft: Draft - not a GitLab marker")
        _, sent = self.open_mr({**MR_ARGS, "draft": False, "base": "develop"})
        body = json.loads(sent[0].data)
        self.assertEqual((body["title"], body["target_branch"]), ("Add the thing", "develop"))

    def test_nested_groups_are_one_encoded_path(self):
        _, sent = self.open_mr({**MR_ARGS, "repo": f"{GL}/a/b/c/svc"})
        self.assertEqual(sent[0].full_url, f"https://{GL}/api/v4/projects/a%2Fb%2Fc%2Fsvc/merge_requests")

    def test_refusals_before_the_card(self):
        cards = self.cards()
        cases = [({**MR_ARGS, "repo": "acme/widgets"}, "github_create_pr"),
                 ({**MR_ARGS, "head": "main"}, "protected"),
                 ({**MR_ARGS, "head": "feature"}, "factory/"),
                 ({**MR_ARGS, "title": "two\nlines"}, "single line"),
                 ({**MR_ARGS, "title": "   "}, "single line"),
                 ({**MR_ARGS, "title": "x" * 250}, "over 255"),
                 ({**MR_ARGS, "title": "ghp_" + "a" * 36}, "secret"),
                 ({**MR_ARGS, "body": "x" * 65537}, "at most 65536"),
                 ({**MR_ARGS, "body": "key AKIA" + "C" * 16}, "secret")]
        for args, fragment in cases:
            with mock.patch.object(git.NO_REDIRECT_OPENER, "open") as opened, self.assertRaises(Denied) as cm:
                git.tool_gitlab_create_mr(args, GL_POLICY)
            self.assertIn(fragment, str(cm.exception), args)
            opened.assert_not_called()
        git.gitlab_token()
        with self.assertRaises(Denied) as cm:
            git.tool_gitlab_create_mr({**MR_ARGS, "body": f"see {GL_TOKEN}"}, GL_POLICY)
        self.assertFalse(leaks(str(cm.exception)))
        self.assertEqual(cards, [])

    def test_no_token_is_refused_before_anyone_is_asked(self):
        (credentials.secrets_dir() / "gitlab").unlink()
        cards = self.cards()
        with self.assertRaises(Denied) as cm:
            git.tool_gitlab_create_mr(MR_ARGS, GL_POLICY)
        self.assertIn("no gitlab credential", str(cm.exception))
        self.assertEqual(cards, [])

    def test_errors_are_redacted_and_hinted(self):
        git.gitlab_token()

        def fail(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "no", None,
                                         __import__("io").BytesIO(f"bad token {GL_TOKEN}".encode()))
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fail):
            out = git.tool_gitlab_create_mr(MR_ARGS, GL_POLICY)
        self.assertIn("GitLab API returned HTTP 401", out)
        self.assertIn("Developer role", out)
        self.assertFalse(leaks(out))

    def test_unexpected_answer(self):
        with self.assertRaises(Denied):
            self.open_mr(answer=b'{"message": "?"}')

    def test_github_pr_tool_refuses_gitlab_projects(self):
        with self.assertRaises(Denied) as cm:
            git.tool_github_create_pr({**MR_ARGS}, GL_POLICY)
        self.assertIn("gitlab_create_mr", str(cm.exception))


class WebFetchTests(GitLabBase):
    def test_the_gitlab_host_is_credentialed(self):
        self.assertEqual(web.credentialed_host_tool(GL, GL_POLICY), "git_fetch, git_push or gitlab_create_mr")
        self.assertEqual(web.credentialed_host_tool(GL.upper(), GL_POLICY), "git_fetch, git_push or gitlab_create_mr")
        self.assertIsNone(web.credentialed_host_tool(GL, POLICY))
        # A trailing dot on either side changes nothing.
        dotted = {**GL_POLICY, "gitlab": {"url": f"https://{GL}./"}}
        self.assertIsNotNone(web.credentialed_host_tool(GL, dotted))
        self.assertIsNotNone(web.credentialed_host_tool(GL + ".", GL_POLICY))
        # Even malformed, the URL's host stays blocked.
        self.assertIsNotNone(web.credentialed_host_tool(GL, {"gitlab": {"url": f"http://{GL}"}}))
        pol = {**GL_POLICY, "web": {**GL_POLICY["web"], "allow_hosts": [GL]}}
        with self.assertRaises(Denied) as cm:
            web.tool_web_fetch({"url": f"https://{GL}/platform/api"}, pol)
        self.assertIn("gitlab_create_mr", str(cm.exception))


class DoctorTests(GitLabBase):
    def test_policy(self):
        report = doctor.Report()
        doctor._check_policy_shape(report, GL_POLICY)
        self.assertEqual(report.messages(doctor.WARN), [])

        def check(pol):
            path = Path(self.tmp.name) / "policy.toml"
            lines = []
            for table, values in pol.items():
                lines.append(f"[{table}]")
                lines += [f"{k} = {json.dumps(v)}" for k, v in values.items()]
            path.write_text("\n".join(lines) + "\n")
            report = doctor.Report()
            doctor.check_policy(report, path)
            return report
        self.assertEqual(check(GL_POLICY).messages(doctor.FAIL), [])
        bad = check({**GL_POLICY, "gitlab": {"url": f"http://{GL}/"}})
        self.assertTrue(any("[gitlab].url must be https" in m for m in bad.messages(doctor.FAIL)))
        unset = check({**GL_POLICY, "gitlab": {"url": ""}})
        self.assertTrue(any(PROJECT in m and "not set" in m for m in unset.messages(doctor.FAIL)))
        none_on_it = check({**GL_POLICY, "git": {**GL_POLICY["git"], "repos": ["acme/widgets"]}})
        self.assertTrue(any("no [git].repos entry is on it" in m for m in none_on_it.messages(doctor.INFO)))

    def test_egress_hosts(self):
        self.assertIn(GL, doctor.egress_hosts(GL_POLICY))
        self.assertIn(GL, doctor.egress_hosts({**GL_POLICY, "gitlab": {"url": f"https://{GL}:8443/"}}))
        self.assertIn(GL, doctor.egress_hosts(GL_POLICY, {"gitlab_create_mr"}))
        self.assertNotIn(GL, doctor.egress_hosts(GL_POLICY, {"web_fetch"}))
        self.assertNotIn(GL, doctor.egress_hosts(POLICY))

    def test_live(self):
        seen = []
        answers = {}

        def fake_http(url, headers=None, data=None, timeout=10):
            if data is not None:
                return 401, b""
            seen.append((url, (headers or {}).get("Authorization")))
            return answers.get(url, (200, json.dumps({"permissions": {"push": True}}).encode()))
        api = f"https://{GL}/api/v4/projects/platform%2Fapi"
        nested = f"https://{GL}/api/v4/projects/a%2Fb%2Fc%2Fsvc"
        cases = [((200, json.dumps({"permissions": {"project_access": {"access_level": 40}}}).encode()),
                  doctor.OK, "can read and push"),
                 ((200, json.dumps({"permissions": {"project_access": None,
                                                    "group_access": {"access_level": 30}}}).encode()),
                  doctor.OK, "can read and push"),
                 ((200, json.dumps({"permissions": {"project_access": {"access_level": 20}}}).encode()),
                  doctor.FAIL, "but not push"),
                 ((200, b"{}"), doctor.WARN, "could not be confirmed"),
                 ((200, b'{"permissions": "odd"}'), doctor.WARN, "could not be confirmed"),
                 ((200, b'{"permissions": {"project_access": {"access_level": "40"}}}'), doctor.WARN,
                  "could not be confirmed"),
                 ((404, b""), doctor.FAIL, "404"),
                 ((401, b""), doctor.FAIL, "rejects the token")]
        for answer, level, fragment in cases:
            answers[api] = answer
            answers[nested] = answer
            report = doctor.Report()
            doctor.check_live(report, GL_POLICY, "http://127.0.0.1:1/mcp", http=fake_http)
            self.assertTrue(any(fragment in m for m in report.messages(level)), (answer, report.items))
            self.assertFalse(leaks(json.dumps(report.to_dict())))
        gl_calls = [(u, a) for u, a in seen if GL in u]
        self.assertTrue(gl_calls)
        self.assertEqual({a for _, a in gl_calls}, {f"Bearer {GL_TOKEN}"})
        self.assertFalse(any(a == f"Bearer {GL_TOKEN}" for u, a in seen if GL not in u))

    def test_live_without_a_token(self):
        (credentials.secrets_dir() / "gitlab").unlink()
        report = doctor.Report()
        doctor.check_live(report, GL_POLICY, "http://127.0.0.1:1/mcp",
                          http=lambda url, headers=None, data=None, timeout=10: (401, b"") if data else (200, b"{}"))
        self.assertTrue(any("no usable gitlab credential" in m for m in report.messages(doctor.WARN)))

    def test_credential_is_checked_only_when_gitlab_is_configured(self):
        granted = {"git_fetch", "git_push", "gitlab_create_mr"}
        (credentials.secrets_dir() / "gitlab").unlink()
        report = doctor.Report()
        doctor.check_secrets(report, GL_POLICY, granted)
        self.assertTrue(any("no gitlab credential" in m for m in report.messages(doctor.WARN)))
        report = doctor.Report()
        doctor.check_secrets(report, POLICY, granted)
        self.assertFalse(any("gitlab" in m for m in report.messages(doctor.WARN)))
        self.write_secret(credentials.secrets_dir() / "gitlab", "not a token!")
        report = doctor.Report()
        doctor.check_secrets(report, GL_POLICY, granted)
        self.assertTrue(any("not a GitLab token" in m for m in report.messages(doctor.FAIL)))


class PushTests(GitLabBase):
    """git_push to a GitLab project: both network git calls (the base fetch and
    the push itself) carry the GitLab header and never GitHub's, even when the
    gateway holds both tokens."""

    def sh(self, *cmd, cwd):
        from passdesk.core import workspace
        cp = workspace.run(list(cmd), cwd=cwd, extra_env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_push_carries_only_the_gitlab_header(self):
        from passdesk.core import workspace
        self.write_secret(credentials.secrets_dir() / "github", GH_TOKEN)
        config.CREDENTIAL_PROVIDERS = "file,env-file"  # standalone: the gateway holds both
        root = Path(self.tmp.name)
        origin = root / "origin.git"
        self.sh("git", "init", "--bare", "-q", "-b", "main", str(origin), cwd=root)
        seed = root / "seed"
        self.sh("git", "clone", "-q", str(origin), str(seed), cwd=root)
        (seed / "README").write_text("hello\n")
        self.sh("git", "add", ".", cwd=seed)
        self.sh("git", "commit", "-q", "-m", "init", cwd=seed)
        self.sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
        self.sh("git", "checkout", "-q", "-b", "factory/feature", cwd=seed)
        (seed / "feature.txt").write_text("new\n")
        self.sh("git", "add", "feature.txt", cwd=seed)
        self.sh("git", "commit", "-q", "-m", "add feature", cwd=seed)
        outgoing = config.WORKSPACE / ".factory/outgoing"
        outgoing.mkdir(parents=True, exist_ok=True)
        self.sh("git", "bundle", "create", "-q", str(outgoing / "f.bundle"), "main..factory/feature", cwd=seed)

        orig = git.ensure_mirror

        def ensure(repo, remote=None):
            self.assertEqual(remote.forge, "gitlab")
            path = orig(repo, remote)
            git.git("remote", "set-url", "origin", str(origin), cwd=path)  # a local stand-in for the project
            return path
        runs = []
        real = workspace.run

        def spy(cmd, cwd=None, timeout=600, extra_env=None):
            runs.append((cmd, extra_env))
            return real(cmd, cwd=cwd, timeout=timeout, extra_env=extra_env)
        with mock.patch.object(git, "ensure_mirror", ensure), mock.patch.object(git, "run", spy):
            out = git.tool_git_push({"repo": PROJECT, "branch": "factory/feature", "bundle": "f.bundle"}, GL_POLICY)
        self.assertIn(f"Pushed 1 commit(s) to {PROJECT}:factory/feature", out)
        authed = [(cmd, env) for cmd, env in runs if env and "GIT_CONFIG_COUNT" in env]
        self.assertEqual([next(a for a in cmd if a in ("fetch", "push")) for cmd, _ in authed], ["fetch", "push"])
        for cmd, env in authed:
            text = json.dumps(env)
            self.assertIn(f"http.https://{GL}/.extraHeader", text)
            self.assertIn(GL_B64, text)
            self.assertFalse(leaks(text, GH_TOKEN, GH_B64), cmd)
            self.assertNotIn("github.com", text)
        self.sh("git", "rev-parse", "--verify", "refs/heads/factory/feature", cwd=origin)  # it arrived


class StricterNamesTests(GitLabBase):
    def test_odd_url_paths_are_refused(self):
        for bad in (f"https://{GL}/../", f"https://{GL}//", f"https://{GL}/x/../", f"https://{GL}/./",
                    f"https://{GL}/x@y/", f"https://{GL}/%2e%2e/", f"https://{GL}/a\\b/"):
            with self.assertRaises(Denied, msg=bad):
                git.gitlab_base({"gitlab": {"url": bad}})
        self.assertEqual(git.gitlab_base({"gitlab": {"url": f"https://{GL}/a/b/"}}), f"https://{GL}/a/b/")

    def test_twenty_subgroups_fit(self):
        deepest = GL + "/" + "/".join(f"g{i}" for i in range(21)) + "/project"  # top group + 20 subgroups
        too_deep = GL + "/" + "/".join(f"g{i}" for i in range(22)) + "/project"
        self.assertEqual(git.check_repo(deepest, {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": [deepest]}}),
                         deepest)
        with self.assertRaises(Denied):
            git.check_repo(too_deep, {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": [too_deep]}})


class GitLabOnlyDoctorTests(GitLabBase):
    def test_no_github_hosts_or_token_needed_when_every_repo_is_on_gitlab(self):
        only = {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": [PROJECT]}}
        hosts = doctor.egress_hosts(only)
        self.assertIn(GL, hosts)
        self.assertNotIn("github.com", hosts)
        self.assertIn("github.com", doctor.egress_hosts(GL_POLICY))  # a GitHub repo listed: still needed
        self.assertIn("api.github.com", doctor.egress_hosts(only))   # github_create_pr is a tool of its own
        report = doctor.Report()
        doctor.check_standalone(report, {"git_fetch", "git_push", "gitlab_create_mr"}, only)
        self.assertFalse(any("no github credential" in m for m in report.messages(doctor.WARN)), report.items)
        report = doctor.Report()
        doctor.check_standalone(report, {"git_fetch", "git_push"}, GL_POLICY)
        self.assertTrue(any("no github credential" in m for m in report.messages(doctor.WARN)), report.items)
