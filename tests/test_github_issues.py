"""github_create_issue: the issue allowlist, input checks, secret scanning,
existing-labels-only, the approval card, and token handling in both the
standalone (gateway holds the token) and proxy (sbx) modes."""

import hashlib
import json
import urllib.error
import urllib.request
from unittest import mock

from passdesk import doctor
from passdesk.core import approvals, config, credentials, protocol
from passdesk.core.errors import Denied
from passdesk.tools import git

from .base import FAKE_GH, POLICY, Base, _FakeHTTPResponse, fake_http_error

TOKEN = "TESTONLY_" + "k5" * 20
ISSUE_POLICY = {**POLICY, "github": {"issue_repos": ["acme/tracker"]}}
ARGS = {"repo": "acme/tracker", "title": "Something broke", "body": "Steps:\n1. run it"}
CREATED = b'{"number": 5, "html_url": "https://github.com/acme/tracker/issues/5"}'


def labels_page(names):
    return json.dumps([{"name": n, "color": "ffffff"} for n in names]).encode()


class Fake:
    """Answers GitHub API requests: label pages by page number, then the new issue."""

    def __init__(self, label_pages=(), created=CREATED, error=None):
        self.requests = []
        self.label_pages = list(label_pages)
        self.created = created
        self.error = error

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        if req.get_method() == "GET":
            return _FakeHTTPResponse(self.label_pages.pop(0) if self.label_pages else b"[]")
        return _FakeHTTPResponse(self.created)

    def posts(self):
        return [r for r in self.requests if r.get_method() == "POST"]


