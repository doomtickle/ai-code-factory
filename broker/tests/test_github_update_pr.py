"""github_update_pr: the [git].repos allowlist, argument checks, secret scanning,
the size cap, a PATCH that carries only title and body, the approval card and
the gateway making a staged update, and token handling in both deployment modes."""

import hashlib
import json
import time
import urllib.error
import urllib.request
from unittest import mock

from passdesk import doctor
from passdesk.core import approvals, config, credentials, holds, notify, protocol, registry
from passdesk.core.errors import Denied, Made, Staged
from passdesk.tools import git

from .base import FAKE_GH, POLICY, Base, _FakeHTTPResponse, fake_http_error

TOKEN = "TESTONLY_" + "k5" * 20
ARGS = {"repo": "acme/widgets", "number": 7, "title": "Better title", "body": "Why:\n- it is better"}
UPDATED = b'{"number": 7, "html_url": "https://github.com/acme/widgets/pull/7", "draft": true}'
GITLAB_POLICY = {**POLICY, "gitlab": {"url": "https://gitlab.example.com/"},
                 "git": {**POLICY["git"], "repos": ["acme/widgets", "gitlab.example.com/grp/proj"]}}


class Fake:
    """Answers GitHub API requests with `answer`, or raises `error`."""

    def __init__(self, answer=UPDATED, error=None):
        self.requests = []
        self.answer = answer
        self.error = error

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return _FakeHTTPResponse(self.answer)


class HoldMixin:
    def held(self, policy=POLICY):
        held_policy = {**policy, "approvals": {"tools": ["github_update_pr"], "expire_seconds": 600}}
        for p in (mock.patch.object(holds, "load_policy", lambda: held_policy),
                  mock.patch.object(notify, "send_async", lambda *a: None),
                  mock.patch.object(notify, "send_result_async", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)
        approvals.set_hook(holds.hook)
        self.addCleanup(approvals.set_hook, None)

    def approve(self, cid):
        pending = json.loads((config.STATE / "approvals" / "pending" / f"{cid}.json").read_text())
        path = config.STATE / "approvals" / "decisions" / f"{cid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": cid, "nonce": pending["nonce"], "decision": "approve", "approver": "daron",
                                    "ts": time.time()}))

    def done(self, cid):
        return json.loads((config.STATE / "approvals" / "done" / f"{cid}.json").read_text())


