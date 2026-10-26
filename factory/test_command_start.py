"""Tests for factory/commands/start.py (factory start, up, and `factory _onboarding-hints`).
Ported from scripts/test_factory_layout.sh (resolve_layout and how a start ends),
scripts/test_factory_restart.sh (restart through the bin/factory shim, and the real
03-stop-factory.sh), the project switch of scripts/test_factory_state.sh (2c) and the
require_project_commits part of scripts/test_factory_helpers.sh, plus the onboarding hints,
each on and off. The output was compared with the bash it replaces on 83 fixtures before
that was deleted (docs/py-cli-migration.md, step 7 part C2).

sbx is the stateful fake of test_launch.py, the keychain a fake secret-tool (or none), and the
broker and dispatch starts are stubs; nothing needs a real sandbox, keychain or network.
"""

import io
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from factory import fingerprints
from factory.cli import COMMANDS, INTERNAL, main
from factory.commands import runstate, start
from factory.context import FactoryError
from factory.fixtures import REPO, Tty
from factory.test_launch import FAKE_SBX, LOGIN, LaunchTest

WEB_WARNING = "warning: the '%s' layout is gone; using the dashboard (web)\n"
SAVED_WARNING = "warning: your saved '%s' layout is gone; using the dashboard (web)\n"


class StartTest(LaunchTest):
    def setUp(self):
        super().setUp()
        self.write("repo/scripts/03-stop-factory.sh", '#!/bin/sh\necho "[stop]"\n', 0o755)
        self.environ["FAKE_SBX"] = self.sbx_state

    def start(self, *args, stdin=None, tty=False):
        ctx = self.ctx(tty)
        if stdin is not None:
            ctx.stdin = stdin
        try:
            rc = COMMANDS["start"](ctx, list(args))
        except FactoryError as e:
            rc = "error: %s" % e
        return rc, self.out.getvalue(), self.err.getvalue()

    def layout_file(self):
        return os.path.join(self.config, "layout")


class LayoutTest(StartTest):
    """resolve_layout, from scripts/test_factory_layout.sh parts 1-4."""

    def resolve(self, flag="", env=""):
        ctx = self.ctx()
        try:
            return start.resolve_layout(ctx, flag, env), self.err.getvalue()
        except FactoryError as e:
            return "error: %s" % e, self.err.getvalue()

    def test_nothing_saved_no_flag_no_env_is_web_quietly_and_writes_nothing(self):
        self.assertEqual(self.resolve(), ("web", ""))
        self.assertFalse(os.path.exists(self.layout_file()))

    def test_web_from_the_flag_or_the_environment(self):
        self.assertEqual(self.resolve("web"), ("web", ""))
        self.assertEqual(self.resolve("", "web"), ("web", ""))
        self.assertFalse(os.path.exists(self.layout_file()))

    def test_the_removed_layouts_fall_back_to_web_with_one_warning(self):
        for old in ("watch", "tabs"):
            self.assertEqual(self.resolve(old), ("web", WEB_WARNING % old))
            self.assertEqual(self.resolve("", old), ("web", WEB_WARNING % old))
            self.assertFalse(os.path.exists(self.layout_file()))
            self.write("config/ai-code-factory/layout", old + "\n")
            self.assertEqual(self.resolve(), ("web", SAVED_WARNING % old))
            self.assertFalse(os.path.exists(self.layout_file()), "the stale file is removed")
            self.assertEqual(self.resolve(), ("web", ""), "the second run is quiet")

    def test_the_flag_wins_over_the_environment(self):
        self.assertEqual(self.resolve("web", "bogus"), ("web", ""))
        self.assertEqual(self.resolve("watch", "web"), ("web", WEB_WARNING % "watch"))

    def test_anything_else_is_a_usage_error_and_a_junk_saved_value_is_ignored(self):
        self.assertEqual(self.resolve("bogus"), ("error: " + start.USAGE, ""))
        self.assertEqual(self.resolve("", "bogus"), ("error: " + start.USAGE, ""))
        self.write("config/ai-code-factory/layout", "bogus\n")
        self.assertEqual(self.resolve(), ("web", ""))
        self.assertTrue(os.path.exists(self.layout_file()))


