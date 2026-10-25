"""Tests for factory/conf.py, the one KEY="value" parser. Run from the repo root:
  python3 -m unittest discover -s factory -t . -p 'test_*.py'
"""

import os
import subprocess
import tempfile
import unittest

from factory import conf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ParseTest(unittest.TestCase):
    def test_key_value_lines(self):
        self.assertEqual(conf.parse_conf('A="1"\nB_C=""\nD="two words"\n'),
                         {"A": "1", "B_C": "", "D": "two words"})

    def test_last_line_for_a_key_wins(self):
        self.assertEqual(conf.parse_conf('A="1"\nA="2"\n'), {"A": "2"})

    def test_comments_blanks_and_other_lines_are_skipped(self):
        text = '# A="no"\n\n  # B="no"\n  C="indented"\nD=unquoted\nE=\'single\'\nf="lower"\nexport G="x"\nH="ok"\n'
        self.assertEqual(conf.parse_conf(text), {"H": "ok"})

    def test_whitespace_after_the_closing_quote(self):
        self.assertEqual(conf.parse_conf('A="1"  \nB="2"\t\nC="3"\r\n'), {"A": "1", "B": "2", "C": "3"})

    def test_only_ascii_whitespace_ends_a_line(self):
        self.assertEqual(conf.parse_conf('A="1"\u00a0\nB="2"\u2003\n'), {})

    def test_text_after_the_closing_quote_is_not_a_match(self):
        self.assertEqual(conf.parse_conf('A="1" # note\nB="2"x\n'), {})

    def test_quotes_inside_a_value_are_kept(self):
        self.assertEqual(conf.parse_conf('A="x"y"\n'), {"A": 'x"y'})

    def test_no_trailing_newline(self):
        self.assertEqual(conf.parse_conf('A="1"'), {"A": "1"})

    def test_lines_end_at_newline_only(self):
        # str.splitlines would break the value at the form feed and drop the line.
        self.assertEqual(conf.parse_conf('A="x\x0cy"\nB="x\u2028y"\n'), {"A": "x\x0cy", "B": "x\u2028y"})

    def test_values_are_data_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "pwned")
            self.assertEqual(conf.parse_conf('A="$(touch %s)"\n' % marker), {"A": "$(touch %s)" % marker})
            self.assertFalse(os.path.exists(marker))

    def test_match_conf_line(self):
        self.assertEqual(conf.match_conf_line('LANES="2"'), ("LANES", "2"))
        self.assertIsNone(conf.match_conf_line("LANES=2"))
        self.assertIsNone(conf.match_conf_line(""))


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, data):
        path = os.path.join(self.tmp.name, "x.conf")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_absent_file_is_empty(self):
        missing = os.path.join(self.tmp.name, "missing.conf")
        self.assertEqual(conf.read_conf(missing), {})
        self.assertEqual(conf.conf_get(missing, "A"), "")
        self.assertEqual(conf.read_conf(os.path.join(missing, "below-a-file")), {})

    def test_conf_get(self):
        path = self.write(b'A="1"\nB="2"\nA="3"\n')
        self.assertEqual(conf.conf_get(path, "A"), "3")
        self.assertEqual(conf.conf_get(path, "B"), "2")
        self.assertEqual(conf.conf_get(path, "C"), "")

    def test_bytes_that_are_not_utf8_do_not_raise(self):
        path = self.write(b'A="\xff"\nB="ok"\n')
        self.assertEqual(conf.read_conf(path)["B"], "ok")
        self.assertEqual(conf.read_conf(path)["A"].encode("utf-8", "surrogateescape"), b"\xff")

    def test_a_directory_is_an_error_not_silence(self):
        with self.assertRaises(OSError):
            conf.read_conf(self.tmp.name)


class BashParityTest(unittest.TestCase):
    """scripts/lib/roles.sh still parse role.conf with sed; until they
    are ported, both must read every file the same."""

    CASES = [
        'A="1"\n', 'A="1"\nA="2"\n', 'A=""\n', 'A="x"y"\n', 'A="1"  \n', 'A="1"\t\n', 'A="1"\r\n',
        'A="1" # c\n', ' A="1"\n', '# A="1"\n', 'A=1\n', "A='1'\n", 'a="1"\n', 'A="1"', 'A="1"\n\n\nA="9"',
        'A="$(echo hi)"\n', 'A="`id`"\n', 'A="a\\"b"\n', 'A="a b  c"\n', 'A="  pad  "\n', 'A="ünï"\n',
        'A="1"\u00a0\n', 'A="1"\x0b\n', 'A="1"\x0c\n', 'A="x\x0cy"\n', 'A="&"\n', 'A="a/b|c"\n', 'A="\\1"\n',
    ]

    def test_conf_get_matches_bash(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i, text in enumerate(self.CASES):
                path = os.path.join(tmp, "c%d.conf" % i)
                with open(path, "wb") as f:
                    f.write(text.encode("utf-8"))
                cp = subprocess.run(
                    ["bash", "-c", 'ROOT="$1"; . "$1/scripts/lib/common.sh"; . "$1/scripts/lib/roles.sh"; conf_get "$2" A',
                     "_", ROOT, path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, LC_ALL="C.UTF-8"))
                self.assertEqual(cp.returncode, 0, cp.stderr)
                expected = cp.stdout.decode("utf-8").rstrip("\n")
                self.assertEqual(conf.conf_get(path, "A").rstrip("\n"), expected, repr(text))


if __name__ == "__main__":
    unittest.main()
