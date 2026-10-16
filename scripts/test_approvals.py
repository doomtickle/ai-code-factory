"""factory approvals (scripts/approvals.py): the console against a temp state dir.

Run with the host Python: python3 -m unittest discover -s scripts -p 'test_approvals.py'
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approvals  # noqa: E402


def card(cid="abcdef0123456789", tool="git_push", **over):
    base = {"id": cid, "nonce": "n0nce", "tool": tool, "principal": "developer", "repo": "acme/widgets", "branch": "factory/x",
            "base": "main", "commits": 2, "file_count": 3, "files": ["a", "b", "c"], "shortstat": "3 files changed",
            "created": time.time(), "expires": time.time() + 600}
    base.update(over)
    return base


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / "approvals" / "pending").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, c):
        (self.state / "approvals" / "pending" / "{}.json".format(c["id"])).write_text(json.dumps(c))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = approvals.main(["--state", str(self.state)] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_empty_list(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("No approvals pending", out)

    def test_list_and_show(self):
        self.put(card())
        self.put(card("ffff000011112222", tool="github_create_pr", head="factory/y", title="Add thing", draft=True))
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("abcdef0123456789  developer  push factory/x -> acme/widgets (2 commit(s), 3 file(s))", out)
        self.assertIn('draft PR on acme/widgets: factory/y -> main "Add thing"', out)
        code, out, _ = self.run_cli("show", "abcdef")  # prefix match
        self.assertEqual(code, 0)
        self.assertIn("shortstat      3 files changed", out)
        self.assertNotIn("nonce", out)  # bookkeeping, not something to read
        self.assertIn("    a\n    b\n    c", out)
        code, _, err = self.run_cli("show", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no pending approval matches", err)

    def test_approve_writes_a_decision_atomically(self):
        self.put(card())
        code, out, _ = self.run_cli("approve", "abcdef0123456789", "--as", "daron")
        self.assertEqual(code, 0)
        self.assertIn("approved abcdef0123456789 as daron", out)
        path = self.state / "approvals" / "decisions" / "abcdef0123456789.json"
        decision = json.loads(path.read_text())
        self.assertEqual((decision["decision"], decision["approver"], decision["reason"]), ("approve", "daron", ""))
        self.assertEqual(decision["nonce"], "n0nce")
        self.assertAlmostEqual(decision["ts"], time.time(), delta=5)
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_decline_needs_a_reason(self):
        self.put(card())
        code, _, err = self.run_cli("decline", "abcdef0123456789")
        self.assertEqual(code, 1)
        self.assertIn("reason", err)
        code, out, _ = self.run_cli("decline", "abcdef0123456789", "--reason", "wrong branch", "--as", "sam")
        self.assertEqual(code, 0)
        decision = json.loads((self.state / "approvals" / "decisions" / "abcdef0123456789.json").read_text())
        self.assertEqual((decision["decision"], decision["approver"], decision["reason"]), ("decline", "sam", "wrong branch"))

    def test_expired_card_cannot_be_decided(self):
        self.put(card(expires=time.time() - 120))
        code, _, err = self.run_cli("approve", "abcdef0123456789")
        self.assertEqual(code, 1)
        self.assertIn("expired", err)

    def test_missing_state_dir(self):
        code, _, err = self.run_cli_with_state(self.state / "missing")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def run_cli_with_state(self, state):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = approvals.main(["--state", str(state), "list"])
        return code, out.getvalue(), err.getvalue()


if __name__ == "__main__":
    unittest.main()