class StandaloneUpdateTests(HoldMixin, Base):
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

    def run_tool(self, args, fake=None, policy=POLICY):
        fake = fake or Fake()
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("followed redirects")):
            return git.tool_github_update_pr(args, policy)

    def test_patches_title_and_body_with_a_bearer_token(self):
        fake = Fake()
        out = self.run_tool(ARGS, fake)
        self.assertEqual(out, "Updated PR #7 (title and body): https://github.com/acme/widgets/pull/7")
        [req] = fake.requests
        self.assertEqual((req.get_method(), req.full_url), ("PATCH", "https://api.github.com/repos/acme/widgets/pulls/7"))
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(json.loads(req.data), {"title": "Better title", "body": "Why:\n- it is better"})

    def test_only_what_was_given_is_sent_and_never_draft_state_or_base(self):
        for args, sent in (({"title": "T"}, {"title": "T"}), ({"body": "B"}, {"body": "B"}),
                           ({"body": ""}, {"body": ""})):
            with self.subTest(args=args):
                fake = Fake()
                self.run_tool({"repo": "acme/widgets", "number": 7, **args}, fake)
                self.assertEqual(json.loads(fake.requests[0].data), sent)
        # Even if more arguments reach the function, the payload is built from title and body alone.
        fake = Fake()
        self.run_tool({**ARGS, "draft": False, "state": "closed", "base": "main", "head": "x"}, fake)
        self.assertEqual(set(json.loads(fake.requests[0].data)), {"title", "body"})

    def test_the_call_layer_refuses_other_fields_and_wrong_types(self):
        with mock.patch.object(protocol, "principal_tools", return_value={"github_update_pr"}), \
                mock.patch.object(protocol, "audit"), \
                mock.patch.object(git, "_github_api", side_effect=AssertionError("must not be called")):
            for extra, message in (({"draft": False}, "unexpected argument 'draft'"),
                                   ({"state": "closed"}, "unexpected argument 'state'"),
                                   ({"base": "dev"}, "unexpected argument 'base'"),
                                   ({"number": "7"}, "must be a integer"),
                                   ({"number": True}, "must be a integer"),
                                   ({"title": 5}, "must be a string")):
                with self.subTest(extra=extra):
                    text, is_error = protocol.call_tool("github_update_pr", {**ARGS, **extra}, "developer")
                    self.assertTrue(is_error)
                    self.assertIn(message, text)
            text, is_error = protocol.call_tool("github_update_pr", {"repo": "acme/widgets"}, "developer")
            self.assertIn("missing required argument 'number'", text)

    def test_only_repos_on_the_git_allowlist_and_only_on_github(self):
        for repo in ("evil/widgets", "acme/../x", "acme", "../acme/widgets"):
            with self.subTest(repo=repo), self.assertRaises(Denied):
                self.run_tool({**ARGS, "repo": repo}, Fake())
        fake = Fake()
        with self.assertRaises(Denied) as cm:
            self.run_tool({**ARGS, "repo": "gitlab.example.com/grp/proj"}, fake, GITLAB_POLICY)
        self.assertIn("GitLab", str(cm.exception))
        # [github].issue_repos is for issues; it grants nothing here.
        with self.assertRaises(Denied):
            self.run_tool({**ARGS, "repo": "acme/tracker"}, fake, {**POLICY, "github": {"issue_repos": ["acme/tracker"]}})
        self.assertEqual(fake.requests, [])

    def test_the_number_must_be_a_positive_int_and_not_a_bool(self):
        fake = Fake()
        for number in (0, -3, True, False, "7", 7.0, None, 10**9 + 1):
            with self.subTest(number=number), self.assertRaises(Denied):
                self.run_tool({**ARGS, "number": number}, fake)
        self.assertEqual(fake.requests, [])

    def test_at_least_one_of_title_and_body(self):
        fake = Fake()
        with self.assertRaises(Denied) as cm:
            self.run_tool({"repo": "acme/widgets", "number": 7}, fake)
        self.assertIn("title", str(cm.exception))
        self.assertEqual((fake.requests, self.cards), ([], []))

    def test_title_checks(self):
        fake = Fake()
        for title in ("", "   ", "x" * 257, "two\nlines", "tab\there", f"leak {FAKE_GH}"):
            with self.subTest(title=title), self.assertRaises(Denied):
                self.run_tool({**ARGS, "title": title}, fake)
        self.run_tool({**ARGS, "title": "x" * 256}, fake)
        self.assertEqual(len(fake.requests), 1)

    def test_body_size_cap_and_secret_scan(self):
        fake = Fake()
        with self.assertRaises(Denied) as cm:
            self.run_tool({**ARGS, "body": "x" * 65537}, fake)
        self.assertIn("65536", str(cm.exception))
        with self.assertRaises(Denied) as cm:
            self.run_tool({**ARGS, "body": f"see {FAKE_GH}"}, fake)
        self.assertNotIn(FAKE_GH, str(cm.exception))
        self.assertEqual((fake.requests, self.cards), ([], []))
        self.run_tool({**ARGS, "body": "x" * 65536}, fake)  # exactly the cap
        self.assertEqual(len(fake.requests), 1)

    def test_the_approval_card_carries_the_whole_bodys_digest_and_absent_is_not_empty(self):
        body = "x" * 3000
        self.run_tool({**ARGS, "body": body})
        card = self.cards[-1]
        self.assertEqual((card["tool"], card["repo"], card["number"], card["title"]),
                         ("github_update_pr", "acme/widgets", 7, "Better title"))
        self.assertEqual(card["body"], body[:2000])
        self.assertEqual((card["body_sha256"], card["body_chars"]), (hashlib.sha256(body.encode()).hexdigest(), 3000))
        self.assertNotIn("draft", card)
        self.run_tool({"repo": "acme/widgets", "number": 7, "title": "T"})
        self.assertEqual((self.cards[-1]["body"], self.cards[-1]["body_sha256"]), (None, None))

        base = {"tool": "github_update_pr", "principal": "developer", "repo": "acme/widgets", "number": 7,
                "title": "T", "body": None, "body_sha256": None}
        ids = {holds.card_id(c) for c in (
            base, {**base, "body": "x", "body_sha256": "a" * 64}, {**base, "body": "x", "body_sha256": "b" * 64},
            {**base, "body": "", "body_sha256": hashlib.sha256(b"").hexdigest()}, {**base, "number": 8},
            {**base, "title": None}, {**base, "repo": "acme/other"})}
        self.assertEqual(len(ids), 7)
        # Two bodies that share their first 2,000 characters are different cards.
        a, b = "y" * 2000 + "A", "y" * 2000 + "B"
        self.run_tool({**ARGS, "body": a})
        self.run_tool({**ARGS, "body": b})
        self.assertNotEqual(holds.card_id({**self.cards[-2], "principal": "p"}),
                            holds.card_id({**self.cards[-1], "principal": "p"}))

    def test_a_declined_update_writes_nothing(self):
        def decline(card):
            raise Denied("declined by a person")
        approvals.set_hook(decline)
        fake = Fake()
        with self.assertRaises(Denied):
            self.run_tool(ARGS, fake)
        self.assertEqual(fake.requests, [])

    def test_a_held_update_is_staged_and_made_by_the_gateway_once_on_approval(self):
        self.held()
        fake = Fake()
        with self.assertRaises(Staged) as cm:
            self.run_tool(ARGS, fake)
        cid = cm.exception.cid
        self.assertTrue(cm.exception.text.startswith(f"Staged for approval (id {cid}); nothing has been sent yet."))
        self.assertIn("update of pull request #7 on acme/widgets", cm.exception.text)
        self.assertEqual(fake.requests, [])
        self.approve(cid)
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake):
            holds.sweep_idle()
            holds.sweep_idle()
        [req] = fake.requests
        self.assertEqual((req.get_method(), req.full_url), ("PATCH", "https://api.github.com/repos/acme/widgets/pulls/7"))
        self.assertEqual(json.loads(req.data), {"title": "Better title", "body": "Why:\n- it is better"})
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        done = self.done(cid)
        self.assertEqual((done["write"], done["result"]),
                         ("done", "Updated PR #7 (title and body): https://github.com/acme/widgets/pull/7"))
        # The same call again returns the result and sends nothing new.
        with self.assertRaises(Made):
            self.run_tool(ARGS, fake)
        self.assertEqual(len(fake.requests), 1)

    def test_a_staged_title_only_update_sends_no_body(self):
        self.held()
        fake = Fake()
        with self.assertRaises(Staged) as cm:
            self.run_tool({"repo": "acme/widgets", "number": 7, "title": "Only the title"}, fake)
        self.approve(cm.exception.cid)
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake):
            holds.sweep_idle()
        self.assertEqual(json.loads(fake.requests[0].data), {"title": "Only the title"})
        self.assertEqual(self.done(cm.exception.cid)["result"],
                         "Updated PR #7 (title): https://github.com/acme/widgets/pull/7")

    def test_a_staged_update_whose_job_does_not_match_its_card_is_never_made(self):
        self.held()
        fake = Fake()
        mismatches = ({"body": "something else"}, {"title": "another"}, {"number": 8}, {"repo": "acme/other"},
                      {"body": None}, {"title": None}, {"body": ARGS["body"] + "!"}, {"number": True},
                      {"number": "7"}, {"title": f"leak {FAKE_GH}"}, {"body": f"leak {FAKE_GH}"},
                      {"title": 5}, {"body": 5}, {"title": "x" * 300}, {"body": "x" * 70000}, {"title": "a\nb"})
        for job in mismatches:
            with self.subTest(job=job):
                with self.assertRaises(Staged) as cm:
                    self.run_tool(ARGS, fake)
                cid = cm.exception.cid
                path = config.STATE / "approvals" / "queued" / f"{cid}.json"
                rec = json.loads(path.read_text())
                path.write_text(json.dumps({**rec, "job": {**rec["job"], **job}}))
                self.approve(cid)
                with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake):
                    holds.sweep_idle()
                self.assertEqual(self.done(cid)["write"], "failed")
                (config.STATE / "approvals" / "done" / f"{cid}.json").unlink()
        self.assertEqual(fake.requests, [])

    def test_a_staged_update_is_checked_against_the_policy_again(self):
        self.held()
        fake = Fake()
        with self.assertRaises(Staged) as cm:
            self.run_tool(ARGS, fake)
        self.approve(cm.exception.cid)
        shrunk = {**POLICY, "git": {**POLICY["git"], "repos": ["acme/other"]}, "approvals": {"tools": ["github_update_pr"]}}
        with mock.patch.object(holds, "load_policy", lambda: shrunk), \
                mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=fake):
            holds.sweep_idle()
        self.assertEqual(self.done(cm.exception.cid)["write"], "failed")
        self.assertEqual(fake.requests, [])

    def test_http_errors_are_reported_with_a_hint_and_never_leak_the_token(self):
        out = self.run_tool(ARGS, Fake(error=fake_http_error(403, f"denied for {TOKEN}".encode())))
        self.assertIn("HTTP 403", out)
        self.assertIn("Pull requests read/write access to acme/widgets", out)
        self.assertIn("[REDACTED:live-credential]", out)
        self.assertNotIn(TOKEN, out)
        self.assertIn("UNTRUSTED CONTENT", out)
        redirect = urllib.error.HTTPError("https://api.github.com/x", 302, "refusing to follow a redirect", None, None)
        self.assertIn("HTTP 302", self.run_tool(ARGS, Fake(error=redirect)))

    def test_a_failed_staged_update_is_recorded_as_failed(self):
        self.held()
        with self.assertRaises(Staged) as cm:
            self.run_tool(ARGS, Fake())
        self.approve(cm.exception.cid)
        with mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=Fake(error=fake_http_error(404, b"Not Found"))):
            holds.sweep_idle()
        done = self.done(cm.exception.cid)
        self.assertEqual(done["write"], "failed")
        self.assertIn("HTTP 404", done["result"])

    def test_an_unexpected_response_is_refused(self):
        for answer in (b'{"message": "odd"}', b"[]"):
            with self.subTest(answer=answer), self.assertRaises(Denied):
                self.run_tool(ARGS, Fake(answer))


