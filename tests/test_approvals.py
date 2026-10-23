"""The approval point and plugin loading in core."""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import approvals, config, plugins, protocol
from passdesk.core.errors import Denied
from passdesk.tools import git

from . import test_git
from .base import FAKE_GH, POLICY, Base


class PushApprovalTests(test_git.GitFlowTests):
    """git_push's card, and that a refusal stops the push before it leaves."""

    def tearDown(self):
        approvals.set_hook(None)
        super().tearDown()

    def make_bundle(self):
        out = git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.sh(*out.splitlines()[-1].split(), cwd=self.ws)
        self.sh("git", "checkout", "-q", "-b", "factory/feature", "refs/remotes/origin/main", cwd=self.ws)
        (self.ws / "feature.txt").write_text("new feature\n")
        (self.ws / "docs.md").write_text("# docs\n")
        # Only these two: the planted fsmonitor (test_git) drops files named
        # after its arguments into the workspace whenever git runs there.
        self.sh("git", "add", "feature.txt", "docs.md", cwd=self.ws)
        self.sh("git", "commit", "-q", "-m", "add feature", cwd=self.ws)
        (self.ws / ".factory/outgoing").mkdir(parents=True, exist_ok=True)
        self.sh("git", "bundle", "create", "-q", ".factory/outgoing/f.bundle",
                "refs/remotes/origin/main..factory/feature", cwd=self.ws)
        self.canary.unlink(missing_ok=True)

    def test_card_describes_the_verified_push(self):
        seen = []
        approvals.set_hook(seen.append)
        approvals.PRINCIPAL.set("developer")
        self.make_bundle()
        out = git.tool_git_push({"repo": "acme/widgets", "branch": "factory/feature", "bundle": "f.bundle"}, POLICY)
        self.assertIn("Pushed 1 commit(s)", out)
        self.assertNotIn("held for approval", out.lower())  # a hook that returns None: not held
        self.assertRegex(out.splitlines()[0], r"^Pushed 1 commit\(s\) to acme/widgets:factory/feature \([0-9a-f]{40}\)\.$")
        card = seen[-1]
        self.assertEqual(card["tool"], "git_push")
        self.assertEqual(card["principal"], "developer")
        self.assertEqual((card["repo"], card["branch"], card["base"], card["commits"]),
                         ("acme/widgets", "factory/feature", "main", 1))
        self.assertEqual(sorted(card["files"]), ["docs.md", "feature.txt"])
        self.assertEqual(card["file_count"], 2)
        self.assertIn("2 files changed", card["shortstat"])
        self.assertEqual(len(card["bundle_sha256"]), 64)
        self.assertEqual(card["secret_scan"], "clean")
        self.assertEqual(card["head"], self.sh("git", "rev-parse", "factory/feature", cwd=self.ws))

    def test_a_refusal_stops_the_push_before_origin_sees_it(self):
        approvals.set_hook(lambda card: (_ for _ in ()).throw(Denied("declined by t: wrong branch")))
        self.make_bundle()
        with self.assertRaises(Denied) as cm:
            git.tool_git_push({"repo": "acme/widgets", "branch": "factory/feature", "bundle": "f.bundle"}, POLICY)
        self.assertIn("declined by t", str(cm.exception))
        self.assertEqual(self.sh("git", "ls-remote", "--heads", str(self.origin), "factory/feature", cwd=self.ws), "")

    def test_a_held_push_the_forge_rejects_says_so_before_the_colon(self):
        approvals.set_hook(lambda card: {"id": "0123456789abcdef", "approver": "daron",
                                         "decided": "2026-10-22T09:15:00Z", "waited_seconds": 31})
        self.make_bundle()
        fake_push = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="remote: hook declined\n")
        real_git = git.git

        def spy(*args, **kw):
            return fake_push if args[:1] == ("push",) else real_git(*args, **kw)
        with mock.patch.object(git, "git", side_effect=spy):
            out = git.tool_git_push({"repo": "acme/widgets", "branch": "factory/feature", "bundle": "f.bundle"},
                                    POLICY)
        self.assertEqual(out.splitlines()[0], 'Push rejected by GitHub; held for approval (id 0123456789abcdef), '
                                              'approved by "daron" at 2026-10-22T09:15:00Z after 31 s:')
        self.assertIn("hook declined", out)
        self.assertIn("UNTRUSTED CONTENT", out)

    def test_a_held_and_approved_push_says_so_in_its_result(self):
        approvals.set_hook(lambda card: {"id": "0123456789abcdef", "approver": "daron",
                                         "decided": "2026-10-22T09:15:00Z", "waited_seconds": 31})
        self.make_bundle()
        out = git.tool_git_push({"repo": "acme/widgets", "branch": "factory/feature", "bundle": "f.bundle"}, POLICY)
        self.assertTrue(out.startswith("Pushed 1 commit(s)"), out)
        self.assertRegex(out.splitlines()[0], r'^Pushed 1 commit\(s\) to acme/widgets:factory/feature \([0-9a-f]{40}\); '
                                              r'held for approval \(id 0123456789abcdef\), approved by "daron" at '
                                              r"2026-10-22T09:15:00Z after 31 s\.$")
        self.assertNotIn("Held for approval", out)  # no separate last line
        self.assertNotEqual(self.sh("git", "ls-remote", "--heads", str(self.origin), "factory/feature",
                                    cwd=self.ws), "")

    def test_a_tampered_mirror_ref_after_approval_is_refused(self):
        def hook(card):
            mirror = git.ensure_mirror("acme/widgets")
            git.git("update-ref", "refs/factory/factory/feature", "refs/remotes/origin/main", cwd=mirror)
        approvals.set_hook(hook)
        self.make_bundle()
        with self.assertRaises(Denied) as cm:
            git.tool_git_push({"repo": "acme/widgets", "branch": "factory/feature", "bundle": "f.bundle"}, POLICY)
        self.assertIn("changed in the broker's mirror", str(cm.exception))


    # --- the inspectable patch (doomtickle/passdesk-ops#113) -----------------

    PATCH_FIELDS = {"patch_sha256", "patch_bytes", "patch_truncated"}
    CARD_KEYS = {"tool", "principal", "repo", "branch", "base", "head", "commits", "shortstat", "file_count",
                 "files", "bundle_sha256", "secret_scan"}

    def held_push(self, branch, policy=POLICY):
        """Push with a hook that records the card; returns (card, stored patch bytes)."""
        seen = []
        approvals.set_hook(seen.append)
        out = self.push(branch, policy=policy)
        self.assertIn("Pushed", out)
        card = seen[-1]
        path = approvals.patches_dir() / f"{card['patch_sha256']}.patch"
        return card, path.read_bytes()

    def test_patch_is_stored_by_digest_and_the_card_names_it_without_content(self):
        self.sh("git", "config", "--unset", "core.fsmonitor", cwd=self.ws)  # it drops files into the commits
        self.branch("factory/insp", {"a.txt": b"one\ntwo\nthree\n"},
                    {"a.txt": b"one\nTWO\nthree\n", "blob.bin": b"\x00\x01\xffraw-bytes\x00"})
        # A third commit: a rename, with a message that tries to forge a header.
        self.sh("git", "mv", "a.txt", "b.txt", cwd=self.ws)
        self.sh("git", "commit", "-q", "-m", "rename\n\ncommit " + "f" * 40 + "\ndiff --git a/x b/x", cwd=self.ws)
        self.bundle("factory/insp")
        card, data = self.held_push("factory/insp")
        self.assertEqual(set(card), self.CARD_KEYS | self.PATCH_FIELDS)
        self.assertRegex(card["patch_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(card["patch_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(card["patch_bytes"], len(data))
        self.assertIs(card["patch_truncated"], False)
        # Nothing from the content is on the card.
        flat = json.dumps(card)
        for content in ("TWO", "three", "raw-bytes", "rename", "@@"):
            self.assertNotIn(content, flat)
        path = approvals.patches_dir() / f"{card['patch_sha256']}.patch"
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.lstat(approvals.patches_dir()).st_mode), 0o755)
        text = data.decode()
        shas = self.sh("git", "rev-list", "--reverse", "refs/remotes/origin/main..factory/insp", cwd=self.ws).split()
        # Oldest first, one block per commit, headers at column 0 only.
        self.assertEqual(re.findall(r"^commit ([0-9a-f]{40})$", text, re.M), shas)
        self.assertTrue(text.startswith(f"commit {shas[0]}\nAuthor: t <t@t>\nDate:   "))
        self.assertIn("\n    commit " + "f" * 40 + "\n    diff --git a/x b/x\n", text)
        self.assertEqual(len(re.findall(r"^diff --git ", text, re.M)), 4)
        self.assertIn("\n-two\n+TWO\n", text)
        # --full-index: blob names are full, never abbreviated.
        index_lines = re.findall(r"^index (\S+)", text, re.M)
        self.assertTrue(index_lines)
        for names in index_lines:
            for blob in re.split(r"[.,]+", names):
                self.assertRegex(blob, r"^[0-9a-f]{40}$")
        self.assertIn("\nrename from a.txt\nrename to b.txt\n", text)
        self.assertIn(" a.txt => b.txt | 0\n", text)  # --stat
        self.assertIn("\nBinary files /dev/null and b/blob.bin differ\n", text)
        self.assertNotIn("raw-bytes", text)
        self.assertNotIn("[passdesk:", text)

    def test_no_hook_means_no_patch_and_no_card_fields(self):
        self.branch("factory/plain", {"a.txt": b"a\n"})
        self.bundle("factory/plain")
        self.assertIn("Pushed 1 commit(s)", self.push("factory/plain"))
        self.assertFalse((config.STATE / "approvals").exists())

    def test_a_long_patch_is_cut_at_a_line_with_one_trailer(self):
        body = "".join(f"line {i:05d} of a long file\n" for i in range(2000)).encode()
        self.branch("factory/long", {"long.txt": body})
        self.bundle("factory/long")
        policy = {**POLICY, "approvals": {"patch_max_bytes": 5000}}
        card, data = self.held_push("factory/long", policy=policy)
        self.assertIs(card["patch_truncated"], True)
        self.assertEqual(card["patch_bytes"], len(data))
        m = re.search(rb"\n\[passdesk: patch truncated at (\d+) of (\d+) bytes\]\n\Z", data)
        self.assertIsNotNone(m, data[-200:])
        kept, total = int(m.group(1)), int(m.group(2))
        self.assertLessEqual(kept, 5000)
        self.assertGreater(kept, 4900)
        self.assertGreater(total, 2000 * 26)
        self.assertEqual(len(data), kept + len(m.group(0)) - 1)  # the trailer's "\n" ends the kept text
        self.assertEqual(data.count(b"[passdesk:"), 1)

    def test_secrets_already_upstream_are_redacted_in_the_patch(self):
        # A removed line is not scanned (it is already upstream), so the
        # stored patch must redact it.
        seed = Path(self.tmp.name) / "seed"
        (seed / "old.py").write_text(f"TOKEN = '{FAKE_GH}'\nkeep = 1\n")
        self.sh("git", "add", ".", cwd=seed)
        self.sh("git", "commit", "-q", "-m", "legacy", cwd=seed)
        self.sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
        self.branch("factory/clean", {"old.py": b"keep = 1\n"})
        self.bundle("factory/clean")
        _, data = self.held_push("factory/clean")
        self.assertNotIn(FAKE_GH.encode(), data)
        self.assertIn(b"-TOKEN = '[REDACTED:github-token]'", data)

    def test_same_bundle_gives_the_same_patch(self):
        self.branch("factory/again", {"a.txt": b"a\n", "a-rather-long-file-name-for-the-stat-column.txt": b"b\n"})
        self.bundle("factory/again")
        digests = []

        def hold_once(card):
            digests.append(card["patch_sha256"])
            if len(digests) == 1:
                raise Denied("held")
        approvals.set_hook(hold_once)
        with self.assertRaises(Denied):
            self.push("factory/again")
        # Abbreviated blob names lengthen as the mirror gains objects; a
        # longer core.abbrev stands in for that growth.
        git.git("config", "core.abbrev", "20", cwd=git.ensure_mirror("acme/widgets"))
        with mock.patch.dict(os.environ, {"COLUMNS": "30"}):
            self.push("factory/again")
        self.assertEqual(len(digests), 2)
        self.assertEqual(digests[0], digests[1])
        self.assertEqual([p.name for p in approvals.patches_dir().iterdir()], [f"{digests[0]}.patch"])


