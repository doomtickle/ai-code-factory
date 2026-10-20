"""Tests for the diff inspector's parser and sanitiser (scripts/patchview.py).

Fixtures come from real `git log` runs over throwaway repositories, with the
exact command and format the gateway uses, plus hand-written adversarial
patches.

  python3 -m unittest discover -s scripts -p 'test_patchview.py' -v
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import patchview as pv

FORMAT = "commit %H%nAuthor: %an <%ae>%nDate:   %aI%n%n%w(0,4,4)%B%n"
LOG = ["log", "-p", "--stat", "--cc", "--reverse", "--no-ext-diff", "--no-textconv", "--no-color",
       "--format=" + FORMAT]
SHA = "a" * 40


class Repo:
    """A throwaway git repository (no user config, no hooks)."""

    def __init__(self, add_cleanup):
        self.tmp = tempfile.TemporaryDirectory()
        add_cleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "COLUMNS"}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Dev",
                        GIT_AUTHOR_EMAIL="dev@example.com", GIT_COMMITTER_NAME="Dev",
                        GIT_COMMITTER_EMAIL="dev@example.com", GIT_AUTHOR_DATE="2026-10-20T12:00:00+02:00",
                        GIT_COMMITTER_DATE="2026-10-20T12:00:00+02:00")
        self.git("init", "-q", "-b", "main")

    def git(self, *args, stdin=None) -> str:
        return subprocess.run(["git", *args], cwd=self.path, env=self.env, check=True, capture_output=True,
                              input=stdin).stdout.decode("utf-8", "replace")

    def write(self, name, data):
        p = self.path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-F", "-", stdin=message.encode("utf-8"))
        return self.git("rev-parse", "HEAD").strip()

    def log(self, rng) -> str:
        return self.git(*LOG, rng)


def files(result):
    return [f for c in result["commits"] for f in c["files"]]


@unittest.skipIf(shutil.which("git") is None, "no git")
class RealGitTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo(self.addCleanup)
        self.r.write("f.txt", "one\ntwo\nthree\n")
        self.base = self.r.commit("init")

    def test_commit_header_message_and_stat_skipped(self):
        r = self.r
        r.write("f.txt", "one\n2\nthree\n")
        sha = r.commit("Subject\n\nBody line\ncommit " + "0" * 40 + "\ndiff --git a/x b/x\n@@ -1 +1 @@\n\nlast\n")
        text = r.log(f"{self.base}..HEAD")
        self.assertIn("\n    \n", text)  # blank message lines: four spaces
        out = pv.view(text, patch_bytes=len(text), files_total=1)
        self.assertEqual(len(out["commits"]), 1)
        c = out["commits"][0]
        self.assertEqual((c["sha"], c["author"], c["date"], c["merge"]),
                         (sha, "Dev <dev@example.com>", "2026-10-20T12:00:00+02:00", False))
        self.assertEqual(c["message"], "Subject\n\nBody line\ncommit " + "0" * 40
                         + "\ndiff --git a/x b/x\n@@ -1 +1 @@\n\nlast")
        self.assertEqual(len(c["files"]), 1)  # the fake headers in the message made no files
        f = c["files"][0]
        self.assertEqual((f["path"], f["old_path"], f["status"], f["adds"], f["dels"], f["flags"]),
                         ("f.txt", None, "M", 1, 1, []))
        self.assertEqual(f["hunks"][0]["header"], "@@ -1,3 +1,3 @@")
        self.assertEqual(f["hunks"][0]["lines"], [[" ", "one"], ["-", "two"], ["+", "2"], [" ", "three"]])
        self.assertEqual((out["truncated"], out["files_shown"], out["files_total"]), (False, 1, 1))

    def test_fake_headers_in_content_stay_content(self):
        r = self.r
        evil = ("--- a/f.txt\n+++ b/evil.txt\n@@ -1,1 +1,1 @@\ndiff --git a/x b/x\ncommit " + SHA + "\n"
                "[passdesk: patch truncated at 1 of 2 bytes]\n\\ No newline at end of file\n")
        r.write("f.txt", evil)
        r.write("g.txt", "after\n")
        r.commit("two files")
        out = pv.parse(r.log(f"{self.base}..HEAD"))
        fs = files(out)
        self.assertEqual([f["path"] for f in fs], ["f.txt", "g.txt"])
        lines = fs[0]["hunks"][0]["lines"]
        added = [t for k, t in lines if k == "+"]
        self.assertEqual(added, evil.splitlines())  # every fake header is an added line
        self.assertEqual(sum(1 for k, _ in lines if k == "-"), 3)
        self.assertFalse(out["truncated"])
        self.assertEqual(fs[1]["hunks"][0]["lines"], [["+", "after"]])
        self.assertEqual(fs[1]["status"], "A")

    def test_removed_lines_that_look_like_file_headers(self):
        r = self.r
        r.write("f.txt", "-- a/x\n++ b/x\nkeep\n")
        r.commit("dashes")
        r.write("f.txt", "keep\n")
        r.commit("drop them")
        out = pv.parse(r.log("HEAD~1..HEAD"))
        f = files(out)[0]
        self.assertEqual(f["hunks"][0]["lines"], [["-", "-- a/x"], ["-", "++ b/x"], [" ", "keep"]])
        self.assertEqual((f["adds"], f["dels"]), (0, 2))

    def test_binary_rename_delete_no_newline(self):
        r = self.r
        r.write("b.bin", b"\x00\x01\x02bin")
        r.write("gone.txt", "bye\n")
        r.commit("add")
        r.git("mv", "f.txt", "g.txt")
        r.write("g.txt", "one\ntwo\nthree\nfour")
        (r.path / "gone.txt").unlink()
        r.write("b.bin", b"\x00\x01\x02bin2")
        r.commit("change")
        out = pv.parse(r.log("HEAD~1..HEAD"))
        by = {f["path"]: f for f in files(out)}
        self.assertEqual(by["b.bin"]["status"], "B")
        self.assertEqual(by["b.bin"]["hunks"], [])
        self.assertEqual(by["gone.txt"]["status"], "D")
        self.assertEqual(by["gone.txt"]["hunks"][0]["lines"], [["-", "bye"]])
        g = by["g.txt"]
        self.assertEqual((g["status"], g["old_path"]), ("R", "f.txt"))
        self.assertEqual(g["hunks"][0]["lines"][-2:], [["+", "four"], ["\\", "No newline at end of file"]])

    def test_odd_names(self):
        r = self.r
        r.write("sp ace.txt", "x\n")
        r.write("tab\tqé.txt", "y\n")
        r.write("rtl‮.txt", "z\n")
        r.commit("names")
        out = pv.parse(r.log("HEAD~1..HEAD"))
        by = {f["path"]: f for f in files(out)}
        self.assertIn("sp ace.txt", by)
        self.assertIn("tab\tqé.txt", by)
        self.assertEqual(by["rtl⟨U+202E⟩.txt"]["flags"], ["bidi"])

    def test_crlf_bidi_control_longline(self):
        r = self.r
        r.write("crlf.txt", "a\r\nb\r\n")
        r.write("bidi.py", "x = 1  # ‮⁦ admin ⁩‬\nzw​sp﻿\U000e0041\n")
        r.write("ctl.txt", "bell\x07 esc\x1b[31m del\x7f c1\x85 tab\tok\n")
        r.write("long.txt", "y" * 5000 + "\n")
        r.commit("nasty")
        out = pv.parse(r.log("HEAD~1..HEAD"))
        by = {f["path"]: f for f in files(out)}
        self.assertEqual(by["crlf.txt"]["flags"], ["crlf"])
        self.assertEqual(by["crlf.txt"]["hunks"][0]["lines"], [["+", "a"], ["+", "b"]])
        b = by["bidi.py"]
        self.assertEqual(b["flags"], ["bidi", "invisible"])
        self.assertEqual(b["hunks"][0]["lines"][0][1], "x = 1  # ⟨U+202E⟩⟨U+2066⟩ admin ⟨U+2069⟩⟨U+202C⟩")
        self.assertEqual(b["hunks"][0]["lines"][1][1], "zw⟨U+200B⟩sp⟨U+FEFF⟩⟨U+E0041⟩")
        c = by["ctl.txt"]
        self.assertEqual(c["flags"], ["control"])
        self.assertEqual(c["hunks"][0]["lines"][0][1],
                         "bell⟨U+0007⟩ esc⟨U+001B⟩[31m del⟨U+007F⟩ c1⟨U+0085⟩ tab\tok")
        ln = by["long.txt"]
        self.assertEqual(ln["flags"], ["longline"])
        self.assertEqual(ln["hunks"][0]["lines"][0][1], "y" * 2000 + "⟨… 3000 more chars⟩")

    def test_merge_cc(self):
        r = self.r
        r.git("checkout", "-q", "-b", "side")
        r.write("f.txt", "one\ntwo\nthree\nside\n")
        r.commit("side")
        r.git("checkout", "-q", "main")
        r.write("f.txt", "zero\none\ntwo\nthree\n")
        r.commit("main")
        r.git("merge", "-q", "--no-ff", "--no-commit", "side")
        r.write("f.txt", "zero\none\ntwo\nthree\nside\nevil\n")
        r.write("m.txt", "m\n")
        merge = r.commit("merge side")
        text = r.log(f"{self.base}..HEAD")
        self.assertIn("diff --cc", text)
        out = pv.parse(text)
        mc = [c for c in out["commits"] if c["sha"] == merge][0]
        self.assertTrue(mc["merge"])
        by = {f["path"]: f for f in mc["files"]}
        f = by["f.txt"]
        self.assertTrue(f["hunks"][0]["header"].startswith("@@@ "))
        lines = f["hunks"][0]["lines"]
        self.assertTrue(all(len(x) == 3 and len(x[2]) == 2 for x in lines))
        self.assertIn(["+", "evil", "++"], lines)  # in neither parent: the evil-merge line
        self.assertEqual(f["flags"], [])
        self.assertEqual(by["m.txt"]["hunks"][0]["lines"], [["+", "m", "++"]])
        self.assertEqual([c["sha"] != merge and not c["merge"] for c in out["commits"]].count(True), 2)

    def test_truncation_mid_hunk_and_mid_header(self):
        r = self.r
        r.write("f.txt", "".join(f"line {i}\n" for i in range(200)))
        r.write("z.txt", "z\n")
        r.commit("big")
        text = r.log("HEAD~1..HEAD")
        full = pv.parse(text)
        for cut in (text.index("line 50"), text.index("+++ b/z.txt"), text.index("Date:"), 0):
            body = text[:cut]
            body = body[:body.rfind("\n") + 1] if "\n" in body else ""
            t = body + f"[passdesk: patch truncated at {len(body)} of {len(text)} bytes]\n"
            out = pv.view(t, patch_bytes=len(t), files_total=2)
            self.assertTrue(out["truncated"], cut)
            for f in files(out):
                for h in f["hunks"]:
                    for k, line in h["lines"]:
                        self.assertNotIn("passdesk: patch truncated", line)
                self.assertNotIn("malformed", f["flags"])
        out = pv.view(text[:text.index("line 50")] + "[passdesk: patch truncated at 1 of 2 bytes]\n",
                      patch_bytes=1, files_total=2)
        self.assertEqual(out["files_shown"], 1)
        self.assertLess(len(files(out)[0]["hunks"][0]["lines"]), len(files(full)[0]["hunks"][0]["lines"]))
        only = pv.view("[passdesk: patch truncated at 0 of 99 bytes]\n", patch_bytes=45, files_total=3)
        self.assertEqual((only["truncated"], only["commits"], only["files_shown"]), (True, [], 0))

    def test_several_commits_oldest_first(self):
        r = self.r
        shas = []
        for i in range(3):
            r.write("f.txt", f"v{i}\n")
            shas.append(r.commit(f"c{i}"))
        out = pv.parse(r.log(f"{self.base}..HEAD"))
        self.assertEqual([c["sha"] for c in out["commits"]], shas)
        self.assertEqual([c["message"] for c in out["commits"]], ["c0", "c1", "c2"])


class HandWrittenTests(unittest.TestCase):
    def head(self, message="m"):
        return f"commit {SHA}\nAuthor: A <a@b>\nDate:   2026-10-20T00:00:00Z\n\n    {message}\n\n---\n x | 1 +\n\n"

    def test_trailer_only_counts_as_the_last_line(self):
        t = self.head() + ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n"
                           "-[passdesk: patch truncated at 1 of 2 bytes]\n+b\n c\n")
        out = pv.parse(t)
        self.assertFalse(out["truncated"])
        self.assertEqual(files(out)[0]["hunks"][0]["lines"][0], ["-", "[passdesk: patch truncated at 1 of 2 bytes]"])
        self.assertTrue(pv.parse(t + "[passdesk: patch truncated at 3 of 4 bytes]\n")["truncated"])
        self.assertFalse(pv.parse(t + "[passdesk: patch truncated at 3 of 4 bytes] \n")["truncated"])

    def test_counts_not_prefixes(self):
        # Counts say one old, one new line: the "@@" and "diff" lines after them
        # start new structure; inside the counts they would be content.
        t = self.head() + ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n"
                           " @@ -9 +9 @@\n-diff --git a/y b/y\n+commit " + SHA + "\n ok\n"
                           "@@ -20 +20 @@\n-q\n+r\n")
        f = files(pv.parse(t))[0]
        self.assertEqual(len(f["hunks"]), 2)
        self.assertEqual(f["hunks"][0]["lines"], [[" ", "@@ -9 +9 @@"], ["-", "diff --git a/y b/y"],
                                                  ["+", "commit " + SHA], [" ", "ok"]])
        self.assertEqual((f["adds"], f["dels"], f["flags"]), (2, 2, []))

    def test_malformed(self):
        t = self.head() + ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\nGARBAGE\n"
                           "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n-b\n+c\n"
                           "diff --git a/z b/z\n--- a/z\n+++ b/z\n@@ nonsense @@\n+x\n")
        fs = files(pv.parse(t))
        self.assertEqual([f["path"] for f in fs], ["x", "y", "z"])
        self.assertEqual([f["flags"] for f in fs], [["malformed"]] * 3)
        self.assertEqual(fs[0]["hunks"][0]["lines"], [[" ", "a"]])
        self.assertEqual(fs[1]["hunks"][0]["lines"], [["-", "a"]])  # a second "-" is more than -1 allows
        self.assertEqual(fs[2]["hunks"], [])

    def test_empty_counts_and_markers(self):
        t = self.head() + ("diff --git a/x b/x\nnew file mode 100644\n--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n"
                           "+a\n\\ No newline at end of file\n"
                           "diff --git a/e b/e\nnew file mode 100644\nindex 0000000..e69de29\n"
                           "diff --git a/m b/m\nold mode 100644\nnew mode 100755\n")
        fs = files(pv.parse(t))
        self.assertEqual(fs[0]["hunks"][0]["lines"], [["+", "a"], ["\\", "No newline at end of file"]])
        self.assertEqual((fs[1]["path"], fs[1]["status"], fs[1]["hunks"]), ("e", "A", []))
        self.assertEqual((fs[2]["path"], fs[2]["status"]), ("m", "M"))

    def test_bare_diff_and_garbage(self):
        out = pv.parse("hello\n\x00\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual(out["commits"][0]["sha"], "")
        self.assertEqual(files(out)[0]["path"], "x")
        self.assertEqual(pv.parse(""), {"truncated": False, "commits": []})
        self.assertEqual(pv.parse("\n\n\n random\n"), {"truncated": False, "commits": []})

    def test_split_only_on_newline(self):
        # \x0b, \x1c, \x85, U+2028 are content, never line ends.
        t = self.head("a\x1cb c") + "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\x0bb\n+c\x85d e\n"
        out = pv.parse(t)
        self.assertEqual(out["commits"][0]["message"], "a⟨U+001C⟩b⟨U+2028⟩c")
        self.assertEqual(out["commits"][0]["flags"], ["control"])
        self.assertEqual(files(out)[0]["hunks"][0]["lines"], [["-", "a⟨U+000B⟩b"], ["+", "c⟨U+0085⟩d⟨U+2029⟩e"]])

    def test_header_text_is_sanitised(self):
        t = self.head() + "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@ def f(‮):\n-a\n+b\n"
        f = files(pv.parse(t))[0]
        self.assertEqual(f["hunks"][0]["header"], "@@ -1 +1 @@ def f(⟨U+202E⟩):")
        self.assertEqual(f["flags"], ["bidi"])

    def test_cc_hunk_rules(self):
        t = self.head() + ("diff --cc m\nindex 1,2..3\n--- a/m\n+++ b/m\n@@@ -1,1 -1,1 +1,2 @@@\n"
                           "  same\n++new\n- gone\n")
        f = files(pv.parse(t))[0]
        self.assertEqual(f["flags"], ["malformed"])  # "- gone" would make parent 1 owe two lines
        t = self.head() + ("diff --cc m\n--- a/m\n+++ b/m\n@@@ -1,2 -1,1 +1,2 @@@\n"
                           "  same\n++new\n- gone\n")
        f = files(pv.parse(t))[0]
        self.assertEqual(f["flags"], [])
        self.assertEqual(f["hunks"][0]["lines"], [[" ", "same", "  "], ["+", "new", "++"], ["-", "gone", "- "]])
        self.assertEqual((f["adds"], f["dels"]), (1, 1))
        # Three parents: four @, three ranges.
        t = self.head() + "diff --cc m\n@@@@ -1 -1 -1 +1 @@@@\n   x\n"
        self.assertEqual(files(pv.parse(t))[0]["hunks"][0]["lines"], [[" ", "x", "   "]])
        t = self.head() + "diff --cc m\n@@@@ -1 -1 +1 @@@@\n   x\n"
        self.assertEqual(files(pv.parse(t))[0]["flags"], ["malformed"])

    def test_paths(self):
        self.assertEqual(pv.git_paths('"a/x\\303\\251" "b/x\\303\\251"'), ("xé", "xé"))
        self.assertEqual(pv.git_paths("a/a b/c b/a b/c"), ("a b/c", "a b/c"))
        self.assertEqual(pv.header_path("b/sp ace\t"), "sp ace")
        self.assertIsNone(pv.header_path("/dev/null"))
        self.assertEqual(pv.unquote('"a\\"b\\\\c\\t"rest'), ('a"b\\c\t', "rest"))

    def test_response_cap(self):
        body = "".join(f"+{'x' * 100}\n" for _ in range(50))
        t = self.head()
        for i in range(40):
            t += f"diff --git a/f{i} b/f{i}\n--- /dev/null\n+++ b/f{i}\n@@ -0,0 +1,50 @@\n{body}"
        out = pv.view(t, patch_bytes=len(t), files_total=40, max_response=60000)
        self.assertTrue(out["truncated"])
        self.assertLess(len(json.dumps(out, ensure_ascii=False).encode()), 60000)
        self.assertTrue(0 < out["files_shown"] < 40)
        self.assertEqual(out["files_total"], 40)
        full = pv.view(t, patch_bytes=len(t), files_total=40)
        self.assertEqual((full["truncated"], full["files_shown"]), (False, 40))
        self.assertTrue(pv.view(t, patch_bytes=1, files_total=40, card_truncated=True)["truncated"])
        self.assertIsNone(pv.view("", patch_bytes=0, files_total="7")["files_total"])
        self.assertIsNone(pv.view("", patch_bytes=0, files_total=True)["files_total"])

    def test_eof_mid_hunk(self):
        t = self.head() + "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n-b\n"
        f = files(pv.parse(t))[0]
        self.assertEqual(f["flags"], ["malformed"])  # git never stops mid-hunk: lines were lost
        f = files(pv.parse(t + "[passdesk: patch truncated at 9 of 99 bytes]\n"))[0]
        self.assertEqual(f["flags"], [])  # the cap did it, and says so
        self.assertEqual(f["hunks"][0]["lines"], [[" ", "a"], ["-", "b"]])

    def three_files(self, cut_in=None):
        t = self.head()
        for name in ("a.py", "b.py", "c.py"):
            t += f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1,2 +1,2 @@\n x\n-y\n+z\n"
        if cut_in:
            t = t[:t.index(f"diff --git a/{cut_in}")] + "[passdesk: patch truncated at 1 of 2 bytes]\n"
        return t

    def test_cut_marker_and_missing_files(self):
        t = self.three_files(cut_in="b.py")
        out = pv.view(t, patch_bytes=1, files_total=3, card_files=["a.py", "b.py", "c.py"])
        self.assertEqual((out["truncated"], out["files_shown"], out["files_missing"], out["files_missing_more"]),
                         (True, 1, ["b.py", "c.py"], 0))
        last = files(out)[-1]["hunks"][-1]["lines"][-1]
        self.assertEqual(last, ["✂", "CUT HERE · the rest of this push is not shown"])
        self.assertEqual(files(out)[-1]["adds"], 1)  # the marker counts as nothing
        # The card's list capped (file_count 80, 2 names): the rest is counted, not named.
        out = pv.view(t, patch_bytes=1, files_total=80, card_files=["a.py", "b\u202e.py"])
        self.assertEqual((out["files_shown"], out["files_missing"], out["files_missing_more"]),
                         (1, ["b⟨U+202E⟩.py"], 78))
        # A cut inside a file's headers: the marker gets a hunk of its own.
        t = self.three_files()
        t = t[:t.index("+++ b/b.py")] + "[passdesk: patch truncated at 1 of 2 bytes]\n"
        f = files(pv.view(t, patch_bytes=1, files_total=3))[-1]
        self.assertEqual((f["path"], f["hunks"]), ("b.py", [{"header": "", "lines": [list(pv.CUT)]}]))
        # Not truncated: no marker, nothing missing, even if the card lists more.
        out = pv.view(self.three_files(), patch_bytes=1, files_total=3, card_files=["a.py", "b.py", "c.py", "d.py"])
        self.assertEqual((out["truncated"], out["files_missing"], out["files_missing_more"]), (False, [], 0))
        self.assertNotIn("✂", json.dumps(out, ensure_ascii=False))
        self.assertEqual(pv.view("[passdesk: patch truncated at 0 of 9 bytes]\n", patch_bytes=1, files_total=2,
                                 card_files=["a", "b"])["files_missing"], ["a", "b"])

    def test_files_shown_never_exceeds_the_card(self):
        # A merge's --cc section and its commits can name files the net change
        # doesn't (a file changed, then changed back): count the card's own files.
        t = self.three_files()
        out = pv.view(t, patch_bytes=1, files_total=1, card_files=["c.py"])
        self.assertEqual(out["files_shown"], 1)
        out = pv.view(t, patch_bytes=1, files_total=2, card_files=["c.py"])  # list incomplete
        self.assertEqual(out["files_shown"], 2)                             # min(3 distinct, 2)
        out = pv.view(t, patch_bytes=1, files_total=None, card_files=None)
        self.assertEqual(out["files_shown"], 3)
        # A rename counts by either name.
        t = self.head() + "diff --git a/o b/n\nsimilarity index 90%\nrename from o\nrename to n\n"
        self.assertEqual(pv.view(t, patch_bytes=1, files_total=2, card_files=["o", "n"])["files_shown"], 2)

    def test_sanitised_blowup_is_capped(self):
        # 2 MiB of bidi controls: every one becomes a 8-character token.
        line = "‮" * 1999
        t = self.head() + "diff --git a/x b/x\n--- /dev/null\n+++ b/x\n@@ -0,0 +1,350 @@\n" + f"+{line}\n" * 350
        out = pv.view(t, patch_bytes=len(t.encode()), files_total=1)
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False).encode()), pv.MAX_RESPONSE)
        self.assertTrue(out["truncated"])


if __name__ == "__main__":
    unittest.main()
