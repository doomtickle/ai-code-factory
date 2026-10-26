"""Tests for factory/security_init.py (`factory _init-security`, what scripts/01-init-security.sh
runs). Before the bash was replaced, the script and its Python were compared on fixtures
(docs/py-cli-migration.md, step 8 part A): the same status, stdout, stderr and sbx calls.

sbx is a stub: it logs each call and answers from the env vars the test sets.
"""

import json
import os
import sys

from factory import security_init
from factory.context import FactoryError
from factory.fixtures import CommandTest

STUB_SBX = r'''import json, os, sys
a = sys.argv[1:]
E = os.environ
with open(os.path.join(E["STUBS"], "log"), "a") as f:
    f.write(json.dumps(a) + "\n")
if a[:1] == ["version"]:
    print("sbx version: v%s stub" % E["SBX_VERSION"])
elif a[:3] == ["settings", "get", "mcp.forceLocalGateway"]:
    print(E.get("GATEWAY", "true"))
elif a[:2] == ["ls", "-q"]:
    sys.stdout.write(E.get("SANDBOXES", ""))
elif a[:3] == ["policy", "init", "deny-all"]:
    if E.get("INIT") == "already":
        print("global policy already initialized")
        sys.exit(1)
    if E.get("INIT") == "fail":
        print("sbx: cannot write the policy")
        sys.exit(2)
elif a[:3] == ["policy", "check", "network"]:
    if a[3] == "example.com":
        print(E.get("EXAMPLE", "Denied: default deny"))
        sys.exit(1)
    print(E.get("ANTHROPIC", "Allowed: rule"))
elif a[:2] == ["policy", "allow"] and E.get("ALLOW") == "fail":
    sys.exit(4)
elif a[:2] == ["policy", "ls"]:
    print("rules...")
    sys.exit(int(E.get("LS_STATUS", "0")))
'''


class SecurityInitTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(self.stubs)
        self.write("bin/sbx", "#!%s\n%s" % (sys.executable, STUB_SBX), 0o755)
        from factory.preflight import SBX_VERSION
        self.environ.update(STUBS=self.stubs, SBX_VERSION=SBX_VERSION)

    def init(self, **env):
        self.environ.update(env)
        ctx = self.ctx()
        try:
            status = security_init.main(ctx, [])
            error = ""
        except FactoryError as e:
            status, error = 1, str(e)
        return status, self.out.getvalue(), self.err.getvalue(), error

    def calls(self):
        with open(os.path.join(self.stubs, "log")) as f:
            return [c for c in (json.loads(line) for line in f) if c != ["version"]]

    def test_an_established_baseline_is_verified_and_changes_nothing(self):
        status, out, err, _ = self.init()
        self.assertEqual((status, out), (0, "Initializing Docker Sandbox baseline security...\n"
                                            "Security baseline established. Verification:\nrules...\n"))
        self.assertEqual(self.calls(), [
            ["settings", "get", "mcp.forceLocalGateway"], ["policy", "init", "deny-all"],
            ["policy", "check", "network", "example.com"], ["policy", "check", "network", "api.anthropic.com"],
            ["policy", "ls"]])

    def test_a_first_run_enables_the_local_gateway_and_allows_only_anthropic(self):
        status, out, _, _ = self.init(GATEWAY="false", ANTHROPIC="Denied: default deny")
        self.assertEqual(status, 0)
        calls = self.calls()
        self.assertIn(["settings", "set", "mcp.forceLocalGateway", "true"], calls)
        self.assertLess(calls.index(["settings", "set", "mcp.forceLocalGateway", "true"]),
                        calls.index(["daemon", "restart"]))
        allows = [c for c in calls if c[:2] == ["policy", "allow"]]
        self.assertEqual(allows, [["policy", "allow", "network", "api.anthropic.com"]])

    def test_running_sandboxes_stop_the_gateway_change(self):
        status, out, _, error = self.init(GATEWAY="false", SANDBOXES="factory-broker\n")
        self.assertEqual(status, 1)
        self.assertEqual(error, "stop and remove running sandboxes first; enabling the local MCP gateway restarts "
                                "the sbx daemon")
        self.assertEqual(out, "Initializing Docker Sandbox baseline security...\n")
        self.assertNotIn(["daemon", "restart"], self.calls())

    def test_an_already_initialized_policy_is_verified_not_failed(self):
        status, out, _, _ = self.init(INIT="already")
        self.assertEqual(status, 0)
        self.assertIn("Global network policy already initialized; verifying it is deny-all.\n", out)

    def test_another_init_failure_stops_with_what_sbx_said(self):
        status, _, _, error = self.init(INIT="fail")
        self.assertEqual((status, error), (1, "sbx: cannot write the policy"))
        self.assertNotIn(["policy", "ls"], self.calls())

    def test_a_policy_that_is_not_deny_all_is_never_allowed_anything(self):
        status, out, _, error = self.init(EXAMPLE="Allowed: example.com")
        self.assertEqual(status, 1)
        self.assertEqual(error, "global network policy is not deny-all; run 'sbx policy reset' and re-run this script")
        self.assertEqual([c for c in self.calls() if c[:2] == ["policy", "allow"]], [])

    def test_a_failed_allow_stops_with_its_status(self):
        status, out, _, _ = self.init(ANTHROPIC="Denied: default deny", ALLOW="fail")
        self.assertEqual(status, 4)
        self.assertNotIn("Security baseline established", out)

    def test_the_status_of_the_closing_listing_is_the_exit_status(self):
        self.assertEqual(self.init(LS_STATUS="5")[0], 5)

    def test_the_wrong_sbx_stops_before_anything_changes(self):
        status, out, _, error = self.init(SBX_VERSION="0.0.1")
        self.assertEqual(status, 1)
        self.assertIn("required, found '0.0.1'", error)
        self.assertEqual(self.calls(), [])