class StandaloneIssueTests(Base):
    def setUp(self):
        super().setUp()
        for name in ("CREDENTIAL_PROVIDERS", "DEPLOYMENT"):
            self.addCleanup(setattr, config, name, getattr(config, name))
        config.CREDENTIAL_PROVIDERS = "file,env-file"
        config.DEPLOYMENT = "standalone"
        self.write_secret(credentials.secrets_dir() / "github", TOKEN + "\n")
        self.cards = []
        approvals.set_hook(self.cards.append)
        self.addCleanup(approvals.set_hook, None)

    def run_tool(self, args, fake, policy=ISSUE_POLICY):
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("followed redirects")):
            return git.tool_github_create_issue(args, policy)

    def test_files_an_issue_with_a_bearer_token(self):
        fake = Fake()
        out = self.run_tool(ARGS, fake)
        self.assertEqual(out, "Opened issue #5 in acme/tracker: https://github.com/acme/tracker/issues/5")
        [req] = fake.requests  # no labels asked for: no label lookup
        self.assertEqual(req.full_url, "https://api.github.com/repos/acme/tracker/issues")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(json.loads(req.data), {"title": "Something broke", "body": "Steps:\n1. run it"})

    def test_only_repos_on_the_issue_allowlist(self):
        fake = Fake()
        for policy, repo in ((ISSUE_POLICY, "acme/widgets"),  # in [git].repos, not issue_repos
                             (POLICY, "acme/tracker"),         # no [github] table at all
                             ({**POLICY, "github": {"issue_repos": "acme/tracker"}}, "acme/tracker")):
            with self.assertRaises(Denied) as cm:
                self.run_tool({**ARGS, "repo": repo}, fake, policy)
            self.assertIn("issue allowlist", str(cm.exception))
        with self.assertRaises(Denied):
            self.run_tool({**ARGS, "repo": "acme/../tracker"}, fake)
        self.assertEqual(fake.requests, [])
        self.assertEqual(self.cards, [])
        # Case-insensitive, as GitHub is.
        self.assertIn("Opened issue", self.run_tool({**ARGS, "repo": "ACME/Tracker"}, Fake()))

    def test_title_checks(self):
        for title in ("", "   ", "x" * 257, "two\nlines", "tab\there", "nul\x00"):
            with self.assertRaises(Denied):
                self.run_tool({**ARGS, "title": title}, Fake())
        with self.assertRaises(Denied):
            self.run_tool({**ARGS, "body": "x" * 65537}, Fake())
        self.assertEqual(self.cards, [])

    def test_secrets_are_refused_before_any_request_or_card(self):
        git.github_token()  # the gateway's own token is remembered for redaction
        fake = Fake([labels_page([f"see {FAKE_GH}"])])
        for field, value in (("title", f"leak {FAKE_GH}"), ("body", f"key {FAKE_GH}"),
                             ("body", f"the gateway's own {TOKEN}"), ("labels", f"bug, {FAKE_GH}")):
            with self.assertRaises(Denied) as cm:
                self.run_tool({**ARGS, field: value}, fake)
            self.assertIn("appears to contain secrets", str(cm.exception))
            self.assertNotIn(TOKEN, str(cm.exception))
        self.assertEqual(fake.requests, [])
        self.assertEqual(self.cards, [])

    def test_labels_must_already_exist_and_go_out_as_github_spells_them(self):
        fake = Fake([labels_page(["bug", "Priority:P2", "repo:broker"])])
        self.run_tool({**ARGS, "labels": " BUG , priority:p2,bug"}, fake)
        get, post = fake.requests
        self.assertEqual(get.get_method(), "GET")
        self.assertEqual(get.full_url, "https://api.github.com/repos/acme/tracker/labels?per_page=100&page=1")
        self.assertEqual(get.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(json.loads(post.data)["labels"], ["bug", "Priority:P2"])
        # The card shows what the agent asked for; nothing on it comes from the network.
        self.assertEqual(self.cards[0]["labels"], ["BUG", "priority:p2"])

    def test_an_unknown_label_is_refused_and_nothing_is_filed(self):
        fake = Fake([labels_page(["bug", "docs"])])
        with self.assertRaises(Denied) as cm:
            self.run_tool({**ARGS, "labels": "bug,wontfix"}, fake)
        self.assertIn("not defined in acme/tracker: wontfix", str(cm.exception))
        self.assertIn("existing: bug, docs", str(cm.exception))
        self.assertEqual(fake.posts(), [])
        self.assertEqual(self.cards, [])

    def test_label_lookup_follows_pages(self):
        first = [f"l{i}" for i in range(100)]
        fake = Fake([labels_page(first), labels_page(["late"])])
        self.run_tool({**ARGS, "labels": "late"}, fake)
        self.assertEqual([r.full_url.rsplit("page=", 1)[1] for r in fake.requests if r.get_method() == "GET"],
                         ["1", "2"])
        self.assertEqual(json.loads(fake.posts()[0].data)["labels"], ["late"])

    def test_too_many_labels(self):
        with self.assertRaises(Denied):
            self.run_tool({**ARGS, "labels": ",".join(f"l{i}" for i in range(11))}, Fake())

    def test_the_approval_card_and_a_declined_issue(self):
        body = "x" * 3000
        self.run_tool({**ARGS, "body": body}, Fake())
        card = self.cards[0]
        self.assertEqual(card["tool"], "github_create_issue")
        self.assertEqual(card["body"], body[:2000])
        self.assertEqual(card["body_sha256"], hashlib.sha256(body.encode()).hexdigest())

        def decline(card):
            raise Denied("declined by a person")
        approvals.set_hook(decline)
        fake = Fake()
        with self.assertRaises(Denied):
            self.run_tool(ARGS, fake)
        self.assertEqual(fake.requests, [])

    def test_redirect_and_auth_errors_never_leak_the_token(self):
        redirect = urllib.error.HTTPError("https://api.github.com/x", 302, "refusing to follow a redirect",
                                          None, None)
        out = self.run_tool(ARGS, Fake(error=redirect))
        self.assertIn("HTTP 302", out)
        out = self.run_tool(ARGS, Fake(error=fake_http_error(403, f"denied for {TOKEN}".encode())))
        self.assertIn("HTTP 403", out)
        self.assertIn("Issues read/write access to acme/tracker", out)
        self.assertIn("[REDACTED:live-credential]", out)
        self.assertNotIn(TOKEN, out)
        self.assertIn("UNTRUSTED CONTENT", out)

    def test_a_label_lookup_error_is_reported_and_nothing_is_filed(self):
        fake = Fake(error=fake_http_error(404, b"Not Found"))
        out = self.run_tool({**ARGS, "labels": "bug"}, fake)
        self.assertIn("HTTP 404", out)
        self.assertEqual(fake.posts(), [])
        self.assertEqual(self.cards, [])

    def test_an_unexpected_response_is_refused(self):
        with self.assertRaises(Denied):
            self.run_tool(ARGS, Fake(created=b'{"message": "odd"}'))


class ProxyModeIssueTests(Base):
    """sbx: the egress proxy authenticates; the gateway sends no token."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, config, "CREDENTIAL_PROVIDERS", config.CREDENTIAL_PROVIDERS)
        config.CREDENTIAL_PROVIDERS = "proxy,file,env-file"

    def test_uses_the_plain_opener_without_authorization(self):
        fake = Fake([labels_page(["bug"])])
        with mock.patch.object(urllib.request, "urlopen", side_effect=fake), \
                mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=AssertionError("changed opener")):
            out = git.tool_github_create_issue({**ARGS, "labels": "bug"}, ISSUE_POLICY)
        self.assertIn("Opened issue #5", out)
        self.assertTrue(all(r.get_header("Authorization") is None for r in fake.requests))

    def test_auth_failure_hint_names_the_deployment(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_http_error(401, b"Bad credentials")):
            out = git.tool_github_create_issue(ARGS, ISSUE_POLICY)
        self.assertIn("supplied by the deployment (with sbx: sbx secret set github", out)


class ProtocolIssueTests(Base):
    def test_it_is_an_external_write_with_an_intent_record(self):
        (config.STATE / "roles.json").write_text('{"reviewer": ["github_create_issue"]}')
        events = []
        real = protocol.audit

        def spy(record):
            events.append(dict(record))
            real(record)
        with mock.patch.object(protocol, "audit", side_effect=spy), \
                mock.patch.object(protocol.policy, "load_policy", return_value=ISSUE_POLICY), \
                mock.patch.object(urllib.request, "urlopen", side_effect=Fake()):
            text, err = protocol.call_tool("github_create_issue", ARGS, "reviewer")
        self.assertFalse(err, text)
        self.assertEqual(events[0].get("event"), "intent")
        self.assertEqual(events[-1]["decision"], "allow")


class DoctorIssueTests(Base):
    def report_for(self, pol):
        report = doctor.Report()
        with mock.patch.object(doctor.policy_mod, "load_policy", return_value=pol):
            doctor.check_policy(report)
        return report

    def test_github_table_is_known_and_an_empty_list_is_explained(self):
        report = self.report_for({**POLICY, "github": {"issue_repos": []}})
        texts = report.messages()
        self.assertFalse(any("[github] is not a table" in t for t in texts), texts)
        self.assertTrue(any("github_create_issue returns DENIED" in t for t in texts), texts)

    def test_a_malformed_list_fails(self):
        for bad in ("acme/tracker", ["acme"], ["acme/../x"], [5]):
            self.assertEqual(self.report_for({**POLICY, "github": {"issue_repos": bad}}).problems, 1, bad)

    def test_egress_and_credential_maps_cover_the_tool(self):
        self.assertIn("api.github.com", doctor.egress_hosts({}, granted={"github_create_issue"}))
        self.assertIn("github_create_issue", doctor._GITHUB_TOOLS)
