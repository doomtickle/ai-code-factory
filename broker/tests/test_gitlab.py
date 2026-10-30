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
GROUP = f"{GL}/platform/*"
GROUP_POLICY = {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": [GROUP]}}
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

    def push(self, policy, repo):
        """git_push of a one-commit bundle to repo, with a local bare repo standing in for the project.
        Returns (the tool's answer, the git runs that carried a token, the stand-in)."""
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
            out = git.tool_git_push({"repo": repo, "branch": "factory/feature", "bundle": "f.bundle"}, policy)
        authed = [(cmd, env) for cmd, env in runs if env and "GIT_CONFIG_COUNT" in env]
        return out, authed, origin

    def test_push_carries_only_the_gitlab_header(self):
        out, authed, origin = self.push(GL_POLICY, PROJECT)
        self.assertIn(f"Pushed 1 commit(s) to {PROJECT}:factory/feature", out)
        self.assertEqual([next(a for a in cmd if a in ("fetch", "push")) for cmd, _ in authed], ["fetch", "push"])
        for cmd, env in authed:
            text = json.dumps(env)
            self.assertIn(f"http.https://{GL}/.extraHeader", text)
            self.assertIn(GL_B64, text)
            self.assertFalse(leaks(text, GH_TOKEN, GH_B64), cmd)
            self.assertNotIn("github.com", text)
        self.sh("git", "rev-parse", "--verify", "refs/heads/factory/feature", cwd=origin)  # it arrived

    def test_push_through_a_group_entry(self):
        repo = f"{GL}/Platform/Sub/Svc"
        out, authed, origin = self.push(GROUP_POLICY, repo)
        self.assertIn(f"Pushed 1 commit(s) to {repo}:factory/feature", out)
        self.assertEqual(len(authed), 2)
        self.sh("git", "rev-parse", "--verify", "refs/heads/factory/feature", cwd=origin)

    def test_push_outside_a_group_entry_is_refused_before_anything_runs(self):
        from passdesk.core import workspace
        for repo in (f"{GL}/platform-evil/svc", f"{GL}/other/svc", "evil.example/platform/svc"):
            with mock.patch.object(git, "ensure_mirror") as mirror, mock.patch.object(workspace, "run") as ran, \
                    self.assertRaises(Denied, msg=repo):
                git.tool_git_push({"repo": repo, "branch": "factory/feature", "bundle": "f.bundle"}, GROUP_POLICY)
            mirror.assert_not_called()
            ran.assert_not_called()


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


def shared_group_entries():
    """The table broker/tests/repos_entries.json, also run through the host's validator
    (factory/test_policy.py): both sides must agree on every row."""
    return json.loads((Path(__file__).parent / "repos_entries.json").read_text())["entries"]


class GroupEntryTests(GitLabBase):
    """[git].repos group entries ("<host>/<group>/*", owner decision 2026-10-30): one matcher,
    boundaries on whole path components, malformed entries refused (and matching nothing)."""

    def pol(self, *entries):
        return {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": list(entries)}}

    def test_shared_entry_table(self):
        rows = shared_group_entries()
        self.assertGreater(len(rows), 60)
        self.assertTrue(any(r["valid"] for r in rows) and any(not r["valid"] for r in rows))
        for row in rows:
            entry = row["entry"]
            self.assertIn("*", entry)
            if row["valid"]:
                self.assertEqual(git.check_repos_entry(entry), entry, entry)
                self.assertTrue(git.is_group_entry(entry), entry)
            else:
                with self.assertRaises(Denied, msg=repr(entry)):
                    git.check_repos_entry(entry)
                # an entry that fails the check matches nothing, even a repo it looks like it covers
                prefix = entry[:-1] if entry.endswith("*") else entry
                self.assertFalse(git.repo_allowed(prefix + "project", [entry]), repr(entry))

    def test_matching(self):
        for repo in (f"{GL}/platform/api", f"{GL}/platform/a/b/c/svc", f"{GL}/PLATFORM/API", f"{GL.upper()}/Platform/x",
                     f"{GL}/platform/x.y", f"{GL}/platform/_x"):
            self.assertEqual(git.check_repo(repo, GROUP_POLICY), repo)
            self.assertTrue(git.repo_allowed(repo, [GROUP]), repo)
        # A subgroup entry covers its own subtree only.
        sub = self.pol(f"{GL}/Platform/Tools/*")
        self.assertEqual(git.check_repo(f"{GL}/platform/tools/cli", sub), f"{GL}/platform/tools/cli")
        self.assertEqual(git.check_repo(f"{GL}/platform/tools/deep/er/cli", sub), f"{GL}/platform/tools/deep/er/cli")
        for repo in (f"{GL}/platform/api", f"{GL}/platform/tools-evil/cli", f"{GL}/platform/tool/cli"):
            with self.assertRaises(Denied, msg=repo):
                git.check_repo(repo, sub)

    def test_boundaries(self):
        for repo in (f"{GL}/platform-evil/x", f"{GL}/platformx/y", f"{GL}/platform_/y", f"{GL}/platform.evil/y",
                     f"{GL}/other/platform/x", f"{GL}/other/svc", f"{GL}/x/platform/y"):
            with self.assertRaises(Denied, msg=repo) as cm:
                git.check_repo(repo, GROUP_POLICY)
            self.assertIn("not in the broker allowlist", str(cm.exception))
            self.assertFalse(git.repo_allowed(repo, [GROUP]), repo)
        # The group itself is not a project under it (the name checks refuse a one-segment path
        # first; repo_allowed refuses it on its own too).
        for repo in (f"{GL}/platform", f"{GL}/platform/"):
            self.assertFalse(git.repo_allowed(repo, [GROUP]), repo)
            with self.assertRaises(Denied, msg=repo):
                git.check_repo(repo, GROUP_POLICY)

    def test_another_instance_or_port_is_refused_by_remote_of(self):
        for repo in ("gitlab.evil.example/platform/x", f"{GL}:8443/platform/x"):
            with self.assertRaises(Denied, msg=repo):  # allowed by name only if the host is the entry's
                git.check_repo(repo, GROUP_POLICY)
        # An entry on another host: the name matches it, remote_of refuses the host.
        other = self.pol("gitlab.evil.example/platform/*")
        with self.assertRaises(Denied) as cm:
            git.check_repo("gitlab.evil.example/platform/x", other)
        self.assertIn("not on the configured GitLab", str(cm.exception))
        with self.assertRaises(Denied) as cm:
            git.check_repo(PROJECT, {**GROUP_POLICY, "gitlab": {"url": ""}})
        self.assertIn("[gitlab].url is not set", str(cm.exception))
        self.assertEqual(git.group_remote(GROUP, GROUP_POLICY).path, "platform")
        with self.assertRaises(Denied):
            git.group_remote("gitlab.evil.example/platform/*", GROUP_POLICY)
        with self.assertRaises(Denied):
            git.group_remote(f"{GL}/platform/x", GROUP_POLICY)

    def test_the_name_is_checked_before_matching(self):
        for bad in (f"{GL}/platform/x\n", f"{GL}/platform/x\r", f"{GL}/platform/../other/x", f"{GL}/platform/.x",
                    f"{GL}/platform/-x", f"{GL}/platform/x.git", f"{GL}/platform/x.atom", f"{GL}/platform//x",
                    f"{GL}/platform/x/", f"{GL}/platform/x y", f"{GL}/platform/x?y=1", f"{GL}/platform/*",
                    f"{GL}/platform/%2e%2e/x", f"{GL}/platform/\u0430dmin/x", f"{GL}/platform/x\x00",
                    f"{GL}/platform/a\\b", " " + PROJECT, 7, None, ["x"]):
            with self.assertRaises(Denied, msg=bad):
                git.check_repo(bad, GROUP_POLICY)

    def test_a_trailing_newline_does_not_slip_through_a_group(self):
        # "$" in a regex matches before a final newline; the names are matched whole.
        self.assertEqual(git._GITLAB_REPO_RE.fullmatch(f"{GL}/platform/x\n"), None)
        with self.assertRaises(Denied):
            git.check_repo("acme/widgets\n", GL_POLICY)

    def test_exact_entries_still_work_beside_a_group(self):
        pol = self.pol(GROUP, "acme/widgets", f"{GL}/a/b/c/svc")
        for repo in (PROJECT, "ACME/Widgets", f"{GL}/a/b/c/svc", f"{GL}/platform/q/r"):
            self.assertEqual(git.check_repo(repo, pol), repo)
        for repo in ("acme/other", f"{GL}/a/b/c/other", f"{GL}/a/b/x"):
            with self.assertRaises(Denied, msg=repo):
                git.check_repo(repo, pol)

    def test_github_never_matches_a_wildcard(self):
        for entry in ("acme/*", "*/*", "acme/wid*", "*"):
            pol = self.pol(entry)
            for repo in ("acme/widgets", "acme/x", "acme/*"):
                with self.assertRaises(Denied, msg=(entry, repo)):
                    git.check_repo(repo, pol)
            with self.assertRaises(Denied, msg=entry):
                git.check_repos_entry(entry)
        with self.assertRaises(Denied) as cm:
            git.check_repos_entry("acme/*")
        self.assertIn("GitLab only", str(cm.exception))

    def test_malformed_wildcards_are_refused_and_match_nothing(self):
        bad = [f"{GL}/*",                    # the whole instance
               f"{GL}/",                     # nothing
               f"{GL}/platform/**", f"{GL}/**", f"{GL}/platform/*/*", f"{GL}/*/platform/*", f"{GL}/pla*/x/*",
               f"{GL}/platform/x*", f"{GL}/platform*", f"{GL}/platform/*x", f"{GL}/platform*/*",
               f"{GL}/platform//*", f"{GL}//platform/*", f"{GL}/platform/../*", f"{GL}/../*", f"{GL}/platform/./*",
               f"{GL}/platform/.x/*", f"{GL}/platform/-x/*", f"{GL}/.platform/*", f"{GL}/platform/x.git/*",
               f"{GL}/platform/x.atom/*", f"{GL}/platform/ x/*", f"{GL}/platform/x\n/*", f"{GL}/platform/*\n",
               f"{GL}/platform/a?b/*", f"{GL}/platform/a%2fb/*", f"{GL}/platform/a:b/*", f"{GL}/platform/a@b/*",
               f"https://{GL}/platform/*", f"{GL}/platform/*/", f"{GL}/{'/'.join(f'g{i}' for i in range(22))}/*",
               "gitlab acme.net/platform/*", "-.net/platform/*", f"{GL}:99999999/platform/*",
               "/platform/*", "*", "/*", "**", "*/*", f"{GL}/platform/*/x"]
        for entry in bad:
            with self.assertRaises(Denied, msg=entry):
                git.check_repos_entry(entry)
            pol = self.pol(entry)
            for repo in (PROJECT, f"{GL}/platform/x", f"{GL}/platform/sub/x", f"{GL}/x/y", f"{GL}/a/b/c/svc"):
                self.assertFalse(git.repo_allowed(repo, [entry]), (entry, repo))
                with self.assertRaises(Denied, msg=(entry, repo)):
                    git.check_repo(repo, pol)
        self.assertFalse(git.repo_allowed(PROJECT, "not a list"))
        self.assertFalse(git.repo_allowed(PROJECT, [None, 7, ["x"]]))

    def test_good_group_entries(self):
        deepest = GL + "/" + "/".join(f"g{i}" for i in range(21)) + "/*"  # top group + 20 subgroups
        for entry in (GROUP, f"{GL}/platform/tools/*", f"{GL}/Platform/*", f"{GL}:8443/platform/*", f"{GL}/p.q/_r-s/*",
                      "10.0.0.5/platform/*", deepest):
            self.assertEqual(git.check_repos_entry(entry), entry)
        self.assertEqual(git.check_repos_entry(PROJECT), PROJECT)
        self.assertEqual(git.check_repos_entry("acme/widgets"), "acme/widgets")
        self.assertEqual(git.describe_repos_entry(GROUP), f"{GROUP} (any project in the group)")
        self.assertEqual(git.describe_repos_entry(PROJECT), PROJECT)


class GroupToolTests(GitLabBase):
    """git_fetch, gitlab_create_mr and git_push all ask the same matcher."""

    def open_mr(self, repo, policy=GROUP_POLICY):
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open",
                               side_effect=lambda req, timeout: _FakeHTTPResponse(b'{"iid": 3, "web_url": "u"}')) as opened:
            out = git.tool_gitlab_create_mr({**MR_ARGS, "repo": repo}, policy)
        return out, opened

    def test_create_mr_in_a_group(self):
        for repo, path in ((f"{GL}/platform/api", "platform%2Fapi"), (f"{GL}/Platform/Sub/Svc", "Platform%2FSub%2FSvc")):
            out, opened = self.open_mr(repo)
            self.assertTrue(out.startswith("Opened MR !3"))
            self.assertEqual(opened.call_args.args[0].full_url, f"https://{GL}/api/v4/projects/{path}/merge_requests")

    def test_create_mr_outside_a_group_is_refused_before_a_card_or_a_call(self):
        cards = self.cards()
        for repo in (f"{GL}/platform-evil/api", f"{GL}/other/api", f"{GL}/platform", "acme/widgets"):
            with mock.patch.object(git.NO_REDIRECT_OPENER, "open") as opened, self.assertRaises(Denied, msg=repo):
                git.tool_gitlab_create_mr({**MR_ARGS, "repo": repo}, GROUP_POLICY)
            opened.assert_not_called()
        self.assertEqual(cards, [])

    def test_a_held_mr_is_checked_again_against_the_group_on_completion(self):
        import hashlib
        job = {"repo": f"{GL}/platform/api", "head": "factory/x", "base": "main", "title": "Draft: T", "body": "b",
               "draft": True}
        card = {**job, "body_sha256": hashlib.sha256(b"b").hexdigest()}
        out = None
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open",
                               side_effect=lambda req, timeout: _FakeHTTPResponse(b'{"iid": 3, "web_url": "u"}')):
            out = git.complete_gitlab_create_mr(job, card, GROUP_POLICY)
        self.assertTrue(out.startswith("Opened MR !3"), out)
        # The policy shrank (or the job was forged) since it was staged: nothing is sent.
        for repo in (f"{GL}/platform-evil/api", f"{GL}/other/api"):
            with mock.patch.object(git.NO_REDIRECT_OPENER, "open") as opened, self.assertRaises(Denied, msg=repo):
                git.complete_gitlab_create_mr({**job, "repo": repo}, {**card, "repo": repo}, GROUP_POLICY)
            opened.assert_not_called()
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open") as opened, self.assertRaises(Denied):
            git.complete_gitlab_create_mr(job, card, self.exact_only())
        opened.assert_not_called()

    def exact_only(self):
        return {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": [f"{GL}/platform/other"]}}

    def test_group_entries_for_fetch(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        srv.seen = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        host = f"127.0.0.1:{srv.server_address[1]}"
        p = mock.patch.object(git, "gitlab_base", lambda policy: f"http://{host}/")
        p.start()
        self.addCleanup(p.stop)
        pol = {**POLICY, "git": {**POLICY["git"], "repos": [f"{host}/platform/*"]}}
        with self.assertRaises(RuntimeError):  # git itself fails (the stand-in answers 404), past the allowlist
            git.tool_git_fetch({"repo": f"{host}/Platform/sub/api", "branch": "main"}, pol)
        self.assertEqual(srv.seen[0][0].split("?")[0], "/Platform/sub/api.git/info/refs")
        self.assertEqual({a for _, a in srv.seen}, {f"Basic {GL_B64}"})
        seen = len(srv.seen)
        for repo in (f"{host}/platform-evil/api", f"{host}/other/api", f"{host}/platform"):
            with self.assertRaises(Denied, msg=repo):
                git.tool_git_fetch({"repo": repo, "branch": "main"}, pol)
        self.assertEqual(len(srv.seen), seen, "a refused repo reached the network")


class GroupDoctorTests(GitLabBase):
    """The doctor on group entries: policy shape, what it covers, and the live group check."""

    def policy_report(self, pol):
        path = Path(self.tmp.name) / "policy.toml"
        lines = []
        for table, values in pol.items():
            lines.append(f"[{table}]")
            lines += [f"{k} = {json.dumps(v)}" for k, v in values.items()]
        path.write_text("\n".join(lines) + "\n")
        report = doctor.Report()
        doctor.check_policy(report, path)
        return report

    def pol(self, *entries, url=f"https://{GL}/"):
        return {**GL_POLICY, "git": {**GL_POLICY["git"], "repos": list(entries)}, "gitlab": {"url": url}}

    def test_a_group_entry_is_shown_as_a_group(self):
        report = self.policy_report(self.pol(GROUP, PROJECT))
        self.assertEqual(report.messages(doctor.FAIL), [])
        self.assertIn(f"[git].repos covers {GROUP} (any project in the group)", report.messages(doctor.OK))
        self.assertFalse(any("no [git].repos entry is on it" in m for m in report.messages(doctor.INFO)))
        only_group = self.policy_report(self.pol(GROUP))
        self.assertEqual(only_group.messages(doctor.FAIL), [])
        self.assertFalse(any("no [git].repos entry is on it" in m for m in only_group.messages(doctor.INFO)))

    def test_bad_group_entries_fail_at_doctor_time(self):
        for entry in (f"{GL}/*", f"{GL}/platform/**", f"{GL}/pla*/x/*", f"{GL}/platform/../*", f"{GL}/platform//*",
                      "acme/*", "*", f"{GL}/platform/*/x"):
            report = self.policy_report(self.pol(entry))
            self.assertTrue(any("[git].repos entry" in m for m in report.messages(doctor.FAIL)), (entry, report.items))
            shape = doctor.Report()
            doctor._check_policy_shape(shape, self.pol(entry))
            self.assertTrue(any("[git].repos entry" in m for m in shape.messages(doctor.FAIL)), entry)
        github = self.policy_report(self.pol("acme/*"))
        self.assertTrue(any("GitLab only" in m for m in github.messages(doctor.FAIL)))

    def test_a_group_on_another_host_or_with_no_gitlab_fails(self):
        other = self.policy_report(self.pol("gitlab.evil.example/platform/*"))
        self.assertTrue(any("not on the configured GitLab" in m for m in other.messages(doctor.FAIL)), other.items)
        unset = self.policy_report(self.pol(GROUP, url=""))
        self.assertTrue(any(GROUP in m and "not set" in m for m in unset.messages(doctor.FAIL)), unset.items)

    def test_a_group_only_policy_needs_no_github(self):
        pol = self.pol(GROUP)
        self.assertNotIn("github.com", doctor.egress_hosts(pol))
        self.assertIn(GL, doctor.egress_hosts(pol))

    def live(self, pol, answers, default=(200, b"{}")):
        seen = []

        def fake_http(url, headers=None, data=None, timeout=10):
            if data is not None:
                return 401, b""
            seen.append((url, (headers or {}).get("Authorization")))
            answer = answers.get(url, default)
            if isinstance(answer, Exception):
                raise answer
            return answer
        report = doctor.Report()
        doctor.check_live(report, pol, "http://127.0.0.1:1/mcp", http=fake_http)
        self.assertFalse(leaks(json.dumps(report.to_dict())))
        return report, seen

    USER_API = f"https://{GL}/api/v4/user"
    MEMBER_API = f"https://{GL}/api/v4/groups/platform/members/all/4242"
    BOT = (200, b'{"id": 4242, "username": "group_1_bot"}')

    def member(self, level):
        return 200, json.dumps({"id": 4242, "access_level": level}).encode()

    def group_live(self, member_answer, user_answer=BOT):
        return self.live(self.pol(GROUP), {self.USER_API: user_answer, self.MEMBER_API: member_answer})

    def test_group_token_is_checked_by_its_role_in_the_group(self):
        shown = f"{GROUP} (any project in the group)"
        for level in (30, 40, 50):
            report, seen = self.group_live(self.member(level))
            self.assertTrue(any(f"member of {shown} with push access (access level {level})" in m
                                for m in report.messages(doctor.OK)), report.items)
            self.assertEqual(report.messages(doctor.WARN) + report.messages(doctor.FAIL), [])
            self.assertEqual(seen, [(self.USER_API, f"Bearer {GL_TOKEN}"), (self.MEMBER_API, f"Bearer {GL_TOKEN}")])

    def test_a_group_role_below_developer_warns(self):
        for level in (0, 5, 10, 20):
            report, _ = self.group_live(self.member(level))
            self.assertTrue(any(f"access level {level}, below the Developer role" in m
                                for m in report.messages(doctor.WARN)), report.items)
            self.assertFalse([m for m in report.messages(doctor.OK) if "push access" in m])

    def test_not_a_member_warns_and_names_the_project_token_cause(self):
        report, seen = self.group_live((404, b'{"message": "404 Not found"}'))
        warns = [(text, fix) for _, level, text, fix in report.items if level == doctor.WARN]
        self.assertEqual(len(warns), 1)
        text, fix = warns[0][0], " ".join(warns[0][1])
        self.assertIn("not a member", text)
        self.assertIn("project access token", text)
        self.assertIn("group access token", fix)
        self.assertIn("Developer", fix)
        self.assertEqual(report.messages(doctor.FAIL), [])
        self.assertFalse([m for m in report.messages(doctor.OK) if "member" in m])

    def test_group_role_that_cannot_be_confirmed_warns_and_never_says_ok(self):
        member_answers = ((500, b""), (401, b""), (403, b""), (200, b"not json"), (200, b"{}"),
                          (200, b'{"access_level": "40"}'), (200, b'{"access_level": true}'),
                          (200, b'{"access_level": null}'), (200, b"[]"))
        for answer in member_answers:
            report, _ = self.group_live(answer)
            self.assertTrue(any("its group role could not be confirmed" in m for m in report.messages(doctor.WARN)),
                            (answer, report.items))
            self.assertFalse([m for m in report.messages(doctor.OK) if "member" in m], (answer, report.items))
            self.assertEqual(report.messages(doctor.FAIL), [], answer)

    def test_a_failed_user_lookup_warns_and_skips_the_member_lookup(self):
        for answer in ((401, b""), (403, b""), (404, b""), (500, b""), (200, b"not json"), (200, b"{}"),
                       (200, b'{"id": "4242"}'), (200, b'{"id": true}'), (200, b'{"id": -1}'), (200, b"[]")):
            report, seen = self.group_live(self.member(40), user_answer=answer)
            self.assertTrue(any("its group role could not be confirmed" in m for m in report.messages(doctor.WARN)),
                            (answer, report.items))
            self.assertFalse([m for m in report.messages(doctor.OK) if "member" in m], (answer, report.items))
            self.assertEqual(report.messages(doctor.FAIL), [], answer)
            self.assertEqual([u for u, _ in seen], [self.USER_API], answer)

    def test_user_is_looked_up_once_for_several_groups(self):
        pol = self.pol(GROUP, f"{GL}/Platform/Tools/*")
        report, seen = self.live(pol, {self.USER_API: self.BOT,
                                       self.MEMBER_API: self.member(30),
                                       f"https://{GL}/api/v4/groups/Platform%2FTools/members/all/4242": self.member(20)})
        self.assertEqual([u for u, _ in seen].count(self.USER_API), 1)
        self.assertEqual(len([m for m in report.messages(doctor.OK) if "member of" in m]), 1)
        self.assertEqual(len(report.messages(doctor.WARN)), 1)

    def test_the_token_is_only_sent_to_the_gitlab_api_and_never_shown(self):
        report, seen = self.group_live(self.member(40))
        self.assertEqual({a for _, a in seen}, {f"Bearer {GL_TOKEN}"})
        self.assertTrue(all(u.startswith(f"https://{GL}/api/v4/") for u, _ in seen))
        self.assertNotIn(GL_TOKEN, json.dumps(report.to_dict()))

    def test_an_unreachable_gitlab_on_either_lookup_fails_once(self):
        err = urllib.error.URLError("no route")
        for answers, calls in (({self.USER_API: err}, 1), ({self.USER_API: self.BOT, self.MEMBER_API: err}, 2)):
            report, seen = self.live(self.pol(GROUP, f"{GL}/other/*"), answers)
            self.assertEqual(len([m for m in report.messages(doctor.FAIL) if "cannot reach" in m]), 1)
            self.assertEqual(len(seen), calls)

    def test_live_subgroup_is_one_encoded_path_and_projects_keep_their_check(self):
        pol = self.pol(f"{GL}/Platform/Tools/*", PROJECT)
        report, seen = self.live(pol, {self.USER_API: self.BOT})
        urls = [u for u, _ in seen if GL in u]
        self.assertEqual(urls, [self.USER_API, f"https://{GL}/api/v4/groups/Platform%2FTools/members/all/4242",
                                f"https://{GL}/api/v4/projects/platform%2Fapi"])
        self.assertEqual({a for u, a in seen if GL in u}, {f"Bearer {GL_TOKEN}"})

    def test_no_token_skips_the_group_check_and_says_so(self):
        (credentials.secrets_dir() / "gitlab").unlink()
        report, seen = self.live(self.pol(GROUP), {})
        self.assertTrue(any("no usable gitlab credential: skipped the access check for 1 GitLab entry" in m
                            for m in report.messages(doctor.WARN)), report.items)
        self.assertEqual([u for u, _ in seen if GL in u], [])

    def test_the_token_wording_covers_group_tokens(self):
        report, _ = self.live(self.pol(PROJECT), {f"https://{GL}/api/v4/projects/platform%2Fapi":
                                                  (200, b'{"permissions": {"project_access": {"access_level": 20}}}')})
        self.assertTrue(any("project or group access token" in " ".join(fix) for _, _, _, fix in report.items), report.items)


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
