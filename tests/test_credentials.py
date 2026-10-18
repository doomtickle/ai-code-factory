"""core/credentials: provider order, each built-in provider, plugin providers,
name validation, file safety, and that no value leaks."""

import os
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from passdesk.core import config, credentials, plugins
from passdesk.core.credentials import CredentialError
from passdesk.tools import gemini

from .base import POLICY, Base

FIXTURE_VALUE = "fixture-credential-value"


class CredentialTestBase(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, config, "CREDENTIAL_PROVIDERS", config.CREDENTIAL_PROVIDERS)
        config.CREDENTIAL_PROVIDERS = "proxy,file,env-file"
        self.addCleanup(credentials._registered.clear)
        self.secrets = credentials.SECRETS_DIR
        self.docker = credentials.DOCKER_SECRETS_DIR

    def audit_log(self) -> str:
        path = config.STATE / "audit.jsonl"
        return path.read_text() if path.exists() else ""


class OrderTests(CredentialTestBase):
    def test_default_order(self):
        self.assertEqual(credentials.provider_order(), ["proxy", "file", "env-file"])

    def test_config_default_is_todays_behaviour(self):
        env = {k: v for k, v in os.environ.items() if k != "PASSDESK_CREDENTIAL_PROVIDERS"}
        code = "from passdesk.core import config; print(config.CREDENTIAL_PROVIDERS)"
        out = subprocess.run([sys.executable, "-c", code],
                             env=env, cwd=Path(__file__).resolve().parents[1],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "proxy,file,env-file")

    def test_github_is_left_to_the_proxy(self):
        # Even with a github file and env-file present, the default order never returns it.
        self.write_secret(self.secrets / "github", FIXTURE_VALUE)
        self.write_secret(self.docker / "github", FIXTURE_VALUE)
        other = self.secrets.parent / "elsewhere" / "github"
        self.write_secret(other, FIXTURE_VALUE)
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_GITHUB_FILE": str(other)}):
            self.assertIsNone(credentials.get("github"))
            config.CREDENTIAL_PROVIDERS = "file,env-file"
            self.assertEqual(credentials.get("github"), FIXTURE_VALUE)

    def test_first_provider_with_a_value_wins(self):
        self.write_secret(config.GEMINI_SECRET_PATH, "from-file")
        other = self.secrets.parent / "elsewhere" / "gemini"
        self.write_secret(other, "from-env-file")
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_GEMINI_FILE": str(other)}):
            self.assertEqual(credentials.get("gemini"), "from-file")
            config.CREDENTIAL_PROVIDERS = "env-file,file"
            self.assertEqual(credentials.get("gemini"), "from-env-file")
            config.CREDENTIAL_PROVIDERS = "env-file"
            config.GEMINI_SECRET_PATH.unlink()
            self.assertEqual(credentials.get("gemini"), "from-env-file")

    def test_duplicates_and_blanks_in_the_list_are_ignored(self):
        config.CREDENTIAL_PROVIDERS = " file , ,file,proxy "
        self.assertEqual(credentials.provider_order(), ["file", "proxy"])

    def test_unknown_provider_is_an_error(self):
        config.CREDENTIAL_PROVIDERS = "proxy,vault"
        with self.assertRaisesRegex(CredentialError, "unknown credential provider 'vault'"):
            credentials.get("jira")

    def test_nothing_anywhere_is_none(self):
        self.assertIsNone(credentials.get("jira"))
        config.CREDENTIAL_PROVIDERS = ""
        self.write_secret(config.JIRA_SECRET_PATH, "me@example.com:tok")
        self.assertIsNone(credentials.get("jira"))


