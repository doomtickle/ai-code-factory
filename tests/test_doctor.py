"""passdesk doctor: every check, against temp policy, state, workspace and secrets."""

import json
import os
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from passdesk import doctor
from passdesk.core import config

from .base import Base

REPO_POLICY = Path(__file__).parents[1] / "policy.toml"

GOOD_POLICY = """
[web]
allow_hosts = ["docs.python.org", "*.example.com"]
max_bytes = 1000
[git]
repos = ["acme/widgets"]
protected_branches = ["main"]
push_branch_prefix = "factory/"
default_base = "main"
[packages]
npm_private_registry = "https://acme.jfrog.io/acme/api/npm/npm-local/"
npm_redirect_hosts = ["bucket.s3.amazonaws.com"]
[jira]
site = "acme.atlassian.net"
cloud_id = "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90"
projects = ["PROJ"]
"""

KIT = """
schemaVersion: "2"
kind: mixin
permissions:
  network:
    # comment
    allow:
      - github.com
      - api.github.com   # trailing comment
      - "registry.npmjs.org"
      - pypi.org
      - files.pythonhosted.org
      - docs.python.org
      - generativelanguage.googleapis.com
      - api.atlassian.com
      - acme.jfrog.io
      - bucket.s3.amazonaws.com
    deny:
      - evil.example
"""


class DoctorBase(Base):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name)
        self.policy_path = self.root / "policy.toml"
        os.chmod(config.STATE, 0o700)  # as a deployment creates it; the doctor warns otherwise

    def policy(self, text: str = GOOD_POLICY) -> Path:
        self.policy_path.write_text(text)
        return self.policy_path

    def state(self, tokens=("developer",), grants=None):
        (config.STATE / "tokens").mkdir(exist_ok=True)
        for name in tokens:
            (config.STATE / "tokens" / f"{name}.sha256").write_text("a" * 64 + "\n")
        if grants is None:
            grants = {"developer": ["git_fetch", "git_push", "github_create_pr", "npm_fetch"]}
        (config.STATE / "roles.json").write_text(json.dumps(grants))

    def run_doctor(self, **kw) -> doctor.Report:
        return doctor.run(policy_path=self.policy_path, **kw)

    def assertFails(self, report, fragment):
        self.assertTrue(any(fragment in m for m in report.messages(doctor.FAIL)),
                        f"{fragment!r} not in failures: {report.messages(doctor.FAIL)}")

    def assertWarns(self, report, fragment):
        self.assertTrue(any(fragment in m for m in report.messages(doctor.WARN)),
                        f"{fragment!r} not in warnings: {report.messages(doctor.WARN)}")

    def assertNoFailures(self, report):
        self.assertEqual(report.messages(doctor.FAIL), [])


