"""Environment-derived settings (core/config.py): defaults, overrides and
the startup validation in config.validate()."""

import importlib
import unittest
from unittest import mock

from passdesk.core import config


class EnvDefaultsAndOverridesTests(unittest.TestCase):
    """BROKER_* -> config.NAME wiring, reloading the module under a patched
    os.environ so the os.environ.get(...) lines themselves run again, then
    reloading once more under the real environment so later tests see the
    same config module state they would have without this test running."""

    def reload_under(self, env: dict) -> None:
        with mock.patch.dict("os.environ", env, clear=False):
            importlib.reload(config)
        self.addCleanup(importlib.reload, config)

    def test_defaults_match_todays_values(self):
        self.reload_under({})
        self.assertEqual(config.DEPLOYMENT, "sbx")
        self.assertEqual(config.SECRETS_DIR, "/etc/factory-broker/secrets")
        self.assertEqual(str(config.JIRA_SECRET_PATH), "/etc/factory-broker/secrets/jira")
        self.assertEqual(str(config.GEMINI_SECRET_PATH), "/etc/factory-broker/secrets/gemini")
        self.assertEqual(str(config.NPM_REGISTRY_SECRET_PATH), "/etc/factory-broker/secrets/npm-registry")
        self.assertEqual(config.REF_NAMESPACE, "refs/factory")
        self.assertEqual(config.DEFAULT_PUSH_BRANCH_PREFIX, "factory/")
        self.assertEqual(config.BIND_HOST, "0.0.0.0")
        self.assertEqual(config.ALLOWED_ORIGINS, frozenset())
        config.validate()  # today's defaults are valid

    def test_secrets_dir_override_moves_all_three_secret_paths(self):
        self.reload_under({"BROKER_SECRETS_DIR": "/run/secrets"})
        self.assertEqual(str(config.JIRA_SECRET_PATH), "/run/secrets/jira")
        self.assertEqual(str(config.GEMINI_SECRET_PATH), "/run/secrets/gemini")
        self.assertEqual(str(config.NPM_REGISTRY_SECRET_PATH), "/run/secrets/npm-registry")

    def test_individual_secret_path_still_overridable_on_top_of_secrets_dir(self):
        self.reload_under({"BROKER_SECRETS_DIR": "/run/secrets", "BROKER_JIRA_SECRET": "/elsewhere/jira-token"})
        self.assertEqual(str(config.JIRA_SECRET_PATH), "/elsewhere/jira-token")
        self.assertEqual(str(config.GEMINI_SECRET_PATH), "/run/secrets/gemini")

    def test_ref_namespace_and_push_prefix_and_deployment_overridable(self):
        self.reload_under({"BROKER_REF_NAMESPACE": "refs/my-gateway", "BROKER_PUSH_PREFIX": "agents/",
                           "BROKER_DEPLOYMENT": "standalone"})
        self.assertEqual(config.REF_NAMESPACE, "refs/my-gateway")
        self.assertEqual(config.DEFAULT_PUSH_BRANCH_PREFIX, "agents/")
        self.assertEqual(config.DEPLOYMENT, "standalone")
        config.validate()

    def test_bind_host_and_allowed_origins_overridable(self):
        self.reload_under({"BROKER_BIND_HOST": "127.0.0.1", "BROKER_ALLOWED_ORIGINS": "https://a.example, https://b.example"})
        self.assertEqual(config.BIND_HOST, "127.0.0.1")
        self.assertEqual(config.ALLOWED_ORIGINS, frozenset({"https://a.example", "https://b.example"}))


class ValidateTests(unittest.TestCase):
    def setUp(self):
        for name in ("DEPLOYMENT", "REF_NAMESPACE", "SECRETS_DIR"):
            self.addCleanup(setattr, config, name, getattr(config, name))

    def test_defaults_are_valid(self):
        config.DEPLOYMENT, config.REF_NAMESPACE, config.SECRETS_DIR = \
            "sbx", "refs/factory", "/etc/factory-broker/secrets"
        config.validate()

    def test_standalone_is_valid(self):
        config.DEPLOYMENT = "standalone"
        config.validate()

    def test_rejects_unknown_deployment(self):
        config.DEPLOYMENT = "production"
        with self.assertRaisesRegex(ValueError, "BROKER_DEPLOYMENT"):
            config.validate()

    def test_rejects_ref_namespace_not_under_refs(self):
        for bad in ["factory", "refs", "refs/", "refs/../heads", "refs//factory", "refs/factory/", "heads/factory"]:
            config.REF_NAMESPACE = bad
            with self.assertRaisesRegex(ValueError, "BROKER_REF_NAMESPACE", msg=bad):
                config.validate()

    def test_accepts_custom_ref_namespace(self):
        config.REF_NAMESPACE = "refs/my-gateway"
        config.validate()

    def test_rejects_secrets_dir_path_traversal(self):
        config.SECRETS_DIR = "/etc/factory-broker/../../etc/secrets"
        with self.assertRaisesRegex(ValueError, "BROKER_SECRETS_DIR"):
            config.validate()

    def test_accepts_custom_secrets_dir(self):
        config.SECRETS_DIR = "/run/secrets"
        config.validate()


if __name__ == "__main__":
    unittest.main()