class UsageTest(StartTest):
    def test_usage_errors(self):
        for args in (("a", "b"), ("-x",), ("--",), ("--layout",), ("--layout", "bogus"), ("dir", "--bogus")):
            self.assertEqual(self.start(*args), ("error: " + start.USAGE, "", ""), args)
        self.assertEqual(self.argvs(), [])

    def test_a_missing_folder(self):
        self.assertEqual(self.start(os.path.join(self.tmp, "nope"))[0],
                         "error: no such directory: %s" % os.path.join(self.tmp, "nope"))

    def test_an_incomplete_checkout_is_refused_first(self):
        os.remove(os.path.join(self.root, "broker", "tools.txt"))
        rc = self.start("-x")[0]
        self.assertIn("broker/tools.txt is missing", rc)

    def test_not_set_up_or_the_wrong_sbx(self):
        self.write("sbx/settings", "false\n")
        self.assertEqual(self.start(self.project)[0],
                         "error: the factory is not set up yet: run 'factory setup' first")
        self.write("sbx/settings", "true\n")
        self.write("sbx/templates", "factory-developer:v2\nfactory-reviewer:v3\nfactory-broker:v3\n")
        self.assertEqual(self.start(self.project)[0],
                         "error: the factory is not set up yet: run 'factory setup' first")
        self.environ["FAKE_VERSION"] = "0.1.0"
        self.assertEqual(self.start(self.project)[0], "error: sbx 0.45.1 required, found '0.1.0' (see README.md)")
        self.assertEqual(self.starts, [])

    def test_start_and_up_are_the_same_command(self):
        self.assertIs(COMMANDS["start"], COMMANDS["up"])
        self.assertIs(INTERNAL["_onboarding-hints"], start.onboarding_hints)


class ProjectCommitsTest(StartTest):
    """require_project_commits, from scripts/test_factory_helpers.sh part 2."""

    def check(self, project):
        try:
            start.require_project_commits(self.ctx(), project)
            return None
        except FactoryError as e:
            return str(e)

    def test_a_repo_with_no_commits_is_refused_by_name(self):
        proj = os.path.join(self.tmp, "proj")
        for name in ("empty-repo", "good-repo"):
            self.git("init", "-q", os.path.join(proj, name))
        os.makedirs(os.path.join(proj, "not-a-repo"))
        self.git("-C", os.path.join(proj, "good-repo"), "commit", "-q", "--allow-empty", "-m", "first")
        empty = os.path.join(proj, "empty-repo")
        self.assertEqual(self.check(proj), "%s is a git repository with no commits yet, so the developer has nothing to "
                                           "branch from. Make the first commit there (git -C '%s' commit --allow-empty "
                                           "-m 'Initial commit'), then run 'factory start' again." % (empty, empty))
        self.assertIn("%s/. is a git repository with no commits" % empty, self.check(empty))
        self.assertIsNone(self.check(os.path.join(proj, "good-repo")))
        self.assertIsNone(self.check(os.path.join(proj, "not-a-repo")))

    def test_git_runs_with_hooks_and_fsmonitor_off(self):
        log = os.path.join(self.tmp, "git.log")
        self.tool("git", 'echo "$*" >> "%s"\nexec %s "$@"\n' % (log, shutil.which("git")))
        self.assertIsNone(self.check(self.project))
        with open(log) as f:
            self.assertEqual(f.read(), "-c core.fsmonitor=false -c core.hooksPath=/dev/null -C %s/. rev-parse --verify "
                                       "--quiet HEAD^{commit}\n" % self.project)

    def test_start_refuses_before_creating_anything(self):
        shutil.rmtree(os.path.join(self.project, ".git"))
        self.git("init", "-q", self.project)
        self.assertIn("is a git repository with no commits yet", self.start(self.project)[0])
        self.assertEqual(self.starts, [])


