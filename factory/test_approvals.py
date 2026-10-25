"""Tests for factory/approvals.py. Ported from approvals_summary and approvals_status_line
(bin/factory); the bash copies were deleted with `status`, so the wording is pinned here.
The summary stub prints what scripts/approvals.py summary prints:
"pending oldest_min expired_unhandled approved running made failed".
"""

import os
import sys

from factory import approvals
from factory.fixtures import CommandTest


class ApprovalsTest(CommandTest):
    def setUp(self):
        super().setUp()
        sysdir = os.path.join(self.tmp, "sys")
        os.makedirs(sysdir)
        os.symlink(sys.executable, os.path.join(sysdir, "python3"))
        self.environ["PATH"] = sysdir

    def ctx(self, tty=False):
        ctx = super().ctx(tty)
        ctx.uname = ("Linux", "x86_64")  # never the host's: macOS would always pick /usr/bin/python3
        return ctx

    def stub(self, text, code=0):
        os.makedirs(os.path.join(self.paths.broker_state, "approvals", "pending"), exist_ok=True)
        self.write("repo/scripts/approvals.py",
                   "import sys\nopen(%r, 'w').write(' '.join(sys.argv[1:]))\nprint(%r)\nsys.exit(%d)\n"
                   % (os.path.join(self.tmp, "argv"), text, code))

    def line(self, text):
        self.stub(text)
        return approvals.status_line(self.ctx())


class SummaryTest(ApprovalsTest):
    def test_the_fields_are_split_on_whitespace(self):
        self.stub("1  2\t3 4 5 6 7")
        self.assertEqual(approvals.summary(self.ctx()), ["1", "2", "3", "4", "5", "6", "7"])
        with open(os.path.join(self.tmp, "argv")) as f:
            self.assertEqual(f.read(), "--state %s summary" % self.paths.broker_state)

    def test_none_without_the_approvals_folder(self):
        self.write("repo/scripts/approvals.py", "print('1 2 3 4 5 6 7')\n")
        self.assertIsNone(approvals.summary(self.ctx()))

    def test_none_when_the_script_fails(self):
        self.stub("1 2 3 4 5 6 7", code=1)
        self.assertIsNone(approvals.summary(self.ctx()))

    def test_none_without_python(self):
        self.stub("1 2 3 4 5 6 7")
        self.environ["PATH"] = os.path.join(self.tmp, "empty")
        os.makedirs(self.environ["PATH"])
        self.assertIsNone(approvals.summary(self.ctx()))


class StatusLineTest(ApprovalsTest):
    def test_the_line_is_the_counts_in_words(self):
        self.assertEqual(self.line("2 7 0 1 0 0 0"), "Approvals: 2 staged (oldest 7 min); 1 approved, being written - factory approvals")
        self.assertEqual(self.line("0 -1 0 1 1 3 2"), "Approvals: none staged; 2 approved, being written; 3 done; 2 failed - factory approvals")
        self.assertEqual(self.line("0 -1 0 0 0 0 1"), "Approvals: none staged; 1 failed - factory approvals")

    def test_nothing_to_show_is_no_line(self):
        self.assertIsNone(self.line("0 -1 0 0 0 0 0"))
        self.assertIsNone(self.line("0 -1 5 0 0 0 0"))
        self.assertIsNone(self.line(""))

    def test_fewer_fields_count_as_zero(self):
        self.assertEqual(self.line("1 3"), "Approvals: 1 staged (oldest 3 min) - factory approvals")

    def test_non_numbers_are_no_line(self):
        self.assertIsNone(self.line("x y z q r s t"))

    def test_the_label_is_bold_on_a_terminal(self):
        self.stub("1 3 0 0 0 0 0")
        self.assertEqual(approvals.status_line(self.ctx(True)), "\033[1mApprovals:\033[0m 1 staged (oldest 3 min) - factory approvals")

    def test_the_words_have_no_held_or_waiting_state(self):
        text = self.line("3 9 1 2 1 4 5").lower()
        for word in ("held", "waiting", "hold_seconds", "retry"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    import unittest
    unittest.main()
