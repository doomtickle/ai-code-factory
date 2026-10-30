"""Tests for factory/commands/status.py (factory status). Ported from the status parts of
scripts/test_factory_helpers.sh (part 5, the approval counts) and scripts/test_factory_layout.sh
(part 8, local settings), plus the output contract: the exact text, the styles on a terminal and
the exit status. Its output was also compared line for line with the bash it replaced on
the same stubs, in plain and styled form, before that was deleted.

sbx, curl, ps and the keychain tools are stubs on a private PATH, so nothing here needs the real
programs, a network or a keychain.
"""

import os
import shutil
import sys
import unittest

from factory import sbx
from factory.cli import main
from factory.commands import status
from factory.context import FactoryError
from factory.fixtures import REPO, CommandTest

SANDBOXES = ('{"sandboxes":[{"name":"factory-broker","status":"running"},{"name":"factory-developer","status":"stopped"},'
             '{"name":"other","status":"running"},{"name":"factory-reviewer-lane2","status":"creating"}]}')
PROJECT = "/work/project"
APPROVALS_DIR = ("approvals", "pending")


class StatusTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        sysdir = os.path.join(self.tmp, "sys")
        os.makedirs(sysdir)
        os.symlink(sys.executable, os.path.join(sysdir, "python3"))
        for name in ("cat", "sh", "bash", "printf", "echo", "true"):
            if shutil.which(name):
                os.symlink(shutil.which(name), os.path.join(sysdir, name))
        self.environ["PATH"] = self.bin + os.pathsep + sysdir
        self.environ["FACTORY_KEYCHAIN"] = "none"
        self.write("repo/broker/tools.txt", "git_fetch\n")
        self.write("repo/roles/developer/role.conf", 'TEMPLATE="developer"\nWORKSPACE="rw"\nLANES="3"\nBROKER_TOOLS="git_fetch"\n')
        self.write("repo/roles/reviewer/role.conf", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nBROKER_TOOLS="git_fetch"\n')
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS=""\n')
        self.write("state/ai-code-factory/broker/project", PROJECT + "\n")
        self.write("state/ai-code-factory/dispatch/pid", "4242\n")
        self.sbx_stub()
        self.broker(401)
        self.ps("python3 %s/dispatch/server.py" % self.root)

    def sbx_stub(self, version=sbx.SBX_VERSION, listing=SANDBOXES, ls_status=0, login=True):
        listing_file = self.write("sandboxes.json", listing)
        self.tool("sbx", '''case "$1 $2" in
"version ") echo "sbx version: v%s (abc)";;
"ls --json") cat "%s"; exit %d;;
"secret ls") %s;;
esac
''' % (version, listing_file, ls_status, 'echo "(global)  -  anthropic  x"' if login else "true"))

    def broker(self, code):
        self.tool("curl", 'echo "$@" > "%s/curl-args"\nprintf %s\n' % (self.tmp, code))

    def ps(self, command):
        self.tool("ps", 'echo "$@" > "%s/ps-args"\necho "%s"\n' % (self.tmp, command))

    def policy(self, text):
        self.write("config/ai-code-factory/policy.toml", text)

    def approvals(self, line, status_code=0):
        os.makedirs(os.path.join(self.paths.broker_state, *APPROVALS_DIR), exist_ok=True)
        self.write("repo/scripts/approvals.py", "import sys\nprint(%r)\nsys.exit(%d)\n" % (line, status_code))

    def status(self, tty=False):
        ctx = self.ctx(tty)
        ctx.uname = ("Linux", "x86_64")
        rc = status.run(ctx, [])
        return rc, self.out.getvalue(), self.err.getvalue()

    def lines(self, tty=False):
        rc, out, err = self.status(tty)
        self.assertEqual((rc, err), (0, ""))
        return out.split("\n")


class OutputTest(StatusTest):
    def test_the_whole_report(self):
        rc, out, err = self.status()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, (
            "Project   /work/project\n"
            "Local settings none (see config.example/)\n"
            "  ✓ broker running (127.0.0.1:18765)\n"
            "  ✓ dispatch bridge running (pid 4242)\n"
            "  ✓ Claude login stored\n"
            "Coordinator broker tools none\n"
            "Roles developer (3 lanes), reviewer (1 lane)\n"
            "\n"
            "Sandboxes\n"
            "  factory-broker          running\n"
            "  factory-developer       stopped\n"
            "  factory-reviewer-lane2  creating\n"
            "\n"
            "Follow a role: factory logs <role>   Roles: factory roles\n"))

    def test_on_a_terminal_the_styles_are_used(self):
        rc, out, err = self.status(tty=True)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, (
            "\033[1mProject\033[0m   /work/project\n"
            "\033[1mLocal settings\033[0m \033[2mnone (see config.example/)\033[0m\n"
            "  \033[32m✓\033[0m broker running (127.0.0.1:18765)\n"
            "  \033[32m✓\033[0m dispatch bridge running (pid 4242)\n"
            "  \033[32m✓\033[0m Claude login stored\n"
            "\033[1mCoordinator broker tools\033[0m none\n"
            "\033[1mRoles\033[0m developer (3 lanes), reviewer (1 lane)\n"
            "\n"
            "\033[1mSandboxes\033[0m\n"
            "  factory-broker          \033[32mrunning\033[0m\n"
            "  factory-developer       \033[2mstopped\033[0m\n"
            "  factory-reviewer-lane2  creating\n"
            "\n"
            "\033[2mFollow a role: factory logs <role>   Roles: factory roles\033[0m\n"))

    def test_with_nothing_set_up_yet(self):
        os.remove(os.path.join(self.paths.broker_state, "project"))
        os.remove(os.path.join(self.paths.dispatch_state, "pid"))
        self.broker("000")
        self.sbx_stub(listing='{"sandboxes":[]}', login=False)
        rc, out, err = self.status(tty=True)
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith("\033[1mProject\033[0m   \033[2mnone yet (run factory start)\033[0m\n"), out)
        self.assertIn("  \033[31m✗\033[0m broker not running\n  \033[31m✗\033[0m dispatch bridge not running\n", out)
        self.assertIn("  \033[33m!\033[0m no Claude login yet: type /login in the Coordinator (POWER ON on the dashboard), "
                      "then run 'factory start' again\n", out)
        self.assertIn("\033[1mSandboxes\033[0m\n\n\033[2mFollow a role", out)

    def test_it_ignores_its_arguments(self):
        self.assertEqual(main(["status", "--nonsense"], root=self.root, environ=self.environ, stdout=self.ctx().stdout,
                              stderr=self.err), 0)

    def test_no_sbx_stops_with_the_message_bash_gave(self):
        os.remove(os.path.join(self.bin, "sbx"))
        with self.assertRaises(FactoryError) as caught:
            self.status()
        self.assertEqual(str(caught.exception), "'sbx' not found in PATH (see README.md)")
        self.assertEqual(self.out.getvalue(), "")

    def test_the_wrong_sbx_version_stops_it(self):
        self.sbx_stub(version="0.1.0")
        with self.assertRaises(FactoryError) as caught:
            self.status()
        self.assertEqual(str(caught.exception), "sbx %s required, found '0.1.0' (see README.md)" % sbx.SBX_VERSION)

    def test_main_reports_it_as_an_error_with_status_1(self):
        os.remove(os.path.join(self.bin, "sbx"))
        ctx = self.ctx()
        rc = main(["status"], root=self.root, environ=self.environ, stdout=ctx.stdout, stderr=ctx.stderr)
        self.assertEqual((rc, self.out.getvalue(), self.err.getvalue()), (1, "", "error: 'sbx' not found in PATH (see README.md)\n"))


