"""Tests for factory/commands/wrappers.py: analytics, approvals, dashboard/gui and watch."""

import os
import unittest

from factory.cli import COMMANDS
from factory.context import basename, bound_project
from factory.fixtures import CommandTest


class WrapperTest(CommandTest):
    def bind(self, text):
        self.write(os.path.join("state", "ai-code-factory", "broker", "project"), text)

    def run_command(self, *argv):
        rc = COMMANDS[argv[0]](self.ctx(), list(argv[1:]))
        self.assertEqual(self.err.getvalue(), "")
        return rc

    def test_analytics_passes_the_state_folder_and_its_arguments(self):
        self.assertEqual(self.run_command("analytics", "--json", "--top", "3"), 0)
        self.assertEqual([a for a, _ in self.execs],
                         [self.script("analytics.py") + ["--state", self.state, "--json", "--top", "3"]])

    def test_approvals_gets_the_broker_state_folder_and_its_arguments(self):
        self.assertEqual(self.run_command("approvals", "decline", "7", "--reason", "no thanks"), 0)
        self.assertEqual([a for a, _ in self.execs], [self.script("approvals.py") + [
            "--state", os.path.join(self.state, "broker"), "decline", "7", "--reason", "no thanks"]])

    def test_approve_is_approvals_and_passes_its_arguments_as_given(self):
        self.assertIs(COMMANDS["approve"], COMMANDS["approvals"])
        self.run_command("approve", "7")
        self.assertEqual(self.execs[0][0][-3:], ["--state", os.path.join(self.state, "broker"), "7"])

    def test_watch_passes_the_dispatch_state_roles_and_the_project_name(self):
        self.bind("/home/me/code/my-app\n")
        self.run_command("watch", "--once")
        self.assertEqual([a for a, _ in self.execs], [self.script("watch.py") + [
            "--state", os.path.join(self.state, "dispatch"), "--roles", os.path.join(self.root, "roles"),
            "--project", "my-app", "--once"]])

    def test_watch_with_no_project_bound_passes_an_empty_name(self):
        self.run_command("watch")
        argv = self.execs[0][0]
        self.assertEqual(argv[argv.index("--project") + 1], "")

    def test_dashboard_gets_the_state_project_and_the_coordinator(self):
        self.bind("/home/me/code/my-app/\n")
        self.run_command("dashboard", "--no-open", "--port", "8123")
        self.assertEqual([a for a, _ in self.execs], [self.script("dashboard.py") + [
            "--state", self.state, "--roles", os.path.join(self.root, "roles"),
            "--config", self.paths.config, "--project", "my-app", "--coordinator", "factory-coordinator", "--no-open", "--port", "8123"]])

    def test_gui_is_dashboard(self):
        self.assertIs(COMMANDS["gui"], COMMANDS["dashboard"])

    def test_demo_is_given_no_state_project_or_coordinator(self):
        self.bind("/home/me/code/my-app\n")
        self.run_command("dashboard", "--demo", "--no-open")
        self.assertEqual([a for a, _ in self.execs], [self.script("dashboard.py") + [
            "--roles", os.path.join(self.root, "roles"), "--demo", "--no-open"]])

    def test_demo_anywhere_in_the_arguments_counts(self):
        self.run_command("dashboard", "--port", "1", "--demo")
        argv = self.execs[0][0]
        self.assertNotIn("--state", argv)
        self.assertNotIn("--coordinator", argv)

    def test_the_environment_is_handed_on(self):
        self.run_command("analytics")
        self.assertEqual(self.execs[0][1], self.environ)

    def test_the_exit_status_is_the_execers(self):
        ctx = self.ctx()
        ctx._execer = lambda argv, env: 7
        self.assertEqual(COMMANDS["analytics"](ctx, []), 7)


class ProjectTest(CommandTest):
    def test_bound_project_drops_trailing_newlines_only(self):
        self.write(os.path.join("state", "ai-code-factory", "broker", "project"), "/a b/c\n\n")
        self.assertEqual(bound_project(self.paths), "/a b/c")

    def test_no_project_file_is_empty(self):
        self.assertEqual(bound_project(self.paths), "")

    def test_basename_is_posix(self):
        for path, want in (("", ""), ("/", "/"), ("//", "/"), ("/a/b", "b"), ("/a/b/", "b"), ("b", "b"), ("/a/b//", "b")):
            self.assertEqual(basename(path), want, path)


if __name__ == "__main__":
    unittest.main()