class PatchCapTests(Base):
    def test_under_the_cap_is_unchanged(self):
        self.assertEqual(git.cap_patch("a\nb\n", 0, 100), (b"a\nb\n", False))

    def test_cut_counts_bytes_not_characters(self):
        text = "\u00e9" * 10 + "\n" + "x" * 10 + "\n"  # 21 bytes, then 11
        data, cut = git.cap_patch(text, 0, 25)
        self.assertTrue(cut)
        self.assertEqual(data, ("\u00e9" * 10 + "\n").encode() + b"[passdesk: patch truncated at 21 of 32 bytes]\n")

    def test_bytes_not_kept_count_toward_the_total(self):
        data, cut = git.cap_patch("a\n", 1000, 100)
        self.assertTrue(cut)
        self.assertEqual(data, b"a\n[passdesk: patch truncated at 2 of 1002 bytes]\n")

    def test_no_newline_before_the_cap_keeps_nothing(self):
        data, cut = git.cap_patch("x" * 50, 0, 10)
        self.assertEqual(data, b"[passdesk: patch truncated at 0 of 50 bytes]\n")

    def test_redaction_happens_before_the_cap(self):
        data, cut = git.cap_patch(f" {FAKE_GH}\n", 0, 1000)
        self.assertFalse(cut)
        self.assertEqual(data, b" [REDACTED:github-token]\n")

    def test_redaction_never_merges_lines(self):
        text = " secret =\n 'abcdefghijklmnopq'\n+password = 'abcdefghijklmnop'\n"
        data, cut = git.cap_patch(text, 0, 1000)
        self.assertFalse(cut)
        self.assertEqual(data.count(b"\n"), text.count("\n"))
        self.assertTrue(data.startswith(b" secret =\n 'abcdefghijklmnopq'\n"))
        self.assertIn(b"+[REDACTED:assigned-secret]\n", data)

    def test_patch_max_bytes_setting(self):
        self.assertEqual(approvals.patch_max_bytes({}), 262_144)
        self.assertEqual(approvals.patch_max_bytes({"approvals": {"patch_max_bytes": 10_000}}), 10_000)
        self.assertEqual(approvals.patch_max_bytes({"approvals": {"patch_max_bytes": 1}}), 4_096)
        self.assertEqual(approvals.patch_max_bytes({"approvals": {"patch_max_bytes": 10**9}}), 2_000_000)
        self.assertEqual(approvals.patch_max_bytes({"approvals": {"patch_max_bytes": "big"}}), 262_144)
        self.assertEqual(approvals.patch_max_bytes({"approvals": {"patch_max_bytes": True}}), 262_144)
        self.assertEqual(approvals.patch_max_bytes({"approvals": "nonsense"}), 262_144)


