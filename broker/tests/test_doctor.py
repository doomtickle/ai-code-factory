"""passdesk doctor: every check, against temp policy, state, workspace and secrets."""

import json
import os
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from passdesk import doctor
from passdesk.core import config, credentials

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

    def all_text(self, report):
        """Every piece of text the report would print: findings and fix lines."""
        out = []
        for _, _, text, fix in report.items:
            out.append(text)
            out.extend(fix)
        return out

    def ordered_sections(self, report):
        """Section headings in the order `render` would print them (consecutive
        duplicates collapsed, as `render` only prints a heading when the
        section changes)."""
        out = []
        for sec, _, _, _ in report.items:
            if not out or out[-1] != sec:
                out.append(sec)
        return out


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

    def test_approvals_is_a_known_table(self):
        self.policy(GOOD_POLICY + '\n[approvals]\ntools = ["git_push"]\n'
                    "expire_seconds = 1800\npatch_max_bytes = 262_144\n")
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertEqual(report.messages(doctor.WARN), [])
        # The retired setting: one warning saying what to do, not "a typo?".
        self.policy(GOOD_POLICY + '\n[approvals]\ntools = ["git_push"]\nhold_seconds = 45\n')
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertNoFailures(report)
        self.assertEqual(report.messages(doctor.WARN),
                         ["[approvals].hold_seconds is no longer used: a held write is staged at once and the gateway "
                          "makes it itself once approved"])
        for bad in ("1", '"big"', "99_000_000"):
            self.policy(GOOD_POLICY + f"\n[approvals]\npatch_max_bytes = {bad}\n")
            report = doctor.Report()
            doctor.check_policy(report, self.policy_path)
            self.assertWarns(report, "[approvals].patch_max_bytes")

    def test_approvals_tools_are_checked(self):
        self.policy(GOOD_POLICY + '\n[approvals]\ntools = "git_push"\n')
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertFails(report, "[approvals].tools must be a list")
        self.policy(GOOD_POLICY + '\n[approvals]\ntools = ["git_push", "git_psuh"]\nexpire_seconds = -1\n')
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertWarns(report, "names git_psuh, which is not a tool")
        self.assertFails(report, "[approvals].expire_seconds = -1")
        self.policy(GOOD_POLICY + '\n[approvals]\ntools = ["git_push"]\nexpire_seconds = 2_000_000\n')
        report = doctor.Report()
        doctor.check_policy(report, self.policy_path)
        self.assertWarns(report, "[approvals].expire_seconds = 2000000 is outside 60 to 604800")
        self.assertTrue(any("uses 604800 instead" in line for line in self.all_text(report)))

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


