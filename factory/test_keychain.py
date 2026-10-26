"""Tests for factory/keychain.py. Ported from keychain_has, keychain_lookup and
keychain_search_empty (scripts/lib/secrets.sh, now gone); lookup, store, delete and the Credential
Manager scripts are tested in factory/test_secrets.py. Each check runs keychain.has on tool stubs
(secret-tool, security, powershell.exe on a private PATH) and requires the status the bash
keychain_has gave on the same stubs. The value a stub returns is never printed.
"""

import os
import unittest

from factory import keychain
from factory.test_preflight import PreflightTest

SERVICE = "factory-jira"


class KeychainTest(PreflightTest):
    def setUp(self):
        super().setUp()
        self.private_path()

    def both(self, service=SERVICE, **env):
        """keychain.has(SERVICE) with ENV; each test asserts the status the bash gave."""
        self.environ.update(env)
        return keychain.has(self.ctx(), service)


class NamesTest(KeychainTest):
    def test_the_names_are_the_bash_ones(self):
        # FACTORY_KEYCHAIN_NAMES of the deleted secrets.sh, in its order.
        self.assertEqual(keychain.KEYCHAIN_NAMES, ("jira", "gemini", "npm-registry", "gitlab", "approvals-webhook", "github"))


class SecretToolTest(KeychainTest):
    def test_a_stored_value_is_found(self):
        self.tool("secret-tool", 'echo secret-value\n')
        self.assertEqual(self.both(), keychain.FOUND)

    def test_a_silent_failure_with_an_empty_search_is_not_stored(self):
        self.tool("secret-tool", 'case "$1" in lookup) exit 1;; search) exit 0;; esac\n')
        self.assertEqual(self.both(), keychain.NOT_STORED)

    def test_a_silent_failure_with_a_listed_item_is_undetermined(self):
        self.tool("secret-tool", 'case "$1" in lookup) exit 1;; search) echo "[/org/x/1]"; echo "label = x";; esac\n')
        self.assertEqual(self.both(), keychain.UNDETERMINED)

    def test_a_search_that_fails_or_complains_is_undetermined(self):
        for search in ("exit 2", "echo oops >&2", "echo oops >&2; exit 1"):
            self.tool("secret-tool", 'case "$1" in lookup) exit 1;; search) %s;; esac\n' % search)
            self.assertEqual(self.both(), keychain.UNDETERMINED, search)

    def test_an_error_on_stderr_or_another_status_is_undetermined(self):
        for body in ("echo locked >&2; exit 1", "exit 2", "echo v; exit 1", "exit 0", "echo v >&2; exit 0"):
            self.tool("secret-tool", body + "\n")
            self.assertEqual(self.both(), keychain.UNDETERMINED, body)

    def test_a_value_with_a_trailing_newline_only_counts_as_empty(self):
        self.tool("secret-tool", 'echo\n')
        self.assertEqual(self.both(), keychain.UNDETERMINED)


class MacosTest(KeychainTest):
    def test_only_existence_is_asked(self):
        self.set_platform("Darwin")
        for code, want in ((0, keychain.FOUND), (44, keychain.NOT_STORED), (1, keychain.UNDETERMINED), (36, keychain.UNDETERMINED)):
            self.tool("security", 'echo "$@" > "%s/args"\nexit %d\n' % (self.tmp, code))
            self.assertEqual(self.both(), want, code)
        with open(os.path.join(self.tmp, "args")) as f:
            self.assertEqual(f.read().strip(), "find-generic-password -s %s" % SERVICE)


class NoKeychainTest(KeychainTest):
    def test_none_means_not_stored(self):
        self.assertEqual(self.both(FACTORY_KEYCHAIN="none"), keychain.NOT_STORED)
        self.assertEqual(self.both(), keychain.NOT_STORED)


class WincredTest(KeychainTest):
    def powershell(self, body):
        path = self.tool("powershell", body)
        return self.both(FACTORY_KEYCHAIN="wincred", FACTORY_POWERSHELL=path)

    def test_lookup_results(self):
        self.assertEqual(self.powershell("printf value\n"), keychain.FOUND)
        self.assertEqual(self.powershell("exit 0\n"), keychain.NOT_STORED)
        self.assertEqual(self.powershell("exit 1\n"), keychain.UNDETERMINED)
        self.assertEqual(self.powershell("echo boom >&2; exit 5\n"), keychain.UNDETERMINED)

    def test_the_script_is_the_lookup_for_that_service_and_the_value_stays_off_argv(self):
        self.powershell('printf "%%s\\n" "$@" > "%s/args"\nprintf value\n' % self.tmp)
        with open(os.path.join(self.tmp, "args")) as f:
            args = f.read().split("\n")
        self.assertEqual(args[:3], ["-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("$v.Retrieve('%s', 'factory')" % SERVICE, "\n".join(args))
        self.assertIn("# factory-keychain op=lookup", args[3])

    def test_only_our_own_service_names_are_embedded(self):
        for service in ("jira", "factory-nope", "factory-jira'; evil", "other-jira", ""):
            self.assertEqual(self.both(service, FACTORY_KEYCHAIN="wincred", FACTORY_POWERSHELL=self.tool("powershell", "printf value\n")),
                             keychain.UNDETERMINED, service)

    def test_without_powershell_it_is_undetermined(self):
        self.assertEqual(self.both(FACTORY_KEYCHAIN="wincred"), keychain.UNDETERMINED)


if __name__ == "__main__":
    unittest.main()
