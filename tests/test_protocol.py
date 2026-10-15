"""Authentication, per-principal tool grants, RPC handling and the audit log."""

import hashlib
import json
import unittest
from pathlib import Path

from agentbroker import tools  # noqa: F401 - registers every tool
from agentbroker.core import auth, config, protocol, registry

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


if __name__ == "__main__":
    unittest.main()
