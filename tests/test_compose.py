"""compose.yaml and scripts/isolation-check.py: the settings the Q2
research showed matter, checked as text (the tests can't assume Docker or
a YAML parser; CI validates the file with `docker compose config`)."""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
# Comments stripped, so a comment that names a forbidden setting doesn't count as using it.
COMPOSE = re.sub(r"\s*#[^\n]*", "", (ROOT / "compose.yaml").read_text())
CHECK = ROOT / "scripts" / "isolation-check.py"


def block(service: str) -> str:
    """The text of one service's block (up to the next service at the same indent)."""
    match = re.search(rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|\Z)", COMPOSE, re.MULTILINE | re.DOTALL)
    assert match, service
    return match.group(1)


class NetworkTests(unittest.TestCase):
    def test_agents_network_is_internal_and_gateway_isolated_without_ipv6(self):
        net = re.search(r"^  agents:\n(.*?)(?=^  \S)", COMPOSE, re.MULTILINE | re.DOTALL).group(1)
        self.assertIn("internal: true", net)
        self.assertIn("com.docker.network.bridge.gateway_mode_ipv4: isolated", net)
        self.assertIn("enable_ipv6: false", net)

    def test_agent_is_only_on_the_agents_network_with_no_way_around_it(self):
        agent = block("agent")
        self.assertRegex(agent, r"networks: \[agents\]")
        for forbidden in ("ports:", "extra_hosts", "host-gateway", "docker.sock", "privileged", "network_mode"):
            self.assertNotIn(forbidden, agent, forbidden)
        self.assertIn("HTTPS_PROXY: http://egress-guard:3128", agent)
        self.assertIn("NO_PROXY: passdesk,localhost,127.0.0.1", agent)

    def test_gateway_and_guard_bridge_both_networks(self):
        for service in ("passdesk", "egress-guard"):
            self.assertIn("networks: [agents, egress]", block(service), service)
        self.assertNotIn("ports:", block("passdesk"))
        self.assertIn('BROKER_INSECURE_BIND: "1"', block("passdesk"))

    def test_github_token_is_a_file_secret_for_the_gateway_only(self):
        self.assertIn("secrets: [github]", block("passdesk"))
        self.assertNotIn("secrets:", block("agent"))
        self.assertNotIn("github", block("agent").lower())

    def test_workspace_is_shared_by_gateway_and_agent(self):
        self.assertIn("${PASSDESK_WORKSPACE:-./workspace}:/workspace", block("passdesk"))
        self.assertIn("${PASSDESK_WORKSPACE:-./workspace}:/home/agent/workspace", block("agent"))

    def test_pulled_images_are_pinned_by_digest(self):
        for image in re.findall(r"^\s+image: (\S+)", COMPOSE, re.MULTILINE):
            if image == "passdesk:standalone":
                continue  # built locally from this checkout until #59 publishes it
            self.assertRegex(image, r"@sha256:[0-9a-f]{64}$", image)

    def test_isolation_check_runs_in_the_agents_namespace(self):
        check = block("isolation-check")
        self.assertIn('network_mode: "service:agent"', check)
        self.assertIn("isolation-check.py:/isolation-check.py:ro", check)
        self.assertIn("profiles: [check]", check)


class IsolationCheckScriptTests(unittest.TestCase):
    def test_script_is_stdlib_only_and_parses(self):
        tree = ast.parse(CHECK.read_text())
        imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | \
                   {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertEqual(imported, {"json", "os", "socket", "ssl", "sys", "urllib"})

    def test_script_fails_on_every_escape_the_research_named(self):
        text = CHECK.read_text()
        for probe in ("host.docker.internal", "gateway.docker.internal", "169.254.169.254", "8.8.8.8",
                      "/proc/net/route", "/proc/net/ipv6_route", "AF_INET6", "SOCK_DGRAM"):
            self.assertIn(probe, text, probe)
        self.assertIn("gateway_mode_ipv4: isolated", text)
        self.assertIn('return 1 if failures else 0', text)


if __name__ == "__main__":
    unittest.main()