class FileProviderTests(CredentialTestBase):
    def test_configured_paths_for_todays_credentials(self):
        for name, path in (("jira", config.JIRA_SECRET_PATH), ("gemini", config.GEMINI_SECRET_PATH),
                           ("npm-registry", config.NPM_REGISTRY_SECRET_PATH)):
            config_path = path.parent / "moved" / path.name
            self.write_secret(config_path, f"{name}-value")
            with mock.patch.object(config, {"jira": "JIRA_SECRET_PATH", "gemini": "GEMINI_SECRET_PATH",
                                            "npm-registry": "NPM_REGISTRY_SECRET_PATH"}[name], config_path):
                self.assertEqual(credentials.get(name), f"{name}-value")

    def test_other_names_live_in_the_secrets_directory(self):
        self.write_secret(self.secrets / "approvals-webhook", "https://hooks.example.com/x")
        self.assertEqual(credentials.get("approvals-webhook"), "https://hooks.example.com/x")

    def test_docker_secret_is_the_fallback(self):
        self.write_secret(self.docker / "jira", "docker@example.com:tok")
        self.assertEqual(credentials.get("jira"), "docker@example.com:tok")
        self.write_secret(config.JIRA_SECRET_PATH, "file@example.com:tok")
        self.assertEqual(credentials.get("jira"), "file@example.com:tok")
        self.write_secret(config.JIRA_SECRET_PATH, "\n")  # empty counts as absent
        self.assertEqual(credentials.get("jira"), "docker@example.com:tok")

    def test_read_fresh_every_call(self):
        self.write_secret(config.GEMINI_SECRET_PATH, "old")
        self.assertEqual(credentials.get("gemini"), "old")
        self.write_secret(config.GEMINI_SECRET_PATH, "new")
        self.assertEqual(credentials.get("gemini"), "new")
        config.GEMINI_SECRET_PATH.unlink()
        self.assertIsNone(credentials.get("gemini"))

    def test_symlink_to_a_regular_file_is_followed(self):
        target = self.secrets.parent / "store" / "gemini"
        self.write_secret(target, "linked")
        self.secrets.mkdir()
        os.symlink(target, config.GEMINI_SECRET_PATH)
        self.assertEqual(credentials.get("gemini"), "linked")


class EnvFileProviderTests(CredentialTestBase):
    def test_variable_name(self):
        self.assertEqual(credentials.env_file_var("npm-registry"), "PASSDESK_SECRET_NPM_REGISTRY_FILE")
        self.assertEqual(credentials.env_file_var("jira"), "PASSDESK_SECRET_JIRA_FILE")

    def test_reads_the_named_file(self):
        path = self.secrets.parent / "mounted" / "token"
        self.write_secret(path, "corp-token\n")
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_NPM_REGISTRY_FILE": str(path)}):
            self.assertEqual(credentials.get("npm-registry"), "corp-token")

    def test_empty_or_missing_is_none(self):
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_JIRA_FILE": ""}):
            self.assertIsNone(credentials.get("jira"))
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_JIRA_FILE": str(self.secrets / "absent")}):
            self.assertIsNone(credentials.get("jira"))

    def test_relative_path_is_refused(self):
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_JIRA_FILE": "secrets/jira"}):
            self.assertIsNone(credentials.get("jira"))
        self.assertIn("PASSDESK_SECRET_JIRA_FILE is not an absolute path", self.audit_log())

    def test_a_plain_env_var_is_never_a_secret(self):
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_JIRA": FIXTURE_VALUE, "JIRA": FIXTURE_VALUE,
                                          "PASSDESK_SECRET_JIRA_FILE": FIXTURE_VALUE}):
            self.assertIsNone(credentials.get("jira"))
        self.assertNotIn(FIXTURE_VALUE, self.audit_log())


