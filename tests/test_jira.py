"""jira_my_issues and jira_get_issue."""

import hashlib
import json
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import config, policy, protocol
from passdesk.core.errors import Denied
from passdesk.tools import jira

from .base import JIRA_POLICY, TEST_CLOUD_ID, Base, _FakeHTTPResponse


class JiraTests(Base):
    def setUp(self):
        super().setUp()
        self.write_secret(config.JIRA_SECRET_PATH, "user@example.com:supersecrettoken123")

    def test_jql_status_variants(self):
        self.assertEqual(jira.build_my_issues_jql(None, JIRA_POLICY),
                          'assignee = currentUser() AND statusCategory != Done '
                          'ORDER BY priority DESC, updated DESC')
        self.assertIn('statusCategory = "To Do"', jira.build_my_issues_jql("todo", JIRA_POLICY))
        self.assertIn('statusCategory = "In Progress"', jira.build_my_issues_jql("in_progress", JIRA_POLICY))
        all_jql = jira.build_my_issues_jql("all", JIRA_POLICY)
        self.assertNotIn("statusCategory", all_jql)
        self.assertTrue(all_jql.startswith("assignee = currentUser() ORDER BY"))

    def test_jql_bad_status_denied(self):
        with self.assertRaisesRegex(Denied, "unknown status"):
            jira.build_my_issues_jql("done", JIRA_POLICY)

    def test_jql_projects_clause(self):
        policy = {**JIRA_POLICY, "jira": {**JIRA_POLICY["jira"], "projects": ["PROJ", "OPS"]}}
        jql = jira.build_my_issues_jql(None, policy)
        self.assertIn('project in ("PROJ", "OPS")', jql)

    def test_get_issue_key_regex(self):
        for bad in ["proj-1", "NODASH", "PROJ-", "123-1", "PROJ-x", "PROJ 1-1",
                    "PROJ-1\n", "PROJ-1\nrm -rf /", "PROJ-１"]:  # trailing newline; Unicode digit
            with self.assertRaisesRegex(Denied, "invalid issue key", msg=repr(bad)):
                jira.tool_jira_get_issue({"key": bad}, JIRA_POLICY)

    def test_get_issue_project_allowlist(self):
        policy = {**JIRA_POLICY, "jira": {**JIRA_POLICY["jira"], "projects": ["PROJ"]}}
        with self.assertRaisesRegex(Denied, "not in the broker allowlist"):
            jira.tool_jira_get_issue({"key": "OPS-1"}, policy)

    def test_get_issue_not_assigned_denied(self):
        resp = _FakeHTTPResponse(json.dumps({"issues": []}).encode())
        with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", return_value=resp):
            with self.assertRaisesRegex(Denied, "not assigned to you"):
                jira.tool_jira_get_issue({"key": "PROJ-1"}, JIRA_POLICY)

    def test_empty_cloud_id_denied(self):
        policy = {**JIRA_POLICY, "jira": {**JIRA_POLICY["jira"], "cloud_id": ""}}
        with self.assertRaisesRegex(Denied, "_edge/tenant_info"):
            jira.tool_jira_my_issues({}, policy)
        with self.assertRaisesRegex(Denied, "_edge/tenant_info"):
            jira.tool_jira_get_issue({"key": "PROJ-1"}, policy)

    def test_malformed_cloud_id_denied(self):
        for bad in ["not-a-uuid", "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90; DROP TABLE",
                    "../../../etc/passwd", "3f2a9c1e5b7d4e8a9c217d4e5f6a8b90"]:
            policy = {**JIRA_POLICY, "jira": {**JIRA_POLICY["jira"], "cloud_id": bad}}
            with self.assertRaisesRegex(Denied, "not a valid UUID", msg=bad):
                jira.tool_jira_my_issues({}, policy)

    def test_missing_credential_file_denied(self):
        config.JIRA_SECRET_PATH.unlink()
        with self.assertRaisesRegex(Denied, "no Jira credential"):
            jira.tool_jira_my_issues({}, JIRA_POLICY)

    def test_adf_flattening(self):
        doc = {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello "},
                                              {"type": "text", "text": "world"}]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]},
            ]},
            {"type": "paragraph", "content": [{"type": "text", "text": "line1"},
                                              {"type": "hardBreak"},
                                              {"type": "text", "text": "line2"}]},
        ]}
        text = jira.adf_to_text(doc)
        self.assertIn("Hello world", text)
        self.assertIn("one", text)
        self.assertIn("two", text)
        self.assertIn("line1\nline2", text)

    def test_adf_flattening_handles_none(self):
        self.assertEqual(jira.adf_to_text(None), "")

    def test_my_issues_untrusted_and_fields(self):
        body = {"issues": [{"key": "PROJ-42", "fields": {
            "summary": "Fix the thing", "status": {"name": "In Progress"},
            "priority": {"name": "High"}, "updated": "2026-09-20T00:00:00.000+0000"}}]}
        resp = _FakeHTTPResponse(json.dumps(body).encode())
        with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", return_value=resp) as urlopen:
            out = jira.tool_jira_my_issues({}, JIRA_POLICY)
        self.assertIn("UNTRUSTED CONTENT", out)
        self.assertIn("PROJ-42", out)
        self.assertIn("https://example.atlassian.net/browse/PROJ-42", out)
        req = urlopen.call_args[0][0]
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        self.assertIn("assignee = currentUser()", qs["jql"][0])

    def test_get_issue_untrusted_and_comments(self):
        search_resp = _FakeHTTPResponse(json.dumps({"issues": [{"key": "PROJ-1"}]}).encode())
        issue_body = {"fields": {
            "summary": "Do the thing", "status": {"name": "To Do"}, "priority": {"name": "Low"},
            "description": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Please do it"}]}]},
            "comment": {"comments": [
                {"author": {"displayName": "A"}, "created": "t1",
                 "body": {"type": "doc", "content": [{"type": "paragraph",
                          "content": [{"type": "text", "text": "first"}]}]}},
                {"author": {"displayName": "B"}, "created": "t2",
                 "body": {"type": "doc", "content": [{"type": "paragraph",
                          "content": [{"type": "text", "text": "second"}]}]}},
            ]}}}
        issue_resp = _FakeHTTPResponse(json.dumps(issue_body).encode())
        with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", side_effect=[search_resp, issue_resp]):
            out = jira.tool_jira_get_issue({"key": "PROJ-1"}, JIRA_POLICY)
        self.assertIn("UNTRUSTED CONTENT", out)
        self.assertIn("Please do it", out)
        self.assertIn("first", out)
        self.assertIn("second", out)

    def test_credential_never_appears_in_output_or_audit(self):
        (config.STATE / "tokens").mkdir()
        (config.STATE / "tokens" / "coordinator.sha256").write_text(hashlib.sha256(b"coord-token").hexdigest())
        (config.STATE / "roles.json").write_text(json.dumps({"coordinator": ["jira_my_issues"]}))
        config.POLICY_PATH = Path(__file__).parents[1] / "policy.toml"
        # Give the real on-disk policy.toml's [jira] a usable (valid-UUID) cloud_id for this call.
        real_policy = policy.load_policy()
        real_policy["jira"]["cloud_id"] = TEST_CLOUD_ID
        with mock.patch("passdesk.core.policy.load_policy", return_value=real_policy):
            body = {"issues": [{"key": "PROJ-1", "fields": {"summary": "s"}}]}
            resp = _FakeHTTPResponse(json.dumps(body).encode())
            with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open", return_value=resp):
                res = protocol.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "jira_my_issues", "arguments": {}}},
                                   "coordinator")["result"]
        self.assertFalse(res["isError"])
        self.assertNotIn("supersecrettoken123", res["content"][0]["text"])
        audit_text = (config.STATE / "audit.jsonl").read_text()
        self.assertNotIn("supersecrettoken123", audit_text)
        self.assertNotIn("user@example.com", audit_text)


if __name__ == "__main__":
    unittest.main()