class EndingTest(StartTest):
    """How a start ends (scripts/test_factory_layout.sh parts 5-6)."""

    def test_by_default_it_ends_in_the_dashboard_with_the_coordinator(self):
        self.login()
        rc, out, err = self.start(self.project)
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.endswith("Roles: developer reviewer \n\nFactory running. Opening the dashboard: talk to the "
                                     "Coordinator on its screen.\nCtrl-C closes the dashboard (and the Coordinator's "
                                     "terminal); the factory keeps\nrunning. Reopen it with factory dashboard; stop "
                                     "everything with factory stop.\n"), out)
        self.assertEqual(len(self.execs), 1)
        argv = self.execs[0][0]
        self.assertEqual(argv[1:2], [os.path.join(self.root, "scripts", "dashboard.py")])
        self.assertEqual(argv[-2:], ["--coordinator", "factory-coordinator"])

    def test_no_dashboard_says_how_to_open_it(self):
        self.login()
        rc, out, _ = self.start(self.project, "--no-dashboard", tty=True)
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith("\nFactory running. Run \033[1mfactory dashboard\033[0m to open it.\n"), out)
        self.assertEqual(self.execs, [])

    def test_no_ui_lists_the_roles_and_opens_nothing(self):
        self.login()
        for args, env in (((self.project, "--no-ui"), ""), ((self.project,), "1")):
            self.environ["FACTORY_NO_UI"] = env
            rc, out, _ = self.start(*args)
            self.assertEqual(rc, 0)
            self.assertIn("Factory ready (no UI)", out)
            self.assertTrue(out.endswith(
                "\nFactory running, without the dashboard.\n  Talk to the Coordinator:  factory chat\n"
                "  Watch every lane:         factory watch\n  Follow one role's log:    factory logs <role>  "
                "(roles: developer reviewer )\n  Stop everything:          factory stop\n"), out)
        self.assertEqual(self.execs, [])

    def test_the_default_folder_is_the_current_one(self):
        self.login()
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(self.start("--no-dashboard")[0], 0)
        self.assertEqual(self.starts[0], ("broker", [self.project]))

    def test_a_failing_launch_ends_it_with_no_hints(self):
        self.start_status["broker"] = 3
        rc, out, _ = self.start(self.project)
        self.assertEqual(rc, 3)
        self.assertNotIn("first time", out)
        self.assertEqual(self.execs, [])

    def test_a_windows_drive_on_wsl_is_warned_about(self):
        self.login()
        proc = self.write("proc-version", "Linux version 5.15 (microsoft-standard-WSL2)\n")
        real = os.path.realpath
        fake = "/mnt/c/code/app"

        def realpath(path, *a, **kw):
            return fake if path == self.project else real(path, *a, **kw)

        warning = ("  ! %s is on a Windows drive: sandboxes will be slow and file permissions unreliable. Clone it into "
                   "the Linux filesystem (e.g. ~/code) instead.\n" % fake)
        for proc_version, warned in ((proc, True), (self.write("proc-linux", "Linux version 6.1\n"), False)):
            ctx = self.ctx()
            ctx.host_files["proc_version"] = proc_version
            with mock.patch("os.path.realpath", realpath), mock.patch("factory.launch.run", return_value=0):
                self.assertEqual(start.start(ctx, self.project, no_dashboard=True), 0)
            self.assertEqual(self.out.getvalue().startswith(warning), warned, self.out.getvalue())


class SwitchTest(StartTest):
    """A start for another project than the bound one (scripts/test_factory_state.sh 2c)."""

    def setUp(self):
        super().setUp()
        self.login()
        self.other = os.path.join(self.tmp, "other")
        os.makedirs(self.other)
        self.write("state/ai-code-factory/broker/project", self.other + "\n")
        self.sandbox("factory-broker", self.paths.broker_state, self.other)

    def answer(self, text):
        return Tty(text)

    def test_declined_nothing_is_removed(self):
        for stdin in (self.answer("n\n"), io.StringIO("y\n")):
            rc, out, _ = self.start(self.project, stdin=stdin)
            self.assertEqual(rc, "error: not switching. Run 'factory start %s' to open the current project." % self.other)
            self.assertTrue(out.startswith("The factory is currently set up for %s.\n" % self.other), out)
            self.assertEqual(self.argvs("rm"), [])
            self.assertEqual(self.starts, [])
        with open(os.path.join(self.paths.broker_state, "project")) as f:
            self.assertEqual(f.read(), self.other + "\n")

    def test_confirmed_it_resets_then_starts_the_new_project(self):
        rc, out, _ = self.start(self.project, "--no-dashboard", stdin=self.answer("y\n"))
        self.assertEqual(rc, 0)
        self.assertIn("Switch to %s? This removes the agent sandboxes (your Claude login is kept). [y/N] " % self.project, out)
        self.assertIn("  ✓ factory is no longer bound to a project\n", out)
        self.assertEqual(self.argvs("rm"), [["rm", "--force", "factory-broker"]])
        self.assertEqual(self.starts[0], ("broker", [self.project]))
        self.assertFalse(os.path.exists(os.path.join(self.paths.broker_state, "project")))

    def test_a_failing_reset_stops_it_before_the_launch(self):
        with mock.patch.object(runstate, "reset", return_value=1):
            rc, out, _ = self.start(self.project, stdin=self.answer("yes\n"))
        self.assertEqual(rc, 1)
        self.assertEqual(self.starts, [])

    def test_no_factory_sandboxes_means_no_question(self):
        self.write("sbx/names", "other\n")
        rc, out, _ = self.start(self.project, "--no-dashboard")
        self.assertEqual(rc, 0)
        self.assertNotIn("currently set up", out)