class ValueTests(CredentialTestBase):
    def test_surrounding_whitespace_is_stripped_as_before(self):
        # The old reader called .strip(); the moved reads keep its behaviour.
        for raw, expected in (("tok\n", "tok"), ("tok\r\n", "tok"), ("tok\n\n", "tok"), ("tok", "tok"),
                              ("  tok  ", "tok"), ("key \n", "key"), (" key", "key"), ("key\t\n", "key"),
                              ("\ttok", "tok"), ("", None), ("\n", None), (" \t\r\n", None)):
            self.write_secret(config.GEMINI_SECRET_PATH, raw)
            self.assertEqual(credentials.get("gemini"), expected, msg=repr(raw))

    def test_whitespace_only_file_falls_through_to_the_next_location(self):
        self.write_secret(config.GEMINI_SECRET_PATH, " \t\n")
        self.write_secret(self.docker / "gemini", "from-docker")
        self.assertEqual(credentials.get("gemini"), "from-docker")

    def test_plugin_values_are_stripped_too(self):
        credentials.register_provider("vault", lambda name: "  plugin-tok\t\n")
        self.assertEqual(credentials.get("gemini"), "plugin-tok")

    def test_control_characters_inside_are_refused_without_the_value(self):
        for raw in (f"{FIXTURE_VALUE}\nsecond-line", f"{FIXTURE_VALUE}\x00", f"{FIXTURE_VALUE}\rinjected: header",
                    f"  {FIXTURE_VALUE}\t{FIXTURE_VALUE}\n"):
            self.write_secret(config.GEMINI_SECRET_PATH, raw)
            self.assertIsNone(credentials.get("gemini"), msg=repr(raw))
        log = self.audit_log()
        self.assertIn("the value contains control characters", log)
        self.assertNotIn(FIXTURE_VALUE, log)


class NameTests(CredentialTestBase):
    def test_traversal_and_bad_names_are_rejected(self):
        self.write_secret(self.secrets.parent / "outside", FIXTURE_VALUE)
        for bad in ("../outside", "..", ".", "a/b", "/etc/passwd", "", "Jira", "npm_registry", "a--b", "-a",
                    "a-", "jira\x00", "jira\n", "j" * 65, None, 7, "jira.txt", "ji ra"):
            with self.assertRaises(CredentialError, msg=repr(bad)):
                credentials.get(bad)

    def test_good_names(self):
        for good in ("jira", "npm-registry", "approvals-webhook", "a1", "j" * 64):
            self.assertIsNone(credentials.get(good))