class GitReposTest(StatusTest):
    def test_a_group_entry_is_shown_as_a_group(self):
        self.policy('[gitlab]\nurl = "https://gitlab.example.com/"\n'
                    '[git]\nrepos = ["acme/app", "gitlab.example.com/my-group/*", "gitlab.example.com/g/p"]\n')
        out = "\n".join(self.lines())
        self.assertIn("Git repos acme/app, gitlab.example.com/my-group/* (any project in the group), "
                      "gitlab.example.com/g/p", out)
        self.assertNotIn("[git].repos entry", out)

    def test_a_bad_entry_is_a_warning(self):
        self.policy('[gitlab]\nurl = "https://gitlab.example.com/"\n[git]\nrepos = ["gitlab.example.com/*"]\n')
        self.assertIn('[git].repos entry "gitlab.example.com/*": name a group, not the whole instance',
                      "\n".join(self.lines()))

    def test_no_repos_no_line(self):
        self.assertNotIn("Git repos", "\n".join(self.lines()))


class LocalSettingsTest(StatusTest):
    def local(self):
        return [l for l in self.lines() if l.startswith("Local settings")][0]

    def test_a_lone_layout_file_does_not_count(self):
        self.write("config/ai-code-factory/layout", "web\n")
        self.assertEqual(self.local(), "Local settings none (see config.example/)")

    def test_each_kind_of_local_setting_counts(self):
        shown = "Local settings %s" % self.config
        for item in ("policy.toml", "enabled-roles", "dashboard.toml", "broker-kit", "roles", "projects", "plugins"):
            self.assertFalse(os.path.exists(self.config) and os.listdir(self.config), item)
            path = os.path.join(self.config, item)
            if "." in item or item == "enabled-roles":
                self.write(os.path.join("config", "ai-code-factory", item), "")
            else:
                os.makedirs(path)
            self.assertEqual(self.local(), shown, item)
            if os.path.isdir(path):
                os.rmdir(path)
            else:
                os.remove(path)

    def test_a_folder_named_like_a_file_does_not_count(self):
        os.makedirs(os.path.join(self.config, "policy.toml"))
        self.assertEqual(self.local(), "Local settings none (see config.example/)")

    def test_state_left_inside_the_repo_is_warned_about(self):
        for name in (".broker", ".dispatch", ".roles"):
            os.makedirs(os.path.join(self.root, name))
            self.assertIn("  ! state is still inside the repo; factory start moves it to %s" % self.state, self.lines(), name)
            os.rmdir(os.path.join(self.root, name))
        self.assertNotIn("state is still", "\n".join(self.lines()))


