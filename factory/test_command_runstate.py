"""Tests for factory/commands/runstate.py (factory chat, stop, restart, reset). Ported from
scripts/test_factory_chat.sh, the reset parts of scripts/test_factory_state.sh and the usage and
ordering parts of scripts/test_factory_restart.sh; what stays shell is the real
03-stop-factory.sh and `restart` driving the real bash `start` (scripts/test_factory_restart.sh).
The output was also compared with the bash these replaced, on the same stubs, before that was deleted.

sbx, ps and the stop script are stubs, so nothing here touches a real sandbox or process.
"""

import os
import subprocess
import sys
import unittest

from factory import sbx
from factory.cli import COMMANDS, main
from factory.commands import runstate
from factory.context import COORDINATOR_SANDBOX, FactoryError
from factory.fixtures import REPO, CommandTest, Tty

PROJECT = "/work/project"


class RunStateTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.calls = os.path.join(self.tmp, "calls.log")
        self.listing = self.write("sandboxes", "")
        self.ps_out = self.write("ps-out", "")
        self.rm_status = self.write("rm-status", "0\n")
        self.stop_status = self.write("stop-status", "0\n")
        self.sbx_stub()
        self.tool("ps", 'cat "%s"\n' % self.ps_out)
        self.write("repo/scripts/03-stop-factory.sh",
                   '#!/bin/sh\necho stop >> "%s"\necho "Factory stopped."\necho "stop-err" >&2\nexit $(cat "%s")\n'
                   % (self.calls, self.stop_status), 0o755)
        self.write("repo/bin/factory", '#!/bin/sh\necho "factory $*" >> "%s"\nexit ${FAKE_START_STATUS:-0}\n' % self.calls, 0o755)

    def sbx_stub(self, version=sbx.SBX_VERSION):
        self.tool("sbx", '''echo "sbx $*" >> "%s"
case "$1" in
version) echo "sbx version: v%s (abc)";;
ls) cat "%s";;
rm) exit $(cat "%s");;
esac
''' % (self.calls, version, self.listing, self.rm_status))

    def sandboxes(self, *names):
        self.write("sandboxes", "".join(n + "\n" for n in names))

    def logged(self):
        try:
            with open(self.calls) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def bind(self, project=PROJECT):
        self.write("state/ai-code-factory/broker/project", project + "\n")
        self.write("state/ai-code-factory/dispatch/project", project + "\n")

    def run_command(self, command, *args, tty=False):
        ctx = self.ctx(tty)
        rc = COMMANDS[command](ctx, list(args))
        return rc, self.out.getvalue(), self.err.getvalue()