class HintsTest(StartTest):
    """The onboarding hints, each on and off."""

    def hints(self, **env):
        self.environ.update(env)
        ctx = self.ctx()
        start.hints(ctx)
        return self.out.getvalue()

    def policy(self, text):
        self.write("config/ai-code-factory/policy.toml", text)

    def keychain(self, stored=(), locked=()):
        """A secret-tool that holds STORED and fails on LOCKED."""
        body = 'case "$3" in\n'
        for service in locked:
            body += "  %s) echo 'secret-tool: locked' >&2; exit 1;;\n" % service
        for service in stored:
            body += '  %s) [ "$1" = lookup ] && printf v; [ "$1" = search ] && echo "[/x/1]"; exit 0;;\n' % service
        self.tool("secret-tool", body + 'esac\n[ "$1" = search ] && exit 0\nexit 1\n')
        self.environ["FACTORY_KEYCHAIN"] = "secret-tool"

    def test_nothing_to_say(self):
        self.login()
        self.assertEqual(self.hints(), "")

    def test_the_first_login(self):
        self.assertEqual(self.hints(), "  ! first time: type /login in the Coordinator (POWER ON on the dashboard) to sign "
                                       "in with your Claude subscription, then run 'factory start' again (or press RESTART "
                                       "on the dashboard) to create the role sandboxes\n")

    def test_out_of_date_sandboxes_except_while_updating(self):
        self.login()
        self.sandbox("factory-broker", "x")
        self.sandbox("factory-reviewer", "x")
        warning = ("  ! out of date (made from an older image or kit): factory-broker factory-reviewer; run 'factory "
                   "update' to recreate them\n")
        self.assertEqual(self.hints(), warning)
        self.assertEqual(self.hints(FACTORY_UPDATING="1"), "")
        fingerprints.record_sandbox_fingerprint(self.paths, "factory-reviewer")
        self.assertEqual(self.hints(FACTORY_UPDATING=""), warning.replace(" factory-reviewer", ""))

    def test_github(self):
        global_token = ("  ! a global GitHub token is stored, so sbx injects it for every sandbox, not just the broker. "
                        "Run 'factory secret save github', then remove the global one: sbx secret rm github\n")
        no_token = ("  ! git push/PR is enabled but no GitHub token is stored: run 'factory secret save github' (see "
                    "README: Enabling git push and pull requests)\n")
        self.policy('[git]\nrepos = ["acme/app", "gitlab.example.com/g/p"]\n')
        self.write("sbx/secrets", LOGIN + "(global)  token  github  configured\n")
        self.assertEqual(self.hints(), global_token)
        self.write("sbx/secrets", LOGIN + "factory-broker  token  github  configured\n")
        self.assertEqual(self.hints(), "")
        self.login()
        self.assertEqual(self.hints(), no_token)
        for repos in ('["gitlab.example.com:8443/g/p"]', "[ ]", '["host.example/x"]'):
            self.policy("[git]\nrepos = %s\n" % repos)
            self.assertEqual(self.hints(), "", repos)

    def test_jira(self):
        self.login()
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS="jira_my_issues jira_get_issue"\n')
        missing = "  ! Jira tools are granted but no token is stored in the keychain (see README: Enabling Jira)\n"
        no_id = ("  ! Jira tools are granted but [jira].cloud_id is not set; set it in %s/policy.toml (see README: "
                 "Enabling Jira)\n" % self.config)
        self.assertEqual(self.hints(), missing + no_id)
        self.policy('[jira]\ncloud_id = "00000000-0000-0000-0000-000000000000"\n')
        self.assertEqual(self.hints(), missing + no_id)
        self.policy('[jira]\ncloud_id = "1234-5678"\n')
        self.keychain(stored=("factory-jira",))
        self.assertEqual(self.hints(), "")
        self.keychain(locked=("factory-jira",))
        self.assertEqual(self.hints(), "  ! the keychain is locked or unavailable, so factory-jira couldn't be checked; "
                                       "unlock it and run this again\n")
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS="web_fetch"\n')
        self.assertEqual(self.hints(), "")

    def test_a_private_npm_registry(self):
        self.login()
        self.policy('[packages]\nnpm_private_registry = "https://npm.example.com/"\n')
        warning = ("  ! npm_fetch can't download private npm packages until a token is stored in the keychain (see "
                   "README: Enabling a private npm registry)\n")
        self.assertEqual(self.hints(), warning)
        self.keychain()
        self.assertEqual(self.hints(), warning)
        self.keychain(stored=("factory-npm-registry",))
        self.assertEqual(self.hints(), "")

    def test_gitlab(self):
        self.login()
        missing = ("  ! [gitlab].url is set but no GitLab token is stored: run 'factory secret save gitlab' (see README: "
                   "Enabling GitLab)\n")
        unheld = ('  ! GitLab merge requests open without approval: [approvals].tools is set without "gitlab_create_mr" '
                  "(see README: Enabling approvals)\n")
        self.policy("[gitlab]\nurl = 'https://gitlab.example.com'\n[approvals]\ntools = [\n  \"git_push\", # x\n]\n")
        self.assertEqual(self.hints(), missing + unheld)
        self.keychain(stored=("factory-gitlab",))
        self.policy('[gitlab]\nurl = "https://gitlab.example.com"\n[approvals]\ntools = ["git_push", "gitlab_create_mr"]\n')
        self.assertEqual(self.hints(), "")
        self.keychain(locked=("factory-gitlab",))
        self.assertIn("so factory-gitlab couldn't be checked", self.hints())

    def test_the_entry_point_bash_setup_runs(self):
        self.sandbox("factory-broker", "x")
        for updating, stale in (("", True), ("1", False)):
            self.environ["FACTORY_UPDATING"] = updating
            self.ctx()
            rc = main(["_onboarding-hints"], root=self.root, environ=self.environ, stdout=self.out, stderr=self.err)
            self.assertEqual(rc, 0)
            self.assertEqual("out of date" in self.out.getvalue(), stale)
            self.assertIn("first time", self.out.getvalue())


