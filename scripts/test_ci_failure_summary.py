"""ci_failure_summary: a unittest log in, the failing tests' names and first error line out.

  python3 -m unittest discover -s scripts -p 'test_ci_failure_summary.py' -v
"""

import unittest

from ci_failure_summary import failures, render

LOG = """test_a (test_x.A.test_a) ... ok
test_b (test_x.A.test_b) ... FAIL
test_c (test_x.A.test_c) ... ERROR
test_d (test_x.A.test_d) ... skipped 'no'

======================================================================
ERROR: test_c (test_x.A.test_c)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/r/scripts/test_x.py", line 9, in test_c
    run()
subprocess.TimeoutExpired: Command '['node']' timed out after 1200 seconds

======================================================================
FAIL: test_b (test_x.A.test_b)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/r/scripts/test_x.py", line 5, in test_b
    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
AssertionError: 1 != 0 : not ok - packet clear of the witness
second line of the message
Error: this later line must not replace the first

----------------------------------------------------------------------
Ran 4 tests in 3.0s

FAILED (failures=1, errors=1, skipped=1)
"""


class FailureSummaryTests(unittest.TestCase):
    def test_lists_each_failure_with_the_first_error_line_only(self):
        self.assertEqual(
            failures(LOG),
            [
                ("ERROR", "test_x.A.test_c", "subprocess.TimeoutExpired: Command '['node']' timed out after 1200 seconds"),
                ("FAIL", "test_x.A.test_b", "AssertionError: 1 != 0 : not ok - packet clear of the witness"),
            ],
        )
        text = render(failures(LOG))
        self.assertIn("2 browser test failures", text)
        self.assertNotIn("second line", text)

    def test_nothing_to_say_when_all_passed(self):
        self.assertEqual(render(failures("test_a (test_x.A.test_a) ... ok\n\nRan 1 test in 1s\n\nOK\n")), "")


if __name__ == "__main__":
    unittest.main()