@unittest.skipIf(os.name != "posix", "POSIX file types")
class FileSafetyTests(CredentialTestBase):
    def assert_refused(self, reason: str):
        self.assertIsNone(credentials.get("gemini"))
        self.assertIn(reason, self.audit_log())
        self.assertNotIn(FIXTURE_VALUE, self.audit_log())

    def test_fifo_is_refused_without_blocking(self):
        self.secrets.mkdir()
        os.mkfifo(config.GEMINI_SECRET_PATH)
        result = []
        t = threading.Thread(target=lambda: result.append(credentials.get("gemini")), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "reading a FIFO blocked")
        self.assertEqual(result, [None])
        self.assertIn("is not a regular file", self.audit_log())

    def test_directory_is_refused(self):
        config.GEMINI_SECRET_PATH.mkdir(parents=True)
        self.assert_refused("is not a regular file")

    def test_oversized_file_is_refused(self):
        self.write_secret(config.GEMINI_SECRET_PATH, FIXTURE_VALUE + "x" * credentials.MAX_SECRET_BYTES)
        self.assert_refused("is larger than")
        self.write_secret(config.GEMINI_SECRET_PATH, "x" * credentials.MAX_SECRET_BYTES)
        self.assertEqual(credentials.get("gemini"), "x" * credentials.MAX_SECRET_BYTES)

    def test_non_utf8_is_refused(self):
        self.secrets.mkdir()
        config.GEMINI_SECRET_PATH.write_bytes(FIXTURE_VALUE.encode() + b"\xff\xfe")
        self.assert_refused("is not UTF-8 text")

    def test_file_in_the_workspace_is_refused(self):
        planted = config.WORKSPACE / "gemini"
        planted.write_text(FIXTURE_VALUE)
        with mock.patch.dict(os.environ, {"PASSDESK_SECRET_GEMINI_FILE": str(planted)}):
            self.assert_refused("is inside the agent workspace")

    def test_symlink_into_the_workspace_is_refused(self):
        planted = config.WORKSPACE / "gemini"
        planted.write_text(FIXTURE_VALUE)
        self.secrets.mkdir()
        os.symlink(planted, config.GEMINI_SECRET_PATH)
        self.assert_refused("is inside the agent workspace")

    def test_device_node_is_refused_without_being_opened(self):
        self.secrets.mkdir()
        os.symlink(os.devnull, config.GEMINI_SECRET_PATH)
        real_open = os.open
        opened = []
        with mock.patch("os.open", side_effect=lambda p, *a, **k: opened.append(p) or real_open(p, *a, **k)):
            self.assert_refused("is not a regular file")
        self.assertNotIn(os.path.realpath(os.devnull), opened)

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "needs /proc")
    def test_swap_into_the_workspace_after_the_check_is_refused(self):
        # The path checks out, but what gets opened is in the workspace (a
        # rename between the realpath check and the open, simulated here).
        planted = config.WORKSPACE / "gemini"
        planted.write_text(FIXTURE_VALUE)
        self.write_secret(config.GEMINI_SECRET_PATH, "decoy")
        real_open = os.open
        with mock.patch("os.open", side_effect=lambda p, *a, **k: real_open(planted, *a, **k)):
            self.assert_refused("is inside the agent workspace")

    def test_without_proc_the_path_check_still_applies(self):
        self.write_secret(config.GEMINI_SECRET_PATH, "tok")
        with mock.patch("os.readlink", side_effect=OSError("no /proc")):
            self.assertEqual(credentials.get("gemini"), "tok")
            planted = config.WORKSPACE / "gemini"
            planted.write_text(FIXTURE_VALUE)
            with mock.patch.dict(os.environ, {"PASSDESK_SECRET_GEMINI_FILE": str(planted)}):
                config.GEMINI_SECRET_PATH.unlink()
                self.assert_refused("is inside the agent workspace")

    def test_refused_file_falls_through_to_the_next_location(self):
        config.GEMINI_SECRET_PATH.mkdir(parents=True)
        self.write_secret(self.docker / "gemini", "from-docker")
        self.assertEqual(credentials.get("gemini"), "from-docker")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode-000 files")
    def test_unreadable_file_is_absent(self):
        self.write_secret(config.GEMINI_SECRET_PATH, FIXTURE_VALUE)
        os.chmod(config.GEMINI_SECRET_PATH, 0)
        self.assertIsNone(credentials.get("gemini"))

    def test_world_readable_file_is_accepted(self):
        # Docker secrets are 0444 by default; the doctor warns about loose modes instead.
        self.write_secret(self.docker / "gemini", "docker-key")
        os.chmod(self.docker / "gemini", 0o444)
        self.assertEqual(credentials.get("gemini"), "docker-key")