class RestartTest(StartTest):
    def test_restart_stops_then_starts_the_bound_project_without_the_dashboard(self):
        self.login()
        self.write("state/ai-code-factory/broker/project", self.project + "\n")
        with mock.patch.object(start, "run", wraps=start.run) as run:
            rc = COMMANDS["restart"](self.ctx(), [])
        self.assertEqual(rc, 0)
        run.assert_called_once_with(mock.ANY, [self.project, "--no-dashboard"])
        out = self.out.getvalue()
        self.assertTrue(out.startswith("\n==> Stopping the factory\n[stop]\n\n==> Starting it again for %s\n[broker start]"
                                       % self.project), out)
        self.assertTrue(out.endswith("Factory running. Run factory dashboard to open it.\n"), out)
        self.assertEqual(self.execs, [])

    def test_a_failing_start_gives_its_status(self):
        self.write("state/ai-code-factory/broker/project", self.project + "\n")
        with mock.patch.object(start, "run", return_value=5):
            self.assertEqual(COMMANDS["restart"](self.ctx(), []), 5)


class ShimTest(unittest.TestCase):
    """bin/factory runs start, up and restart in Python, and bash `setup` and `update` reach it
    (scripts/test_factory_restart.sh parts 1, 2 and 4): a fixture checkout of bin/, scripts/lib,
    factory/, roles/ and kits/, with stub broker and dispatch starts, a stub stop script and dashboard."""

    def setUp(self):
        self.tmp = os.path.realpath(__import__("tempfile").mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        fx = self.fx = os.path.join(self.tmp, "checkout")
        for folder in ("bin", "factory", "roles", "kits", os.path.join("scripts", "lib")):
            shutil.copytree(os.path.join(REPO, folder), os.path.join(fx, folder), ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(fx, "broker"))
        shutil.copy(os.path.join(REPO, "broker", "tools.txt"), os.path.join(fx, "broker"))
        self.calls = os.path.join(self.tmp, "calls.log")
        stub = 'def main(ctx, args):\n    open(%r, "a").write("%%s %%s\\n" %% (__name__, args[0]))\n    return 0\n' % self.calls
        with open(os.path.join(fx, "factory", "broker_start.py"), "w") as f:
            f.write(stub + 'def broker_mcp(role):\n    return "factory-broker-" + role\n')
        with open(os.path.join(fx, "factory", "dispatch_start.py"), "w") as f:
            f.write(stub + 'DISPATCH_MCP = "factory-dispatch"\n')
        for name, body in (("03-stop-factory.sh", 'echo stop >> "%s"\necho "Factory stopped."\n' % self.calls),
                           ("dashboard.py", None)):
            path = os.path.join(fx, "scripts", name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body if body else "open(%r, 'a').write('dashboard\\n')\n" % self.calls)
            os.chmod(path, 0o755)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "sbx"), "w") as f:
            f.write("#!%s\n%s" % (sys.executable, FAKE_SBX))
        os.chmod(os.path.join(self.bin, "sbx"), 0o755)
        os.makedirs(os.path.join(self.tmp, "sbx"))
        with open(os.path.join(self.tmp, "sbx", "secrets"), "w") as f:
            f.write(LOGIN)
        self.project = os.path.join(self.tmp, "project")
        subprocess.run(["git", "init", "-q", self.project], check=True)
        subprocess.run(["git", "-C", self.project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "first"], check=True)
        self.env = {"HOME": self.tmp, "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"), "FAKE_SBX": os.path.join(self.tmp, "sbx"),
                    "FACTORY_KEYCHAIN": "none", "PYTHONDONTWRITEBYTECODE": "1",
                    "PATH": self.bin + os.pathsep + "/usr/bin:/bin"}
        self.bound = os.path.join(self.tmp, "state", "ai-code-factory", "broker", "project")

    def factory(self, *args):
        cp = subprocess.run([os.path.join(self.fx, "bin", "factory")] + list(args), env=self.env, cwd=self.project,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return cp.returncode, cp.stdout.decode()

    def logged(self):
        try:
            with open(self.calls) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_start_and_up_run_in_python(self):
        for command in ("start", "up"):
            os.remove(self.calls) if os.path.exists(self.calls) else None
            rc, out = self.factory(command)
            self.assertEqual(rc, 0, out)
            self.assertEqual(self.logged(), ["factory.broker_start %s" % self.project,
                                             "factory.dispatch_start %s" % self.project, "dashboard"], command)

    def test_restart_stops_then_starts_the_bound_project_with_no_foreground_dashboard(self):
        os.makedirs(os.path.dirname(self.bound))
        with open(self.bound, "w") as f:
            f.write(self.project + "\n")
        rc, out = self.factory("restart")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.logged(), ["stop", "factory.broker_start %s" % self.project,
                                         "factory.dispatch_start %s" % self.project])
        self.assertIn("factory dashboard", out)
        # A stale saved layout ("watch", from an older version) only warns.
        os.makedirs(os.path.join(self.tmp, "config", "ai-code-factory"))
        with open(os.path.join(self.tmp, "config", "ai-code-factory", "layout"), "w") as f:
            f.write("watch\n")
        rc, out = self.factory("restart")
        self.assertEqual(rc, 0, out)
        self.assertIn("your saved 'watch' layout is gone", out)

    def test_help_lists_start_and_restart(self):
        rc, out = self.factory("help")
        self.assertIn("factory start [dir] [--no-ui]", out)
        self.assertIn("factory restart", out)

    def bash(self, snippet):
        """Run SNIPPET with bin/factory sourced and resolve_python replaced by a recorder."""
        fake_py = os.path.join(self.tmp, "fake-python")
        with open(fake_py, "w") as f:
            f.write('#!/bin/sh\necho "python $* updating=${FACTORY_UPDATING:-}" >> "%s"\n' % self.calls)
        os.chmod(fake_py, 0o755)
        cp = subprocess.run(["bash", "-c", '. "$1"; fake_py="$2"; resolve_python() { printf %s "$fake_py"; }; ' + snippet, "_",
                             os.path.join(self.fx, "bin", "factory"), fake_py], env=self.env, cwd=self.project,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return cp.returncode, cp.stdout.decode()

    def test_bash_update_starts_the_bound_project_in_python_without_the_dashboard(self):
        os.makedirs(os.path.dirname(self.bound))
        with open(self.bound, "w") as f:
            f.write(self.project + "\n")
        rc, out = self.bash('cmd_setup() { :; }; stale_sandboxes() { :; }; cmd_update --pulled --yes')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.logged(), ["stop", "python %s start %s --no-dashboard updating="
                                         % (os.path.join(self.fx, "factory"), self.project)])

    def test_bash_setup_ends_with_the_hints_and_update_passes_factory_updating(self):
        for name in ("install-prereqs.sh", "00-build-templates.sh", "01-init-security.sh"):
            path = os.path.join(self.fx, "scripts", name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\necho Building images\n")
            os.chmod(path, 0o755)
        factory_py = os.path.join(self.fx, "factory")
        rc, out = self.bash("sbx_not_signed_in() { return 1; }; cmd_setup")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.logged(), ["python %s doctor updating=" % factory_py,
                                         "python %s _onboarding-hints updating=" % factory_py])
        os.remove(self.calls)
        rc, out = self.bash("sbx_not_signed_in() { return 1; }; FACTORY_UPDATING=1 cmd_setup")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.logged()[-1], "python %s _onboarding-hints updating=1" % factory_py)