class ChatTest(RunStateTest):
    def test_without_the_coordinator_sandbox_it_says_to_start_first(self):
        self.sandboxes("factory-broker")
        ctx = self.ctx()
        rc = main(["chat"], root=self.root, environ=self.environ, stdout=self.out, stderr=self.err, execer=self.execer)
        self.assertEqual((rc, self.out.getvalue(), self.execs), (1, "", []))
        self.assertEqual(self.err.getvalue(), "error: the coordinator sandbox doesn't exist yet; run 'factory start' first\n")
        self.assertNotIn("sbx run", " ".join(self.logged()))

    def test_it_replaces_itself_with_sbx_run_and_forwards_arguments(self):
        self.sandboxes("factory-broker", COORDINATOR_SANDBOX)
        for command in ("chat", "coordinator"):
            self.execs.clear()
            rc, out, err = self.run_command(command, "--resume", "-x")
            self.assertEqual((rc, out, err), (0, "", ""), command)
            self.assertEqual([argv for argv, _ in self.execs],
                             [["sbx", "run", "--name", "factory-coordinator", "--resume", "-x"]], command)

    def test_it_refuses_when_the_coordinator_is_open_elsewhere(self):
        self.sandboxes(COORDINATOR_SANDBOX)
        self.write("ps-out", "/usr/bin/sbx daemon start\nsbx run --name factory-coordinator\n")
        self.ctx()
        rc = main(["chat"], root=self.root, environ=self.environ, stdout=self.out, stderr=self.err, execer=self.execer)
        self.assertEqual(rc, 1)
        self.assertEqual(self.err.getvalue(), "error: the Coordinator is already open elsewhere (the dashboard or "
                                              "another 'factory chat'); close it there first\n")
        self.assertEqual(self.execs, [])

    def test_a_broken_ps_reads_as_nobody(self):
        self.sandboxes(COORDINATOR_SANDBOX)
        self.tool("ps", "exit 1\n")
        self.run_command("chat")
        self.assertEqual(len(self.execs), 1)

    def test_a_wrong_sbx_version_stops_before_anything_else(self):
        self.sbx_stub("0.1.0")
        self.sandboxes(COORDINATOR_SANDBOX)
        rc = main(["chat"], root=self.root, environ=self.environ, stdout=self.ctx().stdout, stderr=self.err,
                  execer=self.execer)
        self.assertEqual(rc, 1)
        self.assertEqual(self.err.getvalue(), "error: sbx %s required, found '0.1.0' (see README.md)\n" % sbx.SBX_VERSION)
        self.assertEqual(self.execs, [])

    def test_the_real_exec_finds_sbx_on_the_path_and_hands_over_the_terminal(self):
        self.sandboxes(COORDINATOR_SANDBOX)
        env = dict(self.environ, PYTHONDONTWRITEBYTECODE="1")
        cp = subprocess.run([sys.executable, os.path.join(REPO, "factory"), "chat", "--resume"], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
        self.assertEqual((cp.returncode, cp.stderr), (0, b""))
        self.assertIn("sbx run --name factory-coordinator --resume", self.logged())


class StopTest(RunStateTest):
    def test_it_runs_the_stop_script_and_shows_what_it_prints(self):
        for command in ("stop", "down"):
            self.write("calls.log", "")
            rc, out, err = self.run_command(command, "ignored")
            self.assertEqual((rc, out, err), (0, "Factory stopped.\n", "stop-err\n"), command)
            self.assertEqual(self.logged(), ["stop"], command)

    def test_its_exit_status_is_the_scripts(self):
        self.write("stop-status", "3\n")
        self.assertEqual(self.run_command("stop")[0], 3)


class RestartTest(RunStateTest):
    def setUp(self):
        super().setUp()
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)
        self.bind(self.project)

    def test_it_takes_no_arguments(self):
        rc = main(["restart", "now"], root=self.root, environ=self.environ, stdout=self.ctx().stdout, stderr=self.err)
        self.assertEqual((rc, self.err.getvalue()), (1, "error: usage: factory restart\n"))
        self.assertEqual(self.logged(), [])

    def test_without_a_bound_project_nothing_is_stopped(self):
        os.remove(os.path.join(self.paths.broker_state, "project"))
        rc = main(["restart"], root=self.root, environ=self.environ, stdout=self.ctx().stdout, stderr=self.err)
        self.assertEqual((rc, self.err.getvalue()),
                         (1, "error: the factory isn't bound to a project yet: run 'factory start' in one\n"))
        self.assertEqual(self.logged(), [])

    def test_a_project_folder_that_is_gone_stops_nothing(self):
        self.bind(os.path.join(self.tmp, "gone"))
        rc = main(["restart"], root=self.root, environ=self.environ, stdout=self.ctx().stdout, stderr=self.err)
        self.assertEqual(rc, 1)
        self.assertEqual(self.err.getvalue(), "error: the factory's project %s no longer exists: run 'factory start' in "
                                              "another\n" % os.path.join(self.tmp, "gone"))
        self.assertEqual(self.logged(), [])

    def test_a_wrong_sbx_stops_nothing(self):
        self.sbx_stub("0.1.0")
        rc = main(["restart"], root=self.root, environ=self.environ, stdout=self.ctx().stdout, stderr=self.err)
        self.assertEqual(rc, 1)
        self.assertIn("required, found '0.1.0'", self.err.getvalue())
        self.assertEqual([c for c in self.logged() if not c.startswith("sbx")], [])

    def test_it_stops_then_starts_the_bound_project_without_the_dashboard(self):
        rc, out, err = self.run_command("restart")
        self.assertEqual((rc, err), (0, "stop-err\n"))
        self.assertEqual(out, "\n==> Stopping the factory\nFactory stopped.\n\n==> Starting it again for %s\n" % self.project)
        self.assertEqual([c for c in self.logged() if not c.startswith("sbx")],
                         ["stop", "factory start %s --no-dashboard" % self.project])

    def test_on_a_terminal_the_steps_are_bold(self):
        rc, out, err = self.run_command("restart", tty=True)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("\n\033[1m==> Stopping the factory\033[0m\n"), out)
        self.assertTrue(out.endswith("\n\033[1m==> Starting it again for %s\033[0m\n" % self.project), out)

    def test_a_failing_stop_ends_it_with_the_scripts_status_and_starts_nothing(self):
        self.write("stop-status", "2\n")
        rc, out, err = self.run_command("restart")
        self.assertEqual(rc, 2)
        self.assertNotIn("Starting it again", out)
        self.assertEqual([c for c in self.logged() if not c.startswith("sbx")], ["stop"])

    def test_a_failing_start_gives_its_status(self):
        self.environ["FAKE_START_STATUS"] = "5"
        self.assertEqual(self.run_command("restart")[0], 5)


