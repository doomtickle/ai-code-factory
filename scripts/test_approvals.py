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
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import approvals  # noqa: E402


def card(cid="abcdef0123456789", tool="git_push", **over):
    base = {"id": cid, "nonce": "n0nce", "tool": tool, "principal": "developer", "repo": "acme/widgets", "branch": "factory/x",
            "base": "main", "commits": 2, "file_count": 3, "files": ["a", "b", "c"], "shortstat": "3 files changed",
            "created": time.time(), "expires": time.time() + 600}
    base.update(over)
    return base


def decision_files(folder):
    """Decision files in folder (its lock file left out), sorted."""
    return sorted(p.name for p in folder.iterdir() if p.name != ".write.lock") if folder.is_dir() else []


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
        self.put(card("eeee000011112222", tool="gitlab_create_mr", repo="gitlab.acme.net/platform/api",
                      head="factory/z", title="Draft: Add it", draft=True))
        code, out, _ = self.run_cli("list")
        self.assertIn('draft MR on gitlab.acme.net/platform/api: factory/z -> main "Draft: Add it"', out)
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

    def test_cli_refuses_a_second_decision(self):
        for first, second, args in (("approve", "decline", ["--reason", "no"]), ("decline", "approve", [])):
            cid = "{}0000000000".format(first)
            self.put(card(cid))
            extra = ["--reason", "first"] if first == "decline" else []
            code, _, _ = self.run_cli(first, cid, "--as", "alice", *extra)
            self.assertEqual(code, 0)
            code, out, err = self.run_cli(second, cid, "--as", "bob", *args)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertRegex(err, r"already {} by alice at \d{{4}}-\d\d-\d\d \d\d:\d\d; waiting for the agent to retry".format(
                "approved" if first == "approve" else "declined"))
            rec = json.loads((self.state / "approvals" / "decisions" / "{}.json".format(cid)).read_text())
            self.assertEqual((rec["decision"], rec["approver"]), (first, "alice"))

    def test_cli_reports_a_decision_that_lands_first(self):
        # Someone else decides between this console's check and its write.
        self.put(card())
        theirs = {"id": "abcdef0123456789", "nonce": "n0nce", "decision": "decline", "approver": "sam", "ts": 0}
        with unittest.mock.patch.object(approvals, "write_decision", side_effect=approvals.AlreadyDecided(theirs)):
            code, _, err = self.run_cli("approve", "abcdef0123456789")
        self.assertEqual(code, 1)
        self.assertIn("already declined by sam at", err)

    def test_list_and_show_mark_decided_and_expired_cards(self):
        now = time.time()
        self.put(card("aaaa000000000000"))
        self.put(card("bbbb000000000000"))
        self.put(card("cccc000000000000", expires=now - 120))
        approvals.write_decision(self.state, card("bbbb000000000000"), "approve", "alice")
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        a = lines.index(next(l for l in lines if l.startswith("aaaa")))
        self.assertFalse(lines[a + 1].startswith("    ["), "an undecided card has no note")
        self.assertRegex(out, r"bbbb000000000000 .*\n    \[approved by alice at .*; waiting for the agent to retry\]")
        self.assertRegex(out, r"cccc000000000000 .*\n    \[expired; the agent's retry will make a fresh card\]")
        code, out, _ = self.run_cli("show", "bbbb")
        self.assertIn("decided        approve", out)
        self.assertIn("decided_by     alice", out)
        self.assertRegex(out, r"decided_at     \d{4}-\d\d-\d\d \d\d:\d\d")
        code, out, _ = self.run_cli("show", "aaaa")
        self.assertIn("decided        no", out)
        self.assertNotIn("decided_by", out)

    def test_summary_none_pending(self):
        code, out, _ = self.run_cli("summary")
        self.assertEqual(code, 0)
        self.assertEqual(out, "0 -1 0 0\n")

    def test_summary_several_with_oldest_age(self):
        now = time.time()
        self.put(card("aaaa000000000000", created=now - 900, expires=now + 600))  # 15 min old
        self.put(card("bbbb000000000000", created=now - 120, expires=now + 600))  # 2 min old
        code, out, _ = self.run_cli("summary")
        self.assertEqual(code, 0)
        self.assertEqual(out, "2 15 0 0\n")

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
        # bbbb is decided: waiting for the agent's retry, not for a person.
        self.assertEqual(out, "1 30 1 1\n")

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
        self.assertEqual(out, "1 30 1 0\n")

    def test_summarize_with_injectable_now(self):
        # A fixed `now` far from wall-clock time makes the expected ages exact,
        # independent of how long the test takes to run.
        now = 1_000_000.0
        self.put(card("aaaa000000000000", created=now - 600, expires=now - 1))  # expired, no decision
        self.put(card("bbbb000000000000", created=now - 1200, expires=now + 600))  # 20 min old, not expired
        self.assertEqual(approvals.summarize(self.state, now=now),
                          {"pending": 2, "oldest_minutes": 20, "expired_unhandled": 1, "decided": 0})

    def test_missing_state_dir(self):
        code, _, err = self.run_cli_with_state(self.state / "missing")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def run_cli_with_state(self, state):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = approvals.main(["--state", str(state), "list"])
        return code, out.getvalue(), err.getvalue()