class PatchPruneTests(Base):
    OLD = time.time() - approvals.PATCH_KEEP_SECONDS - 60

    def patch(self, data: bytes, old: bool = True) -> Path:
        sha = approvals.store_patch(data)
        path = approvals.patches_dir() / f"{sha}.patch"
        if old:
            os.utime(path, (self.OLD, self.OLD))
        return path

    def card(self, name: str, content: str) -> None:
        pending = config.STATE / "approvals" / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        (pending / name).write_text(content)

    def test_prune_rules(self):
        named, orphan, fresh = (self.patch(d, old=False) for d in (b"named\n", b"orphan\n", b"fresh\n"))
        for path in (named, orphan):
            os.utime(path, (self.OLD, self.OLD))
        self.card("a.json", json.dumps({"tool": "git_push", "patch_sha256": named.stem}))
        self.card("broken.json", "{not json")
        self.card("list.json", "[1, 2]")
        self.card("other.txt", json.dumps({"patch_sha256": orphan.stem}))  # not a card
        stale_tmp = approvals.patches_dir() / ".tmp-leftover"
        stale_tmp.write_bytes(b"x")
        os.utime(stale_tmp, (self.OLD, self.OLD))
        unrelated = approvals.patches_dir() / "README"
        unrelated.write_text("x")
        os.utime(unrelated, (self.OLD, self.OLD))
        approvals.prune_patches()
        self.assertTrue(named.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse(orphan.exists())
        self.assertFalse(stale_tmp.exists())
        self.assertTrue(unrelated.exists())

    def test_a_card_symlink_is_not_followed(self):
        target = self.patch(b"target\n")
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text(json.dumps({"patch_sha256": target.stem}))
        (config.STATE / "approvals" / "pending").mkdir(parents=True)
        os.symlink(outside, config.STATE / "approvals" / "pending" / "x.json")
        approvals.prune_patches()
        self.assertFalse(target.exists())

    def test_symlinks_and_directories_in_the_patch_dir_are_left_alone(self):
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("precious\n")
        approvals.store_patch(b"init\n")
        link = approvals.patches_dir() / ("0" * 64 + ".patch")
        os.symlink(victim, link)
        os.utime(link, (self.OLD, self.OLD), follow_symlinks=False)
        sub = approvals.patches_dir() / ("1" * 64 + ".patch")
        sub.mkdir()
        os.utime(sub, (self.OLD, self.OLD))
        approvals.prune_patches()
        self.assertTrue(link.is_symlink())
        self.assertTrue(sub.is_dir())
        self.assertEqual(victim.read_text(), "precious\n")

    def test_store_prunes_first_and_refreshes_a_rewritten_patch(self):
        orphan = self.patch(b"orphan\n")
        again = self.patch(b"again\n")
        approvals.store_patch(b"again\n")
        self.assertFalse(orphan.exists())
        self.assertTrue(again.exists())
        self.assertGreater(again.stat().st_mtime, self.OLD + 60)
        self.assertEqual([p.name for p in approvals.patches_dir().iterdir() if p.name.startswith(".")], [])


class ApprovalPointTests(Base):
    def tearDown(self):
        approvals.set_hook(None)
        super().tearDown()

    def test_no_hook_means_pass_through(self):
        self.assertFalse(approvals.hook_installed())
        approvals.HELD_ID.set("")
        self.assertIsNone(approvals.require({"tool": "git_push"}))  # returns at once, not held
        self.assertEqual(approvals.HELD_ID.get(), "")

    def test_a_plugin_hook_returning_none_allows_without_an_outcome(self):
        approvals.set_hook(lambda card: None)
        approvals.HELD_ID.set("")
        self.assertIsNone(approvals.require({"tool": "git_push"}))
        self.assertEqual(approvals.HELD_ID.get(), "")
        self.assertEqual(approvals.held_note(None), "")
        self.assertEqual(approvals.with_held_note("Opened PR #1", None), "Opened PR #1")

    def test_the_note_joins_the_first_line_and_leaves_the_rest_alone(self):
        outcome = {"id": "abc", "approver": "daron", "decided": "2026-10-22T09:15:00Z", "waited_seconds": 4}
        held = ('; held for approval (id abc), approved by "daron" at 2026-10-22T09:15:00Z after 4 s.')
        self.assertEqual(approvals.with_held_note("Pushed 2 commit(s) to o/r:b (abc).\nremote: ok\nlast", outcome),
                         "Pushed 2 commit(s) to o/r:b (abc)" + held + "\nremote: ok\nlast")
        self.assertEqual(approvals.with_held_note("Opened PR #1: https://x/1", outcome), "Opened PR #1: https://x/1" + held)
        failure = "Push rejected by GitHub:\nhint: fetch first"
        self.assertEqual(approvals.with_held_note(failure, outcome),
                         "Push rejected by GitHub" + held[:-1] + ":\nhint: fetch first")
        for empty in ("", "\nbody"):
            self.assertEqual(approvals.with_held_note(empty, outcome),
                             approvals.held_note(outcome) + ("\nbody" if empty else ""))
        self.assertEqual(approvals.with_held_note("", None), "")

    def test_a_hook_value_that_is_not_an_outcome_is_ignored(self):
        for value in (True, "approved", 1, [], {}, {"id": ""}, {"id": "  "}, {"id": 7, "approver": "x"}):
            approvals.set_hook(lambda card, v=value: v)
            approvals.HELD_ID.set("")
            self.assertIsNone(approvals.require({"tool": "git_push"}), value)
            self.assertEqual(approvals.HELD_ID.get(), "")

    def test_the_note_is_one_filtered_line_whatever_the_outcome_holds(self):
        token = "ghp_" + "a" * 36
        approvals.set_hook(lambda card: {"id": "abc\n123", "approver": f"Mallory\n\x1b[31mBob {token}",
                                         "decided": None, "waited_seconds": float("inf")})
        outcome = approvals.require({"tool": "git_push"})
        note = approvals.held_note(outcome)
        self.assertEqual(note, 'Held for approval (id abc 123), approved by "Mallory Bob '
                               '[REDACTED:github-token]".')
        approvals.set_hook(lambda card: {"id": "abc", "waited_seconds": True})
        self.assertEqual(approvals.held_note(approvals.require({"tool": "git_push"})),
                         'Held for approval (id abc), approved by "unknown".')
        approvals.set_hook(lambda card: {"id": "abc", "approver": "x" * 500, "waited_seconds": 2.6})
        self.assertEqual(approvals.held_note(approvals.require({"tool": "git_push"})),
                         f'Held for approval (id abc), approved by "{"x" * 100}" after 3 s.')

    def test_the_note_drops_format_characters_and_c1_controls(self):
        # filters.sanitize leaves these: bidi overrides and isolates would
        # reorder the line on screen, zero-width characters hide in it, and
        # a C1 CSI (\x9b) starts a terminal escape on some terminals.
        approvals.set_hook(lambda card: {
            "id": "ab\u200bc\u2066", "decided": "2026-10-22T09:15:00Z\u202e",
            "approver": "ev\u202eil\u200b\u202a\u202b\u202c\u202d\u2067\u2068\u2069\ufeff\x9b31m\x85\x80 \"x\" Bob"})
        self.assertEqual(approvals.held_note(approvals.require({"tool": "git_push"})),
                         'Held for approval (id abc), approved by "evil31m \'x\' Bob" at 2026-10-22T09:15:00Z.')

    def call_pr(self, principal="reviewer"):
        fake_pr = {"html_url": "https://github.com/acme/widgets/pull/1", "number": 1, "draft": True}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(fake_pr).encode()
        (config.STATE / "roles.json").write_text(json.dumps({principal: ["github_create_pr"]}))
        with mock.patch("urllib.request.urlopen", return_value=Resp()), \
                mock.patch("passdesk.core.protocol.policy.load_policy", return_value=POLICY):
            text, is_error = protocol.call_tool(
                "github_create_pr", {"repo": "acme/widgets", "head": "factory/x", "title": "T"}, principal)
        last = json.loads((config.STATE / "audit.jsonl").read_text().strip().splitlines()[-1])
        return text, is_error, last

    def test_a_held_call_names_its_approval_in_the_result_and_the_record(self):
        approvals.set_hook(lambda card: {"id": "feedfacecafebeef", "approver": "daron",
                                         "decided": "2026-10-22T09:15:00Z", "waited_seconds": 12})
        text, is_error, last = self.call_pr()
        self.assertFalse(is_error, text)
        self.assertEqual(text, "Opened PR #1: https://github.com/acme/widgets/pull/1; held for approval "
                               '(id feedfacecafebeef), approved by "daron" at 2026-10-22T09:15:00Z after 12 s.')
        self.assertEqual((last["tool"], last["decision"], last["approval"]),
                         ("github_create_pr", "allow", "feedfacecafebeef"))
        # The next call on the same thread wasn't held: no approval on its record.
        approvals.set_hook(lambda card: None)
        text, is_error, last = self.call_pr()
        self.assertFalse(is_error, text)
        self.assertEqual(text, "Opened PR #1: https://github.com/acme/widgets/pull/1")
        self.assertNotIn("approval", last)

    def test_a_call_refused_early_never_inherits_an_earlier_hold(self):
        approvals.HELD_ID.set("feedfacecafebeef")  # left over from an earlier call on this thread
        text, is_error = protocol.call_tool("no_such_tool", {}, "reviewer")
        self.assertTrue(is_error)
        self.assertEqual(text, "DENIED by broker policy: unknown tool 'no_such_tool'")
        last = json.loads((config.STATE / "audit.jsonl").read_text().strip().splitlines()[-1])
        self.assertNotIn("approval", last)

    def test_a_call_with_no_hook_has_no_approval_on_its_record(self):
        text, is_error, last = self.call_pr()
        self.assertFalse(is_error, text)
        self.assertEqual(text, "Opened PR #1: https://github.com/acme/widgets/pull/1")
        self.assertNotIn("approval", last)

    def test_a_write_that_fails_after_approval_still_records_the_hold(self):
        approvals.set_hook(lambda card: {"id": "feedfacecafebeef", "approver": "daron"})
        (config.STATE / "roles.json").write_text('{"reviewer": ["github_create_pr"]}')
        with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("network down")), \
                mock.patch("passdesk.core.protocol.policy.load_policy", return_value=POLICY):
            text, is_error = protocol.call_tool(
                "github_create_pr", {"repo": "acme/widgets", "head": "factory/x", "title": "T"}, "reviewer")
        self.assertTrue(is_error)
        # An exception the tool doesn't turn into a result (a URLError, a
        # timeout) has no note; only the record's `approval` names the hold.
        self.assertEqual(text, "Broker error: network down")
        last = json.loads((config.STATE / "audit.jsonl").read_text().strip().splitlines()[-1])
        self.assertEqual((last["decision"], last["approval"]), ("error", "feedfacecafebeef"))

    def test_hook_sees_the_card_with_the_principal_and_may_refuse(self):
        seen = []

        def hook(card):
            seen.append(card)
            if card["repo"] == "acme/forbidden":
                raise Denied("declined by a person: not today")
        approvals.set_hook(hook)
        self.assertTrue(approvals.hook_installed())
        approvals.PRINCIPAL.set("developer")
        approvals.require({"tool": "git_push", "repo": "acme/widgets"})
        self.assertEqual(seen[-1], {"tool": "git_push", "repo": "acme/widgets", "principal": "developer"})
        with self.assertRaises(Denied):
            approvals.require({"tool": "git_push", "repo": "acme/forbidden"})

    def test_call_tool_sets_the_principal_for_the_hook(self):
        seen = []
        approvals.set_hook(seen.append)
        (config.STATE / "roles.json").write_text('{"reviewer": ["github_create_pr"]}')
        fake_pr = {"html_url": "https://github.com/acme/widgets/pull/1", "number": 1, "draft": True}

        class Resp:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                import json
                return json.dumps(fake_pr).encode()
        with mock.patch("urllib.request.urlopen", return_value=Resp()), \
                mock.patch("passdesk.core.protocol.policy.load_policy", return_value=POLICY):
            text, is_error = protocol.call_tool(
                "github_create_pr", {"repo": "acme/widgets", "head": "factory/x", "title": "T"}, "reviewer")
        self.assertFalse(is_error, text)
        self.assertEqual(seen[-1]["principal"], "reviewer")
        self.assertEqual(seen[-1]["tool"], "github_create_pr")
        self.assertEqual(seen[-1]["head"], "factory/x")

    def test_a_credential_looking_string_is_redacted_before_the_hook_sees_it(self):
        seen = []
        approvals.set_hook(seen.append)
        token = "ghp_" + "a" * 36
        approvals.require({
            "tool": "github_create_pr",
            "title": f"Fix: rotate {token} now",
            "branch": f"factory/{token}",
            "file_count": 2,
            "draft": True,
            "files": ["a.txt", f"{token}.txt"],
            "meta": {"note": f"leaked {token}"},
            "pair": ("clean.txt", f"{token}.txt"),
        })
        card = seen[-1]
        self.assertNotIn(token, card["title"])
        self.assertIn("[REDACTED:github-token]", card["title"])
        self.assertIn("Fix: rotate", card["title"])
        self.assertNotIn(token, card["branch"])
        self.assertNotIn(token, card["files"][1])
        self.assertEqual(card["files"][0], "a.txt")
        self.assertNotIn(token, card["meta"]["note"])
        self.assertNotIn(token, card["pair"][1])
        self.assertEqual(card["pair"][0], "clean.txt")
        self.assertIsInstance(card["pair"], tuple)
        # Non-string values are untouched.
        self.assertEqual(card["file_count"], 2)
        self.assertIs(card["draft"], True)

    def test_a_hook_refusal_is_an_ordinary_denial_on_the_record(self):
        approvals.set_hook(lambda card: (_ for _ in ()).throw(Denied("held: approval abc pending")))
        (config.STATE / "roles.json").write_text('{"developer": ["github_create_pr"]}')
        with mock.patch("passdesk.core.protocol.policy.load_policy", return_value=POLICY):
            text, is_error = protocol.call_tool(
                "github_create_pr", {"repo": "acme/widgets", "head": "factory/x", "title": "T"}, "developer")
        self.assertTrue(is_error)
        self.assertIn("DENIED by broker policy: held: approval abc pending", text)
        last = (config.STATE / "audit.jsonl").read_text().strip().splitlines()[-1]
        self.assertIn('"decision": "deny"', last)
        self.assertIn("approval abc pending", last)