class PolicyChecks(DoctorBase):
    def test_repo_default_policy_is_clean(self):
        report = doctor.Report()
        doctor.check_policy(report, REPO_POLICY)
        self.assertEqual(report.messages(doctor.FAIL), [])
        self.assertEqual(report.messages(doctor.WARN), [])
        self.assertTrue(any("[git].repos is empty" in m for m in report.messages(doctor.INFO)))

    def test_good_policy_is_clean(self):
        self.policy()
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertNoFailures(report)
        self.assertEqual(report.messages(doctor.WARN), [])

    def test_missing_or_broken_policy(self):
        report = doctor.Report()
        self.assertIsNone(doctor.check_policy(report, self.root / "nope.toml"))
        self.assertFails(report, "policy file not found")
        self.policy("[web\nallow_hosts = 1")
        report = doctor.Report()
        self.assertIsNone(doctor.check_policy(report, self.policy_path))
        self.assertFails(report, "does not parse")

    def test_unknown_tables_and_keys_warn(self):
        self.policy(GOOD_POLICY + "\n[wep]\nx = 1\n[git]\nrepo = []\n".replace("[git]\nrepo = []", ""))
        self.policy(GOOD_POLICY.replace("[web]", "[web]\nmax_byte = 5") + "\n[wep]\nx = 1\n")
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertWarns(report, "[wep] is not a table")
        self.assertWarns(report, "[web].max_byte is not a setting")

    def test_shape_errors(self):
        bad = GOOD_POLICY.replace('repos = ["acme/widgets"]', 'repos = ["https://github.com/acme/widgets"]') \
                         .replace('cloud_id = "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90"', 'cloud_id = "not-a-uuid"') \
                         .replace('npm_private_registry = "https://acme.jfrog.io/acme/api/npm/npm-local/"',
                                  'npm_private_registry = "http://acme.jfrog.io/npm"') \
                         .replace('allow_hosts = ["docs.python.org", "*.example.com"]',
                                  'allow_hosts = ["https://docs.python.org"]') \
                         .replace('projects = ["PROJ"]', 'projects = ["proj"]') \
                         .replace("max_bytes = 1000", "max_bytes = -1")
        self.policy(bad)
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertFails(report, "is not owner/name")
        self.assertFails(report, "cloud_id is not a UUID")
        self.assertFails(report, "npm_private_registry must be an https:// URL prefix")
        self.assertFails(report, "is not a host name")
        self.assertFails(report, "is not a Jira project key")
        self.assertFails(report, "max_bytes must be a positive integer")

    def test_jira_half_configured_and_empty_guards(self):
        self.policy(GOOD_POLICY.replace('cloud_id = "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90"', 'cloud_id = ""')
                    .replace('protected_branches = ["main"]', "protected_branches = []")
                    .replace('push_branch_prefix = "factory/"', 'push_branch_prefix = ""'))
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertFails(report, "must be set together")
        self.assertWarns(report, "protected_branches is empty")
        self.assertWarns(report, "push_branch_prefix is empty")

    def test_push_branch_prefix_missing_key_warns_about_the_fallback(self):
        self.policy(GOOD_POLICY.replace('push_branch_prefix = "factory/"\n', ""))
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertWarns(report, "push_branch_prefix is not set")
        self.assertWarns(report, "BROKER_PUSH_PREFIX")

    def test_credentialed_host_in_web_allowlist_warns(self):
        self.policy(GOOD_POLICY.replace('"docs.python.org",', '"docs.python.org", "api.github.com",'))
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertWarns(report, "api.github.com, which web_fetch refuses anyway")


class ConfigChecks(DoctorBase):
    """doctor.run() validates the environment, not just the policy file."""

    def test_run_reports_a_bad_env_setting_as_a_config_failure(self):
        self.policy()
        old = config.REF_NAMESPACE
        self.addCleanup(setattr, config, "REF_NAMESPACE", old)
        config.REF_NAMESPACE = "refs/heads/factory"
        report = self.run_doctor()
        self.assertFails(report, "BROKER_REF_NAMESPACE")
        self.assertTrue(any(sec == "Config" and lvl == doctor.FAIL and "BROKER_REF_NAMESPACE" in text
                            for sec, lvl, text, _ in report.items))

    def test_run_is_silent_on_config_when_the_environment_is_valid(self):
        self.policy()
        report = self.run_doctor()
        self.assertEqual([text for sec, lvl, text, _ in report.items if sec == "Config" and lvl == doctor.FAIL], [])