class ServicesTest(StatusTest):
    def test_the_broker_is_asked_on_its_loopback_port_and_401_means_running(self):
        self.lines()
        with open(os.path.join(self.tmp, "curl-args")) as f:
            self.assertEqual(f.read().strip(), "-s -o /dev/null -w %{http_code} -m 2 -X POST -d {} http://127.0.0.1:18765/mcp")
        for code in ("000", "200", "", "4010"):
            self.broker(code)
            self.assertIn("  ✗ broker not running", self.lines(), code)

    def test_the_dispatch_pid_must_belong_to_this_checkouts_bridge(self):
        self.assertIn("  ✓ dispatch bridge running (pid 4242)", self.lines())
        with open(os.path.join(self.tmp, "ps-args")) as f:
            self.assertEqual(f.read().strip(), "-p 4242 -o command=")
        self.ps("python3 /elsewhere/dispatch/server.py")
        self.assertIn("  ✗ dispatch bridge not running", self.lines())
        self.ps("")
        self.assertIn("  ✗ dispatch bridge not running", self.lines())

    def test_an_empty_pid_file_asks_nothing(self):
        self.write("state/ai-code-factory/dispatch/pid", "\n")
        os.remove(os.path.join(self.tmp, "ps-args")) if os.path.exists(os.path.join(self.tmp, "ps-args")) else None
        self.assertIn("  ✗ dispatch bridge not running", self.lines())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ps-args")))

    def test_the_sandbox_list_failing_is_an_error_after_the_header(self):
        self.sbx_stub(ls_status=1)
        with self.assertRaises(FactoryError) as caught:
            self.status()
        self.assertEqual(str(caught.exception),
                         "could not list the sandboxes ('sbx ls --json' failed: exit status 1)")
        self.assertIn("\nSandboxes\n", self.out.getvalue())

    def test_an_unreadable_sandbox_list_is_an_error(self):
        for listing in ("not json", "{}", '{"sandboxes":null}', "[]"):
            self.sbx_stub(listing=listing)
            with self.assertRaises(FactoryError, msg=listing):
                self.status()

    def test_a_missing_status_reads_as_null_like_jq(self):
        self.sbx_stub(listing='{"sandboxes":[{"name":"factory-a"},{"name":"factory-longer","status":"running"},{"status":"running"}]}')
        out = self.lines()
        self.assertIn("  factory-a       null", out)
        self.assertIn("  factory-longer  running", out)