class ProxyModeUpdateTests(Base):
    """sbx: the egress proxy authenticates; the gateway sends no token."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, config, "CREDENTIAL_PROVIDERS", config.CREDENTIAL_PROVIDERS)
        config.CREDENTIAL_PROVIDERS = "proxy,file,env-file"
        approvals.set_hook(lambda card: None)
        self.addCleanup(approvals.set_hook, None)

    def test_uses_the_plain_opener_without_authorization(self):
        fake = Fake()
        with mock.patch.object(urllib.request, "urlopen", side_effect=fake), \
                mock.patch.object(git.NO_REDIRECT_OPENER, "open", side_effect=AssertionError("changed opener")):
            out = git.tool_github_update_pr(ARGS, POLICY)
        self.assertIn("Updated PR #7", out)
        [req] = fake.requests
        self.assertEqual(req.get_method(), "PATCH")
        self.assertIsNone(req.get_header("Authorization"))

    def test_an_auth_error_points_at_the_deployments_credential(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_http_error(401, b"Bad credentials")):
            out = git.tool_github_update_pr(ARGS, POLICY)
        self.assertIn("supplied by the deployment (with sbx: sbx secret set github", out)


class ProtocolUpdateTests(Base):
    def test_it_is_an_external_write_with_an_intent_record(self):
        (config.STATE / "roles.json").write_text('{"developer": ["github_update_pr"]}')
        events = []
        real = protocol.audit

        def spy(record):
            events.append(dict(record))
            real(record)
        approvals.set_hook(lambda card: None)
        self.addCleanup(approvals.set_hook, None)
        with mock.patch.object(protocol, "audit", side_effect=spy), \
                mock.patch.object(protocol.policy, "load_policy", return_value=POLICY), \
                mock.patch.object(urllib.request, "urlopen", side_effect=Fake()):
            text, err = protocol.call_tool("github_update_pr", ARGS, "developer")
        self.assertFalse(err, text)
        self.assertEqual(events[0].get("event"), "intent")
        self.assertEqual(events[-1]["decision"], "allow")

    def test_registered_with_a_typed_number_and_without_draft_state_or_base(self):
        _, _, props, required = registry.TOOLS["github_update_pr"]
        self.assertEqual(props["number"]["type"], "integer")
        self.assertEqual(sorted(props), ["body", "number", "repo", "title"])
        self.assertEqual(required, ["repo", "number"])


class DoctorUpdateTests(Base):
    def test_it_is_a_github_tool_that_needs_the_api_host(self):
        self.assertIn("github_update_pr", doctor._GITHUB_TOOLS)
        self.assertEqual(doctor._TOOL_HOSTS["github_update_pr"], ["api.github.com"])

    def test_it_can_be_listed_under_approvals_without_a_typo_warning(self):
        report = doctor.Report()
        pol = {**POLICY, "approvals": {"tools": ["github_update_pr"]}}
        with mock.patch.object(doctor.policy_mod, "load_policy", return_value=pol):
            doctor.check_policy(report)
        self.assertFalse(any("not a tool" in t for t in report.messages()), report.messages())


class CardDisplayTests(Base):
    def test_the_webhook_summary_names_what_changes(self):
        card = {"tool": "github_update_pr", "principal": "developer", "repo": "acme/widgets", "number": 7,
                "title": "New", "body": None, "body_sha256": None}
        text = notify.summary(card, "abc", 5)
        self.assertIn("developer wants to change title to \"New\" of pull request #7 on acme/widgets", text)
        text = notify.summary({**card, "title": None, "body": "b"}, "abc", 5)
        self.assertIn("change the body of pull request #7", text)
        text = notify.summary({**card, "body": "b"}, "abc", 5)
        self.assertIn("title to \"New\" and the body of pull request #7", text)


if __name__ == "__main__":
    import unittest
    unittest.main()