class EgressChecks(DoctorBase):
    def test_egress_hosts_cover_every_tool_and_the_policy(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        hosts = doctor.egress_hosts(pol)
        for h in ("github.com", "api.github.com", "registry.npmjs.org", "pypi.org", "files.pythonhosted.org",
                  "generativelanguage.googleapis.com", "api.atlassian.com", "docs.python.org", "*.example.com",
                  "acme.jfrog.io", "bucket.s3.amazonaws.com"):
            self.assertIn(h, hosts)
        self.assertEqual(doctor.egress_hosts(pol, granted={"pip_download"}), ["files.pythonhosted.org", "pypi.org"])

    def test_kit_reader_and_comparison(self):
        kit = self.root / "spec.yaml"
        kit.write_text(KIT)
        self.assertEqual(doctor.kit_allowed_hosts(kit)[:3], ["github.com", "api.github.com", "registry.npmjs.org"])
        self.assertNotIn("evil.example", doctor.kit_allowed_hosts(kit))
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        report = doctor.Report()
        doctor.check_egress(report, pol, [kit])
        self.assertNoFailures(report)
        self.assertWarns(report, "*.example.com")  # wildcards need subdomains listed
        kit.write_text(KIT.replace("      - pypi.org\n", ""))
        report = doctor.Report()
        doctor.check_egress(report, pol, [kit])
        self.assertFails(report, "not in the kit allowlist: pypi.org")


class StateWorkspaceSecretChecks(DoctorBase):
    def test_healthy_state(self):
        self.state()
        (config.STATE / "audit.jsonl").write_text('{"ts": "x", "event": "start"}\n')
        report = doctor.Report()
        granted = doctor.check_state(report)
        self.assertNoFailures(report)
        self.assertEqual(report.messages(doctor.WARN), [])
        self.assertIn("git_push", granted)

    def test_state_problems(self):
        config.STATE.rmdir()
        report = doctor.Report()
        self.assertEqual(doctor.check_state(report), set())
        self.assertFails(report, "state directory does not exist")
        config.STATE.mkdir()
        self.state(tokens=("developer", "orphan"), grants={"developer": ["git_push", "no_such_tool"], "ghost": ["npm_fetch"]})
        (config.STATE / "tokens" / "broken.sha256").write_text("xyz")
        (config.STATE / "audit.jsonl").write_text('{"ok": 1}\n{"truncated": \n')
        (config.STATE / "broker.log").write_text("Traceback\nValueError: boom\n")
        report = doctor.Report()
        doctor.check_state(report)
        self.assertFails(report, "unknown tool(s): no_such_tool")
        self.assertFails(report, "tokens/broken.sha256 does not hold a SHA-256")
        self.assertWarns(report, "ghost has a grant but no token")
        self.assertWarns(report, "orphan has a token but no grant")
        self.assertWarns(report, "last line is not valid JSON")
        self.assertWarns(report, "broker.log is not empty")

    def test_grants_bare_list_and_object_form_both_accepted(self):
        self.state(tokens=("developer", "researcher"), grants={
            "developer": {"tools": ["git_push", "npm_fetch"], "source": "kits/developer/broker.conf"},
            "researcher": ["web_fetch"],
        })
        report = doctor.Report()
        granted = doctor.check_state(report)
        self.assertNoFailures(report)
        self.assertEqual(granted, {"git_push", "npm_fetch", "web_fetch"})
        self.assertTrue(any("git_push, npm_fetch" in m for m in report.messages(doctor.OK)))

    def test_grants_object_form_with_malformed_tools_is_rejected(self):
        self.state(tokens=("developer",), grants={"developer": {"tools": None, "source": "x"}})
        report = doctor.Report()
        granted = doctor.check_state(report)
        self.assertFails(report, "roles.json is invalid")
        self.assertEqual(granted, set())

    def test_grants_path_name_is_reported_when_using_grants_json(self):
        (config.STATE / "tokens").mkdir(exist_ok=True)
        (config.STATE / "tokens" / "developer.sha256").write_text("a" * 64 + "\n")
        (config.STATE / "grants.json").write_text("not json")
        report = doctor.Report()
        doctor.check_state(report)
        self.assertFails(report, "grants.json is invalid")

    def test_workspace_symlinks_are_refused(self):
        report = doctor.Report()
        doctor.check_workspace(report)
        self.assertNoFailures(report)
        real = self.root / "elsewhere"
        real.mkdir()
        (config.WORKSPACE / ".factory").mkdir()
        os.symlink(real, config.WORKSPACE / ".factory" / "outgoing")
        report = doctor.Report()
        doctor.check_workspace(report)
        self.assertFails(report, ".factory/outgoing is a symlink")
        (config.WORKSPACE / ".factory" / "outgoing").unlink()
        (config.WORKSPACE / ".factory").rmdir()
        os.symlink(real, config.WORKSPACE / ".factory")
        report = doctor.Report()
        doctor.check_workspace(report)
        self.assertFails(report, ".factory is a symlink")

    def test_workspace_honors_the_configured_exchange_dir_name(self):
        old = config.EXCHANGE_DIR
        config.EXCHANGE_DIR = ".exchange"
        self.addCleanup(setattr, config, "EXCHANGE_DIR", old)
        report = doctor.Report()
        doctor.check_workspace(report)
        self.assertTrue(any(".exchange/ does not exist yet" in m for m in report.messages(doctor.INFO)))
        real = self.root / "elsewhere"
        real.mkdir()
        os.symlink(real, config.WORKSPACE / ".exchange")
        report = doctor.Report()
        doctor.check_workspace(report)
        self.assertFails(report, ".exchange is a symlink")

    def test_secrets(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertTrue(any("not visible from here" in m for m in report.messages(doctor.INFO)))
        self.assertEqual(report.messages(doctor.WARN), [])
        config.JIRA_SECRET_PATH.parent.mkdir()  # now "inside the sandbox": absence is a finding
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertWarns(report, "no jira credential")
        self.assertWarns(report, "no npm-registry credential")
        self.assertTrue(any("no gemini credential (not needed" in m for m in report.messages(doctor.INFO)))
        self.write_secret(config.JIRA_SECRET_PATH, "just-a-token")
        self.write_secret(config.GEMINI_SECRET_PATH, "")
        self.write_secret(config.NPM_REGISTRY_SECRET_PATH, "tok")
        os.chmod(config.NPM_REGISTRY_SECRET_PATH, 0o644)
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertFails(report, "not in the form you@example.com:TOKEN")
        self.assertFails(report, "is empty")
        self.assertWarns(report, "readable by other users")
        self.write_secret(config.JIRA_SECRET_PATH, "me@example.com:tok")
        config.GEMINI_SECRET_PATH.unlink()  # revoked: absence is only an info line, since nothing granted uses it
        os.chmod(config.NPM_REGISTRY_SECRET_PATH, 0o600)
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertNoFailures(report)
        self.assertIn("jira credential present", report.messages(doctor.OK))


class _Unauthorized(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class LiveChecks(DoctorBase):
    def test_broker_probe_against_a_real_listener(self):
        srv = HTTPServer(("127.0.0.1", 0), _Unauthorized)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            report = doctor.Report()
            doctor.check_live(report, None, f"http://127.0.0.1:{srv.server_port}/mcp")
            self.assertIn(f"broker answers at http://127.0.0.1:{srv.server_port}/mcp and demands a token (401 without one)",
                          report.messages(doctor.OK))
        finally:
            srv.shutdown()
        report = doctor.Report()
        doctor.check_live(report, None, f"http://127.0.0.1:{srv.server_port}/mcp")
        self.assertFails(report, "does not answer")

    def test_github_access_per_repo(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        answers = {}

        def fake_http(url, headers=None, data=None, timeout=10):
            if data is not None:
                return 401, b""
            return answers[url]

        api = "https://api.github.com/repos/acme/widgets"
        cases = [
            ((200, json.dumps({"permissions": {"push": True}}).encode()), doctor.OK, "can read and push"),
            ((200, json.dumps({"permissions": {"push": False, "pull": True}}).encode()), doctor.FAIL, "but not push"),
            ((200, b"{}"), doctor.WARN, "no token was attached"),
            ((404, b""), doctor.FAIL, "404"),
            ((401, b""), doctor.FAIL, "rejects the token"),
        ]
        for answer, level, fragment in cases:
            answers[api] = answer
            report = doctor.Report()
            doctor.check_live(report, pol, "http://127.0.0.1:1/mcp", http=fake_http)
            self.assertTrue(any(fragment in m for m in report.messages(level)), (answer, report.items))

        def down(url, headers=None, data=None, timeout=10):
            raise urllib.error.URLError("no route")
        report = doctor.Report()
        doctor.check_live(report, pol, "http://127.0.0.1:1/mcp", http=down)
        self.assertFails(report, "does not answer")
        self.assertFails(report, "cannot reach api.github.com")

    def test_github_token_hints_drop_sbx_command_in_standalone_deployment(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)

        def fake_http(answer):
            def http(url, headers=None, data=None, timeout=10):
                return (401, b"") if data is not None else answer
            return http

        old = config.DEPLOYMENT
        self.addCleanup(setattr, config, "DEPLOYMENT", old)
        # (200, b"{}"): no token attached (WARN); (401, b""): token rejected (FAIL).
        # Both fix lines name the sbx re-attach command only when DEPLOYMENT=sbx.
        for answer, level in [((200, b"{}"), doctor.WARN), ((401, b""), doctor.FAIL)]:
            for deployment, expect_sbx in [("sbx", True), ("standalone", False)]:
                config.DEPLOYMENT = deployment
                report = doctor.Report()
                doctor.check_live(report, pol, "http://127.0.0.1:1/mcp", http=fake_http(answer))
                fixes = [line for _, lvl, _, fix in report.items if lvl == level for line in fix]
                self.assertEqual(any("sbx secret set github" in line for line in fixes), expect_sbx,
                                 (deployment, answer, fixes))


class CliTests(DoctorBase):
    def test_egress_hosts_flag_and_exit_codes(self):
        self.policy()
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(doctor.main(["--egress-hosts", "--policy", str(self.policy_path)]), 0)
        self.assertIn("api.github.com", out.getvalue().split())
        self.state()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(doctor.main(["--policy", str(self.policy_path), "--no-color"]), 0)
        self.assertIn("No problems found", out.getvalue())
        self.assertIn("==> Policy", out.getvalue())
        (config.STATE / "roles.json").write_text("not json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(doctor.main(["--policy", str(self.policy_path), "--no-color"]), 1)
        self.assertIn("roles.json is invalid", out.getvalue())

    def test_json_flag_matches_the_report(self):
        import contextlib
        import io
        self.policy()
        self.state()
        report = self.run_doctor()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.main(["--json", "--policy", str(self.policy_path)])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(set(data), {"problems", "warnings", "items"})
        self.assertEqual(data["problems"], report.problems)
        self.assertEqual(data["warnings"], report.count(doctor.WARN))
        self.assertEqual([item["section"] for item in data["items"]],
                          [sec for sec, _, _, _ in report.items])
        for item, (sec, level, text, fix) in zip(data["items"], report.items):
            self.assertEqual(item, {"section": sec, "level": level, "text": text, "fix": fix})
        self.assertFalse(any("\033" in item["text"] for item in data["items"]),
                          "--json output must not carry colour codes")

    def test_json_exit_code_matches_human_mode(self):
        import contextlib
        import io
        self.policy()  # no self.state(): leaves real problems so the non-zero path is covered too
        out_json = io.StringIO()
        with contextlib.redirect_stdout(out_json):
            code_json = doctor.main(["--json", "--policy", str(self.policy_path)])
        out_human = io.StringIO()
        with contextlib.redirect_stdout(out_human):
            code_human = doctor.main(["--policy", str(self.policy_path), "--no-color"])
        self.assertEqual(code_json, code_human)
        self.assertNotEqual(code_json, 0)
        data = json.loads(out_json.getvalue())
        self.assertGreater(data["problems"], 0)


if __name__ == "__main__":
    unittest.main()
