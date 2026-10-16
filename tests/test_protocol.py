"""Authentication, per-principal tool grants, RPC handling and the audit log."""

import email
import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import auth, config, protocol, registry

from .base import Base


class ProtocolTests(Base):
    def setUp(self):
        super().setUp()
        (config.STATE / "tokens").mkdir()
        for role, tok in (("developer", b"dev-token"), ("researcher", b"res-token")):
            (config.STATE / "tokens" / f"{role}.sha256").write_text(hashlib.sha256(tok).hexdigest() + "\n")
        (config.STATE / "roles.json").write_text(json.dumps(
            {"developer": list(registry.TOOLS), "researcher": ["web_fetch", "no_such_tool"], "documenter": []}))
        config.POLICY_PATH = Path(__file__).parents[1] / "policy.toml"

    def call(self, role, name, args, mid=1):
        return protocol.handle_rpc({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, role)["result"]

    def test_authenticate_maps_token_to_role(self):
        self.assertEqual(auth.authenticate("Bearer dev-token"), ("developer", None))
        self.assertEqual(auth.authenticate("Bearer res-token"), ("researcher", None))
        for header in [None, "Basic x", "Bearer nope"]:
            role, problem = auth.authenticate(header)
            self.assertIsNone(role)
            self.assertTrue(problem)
            self.assertNotIn("nope", problem)

    def test_tools_list_is_filtered_per_role(self):
        def names(role):
            res = protocol.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, role)["result"]
            return {t["name"] for t in res["tools"]}
        self.assertEqual(names("developer"), set(registry.TOOLS))
        self.assertEqual(names("researcher"), {"web_fetch"})  # unknown names in roles.json are ignored
        self.assertEqual(names("documenter"), set())
        self.assertEqual(names("unknown-role"), set())

    def test_role_cannot_call_tools_outside_its_list(self):
        res = self.call("researcher", "npm_fetch", {})
        self.assertTrue(res["isError"])
        self.assertIn("researcher role may not use npm_fetch", res["content"][0]["text"])
        self.assertIn("roles/researcher/role.conf", res["content"][0]["text"])

    def test_coordinator_deny_message_points_at_broker_conf_not_role_conf(self):
        res = self.call("coordinator", "jira_my_issues", {})
        self.assertTrue(res["isError"])
        text = res["content"][0]["text"]
        self.assertIn("coordinator role may not use jira_my_issues", text)
        self.assertIn("kits/coordinator/broker.conf", text)
        self.assertNotIn("roles/coordinator/role.conf", text)

    def test_rpc_and_audit(self):
        self.assertIsNone(protocol.handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, "developer"))
        res = self.call("developer", "web_fetch", {"url": "https://evil.com"})
        self.assertTrue(res["isError"])
        self.assertIn("DENIED", res["content"][0]["text"])
        res = self.call("developer", "web_fetch", {"url": 5})
        self.assertTrue(res["isError"])
        self.call("researcher", "git_push", {})
        entries = [json.loads(l) for l in (config.STATE / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([e["decision"] for e in entries], ["deny", "deny", "deny"])
        self.assertEqual([e["role"] for e in entries], ["developer", "developer", "researcher"])

    def test_malformed_arguments_are_denied_and_audited_instead_of_crashing(self):
        # "arguments" that isn't a JSON object used to blow up building the
        # audit record itself, before the try/finally that writes it.
        res = self.call("developer", "web_fetch", "not-an-object")
        self.assertTrue(res["isError"])
        self.assertIn("arguments must be a JSON object", res["content"][0]["text"])
        entries = [json.loads(l) for l in (config.STATE / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(entries[-1]["decision"], "deny")
        self.assertEqual(entries[-1]["tool"], "web_fetch")

    def test_git_push_writes_an_intent_audit_record_before_running(self):
        events = []
        real_audit = protocol.audit

        def spy(record):
            events.append(dict(record))
            real_audit(record)
        with mock.patch.object(protocol, "audit", side_effect=spy):
            res = self.call("developer", "git_push",
                            {"repo": "acme/widgets", "branch": "factory/x", "bundle": "x.bundle"})
        self.assertTrue(res["isError"])  # real policy.toml has no [git].repos allowlisted
        intents = [e for e in events if e.get("event") == "intent"]
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["tool"], "git_push")
        self.assertEqual(intents[0]["role"], "developer")
        # The intent record landed before the final allow/deny/error record.
        self.assertNotIn("decision", intents[0])
        self.assertEqual(events[-1]["decision"], "deny")

    def test_read_only_tool_gets_no_intent_record(self):
        events = []
        real_audit = protocol.audit

        def spy(record):
            events.append(dict(record))
            real_audit(record)
        with mock.patch.object(protocol, "audit", side_effect=spy):
            self.call("developer", "web_fetch", {"url": "https://evil.com"})
        self.assertFalse(any(e.get("event") == "intent" for e in events))

    def test_refuses_state_changing_call_when_audit_log_cannot_be_written(self):
        def boom(record):
            if record.get("event") == "intent":
                raise OSError("disk full")
        with mock.patch.object(protocol, "audit", side_effect=boom):
            res = self.call("developer", "git_push",
                            {"repo": "acme/widgets", "branch": "factory/x", "bundle": "x.bundle"})
        self.assertTrue(res["isError"])
        self.assertIn("could not write the audit log", res["content"][0]["text"])


class ContentLengthTests(Base):
    """Handler.do_POST's Content-Length validation (finding 8)."""

    def setUp(self):
        super().setUp()
        (config.STATE / "tokens").mkdir()
        (config.STATE / "tokens" / "developer.sha256").write_text(hashlib.sha256(b"dev-token").hexdigest() + "\n")
        (config.STATE / "roles.json").write_text(json.dumps({"developer": []}))
        config.POLICY_PATH = Path(__file__).parents[1] / "policy.toml"

    def post(self, headers: dict, body: bytes = b"") -> tuple[int, dict | None]:
        handler = protocol.Handler.__new__(protocol.Handler)
        handler.headers = email.message_from_string("".join(f"{k}: {v}\r\n" for k, v in headers.items()))
        handler.rfile = io.BytesIO(body)
        handler.path = "/mcp"
        handler.client_address = ("127.0.0.1", 0)
        sent = []
        handler._send = lambda code, body=None: sent.append((code, body))
        handler.do_POST()
        return sent[0]

    def test_missing_content_length_returns_400(self):
        code, body = self.post({"Authorization": "Bearer dev-token"})
        self.assertEqual(code, 400)

    def test_non_numeric_content_length_returns_400(self):
        code, body = self.post({"Authorization": "Bearer dev-token", "Content-Length": "abc"})
        self.assertEqual(code, 400)

    def test_negative_content_length_returns_400(self):
        code, body = self.post({"Authorization": "Bearer dev-token", "Content-Length": "-1"})
        self.assertEqual(code, 400)

    def test_oversized_content_length_still_returns_413(self):
        code, body = self.post({"Authorization": "Bearer dev-token",
                                "Content-Length": str(config.MAX_REQUEST_BYTES + 1)})
        self.assertEqual(code, 413)

    def test_valid_request_still_works(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        code, body = self.post({"Authorization": "Bearer dev-token", "Content-Length": str(len(payload))}, payload)
        self.assertEqual(code, 200)
        self.assertEqual(body["result"], {})


if __name__ == "__main__":
    unittest.main()