class PluginLoadingTests(Base):
    def tearDown(self):
        config.PLUGINS = ""
        sys.modules.pop("fake_passdesk_plugin", None)
        approvals.set_hook(None)
        super().tearDown()

    def test_nothing_named_loads_nothing(self):
        config.PLUGINS = ""
        self.assertEqual(plugins.load(), [])
        self.assertFalse((config.STATE / "audit.jsonl").exists())

    def test_named_plugin_is_imported_activated_and_recorded(self):
        mod = types.ModuleType("fake_passdesk_plugin")

        def activate():
            approvals.set_hook(lambda card: None)
            return True
        mod.__dict__["activate"] = activate
        sys.modules["fake_passdesk_plugin"] = mod
        config.PLUGINS = " fake_passdesk_plugin , "
        self.assertEqual(plugins.load(), ["fake_passdesk_plugin"])
        self.assertTrue(approvals.hook_installed())
        record = (config.STATE / "audit.jsonl").read_text()
        self.assertIn('"event": "plugins"', record)
        self.assertIn('"active": ["fake_passdesk_plugin"]', record)

    def test_missing_plugin_stops_startup(self):
        config.PLUGINS = "no_such_passdesk_plugin"
        with self.assertRaises(ImportError):
            plugins.load()


if __name__ == "__main__":
    unittest.main()
