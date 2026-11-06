"""The Thoughtfield's plain words for an order (step numbers, one-line state, the hover history, where labels go):
dashboard-ui/field-order.js is pure functions, checked under node by test_field_order.js. Skipped when node is missing.

  python3 -m unittest discover -s scripts -p 'test_field_order.py' -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("node"), "needs node")
class FieldOrderTests(unittest.TestCase):
    def test_order_words_history_and_label_placement(self):
        run = subprocess.run(["node", str(SCRIPTS / "test_field_order.js")], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