class EgressGuardPolicyChecks(DoctorBase):
    def test_bad_allowlist_entry_fails_in_either_mode(self):
        self.policy(GOOD_POLICY + '\n[egress_guard]\nallow_hosts = ["*.cursor.sh"]\n')
        for deployment in ("sbx", "standalone"):
            with mock.patch.object(config, "DEPLOYMENT", deployment), mock.patch.object(config, "BIND_HOST", "127.0.0.1"):
                report = self.run_doctor()
            self.assertFails(report, "exact DNS names only")

    def test_standalone_lists_the_allowlist_and_warns_when_empty(self):
        self.policy(GOOD_POLICY + '\n[egress_guard]\nallow_hosts = ["api.anthropic.com"]\n')
        with mock.patch.object(config, "DEPLOYMENT", "standalone"), mock.patch.object(config, "BIND_HOST", "127.0.0.1"):
            report = self.run_doctor()
            self.assertTrue(any("egress guard forwards CONNECT to port 443 on 1 host(s): api.anthropic.com" in m
                                for m in report.messages(doctor.INFO)))
            self.policy(GOOD_POLICY + "\n[egress_guard]\nallow_hosts = []\n")
            report = self.run_doctor()
            self.assertWarns(report, "[egress_guard].allow_hosts is empty")
            self.policy(GOOD_POLICY)  # no table at all: this deployment doesn't run the guard; nothing to say
            report = self.run_doctor()
        self.assertFalse(any("egress guard" in t.lower() for t in self.all_text(report)))


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

    def test_secrets_finds_a_docker_secret_when_the_configured_path_is_missing(self):
        # The gap this fixes: before, check_secrets only looked at the three
        # configured paths, so a credential delivered only as a Docker secret
        # (or via PASSDESK_SECRET_<NAME>_FILE) was reported absent.
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        config.JIRA_SECRET_PATH.parent.mkdir(parents=True)  # "inside the sandbox"
        self.write_secret(credentials.DOCKER_SECRETS_DIR / "jira", "me@example.com:tok")
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertNoFailures(report)
        self.assertFalse(any("no jira credential" in m for m in report.messages(doctor.WARN)))
        self.assertIn("jira credential present", report.messages(doctor.OK))

    def test_secrets_finds_an_env_file_secret_when_the_configured_path_is_missing(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        config.JIRA_SECRET_PATH.parent.mkdir(parents=True)
        other = self.root / "elsewhere" / "jira"
        self.write_secret(other, "me@example.com:tok")
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_JIRA_FILE": str(other)}):
            report = doctor.Report()
            doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertNoFailures(report)
        self.assertIn("jira credential present", report.messages(doctor.OK))

    def test_secrets_reports_a_credential_error_without_raising(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        config.JIRA_SECRET_PATH.parent.mkdir(parents=True)
        old = config.CREDENTIAL_PROVIDERS
        self.addCleanup(setattr, config, "CREDENTIAL_PROVIDERS", old)
        config.CREDENTIAL_PROVIDERS = "vault"
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertFails(report, "unknown credential provider 'vault'")

    def test_secrets_never_prints_the_value(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        config.JIRA_SECRET_PATH.parent.mkdir(parents=True)
        self.write_secret(credentials.DOCKER_SECRETS_DIR / "jira", "me@example.com:super-secret-token")
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertFalse(any("super-secret-token" in t for t in self.all_text(report)))

    def test_secrets_checks_jira_format_on_a_value_found_via_the_fallback(self):
        # Before, the format check only ran against the configured path; a
        # bad-shaped value delivered only as a Docker secret (or via
        # PASSDESK_SECRET_JIRA_FILE) was reported "present" uncritically.
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        config.JIRA_SECRET_PATH.parent.mkdir(parents=True)
        self.write_secret(credentials.DOCKER_SECRETS_DIR / "jira", "not-the-right-shape")
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertFails(report, "not in the form you@example.com:TOKEN")
        self.assertFalse(any("jira credential present" in m for m in report.messages(doctor.OK)))

    def test_secrets_unusable_fallback_credential_warns_without_auditing(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        config.JIRA_SECRET_PATH.parent.mkdir(parents=True)
        (credentials.DOCKER_SECRETS_DIR / "jira").mkdir(parents=True)  # a directory: refused as unusable
        report = doctor.Report()
        doctor.check_secrets(report, pol, granted={"jira_my_issues", "npm_fetch"})
        self.assertWarns(report, "is not a regular file")
        self.assertFalse((config.STATE / "audit.jsonl").exists())


class StandaloneChecks(DoctorBase):
    """BROKER_DEPLOYMENT=standalone: check_standalone replaces the sbx kit
    checks (doomtickle/passdesk-ops#53)."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, config, "DEPLOYMENT", config.DEPLOYMENT)
        config.DEPLOYMENT = "standalone"
        self.addCleanup(setattr, config, "CREDENTIAL_PROVIDERS", config.CREDENTIAL_PROVIDERS)
        self.addCleanup(setattr, config, "BIND_HOST", config.BIND_HOST)
        config.BIND_HOST = "127.0.0.1"  # off by default in these tests; the TLS tests set it explicitly

    def test_run_uses_standalone_checks_instead_of_egress(self):
        self.policy()
        self.state()
        report = self.run_doctor()
        sections = [sec for sec, _, _, _ in report.items]
        self.assertNotIn("Egress", sections)
        self.assertIn("Standalone", sections)

    def test_github_credential_absent_warns_when_wanted(self):
        # Default providers ("proxy,file,env-file"): proxy still owns "github"
        # until W2.2 drops it for standalone, so this is "absent" today even
        # with files in place -- matching what credentials.get("github") returns.
        report = doctor.Report()
        doctor.check_standalone(report, granted={"git_fetch"})
        self.assertWarns(report, "no github credential")

    def test_github_credential_absent_and_not_wanted_is_only_info(self):
        report = doctor.Report()
        doctor.check_standalone(report, granted=set())
        self.assertTrue(any("no github credential (not needed" in m for m in report.messages(doctor.INFO)))
        self.assertNoFailures(report)
        self.assertEqual(report.messages(doctor.WARN), [])

    def test_github_credential_present_once_proxy_is_dropped(self):
        config.CREDENTIAL_PROVIDERS = "file,env-file"
        self.write_secret(credentials.secrets_dir() / "github", "ghp_" + "a" * 36)
        report = doctor.Report()
        doctor.check_standalone(report, granted={"git_fetch", "git_push"})
        self.assertTrue(any("github credential present" in m for m in report.messages(doctor.OK)))
        self.assertFalse(any("ghp_" in t for t in self.all_text(report)))

    def test_github_credential_error_is_reported_without_raising(self):
        config.CREDENTIAL_PROVIDERS = "proxy,vault"
        report = doctor.Report()
        doctor.check_standalone(report, granted={"git_push"})
        self.assertFails(report, "unknown credential provider 'vault'")

    def test_tls_check_runs_in_standalone_section(self):
        # The TLS check itself is tests/test_tls.py's subject; here, only
        # that check_standalone runs it: quiet on loopback, a failure off it.
        config.BIND_HOST = "127.0.0.1"
        report = doctor.Report()
        doctor.check_standalone(report, granted=set())
        self.assertFalse(any("TLS" in t for t in self.all_text(report)))
        config.BIND_HOST = "0.0.0.0"
        report = doctor.Report()
        doctor.check_standalone(report, granted=set())
        self.assertFails(report, "without TLS: the server refuses to start")

    def test_exchange_directory_writable(self):
        report = doctor.Report()
        doctor.check_standalone(report, granted=set())
        self.assertTrue(any("is writable" in m for m in report.messages(doctor.OK)))
        self.assertNoFailures(report)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores mode bits")
    def test_exchange_directory_not_writable_fails(self):
        exchange = config.WORKSPACE / config.EXCHANGE_DIR
        exchange.mkdir()
        os.chmod(exchange, 0o500)
        try:
            report = doctor.Report()
            doctor.check_standalone(report, granted=set())
            self.assertFails(report, "is not writable")
        finally:
            os.chmod(exchange, 0o700)

    def test_missing_workspace_is_silently_skipped(self):
        # check_workspace already reports the missing workspace; check_standalone
        # must not also raise or double-report it.
        import shutil
        shutil.rmtree(config.WORKSPACE)
        report = doctor.Report()
        doctor.check_standalone(report, granted=set())
        self.assertEqual(report.messages(doctor.FAIL), [])

    def test_symlinked_exchange_dir_is_skipped_not_reported_writable(self):
        # check_workspace already fails a symlinked .factory/; check_standalone
        # must not also call it writable (os.lstat, not .is_dir(), which follows
        # the link).
        real = self.root / "elsewhere"
        real.mkdir()
        os.symlink(real, config.WORKSPACE / config.EXCHANGE_DIR)
        report = doctor.Report()
        doctor.check_standalone(report, granted=set())
        self.assertEqual(report.messages(doctor.FAIL), [])
        self.assertFalse(any("is writable" in m for m in report.messages(doctor.OK)))

    def test_incoming_and_outgoing_writability_are_checked(self):
        exchange = config.WORKSPACE / config.EXCHANGE_DIR
        (exchange / "incoming").mkdir(parents=True)
        (exchange / "outgoing").mkdir(parents=True)
        report = doctor.Report()
        doctor.check_standalone(report, granted=set())
        oks = report.messages(doctor.OK)
        self.assertTrue(any(str(exchange / "incoming") in m for m in oks))
        self.assertTrue(any(str(exchange / "outgoing") in m for m in oks))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores mode bits")
    def test_outgoing_not_writable_fails(self):
        exchange = config.WORKSPACE / config.EXCHANGE_DIR
        outgoing = exchange / "outgoing"
        outgoing.mkdir(parents=True)
        os.chmod(outgoing, 0o500)
        try:
            report = doctor.Report()
            doctor.check_standalone(report, granted=set())
            self.assertFails(report, f"{outgoing} is not writable")
        finally:
            os.chmod(outgoing, 0o700)

    def test_symlinked_incoming_is_skipped(self):
        exchange = config.WORKSPACE / config.EXCHANGE_DIR
        exchange.mkdir()
        real = self.root / "elsewhere"
        real.mkdir()
        os.symlink(real, exchange / "incoming")
        report = doctor.Report()
        doctor.check_standalone(report, granted=set())
        self.assertEqual(report.messages(doctor.FAIL), [])
        self.assertFalse(any("incoming" in m for m in report.messages(doctor.OK)))

    def test_egress_info_line_and_kit_ignored_warning(self):
        self.policy()
        pol = doctor.policy_mod.load_policy(self.policy_path)
        report = doctor.Report()
        doctor.check_standalone(report, granted=set(), pol=pol)
        self.assertTrue(any("host(s) reachable under this policy" in m for m in report.messages(doctor.INFO)))
        self.assertEqual(report.messages(doctor.WARN), [])
        report = doctor.Report()
        doctor.check_standalone(report, granted=set(), pol=pol, kits=[self.root / "spec.yaml"])
        self.assertWarns(report, "--kit is ignored in standalone mode")

    def test_run_warns_when_kit_passed_in_standalone(self):
        self.policy()
        self.state()
        kit = self.root / "spec.yaml"
        kit.write_text(KIT)
        report = self.run_doctor(kits=[kit])
        self.assertWarns(report, "--kit is ignored in standalone mode")

    def test_github_hint_names_proxy_under_the_default_order(self):
        # Default order "proxy,file,env-file": proxy answers "github" first,
        # so a token delivered via file/env-file is never read.
        report = doctor.Report()
        doctor.check_standalone(report, granted={"git_fetch"})
        self.assertWarns(report, "proxy")
        self.assertTrue(any("PASSDESK_CREDENTIAL_PROVIDERS" in t for t in self.all_text(report)))

    def test_github_hint_is_generic_once_proxy_no_longer_precedes_other_providers(self):
        config.CREDENTIAL_PROVIDERS = "file,env-file"  # no proxy at all
        report = doctor.Report()
        doctor.check_standalone(report, granted={"git_fetch"})
        self.assertWarns(report, "no github credential")
        self.assertFalse(any("proxy" in m for m in report.messages(doctor.WARN)))

    def test_loopback_detection_covers_ipv6_and_localhost(self):
        for host in ("::1", "localhost"):
            config.BIND_HOST = host
            report = doctor.Report()
            doctor.check_standalone(report, granted=set())
            self.assertFalse(any("TLS" in t for t in self.all_text(report)), host)

    def test_unusable_credential_file_warns_without_auditing(self):
        # A docker-secret directory (not a regular file) for github: refused
        # by read_secret_file as "unusable." The doctor must show the reason
        # as a warning and must not write it to the audit log (doctor.py:7,
        # "it changes nothing").
        config.CREDENTIAL_PROVIDERS = "file,env-file"
        (credentials.DOCKER_SECRETS_DIR / "github").mkdir(parents=True)
        report = doctor.Report()
        doctor.check_standalone(report, granted={"git_fetch"})
        self.assertWarns(report, "is not a regular file")
        self.assertFalse((config.STATE / "audit.jsonl").exists())

    def test_bad_provider_order_is_reported_once_not_per_credential(self):
        config.CREDENTIAL_PROVIDERS = "vault"
        config.JIRA_SECRET_PATH.parent.mkdir(parents=True)  # "inside the sandbox": every credential is checked
        self.policy()
        self.state(grants={"developer": ["git_fetch", "jira_my_issues", "gemini_flash_query", "npm_fetch"]})
        report = self.run_doctor()
        fails = [m for m in report.messages(doctor.FAIL) if "unknown credential provider 'vault'" in m]
        self.assertEqual(len(fails), 1, report.messages(doctor.FAIL))


class SbxModeUnaffected(DoctorBase):
    """sbx mode (BROKER_DEPLOYMENT=sbx, the default) must keep running the
    sbx kit checks and must never run check_standalone; pins today's shape
    for a healthy deployment (doomtickle/passdesk-ops#53)."""

    def test_sbx_mode_runs_egress_not_standalone(self):
        self.assertEqual(config.DEPLOYMENT, "sbx")
        self.policy()
        self.state()
        report = self.run_doctor()
        sections = [sec for sec, _, _, _ in report.items]
        self.assertIn("Egress", sections)
        self.assertNotIn("Standalone", sections)

    def test_sbx_mode_pinned_report_for_a_healthy_deployment(self):
        self.policy()
        self.state(grants={"developer": ["git_fetch", "git_push", "github_create_pr", "npm_fetch",
                                         "jira_my_issues", "gemini_flash_query"]})
        self.write_secret(config.JIRA_SECRET_PATH, "me@example.com:tok")
        self.write_secret(config.GEMINI_SECRET_PATH, "gem-key")
        self.write_secret(config.NPM_REGISTRY_SECRET_PATH, "npm-tok")
        os.chmod(config.JIRA_SECRET_PATH, 0o600)
        os.chmod(config.GEMINI_SECRET_PATH, 0o600)
        os.chmod(config.NPM_REGISTRY_SECRET_PATH, 0o600)
        report = self.run_doctor()
        self.assertEqual(report.messages(doctor.FAIL), [])
        self.assertEqual(report.messages(doctor.WARN), [])
        self.assertEqual(
            self.ordered_sections(report),
            ["Policy", "Egress", "State", "Workspace", "Credentials", "Approvals", "Plugins"])
        self.assertIn("jira credential present", report.messages(doctor.OK))
        self.assertIn("gemini credential present", report.messages(doctor.OK))
        self.assertIn("npm-registry credential present", report.messages(doctor.OK))


class ApprovalChecks(DoctorBase):
    def pending(self, cid, created, expires):
        d = config.STATE / "approvals" / "pending"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.json").write_text(json.dumps({"tool": "git_push", "id": cid, "nonce": "n",
                                                   "created": created, "expires": expires}))

    def test_off_by_default_is_an_info_with_the_fix(self):
        self.policy()
        self.state()
        report = self.run_doctor()
        self.assertNoFailures(report)
        texts = [t for sec, _, t, _ in report.items if sec == "Approvals"]
        self.assertEqual(len(texts), 1)
        self.assertIn("holds are off", texts[0])
        self.assertTrue(any('tools = ["git_push"' in line for line in self.all_text(report)))

    def test_on_lists_the_tools_the_cards_and_the_webhook(self):
        self.policy(GOOD_POLICY + '\n[approvals]\ntools = ["git_push", "github_create_pr"]\n')
        self.state(grants={"developer": ["git_fetch", "git_push", "github_create_pr"]})  # no npm_fetch: no registry warning
        config.SECRETS_DIR.mkdir()
        hook = config.SECRETS_DIR / "approvals-webhook"
        hook.write_text("https://hooks.example.invalid/x\n")
        os.chmod(hook, 0o600)
        now = time.time()
        self.pending("a" * 16, now - 120, now + 600)
        report = self.run_doctor()
        self.assertNoFailures(report)
        self.assertEqual(report.messages(doctor.WARN), [])
        oks = report.messages(doctor.OK)
        self.assertTrue(any(m.startswith("holds on for git_push, github_create_pr: a held write is staged at once") for m in oks))
        self.assertIn("1 pending approval(s), the oldest 2 min old", oks)
        self.assertFalse(any("of them staged" in t for _, _, t, _ in report.items))
        queued = config.STATE / "approvals" / "queued" / f"{'a' * 16}.json"
        queued.parent.mkdir(parents=True)
        queued.write_text('{"id": "aaaaaaaaaaaaaaaa"}')
        report = self.run_doctor()
        self.assertTrue(any(t.startswith("1 of them staged: the gateway makes") for _, _, t, _ in report.items))
        self.assertIn("approvals-webhook credential present: each new card is posted to it", oks)

    def test_expired_pending_and_a_missing_webhook_are_flagged(self):
        self.policy(GOOD_POLICY + '\n[approvals]\ntools = ["git_push"]\n')
        self.state()
        config.SECRETS_DIR.mkdir()
        now = time.time()
        self.pending("b" * 16, now - 4000, now - 100)
        report = self.run_doctor()
        self.assertNoFailures(report)
        self.assertWarns(report, "1 already expired")
        self.assertTrue(any("no approvals-webhook credential" in t for _, _, t, _ in report.items))

    def test_the_retired_pro_plugin_is_a_failure_with_the_fix(self):
        self.policy()
        self.state()
        config.PLUGINS = "passdesk_pro"
        self.addCleanup(setattr, config, "PLUGINS", "")
        report = self.run_doctor()
        self.assertFails(report, "names passdesk_pro")
        self.assertTrue(any("[approvals].tools" in line for line in self.all_text(report)))


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