class RolesAndToolsTest(StatusTest):
    def test_roles_are_those_this_machine_runs(self):
        self.write("repo/roles/researcher/role.conf", 'OPTIONAL="1"\nTEMPLATE="developer"\nWORKSPACE="ro"\n')
        self.assertIn("Roles developer (3 lanes), reviewer (1 lane)", self.lines())
        self.write("config/ai-code-factory/enabled-roles", "researcher\n")
        self.assertIn("Roles developer (3 lanes), researcher (1 lane), reviewer (1 lane)", self.lines())
        self.write("config/ai-code-factory/roles/reviewer.conf", 'LANES="2"\n')
        self.assertIn("Roles developer (3 lanes), researcher (1 lane), reviewer (2 lanes)", self.lines())

    def test_no_roles_reads_none(self):
        for role in ("developer", "reviewer"):
            os.remove(os.path.join(self.root, "roles", role, "role.conf"))
        self.assertIn("Roles none", self.lines())

    def test_the_coordinators_tools_are_listed(self):
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS="jira_my_issues web_fetch"\n')
        self.assertIn("Coordinator broker tools jira_my_issues web_fetch", self.lines())


class KeychainWarningsTest(StatusTest):
    JIRA = 'Jira keychain entry ("factory-jira") not found'
    LOCKED = "the keychain is locked or unavailable, so %s couldn't be checked; unlock it and run this again"

    def jira(self):
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS="jira_my_issues"\n')

    def warnings(self):
        return [l for l in self.lines() if l.startswith("  ! ")]

    def test_nothing_is_checked_without_the_tools_being_granted(self):
        self.tool("secret-tool", "exit 9\n")
        self.environ.pop("FACTORY_KEYCHAIN")
        self.policy('[jira]\ncloud_id = "x"\n')
        self.assertEqual(self.warnings(), [])

    def test_jira_needs_a_token_and_a_cloud_id(self):
        self.jira()
        self.assertEqual(self.warnings(), ["  ! " + self.JIRA, "  ! [jira].cloud_id is not set (see README: Enabling Jira)"])
        self.policy('[jira]\ncloud_id = "00000000-0000-0000-0000-000000000000"\n')
        self.assertEqual(self.warnings()[1:], ["  ! [jira].cloud_id is not set (see README: Enabling Jira)"])
        self.policy('[jira]\ncloud_id = "5c1f-0"\n')
        self.assertEqual(self.warnings(), ["  ! " + self.JIRA])

    def test_a_granted_role_tool_counts_too(self):
        self.write("repo/roles/reviewer/role.conf", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nBROKER_TOOLS="git_fetch jira_get_issue"\n')
        self.assertEqual(self.warnings()[0], "  ! " + self.JIRA)

    def test_a_stored_token_is_not_warned_about(self):
        self.jira()
        self.tool("secret-tool", "echo status-token-4f7q\n")
        self.environ.pop("FACTORY_KEYCHAIN")
        rc, out, err = self.status()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([l for l in out.split("\n") if l.startswith("  ! ")],
                         ["  ! [jira].cloud_id is not set (see README: Enabling Jira)"])
        self.assertNotIn("status-token-4f7q", out + err)  # only tested for being non-empty, never shown

    def test_a_locked_keychain_says_so_instead(self):
        self.jira()
        self.tool("secret-tool", "echo locked >&2\nexit 1\n")
        self.environ.pop("FACTORY_KEYCHAIN")
        self.assertEqual(self.warnings()[0], "  ! " + self.LOCKED % "factory-jira")

    def test_the_private_npm_registry_and_gitlab_tokens(self):
        self.write("repo/roles/developer/role.conf", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="npm_fetch gitlab_create_mr"\n')
        self.policy('[packages]\nnpm_private_registry = "https://registry.example"\n[gitlab]\nurl = "https://gitlab.example"\n')
        self.assertEqual(self.warnings(), ['  ! private npm registry keychain entry ("factory-npm-registry") not found',
                                           '  ! GitLab keychain entry ("factory-gitlab") not found'])

    def test_a_private_registry_nobody_can_fetch_from_is_not_checked(self):
        self.policy('[packages]\nnpm_private_registry = "https://registry.example"\n')
        self.assertEqual(self.warnings(), [])

    def test_gitlab_merge_requests_opening_unheld(self):
        self.write("repo/roles/developer/role.conf", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="gitlab_create_mr"\n')
        self.policy('[gitlab]\nurl = "https://gitlab.example"\n[approvals]\ntools = ["git_push"]\n')
        self.assertEqual(self.warnings()[-1], '  ! GitLab merge requests open without approval: '
                         '[approvals].tools is set without "gitlab_create_mr"')
        self.policy('[gitlab]\nurl = "https://gitlab.example"\n[approvals]\ntools = ["git_push", "gitlab_create_mr"]\n')
        self.assertNotIn("without approval", "\n".join(self.warnings()))


class ApprovalsLineTest(StatusTest):
    """Ported from part 5 of scripts/test_factory_helpers.sh: `scripts/approvals.py summary` prints
    "pending oldest expired_unhandled approved running made failed"; pending counts only
    undecided staged cards, so an approved card (the gateway acts on it) is reported apart, never
    as needing the owner. Declined cards are in neither count (scripts/test_approvals.py)."""

    def line(self, summary):
        self.approvals(summary)
        found = [l for l in self.lines() if "Approvals:" in l]
        self.assertLessEqual(len(found), 1)
        return found[0] if found else None

    def test_undecided_cards_count_as_staged(self):
        self.assertEqual(self.line("2 7 0 1 0 0 0"), "Approvals: 2 staged (oldest 7 min); 1 approved, being written - factory approvals")

    def test_approved_and_running_writes_and_done_and_failed_ones(self):
        line = self.line("0 -1 0 1 1 3 2")
        self.assertEqual(line, "Approvals: none staged; 2 approved, being written; 3 done; 2 failed - factory approvals")
        self.assertNotIn("oldest", line)
        self.assertNotIn("retry", line)

    def test_a_failed_write_alone_is_shown(self):
        self.assertEqual(self.line("0 -1 0 0 0 0 1"), "Approvals: none staged; 1 failed - factory approvals")

    def test_no_line_when_nothing_is_staged_or_written(self):
        self.assertIsNone(self.line("0 -1 0 0 0 0 0"))

    def test_short_summaries_still_read(self):
        self.assertEqual(self.line("1 3 0"), "Approvals: 1 staged (oldest 3 min) - factory approvals")
        self.assertEqual(self.line("0 -1 0 1"), "Approvals: none staged; 1 approved, being written - factory approvals")
        self.assertIsNone(self.line(""))

    def test_the_line_sits_between_the_tools_and_the_roles_and_is_bold_on_a_terminal(self):
        self.approvals("1 4 0 0 0 0 0")
        lines = self.lines(tty=True)
        at = lines.index("\033[1mApprovals:\033[0m 1 staged (oldest 4 min) - factory approvals")
        self.assertTrue(lines[at - 1].startswith("\033[1mCoordinator broker tools"))
        self.assertTrue(lines[at + 1].startswith("\033[1mRoles"))

    def test_no_line_when_approvals_are_not_in_use(self):
        self.write("repo/scripts/approvals.py", "print('9 9 0 0 0 0 0')\n")
        self.assertNotIn("Approvals:", "\n".join(self.lines()))

    def test_no_line_when_the_summary_fails(self):
        self.approvals("5 5 0 0 0 0 0", status_code=1)
        self.assertNotIn("Approvals:", "\n".join(self.lines()))

    def test_the_summary_is_asked_of_the_checkouts_script_with_the_broker_state(self):
        self.write("repo/scripts/approvals.py", "import sys\nprint(' '.join(sys.argv[1:]))\n")
        os.makedirs(os.path.join(self.paths.broker_state, *APPROVALS_DIR))
        self.approvals_args = None
        from factory import approvals
        self.assertEqual(approvals.summary(self.ctx()), ["--state", self.paths.broker_state, "summary"])

    def test_the_wording_has_no_held_or_waiting_lane_state(self):
        self.approvals("3 9 1 2 1 4 5")
        out = "\n".join(self.lines()).lower()
        for word in ("held", "waiting", "hold_seconds"):
            self.assertNotIn(word, out)


class WiringTest(unittest.TestCase):
    def test_the_constants_match_common_sh(self):
        with open(os.path.join(REPO, "scripts", "lib", "common.sh")) as f:
            self.assertIn('readonly BROKER_PORT="%d"' % status.BROKER_PORT, f.read())

    def test_the_bash_status_is_gone(self):
        with open(os.path.join(REPO, "bin", "factory")) as f:
            text = f.read()
        self.assertNotIn("cmd_status", text)
        self.assertNotIn("\n    status)", text)


if __name__ == "__main__":
    unittest.main()
