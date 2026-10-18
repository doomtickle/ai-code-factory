"""The `python3 -m passdesk principal ...` CLI: argument parsing, exit
codes, and that the token goes to stdout alone while every other message
goes to stderr."""

import contextlib
import io
import unittest

from passdesk import __main__ as cli
from passdesk.core import auth, config

from .base import Base


class CliBase(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class AddCommandTests(CliBase):
    def test_add_prints_only_the_token_on_stdout(self):
        code, out, err = self.run_cli("principal", "add", "alice", "--tools", "git_fetch")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        token = lines[0]
        self.assertEqual(len(token), 64)
        self.assertNotIn(token, err)  # the token appears nowhere else, including stderr
        got, reason = auth.authenticate(f"Bearer {token}")
        self.assertEqual(got, "alice")
        self.assertIsNone(reason)

    def test_add_accepts_multiple_tools(self):
        code, _out, _ = self.run_cli("principal", "add", "alice", "--tools", "git_fetch", "npm_fetch")
        self.assertEqual(code, 0)
        self.assertEqual(auth.principal_tools("alice"), {"git_fetch", "npm_fetch"})

    def test_add_requires_tools_flag(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("principal", "add", "alice")
        self.assertEqual(cm.exception.code, 2)  # argparse usage error

    def test_add_unknown_tool_fails_with_no_stdout_output(self):
        code, out, err = self.run_cli("principal", "add", "alice", "--tools", "bogus_tool")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("unknown tool", err)

    def test_add_invalid_name_fails_with_no_stdout_output(self):
        code, out, err = self.run_cli("principal", "add", "../escape", "--tools", "git_fetch")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid principal name", err)

    def test_add_duplicate_fails(self):
        self.run_cli("principal", "add", "alice", "--tools", "git_fetch")
        code, out, err = self.run_cli("principal", "add", "alice", "--tools", "npm_fetch")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("already exists", err)


class ListCommandTests(CliBase):
    def test_list_empty(self):
        code, out, _ = self.run_cli("principal", "list")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_list_shows_name_and_tools_not_token_or_hash(self):
        self.run_cli("principal", "add", "alice", "--tools", "git_fetch", "npm_fetch")
        digest = (config.STATE / "tokens" / "alice.sha256").read_text().strip()
        code, out, _ = self.run_cli("principal", "list")
        self.assertEqual(code, 0)
        self.assertIn("alice", out)
        self.assertIn("git_fetch", out)
        self.assertIn("npm_fetch", out)
        self.assertNotIn(digest, out)


class RevokeCommandTests(CliBase):
    def test_revoke_removes_the_principal(self):
        self.run_cli("principal", "add", "alice", "--tools", "git_fetch")
        code, _, _ = self.run_cli("principal", "revoke", "alice")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("principal", "list")
        self.assertEqual(out, "")

    def test_revoke_unknown_principal_fails(self):
        code, _out, err = self.run_cli("principal", "revoke", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("no such principal", err)


if __name__ == "__main__":
    unittest.main()
