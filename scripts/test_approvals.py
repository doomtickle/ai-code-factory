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

    def test_summary_none_pending(self):
        code, out, _ = self.run_cli("summary")
        self.assertEqual(code, 0)
        self.assertEqual(out, "0 -1 0\n")

    def test_summary_several_with_oldest_age(self):
        now = time.time()
        self.put(card("aaaa000000000000", created=now - 900, expires=now + 600))  # 15 min old
        self.put(card("bbbb000000000000", created=now - 120, expires=now + 600))  # 2 min old
        code, out, _ = self.run_cli("summary")
        self.assertEqual(code, 0)
        self.assertEqual(out, "2 15 0\n")

    def test_summary_expired_unhandled_vs_expired_but_decided(self):
        now = time.time()
        self.put(card("aaaa000000000000", created=now - 1800, expires=now - 120, nonce="n0nce"))
        self.put(card("bbbb000000000000", created=now - 1800, expires=now - 120, nonce="n0nce"))
        decisions = self.state / "approvals" / "decisions"
        decisions.mkdir(parents=True)
        (decisions / "bbbb000000000000.json").write_text(
            json.dumps({"id": "bbbb000000000000", "nonce": "n0nce", "decision": "approve"}))
        code, out, _ = self.run_cli("summary")
        self.assertEqual(code, 0)
        self.assertEqual(out, "2 30 1\n")

    def test_summary_stale_decision_nonce_still_counts_as_unhandled(self):
        # A retry after expiry mints a fresh nonce; a decision for the old
        # nonce belongs to a card that no longer exists in this form.
        now = time.time()
        self.put(card("aaaa000000000000", created=now - 1800, expires=now - 120, nonce="new-nonce"))
        decisions = self.state / "approvals" / "decisions"
        decisions.mkdir(parents=True)
        (decisions / "aaaa000000000000.json").write_text(
            json.dumps({"id": "aaaa000000000000", "nonce": "stale-nonce", "decision": "approve"}))
        code, out, _ = self.run_cli("summary")
        self.assertEqual(code, 0)
        self.assertEqual(out, "1 30 1\n")

    def test_summarize_with_injectable_now(self):
        # A fixed `now` far from wall-clock time makes the expected ages exact,
        # independent of how long the test takes to run.
        now = 1_000_000.0
        self.put(card("aaaa000000000000", created=now - 600, expires=now - 1))  # expired, no decision
        self.put(card("bbbb000000000000", created=now - 1200, expires=now + 600))  # 20 min old, not expired
        self.assertEqual(approvals.summarize(self.state, now=now),
                          {"pending": 2, "oldest_minutes": 20, "expired_unhandled": 1})

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
