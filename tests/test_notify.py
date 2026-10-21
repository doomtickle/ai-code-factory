"""Format and leakage guarantees for core.notify.summary / send.

Pure format tests: no network (the opener's .open is patched) and no audit
writes outside the temp state directory the base fixture sets up.
"""

import json
import unittest
from unittest import mock

from passdesk.core import notify

from .base import Base

SLACK_LIMIT = 3000

GIT_PUSH_CARD = {
    "tool": "git_push",
    "principal": "developer",
    "repo": "acme-org/widgets-monorepo",
    "branch": "factory/add-notification-format-tests",
    "base": "main",
    "head": "a" * 40,
    "commits": 12,
    "file_count": 50,
    "shortstat": "50 files changed, 4821 insertions(+), 963 deletions(-)",
    "files": [f"src/module_{i:03d}/very/deeply/nested/package/path/component_{i:03d}.py" for i in range(50)],
    "bundle_sha256": "b" * 64,
    "secret_scan": "clean",
}

PR_CARD = {
    "tool": "github_create_pr",
    "principal": "developer",
    "repo": "acme-org/widgets-monorepo",
    "head": "factory/notify-format-tests",
    "base": "main",
    "title": "Add notification format tests",
    "body": "",
    "draft": True,
}


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NotifyTestBase(Base):
    def setUp(self):
        super().setUp()
        self.sent = []

        def fake_open(req, timeout=10):
            self.sent.append(req)
            return FakeResponse()

        self.patch = mock.patch.object(notify.NO_REDIRECT_OPENER, "open", fake_open)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def send(self, card, cid="c" * 16, minutes=30, url="https://hooks.example.com/services/T0/B0/XXXX"):
        ok = notify.send(card, cid, minutes, url=url)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        body = json.loads(self.sent[-1].data.decode())
        self.assertEqual(set(body), {"text", "content", "message"})
        self.assertEqual(body["text"], body["content"])
        self.assertEqual(body["content"], body["message"])
        return body["text"]


class SlackLengthLimit(NotifyTestBase):
    def test_git_push_with_many_files_stays_under_slack_limit(self):
        text = self.send(GIT_PUSH_CARD)
        self.assertLess(len(text), SLACK_LIMIT)

    def test_pr_with_long_body_and_title_stays_under_slack_limit(self):
        card = {**PR_CARD, "title": "T" * 500, "body": "x" * 20000}
        text = self.send(card)
        self.assertLess(len(text), SLACK_LIMIT)

    def test_pathological_branch_repo_shortstat_still_capped(self):
        # A branch name, repo name or diff-stat this long should never occur
        # in practice, but none of those fields is length-limited before
        # being interpolated into the summary, so a buggy or hostile caller
        # could otherwise blow well past Slack's 3,000-char limit.
        cid = "c" * 16
        card = {**GIT_PUSH_CARD, "branch": "b" * 5000, "repo": "r" * 5000, "shortstat": "s" * 5000}
        text = self.send(card, cid=cid)
        self.assertLess(len(text), SLACK_LIMIT)
        # Truncation must drop the padded middle, never the card id or the
        # actionable tail the approver needs to act on the card.
        decision_tail = f". Decide within 30 min: factory approvals approve {cid} (or decline)."
        self.assertTrue(text.endswith(decision_tail))
        self.assertTrue(text.startswith(f"Passdesk approval {cid}: "))
        self.assertEqual(text.count(cid), 2)


class NoSecretLeakage(NotifyTestBase):
    def test_gitlab_merge_request_summary(self):
        text = self.send({**PR_CARD, "tool": "gitlab_create_mr", "repo": "gitlab.acme.net/platform/api"})
        self.assertIn("wants to open a draft merge request on gitlab.acme.net/platform/api", text)
        self.sent.clear()
        text = self.send({**PR_CARD, "tool": "gitlab_create_mr", "draft": False})
        self.assertIn("wants to open a merge request", text)

    def test_bundle_digest_never_sent(self):
        digest = "d" * 64
        card = {**GIT_PUSH_CARD, "bundle_sha256": digest, "head": "a" * 40}
        text = self.send(card)
        self.assertNotIn(digest, text)
        self.assertNotIn(digest[:12], text)
        self.assertNotIn(digest[:32], text)

    def test_credential_like_strings_in_pr_body_never_sent(self):
        # Holds only because summary() never reads `body` at all; it guards
        # against body being echoed in the future, not against credentials
        # planted in a field summary() does echo (title, branch, repo, ...).
        # Built from fragments so no credential-shaped literal appears in
        # source, else the broker's push-time secret scan flags this test data.
        secrets = [
            "ghp" + "_" + "A" * 36,
            "github_pat" + "_" + "B" * 70,
            "xoxb" + "-" + "1234567890-1234567890-" + "C" * 24,
            "AKI" + "A" + "D" * 16,
            "-----BEGIN " + "RSA PRIVATE KEY-----" + "\nMIIE" + "E" * 200 + "\n-----END " + "RSA PRIVATE KEY-----",
        ]
        card = {**PR_CARD, "body": "please review:\n" + "\n".join(secrets)}
        text = self.send(card)
        for secret in secrets:
            self.assertNotIn(secret, text)

    def test_webhook_url_never_sent(self):
        url = "https://hooks.slack.com/services/TSECRET/BSECRET/XSECRETPATH12345"
        ok = notify.send(GIT_PUSH_CARD, "c" * 16, 30, url=url)
        self.assertTrue(ok)
        body = json.loads(self.sent[-1].data.decode())
        for value in body.values():
            self.assertNotIn(url, value)
            self.assertNotIn("XSECRETPATH12345", value)


if __name__ == "__main__":
    unittest.main()