class StopScriptTest(unittest.TestCase):
    """The real 03-stop-factory.sh (which stop, restart, reset and update run, and README.md names
    for stopping a copy by hand) kills only the dispatch bridge's pid and stops the factory-*
    sandboxes; a running dashboard survives it (scripts/test_factory_restart.sh part 3)."""

    def setUp(self):
        import tempfile
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        real = self.real = os.path.join(self.tmp, "real")
        shutil.copytree(os.path.join(REPO, "scripts", "lib"), os.path.join(real, "scripts", "lib"))
        shutil.copy(os.path.join(REPO, "scripts", "03-stop-factory.sh"), os.path.join(real, "scripts"))
        os.makedirs(os.path.join(real, "dispatch"))
        for path in ("dispatch/server.py", "scripts/dashboard.py"):
            with open(os.path.join(real, path), "w") as f:
                f.write("import time\ntime.sleep(60)\n")
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        self.sbx_log = os.path.join(self.tmp, "sbx.log")
        with open(os.path.join(bin_dir, "sbx"), "w") as f:
            f.write('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\ncase "$1" in\n'
                    '  version) echo "sbx version: v0.45.1 test" ;;\n'
                    '  ls) echo \'{"sandboxes":[{"name":"factory-broker","status":"running"},'
                    '{"name":"factory-developer","status":"stopped"},{"name":"other","status":"running"}]}\' ;;\n'
                    "esac\n" % self.sbx_log)
        os.chmod(os.path.join(bin_dir, "sbx"), 0o755)
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                        XDG_STATE_HOME=os.path.join(self.tmp, "state"), HOME=self.tmp)
        self.procs = []
        self.pid_file = os.path.join(self.tmp, "state", "ai-code-factory", "dispatch", "pid")
        os.makedirs(os.path.dirname(self.pid_file))

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()

    def spawn(self, path):
        p = subprocess.Popen([sys.executable, os.path.join(self.real, path)])
        self.procs.append(p)
        return p

    def stop(self):
        return subprocess.run([os.path.join(self.real, "scripts", "03-stop-factory.sh")], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def gone(self, proc):
        for _ in range(25):
            if proc.poll() is not None:
                return True
            time.sleep(0.2)
        return False

    def test_it_kills_only_the_bridge_and_stops_the_factory_sandboxes(self):
        bridge, dash = self.spawn("dispatch/server.py"), self.spawn("scripts/dashboard.py")
        with open(self.pid_file, "w") as f:
            f.write("%d\n" % bridge.pid)
        cp = self.stop()
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(b"Factory stopped.", cp.stdout)
        self.assertTrue(self.gone(bridge))
        self.assertIsNone(dash.poll())
        with open(self.sbx_log) as f:
            stops = [line for line in f.read().splitlines() if line.startswith("stop")]
        self.assertEqual(stops, ["stop factory-broker"])

    def test_a_pid_file_naming_another_process_kills_nothing(self):
        dash = self.spawn("scripts/dashboard.py")
        with open(self.pid_file, "w") as f:
            f.write("%d\n" % dash.pid)
        self.stop()
        time.sleep(0.2)
        self.assertIsNone(dash.poll())


if __name__ == "__main__":
    unittest.main()