class PluginProviderTests(CredentialTestBase):
    def test_registered_provider_is_consulted_after_the_built_ins(self):
        seen = []
        credentials.register_provider("vault", lambda name: seen.append(name) or f"vault-{name}")
        self.assertEqual(credentials.provider_order(), ["proxy", "file", "env-file", "vault"])
        self.assertEqual(credentials.get("jira"), "vault-jira")
        self.write_secret(config.JIRA_SECRET_PATH, "me@example.com:tok")
        self.assertEqual(credentials.get("jira"), "me@example.com:tok")
        self.assertIsNone(credentials.get("github"))  # the proxy still answers first
        self.assertEqual(seen, ["jira"])

    def test_list_places_a_registered_provider(self):
        credentials.register_provider("vault", lambda name: "from-vault")
        self.write_secret(config.JIRA_SECRET_PATH, "me@example.com:tok")
        config.CREDENTIAL_PROVIDERS = "vault,proxy,file"
        self.assertEqual(credentials.get("jira"), "from-vault")
        self.assertEqual(credentials.get("github"), "from-vault")

    def test_none_moves_on_and_injected_stops(self):
        credentials.register_provider("first", lambda name: None)
        credentials.register_provider("injector", lambda name: credentials.INJECTED)
        credentials.register_provider("last", lambda name: "never")
        config.CREDENTIAL_PROVIDERS = "first,injector,last"
        self.assertIsNone(credentials.get("jira"))

    def test_removal(self):
        credentials.register_provider("vault", lambda name: "from-vault")
        credentials.register_provider("vault", None)
        self.assertIsNone(credentials.get("jira"))
        credentials.register_provider("vault", None)  # removing twice is fine

    def test_built_ins_cannot_be_replaced_and_names_are_checked(self):
        for name in ("proxy", "file", "env-file"):
            with self.assertRaises(ValueError):
                credentials.register_provider(name, lambda n: FIXTURE_VALUE)
        for bad in ("Vault", "../x", "", "a_b", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                credentials.register_provider(bad, lambda n: FIXTURE_VALUE)
        self.assertIsNone(credentials.get("jira"))

    def test_failing_provider_error_carries_no_value(self):
        def leaky(name):
            raise RuntimeError(f"lookup failed for value {FIXTURE_VALUE}")
        credentials.register_provider("leaky", leaky)
        with self.assertRaises(CredentialError) as cm:
            credentials.get("jira")
        e = cm.exception
        self.assertIn("'leaky'", str(e))
        self.assertNotIn(FIXTURE_VALUE, str(e) + repr(e))
        self.assertIsNone(e.__cause__)
        self.assertTrue(e.__suppress_context__)

    def test_non_string_value_is_an_error(self):
        credentials.register_provider("bytes", lambda name: FIXTURE_VALUE.encode())
        with self.assertRaises(CredentialError) as cm:
            credentials.get("jira")
        self.assertNotIn(FIXTURE_VALUE, str(cm.exception) + repr(cm.exception))

    def test_plugin_registers_through_passdesk_plugins(self):
        mod = types.ModuleType("fake_credential_plugin")

        def activate():
            credentials.register_provider("keychain", lambda name: "from-keychain" if name == "jira" else None)
            return True
        mod.__dict__["activate"] = activate
        sys.modules["fake_credential_plugin"] = mod
        self.addCleanup(sys.modules.pop, "fake_credential_plugin", None)
        self.addCleanup(setattr, config, "PLUGINS", config.PLUGINS)
        config.PLUGINS = "fake_credential_plugin"
        self.assertEqual(plugins.load(), ["fake_credential_plugin"])
        self.assertEqual(credentials.get("jira"), "from-keychain")
        self.assertIsNone(credentials.get("gemini"))


class NoLeakTests(CredentialTestBase):
    def test_reprs_hold_no_value(self):
        self.write_secret(config.GEMINI_SECRET_PATH, FIXTURE_VALUE)
        self.assertEqual(credentials.get("gemini"), FIXTURE_VALUE)
        self.assertNotIn(FIXTURE_VALUE, repr(vars(credentials)))
        self.assertEqual(repr(credentials.INJECTED), "credentials.INJECTED")

    def test_tool_refuses_a_multiline_key_without_echoing_it(self):
        # Before providers, a key with a newline inside reached http.client,
        # whose "Invalid header value" error quotes the whole header.
        self.write_secret(config.GEMINI_SECRET_PATH, f"{FIXTURE_VALUE}\nsecond-line")
        with mock.patch("passdesk.core.net.NO_REDIRECT_OPENER.open") as urlopen, \
                self.assertRaisesRegex(Exception, "no Gemini API key found") as cm:
            gemini.tool_gemini_flash_query({"prompt": "hello"}, POLICY)
        urlopen.assert_not_called()
        self.assertNotIn(FIXTURE_VALUE, str(cm.exception))
        self.assertNotIn(FIXTURE_VALUE, self.audit_log())


if __name__ == "__main__":
    unittest.main()