class SharedWriterTests(unittest.TestCase):
    """card_fingerprint and write_decision, shared by this console and the
    dashboard's control unit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fingerprint(self):
        import hashlib
        c = card()
        fp = approvals.card_fingerprint(c)
        self.assertEqual(fp, hashlib.sha256(b"abcdef0123456789\0n0nce").hexdigest()[:16])
        self.assertEqual(len(fp), 16)
        self.assertNotIn("n0nce", fp)
        self.assertNotEqual(fp, approvals.card_fingerprint(card(nonce="other")))  # a new card, same id
        self.assertNotEqual(fp, approvals.card_fingerprint(card("abcdef01234567890")))
        self.assertEqual(len(approvals.card_fingerprint({"id": "x"})), 16)  # no nonce: still a fingerprint

    def test_write_decision(self):
        c = card()
        before = time.time()
        out = approvals.write_decision(self.state, c, "decline", "alice", "too big")
        path = self.state / "approvals" / "decisions" / "abcdef0123456789.json"
        rec = json.loads(path.read_text())
        self.assertEqual(rec, out)
        self.assertEqual({k: rec[k] for k in ("id", "nonce", "decision", "approver", "reason")},
                         {"id": c["id"], "nonce": "n0nce", "decision": "decline", "approver": "alice",
                          "reason": "too big"})
        self.assertGreaterEqual(rec["ts"], before)
        self.assertEqual(decision_files(path.parent), [path.name])  # no .tmp left behind
        with self.assertRaises(ValueError):
            approvals.write_decision(self.state, c, "maybe", "alice")

    def test_unsafe_ids_refused(self):
        for cid in ("../x", "a/b", "", "a" * 65, "a.b", "a b", "\u00e9", 5, None):
            with self.assertRaises(ValueError, msg=repr(cid)):
                approvals.write_decision(self.state, card(cid), "approve", "alice")
        approvals.write_decision(self.state, card("A-z_09" + "a" * 58), "approve", "alice")  # 64: fine
        self.assertEqual(decision_files(self.state / "approvals" / "decisions"), ["A-z_09" + "a" * 58 + ".json"])

    def test_non_text_ids_are_skipped(self):
        pending = self.state / "approvals" / "pending"
        pending.mkdir(parents=True)
        (pending / "int.json").write_text(json.dumps(card(12345)))
        (pending / "ok.json").write_text(json.dumps(card("ok1")))
        self.assertEqual([c["id"] for c in approvals.read_cards(self.state)], ["ok1"])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(approvals.main(["--state", str(self.state), "approve", "123"]), 1)  # no crash
        self.assertIn("no pending approval matches 123", err.getvalue())

    def test_cli_refuses_an_unsafe_id(self):
        pending = self.state / "approvals" / "pending"
        pending.mkdir(parents=True)
        (pending / "x.json").write_text(json.dumps(card("bad.id")))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(approvals.main(["--state", str(self.state), "approve", "bad.id"]), 1)
        self.assertIn("cannot decide bad.id", err.getvalue())
        self.assertFalse((self.state / "approvals" / "decisions" / "bad.id.json").exists())

    def test_concurrent_writers_exactly_one_wins(self):
        import threading
        folder = self.state / "approvals" / "decisions"
        for round_ in range(20):
            c = card("race{:02d}".format(round_), nonce="n{}".format(round_))
            if round_ % 2:  # half the rounds start from an earlier card's decision (another nonce)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "{}.json".format(c["id"])).write_text(json.dumps({"id": c["id"], "nonce": "old",
                                                                             "decision": "decline"}))
            start = threading.Barrier(6)
            won, lost, errors = [], [], []

            def write(name, verdict):
                start.wait()
                try:
                    won.append(approvals.write_decision(self.state, c, verdict, name, "r"))
                except approvals.AlreadyDecided as e:
                    lost.append(e.decision)
                except Exception as e:  # noqa: BLE001 - any other failure fails the test
                    errors.append(e)
            threads = [threading.Thread(target=write, args=("p{}".format(i), ("approve", "decline")[i % 2]))
                       for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(won), 1, "exactly one decision is written")
            self.assertEqual(len(lost), 5)
            rec = json.loads((folder / "{}.json".format(c["id"])).read_text())
            self.assertEqual(rec, won[0])
            for d in lost:  # every loser is told what stands
                self.assertEqual((d["decision"], d["approver"], d["nonce"]), (rec["decision"], rec["approver"], c["nonce"]))
        self.assertEqual(decision_files(folder), sorted("race{:02d}.json".format(i) for i in range(20)))

    def test_a_failed_write_leaves_nothing(self):
        c = card()
        folder = self.state / "approvals" / "decisions"
        with unittest.mock.patch.object(approvals.os, "link", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                approvals.write_decision(self.state, c, "decline", "dave", "no")
        self.assertEqual(decision_files(folder), [])  # the temp is cleaned up
        approvals.write_decision(self.state, c, "approve", "erin")  # and nothing blocks a real decision

    def test_second_decision_is_refused_both_orders(self):
        folder = self.state / "approvals" / "decisions"
        for first, second in (("approve", "decline"), ("decline", "approve")):
            c = card("{}first".format(first))
            rec = approvals.write_decision(self.state, c, first, "alice", "r1")
            with self.assertRaises(approvals.AlreadyDecided) as cm:
                approvals.write_decision(self.state, c, second, "bob", "r2")
            self.assertEqual(cm.exception.decision, rec)
            with self.assertRaises(approvals.AlreadyDecided):  # nor the same verdict again
                approvals.write_decision(self.state, c, first, "bob", "r2")
            on_disk = json.loads((folder / "{}.json".format(c["id"])).read_text())
            self.assertEqual((on_disk["decision"], on_disk["approver"], on_disk["reason"]), (first, "alice", "r1"))
            self.assertEqual(approvals.decided(self.state, c)["decided"], first)

    def test_other_nonce_decision_does_not_block_a_new_card(self):
        folder = self.state / "approvals" / "decisions"
        folder.mkdir(parents=True)
        old = {"id": "abcdef0123456789", "nonce": "earlier", "decision": "decline", "approver": "x", "ts": 1.0}
        (folder / "abcdef0123456789.json").write_text(json.dumps(old))
        c = card()  # same id, nonce "n0nce"
        self.assertIsNone(approvals.read_decision(self.state, c))
        self.assertEqual(approvals.decided(self.state, c), {"decided": None, "decided_by": None, "decided_at": None})
        rec = approvals.write_decision(self.state, c, "approve", "alice")
        self.assertEqual(json.loads((folder / "abcdef0123456789.json").read_text()), rec)
        self.assertEqual(approvals.decided(self.state, c)["decided_by"], "alice")
        # An unreadable file in the way is not this card's decision either.
        c2 = card("ffff000011112222")
        (folder / "ffff000011112222.json").write_text("{not json")
        approvals.write_decision(self.state, c2, "decline", "bob", "no")
        self.assertEqual(approvals.decided(self.state, c2)["decided"], "decline")

    def test_lock_file_must_be_a_regular_file(self):
        folder = self.state / "approvals" / "decisions"
        folder.mkdir(parents=True)
        os.symlink(str(self.state / "elsewhere"), str(folder / ".write.lock"))
        with self.assertRaises(OSError):
            approvals.write_decision(self.state, card(), "approve", "alice")
        self.assertFalse((self.state / "elsewhere").exists())  # the symlink was not followed
        self.assertEqual(decision_files(folder), [])

    def test_cli_writes_the_same_record(self):
        c = card()
        (self.state / "approvals" / "pending").mkdir(parents=True)
        (self.state / "approvals" / "pending" / "{}.json".format(c["id"])).write_text(json.dumps(c))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(approvals.main(["--state", str(self.state), "approve", c["id"], "--as", "bob"]), 0)
        rec = json.loads((self.state / "approvals" / "decisions" / "{}.json".format(c["id"])).read_text())
        self.assertEqual(set(rec), {"id", "nonce", "decision", "approver", "reason", "ts"})
        self.assertEqual((rec["decision"], rec["approver"], rec["reason"]), ("approve", "bob", ""))

if __name__ == "__main__":
    unittest.main()