class ResetTest(RunStateTest):
    def setUp(self):
        super().setUp()
        self.bind()
        self.sandboxes("factory-broker", "factory-developer", "other", "factory-reviewer-lane2")
        for name in ("developer", "developer.json", ".hidden"):
            self.write("state/ai-code-factory/dispatch/sessions/" + name, "x")
        os.makedirs(os.path.join(self.paths.dispatch_state, "sessions", "subdir"))

    def sessions(self):
        return sorted(os.listdir(os.path.join(self.paths.dispatch_state, "sessions")))

    def rm_calls(self):
        return [c for c in self.logged() if c.startswith("sbx rm")]

    def test_yes_removes_the_factory_sandboxes_and_forgets_the_project(self):
        rc, out, err = self.run_command("reset", "--yes")
        self.assertEqual((rc, err), (0, "stop-err\n"))
        self.assertEqual(out, "  ✓ removed factory-broker factory-developer factory-reviewer-lane2 \n"
                              "  ✓ factory is no longer bound to a project\n")
        self.assertEqual(self.rm_calls(), ["sbx rm --force factory-broker factory-developer factory-reviewer-lane2"])
        self.assertEqual([c for c in self.logged() if not c.startswith("sbx")], ["stop"])
        self.assertFalse(os.path.exists(os.path.join(self.paths.broker_state, "project")))
        self.assertFalse(os.path.exists(os.path.join(self.paths.dispatch_state, "project")))
        self.assertEqual(self.sessions(), [".hidden", "subdir"])

    def test_the_stop_scripts_output_is_dropped(self):
        rc, out, err = self.run_command("reset", "--yes")
        self.assertNotIn("Factory stopped.", out)

    def test_on_a_terminal_the_ticks_are_green(self):
        rc, out, err = self.run_command("reset", "--yes", tty=True)
        self.assertEqual(out, "  \033[32m✓\033[0m removed factory-broker factory-developer factory-reviewer-lane2 \n"
                              "  \033[32m✓\033[0m factory is no longer bound to a project\n")

    def test_with_no_factory_sandboxes_only_the_binding_is_removed(self):
        self.sandboxes("other")
        rc, out, err = self.run_command("reset", "--yes")
        self.assertEqual((rc, out), (0, "  ✓ factory is no longer bound to a project\n"))
        self.assertEqual(self.rm_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.paths.broker_state, "project")))

    def test_it_works_when_nothing_was_ever_bound(self):
        os.remove(os.path.join(self.paths.broker_state, "project"))
        os.remove(os.path.join(self.paths.dispatch_state, "project"))
        self.write("sandboxes", "")
        os.rmdir(os.path.join(self.paths.dispatch_state, "sessions", "subdir"))
        for name in os.listdir(os.path.join(self.paths.dispatch_state, "sessions")):
            os.remove(os.path.join(self.paths.dispatch_state, "sessions", name))
        os.rmdir(os.path.join(self.paths.dispatch_state, "sessions"))
        rc, out, err = self.run_command("reset", "--yes")
        self.assertEqual((rc, out), (0, "  ✓ factory is no longer bound to a project\n"))

    def test_a_failing_sbx_ls_reads_as_no_sandboxes(self):
        self.tool("sbx", 'echo "sbx $*" >> "%s"\ncase "$1" in version) echo "sbx version: v%s";; ls) exit 1;; esac\n'
                  % (self.calls, sbx.SBX_VERSION))
        rc, out, err = self.run_command("reset", "--yes")
        self.assertEqual((rc, out), (0, "  ✓ factory is no longer bound to a project\n"))

    def test_a_failing_rm_says_what_is_left_and_keeps_the_binding(self):
        self.write("rm-status", "1\n")
        rc, out, err = self.run_command("reset", "--yes")
        self.assertEqual(rc, 1)
        self.assertEqual(out, (
            "  ✗ factory reset: could not remove factory-broker factory-developer factory-reviewer-lane2 \n"
            "  Left stopped: the agent sandboxes, the broker and the dispatch bridge. Nothing was restarted.\n"
            "  Some of those sandboxes may already be gone ('sbx ls' shows what is left).\n"
            "  To bring the factory back up: factory start (in your project).\n"
            "  To retry the removal: factory reset.\n"))
        self.assertEqual([c for c in self.logged() if not c.startswith("sbx")], ["stop"])
        self.assertNotIn("no longer bound", out)
        self.assertTrue(os.path.exists(os.path.join(self.paths.broker_state, "project")))
        self.assertEqual(self.sessions(), [".hidden", "developer", "developer.json", "subdir"])

    def test_a_failing_stop_removes_nothing(self):
        self.write("stop-status", "4\n")
        rc, out, err = self.run_command("reset", "--yes")
        self.assertEqual((rc, out), (4, ""))
        self.assertEqual(self.rm_calls(), [])
        self.assertTrue(os.path.exists(os.path.join(self.paths.broker_state, "project")))

    def test_without_yes_it_asks_and_a_yes_goes_ahead(self):
        for answer in ("y\n", "Y\n", "yes\n", "YES\n", "  y \n"):
            self.bind()
            ctx = self.ctx()
            ctx.stdin = Tty(answer)
            rc = runstate.reset(ctx, [])
            self.assertEqual(rc, 0, answer)
            self.assertEqual(self.out.getvalue(), (
                "This removes these sandboxes: factory-broker factory-developer factory-reviewer-lane2 \n"
                "Your Claude login, roles and settings are kept.\n"
                "Continue? [y/N]   ✓ removed factory-broker factory-developer factory-reviewer-lane2 \n"
                "  ✓ factory is no longer bound to a project\n"), answer)

    def test_any_other_answer_cancels(self):
        for answer in ("n\n", "\n", "", "yep\n", "Yes\n"):
            ctx = self.ctx()
            ctx.stdin = Tty(answer)
            with self.assertRaises(FactoryError) as caught:
                runstate.reset(ctx, [])
            self.assertEqual(str(caught.exception), "cancelled", answer)
            self.assertEqual(self.rm_calls(), [], answer)
            self.assertEqual([c for c in self.logged() if not c.startswith("sbx")], [], answer)
            self.assertTrue(os.path.exists(os.path.join(self.paths.broker_state, "project")))

    def test_without_a_terminal_it_cancels_without_asking(self):
        rc = main(["reset"], root=self.root, environ=self.environ, stdout=self.ctx().stdout, stderr=self.err)
        self.assertEqual(rc, 1)
        self.assertEqual(self.err.getvalue(), "error: cancelled\n")
        self.assertEqual(self.out.getvalue(), (
            "This removes these sandboxes: factory-broker factory-developer factory-reviewer-lane2 \n"
            "Your Claude login, roles and settings are kept.\n"))
        self.assertEqual(self.rm_calls(), [])

    def test_only_a_first_argument_of_yes_skips_the_question(self):
        rc = main(["reset", "-y"], root=self.root, environ=self.environ, stdout=self.ctx().stdout, stderr=self.err)
        self.assertEqual((rc, self.err.getvalue()), (1, "error: cancelled\n"))


class RmFailedAfterStopTest(RunStateTest):
    def test_the_message_is_the_one_update_prints(self):
        # bin/factory keeps its copy for `factory update`.
        for command, names, retry in (("reset", ["factory-a", "factory-b"], "factory reset"),
                                      ("update", ["factory-developer"], "factory update")):
            cp = subprocess.run(["bash", "-c", 'ROOT="$1"; . "$1/bin/factory"; rm_failed_after_stop "$2" "$3" "$4"',
                                 "_", REPO, command, " ".join(names), retry], env=dict(self.environ, LC_ALL="C"),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
            self.assertEqual(cp.returncode, 1)
            ctx = self.ctx()
            self.assertEqual(runstate.rm_failed_after_stop(ctx, command, names, retry), 1)
            self.assertEqual(self.out.getvalue(), cp.stdout.decode(), command)


if __name__ == "__main__":
    unittest.main()
