"""git_fetch / git_push end to end against a local bare repo."""

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import config, workspace
from passdesk.core.errors import Denied
from passdesk.tools import git

from .base import FAKE_ANT, FAKE_GH, POLICY, Base


class GitFlowTests(Base):
    """End-to-end bundle flow against a local bare repo standing in for GitHub."""

    def sh(self, *cmd, cwd):
        cp = workspace.run(list(cmd), cwd=cwd, extra_env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                  "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip()

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.sh("git", "init", "--bare", "-q", "-b", "main", str(self.origin), cwd=root)
        seed = root / "seed"
        self.sh("git", "clone", "-q", str(self.origin), str(seed), cwd=root)
        (seed / "README").write_text("hello\n")
        self.sh("git", "add", ".", cwd=seed)
        self.sh("git", "commit", "-q", "-m", "init", cwd=seed)
        self.sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)

        config.MIRRORS = root / "mirrors"
        self._orig_ensure = git.ensure_mirror

        def ensure_local(repo):
            path = self._orig_ensure(repo)
            git.git("remote", "set-url", "origin", str(self.origin), cwd=path)
            return path
        git.ensure_mirror = ensure_local

        # Agent's clone, with a planted hook + fsmonitor that must never run in the broker.
        self.ws = config.WORKSPACE
        self.sh("git", "init", "-q", "-b", "main", cwd=self.ws)
        self.canary = root / "canary"
        hook = self.ws / ".git/hooks/pre-push"
        hook.write_text(f"#!/bin/sh\ntouch {self.canary}\n")
        hook.chmod(0o755)
        self.sh("git", "config", "core.fsmonitor", f"touch {self.canary}", cwd=self.ws)

    def tearDown(self):
        git.ensure_mirror = self._orig_ensure
        super().tearDown()

    def assert_no_canary(self):
        self.assertFalse(self.canary.exists(), "a workspace hook or fsmonitor ran inside the broker")

    def test_fetch_then_push(self):
        out = git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.assert_no_canary()
        import_cmd = out.splitlines()[-1].split()
        self.sh(*import_cmd, cwd=self.ws)
        self.sh("git", "checkout", "-q", "-b", "factory/feature", "refs/remotes/origin/main", cwd=self.ws)
        (self.ws / "feature.txt").write_text("new feature\n")
        self.sh("git", "add", "feature.txt", cwd=self.ws)
        self.sh("git", "commit", "-q", "-m", "add feature", cwd=self.ws)
        (self.ws / ".factory/outgoing").mkdir(parents=True, exist_ok=True)
        self.sh("git", "bundle", "create", "-q", ".factory/outgoing/f.bundle",
                "refs/remotes/origin/main..factory/feature", cwd=self.ws)
        # The agent-side commands above legitimately trigger the planted fsmonitor
        # (in production they run in the agent's own VM). Reset before the broker call.
        self.canary.unlink(missing_ok=True)

        out = git.tool_git_push({"repo": "acme/widgets", "branch": "factory/feature", "bundle": "f.bundle"}, POLICY)
        self.assertIn("Pushed 1 commit(s)", out)
        pushed = self.sh("git", "rev-parse", "refs/heads/factory/feature", cwd=self.origin)
        self.assert_no_canary()
        self.assertEqual(pushed, self.sh("git", "rev-parse", "factory/feature", cwd=self.ws))

    def test_push_blocks_secret_in_diff(self):
        git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.sh("git", "fetch", "-q", ".factory/incoming/acme__widgets__main.bundle",
                "refs/remotes/origin/main:refs/remotes/origin/main", cwd=self.ws)
        self.sh("git", "checkout", "-q", "-b", "factory/leak", "refs/remotes/origin/main", cwd=self.ws)
        (self.ws / "config.py").write_text(f"TOKEN = '{FAKE_GH}'\n")
        self.sh("git", "add", ".", cwd=self.ws)
        self.sh("git", "commit", "-q", "-m", "oops", cwd=self.ws)
        (self.ws / ".factory/outgoing").mkdir(parents=True, exist_ok=True)
        self.sh("git", "bundle", "create", "-q", ".factory/outgoing/l.bundle",
                "refs/remotes/origin/main..factory/leak", cwd=self.ws)
        with self.assertRaisesRegex(Denied, "github-token"):
            git.tool_git_push({"repo": "acme/widgets", "branch": "factory/leak", "bundle": "l.bundle"}, POLICY)
        cp = workspace.run(["git", "rev-parse", "--verify", "-q", "refs/heads/factory/leak"], cwd=self.origin)
        self.assertNotEqual(cp.returncode, 0, "secret-bearing branch reached origin")

    # --- helpers for the hardening tests below -------------------------------

    def branch(self, name: str, *commits: dict, env: dict | None = None) -> None:
        """Agent side: a branch off origin/main with one commit per {path: bytes|None}."""
        git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.sh("git", "fetch", "-q", ".factory/incoming/acme__widgets__main.bundle",
                "refs/remotes/origin/main:refs/remotes/origin/main", cwd=self.ws)
        self.sh("git", "checkout", "-q", "-b", name, "refs/remotes/origin/main", cwd=self.ws)
        for i, files in enumerate(commits):
            for path, data in files.items():
                if data is None:
                    (self.ws / path).unlink()
                else:
                    (self.ws / path).write_bytes(data)
            self.sh("git", "add", "-A", cwd=self.ws)
            cp = workspace.run(["git", "commit", "-q", "-m", f"commit {i}"], cwd=self.ws, extra_env={
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t", **(env or {})})
            self.assertEqual(cp.returncode, 0, cp.stderr)

    def bundle(self, branch: str, fname: str = "b.bundle") -> str:
        (self.ws / ".factory/outgoing").mkdir(parents=True, exist_ok=True)
        self.sh("git", "bundle", "create", "-q", f".factory/outgoing/{fname}",
                f"refs/remotes/origin/main..{branch}", cwd=self.ws)
        self.canary.unlink(missing_ok=True)
        return fname

    def push(self, branch: str, fname: str = "b.bundle", policy=POLICY) -> str:
        return git.tool_git_push({"repo": "acme/widgets", "branch": branch, "bundle": fname}, policy)

    def assert_not_on_origin(self, branch: str) -> None:
        cp = workspace.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=self.origin)
        self.assertNotEqual(cp.returncode, 0, f"{branch} reached origin")

    # --- finding 2: every commit is scanned, not just the net diff -----------

    def test_push_blocks_secret_added_then_removed(self):
        self.branch("factory/sneaky", {"config.py": f"TOKEN = '{FAKE_GH}'\n".encode()}, {"config.py": None})
        self.bundle("factory/sneaky")
        with self.assertRaisesRegex(Denied, r"commit [0-9a-f]{12} appears to contain secrets \(github-token\)"):
            self.push("factory/sneaky")
        self.assert_not_on_origin("factory/sneaky")

    def test_push_blocks_secret_in_author_identity(self):
        self.branch("factory/who", {"a.txt": b"fine\n"}, env={"GIT_AUTHOR_NAME": FAKE_ANT})
        self.bundle("factory/who")
        with self.assertRaisesRegex(Denied, "author/committer.*anthropic-key"):
            self.push("factory/who")
        self.assert_not_on_origin("factory/who")

    def test_push_blocks_secret_in_committer_email(self):
        self.branch("factory/email", {"a.txt": b"fine\n"}, env={"GIT_COMMITTER_EMAIL": f"{FAKE_GH}@x"})
        self.bundle("factory/email")
        with self.assertRaisesRegex(Denied, "github-token"):
            self.push("factory/email")

    def test_push_scans_merge_commit_changes(self):
        # An "evil merge": the merge commit itself introduces the secret.
        self.sh("git", "config", "--unset", "core.fsmonitor", cwd=self.ws)  # it confuses checkout
        self.branch("factory/merge", {"a.txt": b"a\n"})
        self.sh("git", "checkout", "-q", "-b", "side", "refs/remotes/origin/main", cwd=self.ws)
        (self.ws / "b.txt").write_text("b\n")
        self.sh("git", "add", "-A", cwd=self.ws)
        self.sh("git", "commit", "-q", "-m", "side", cwd=self.ws)
        self.sh("git", "checkout", "-q", "factory/merge", cwd=self.ws)
        self.sh("git", "merge", "-q", "--no-ff", "--no-commit", "side", cwd=self.ws)
        (self.ws / "c.txt").write_text(f"{FAKE_GH}\n")
        self.sh("git", "add", "-A", cwd=self.ws)
        self.sh("git", "commit", "-q", "-m", "merge", cwd=self.ws)
        self.bundle("factory/merge")
        with self.assertRaisesRegex(Denied, "github-token"):
            self.push("factory/merge")

    # --- finding 3: binary files are scanned and counted ---------------------

    def test_push_scans_binary_files(self):
        self.branch("factory/bin", {"blob.bin": b"\x00\x01binary\x00" + FAKE_GH.encode() + b"\x00\xff"})
        self.bundle("factory/bin")
        with self.assertRaisesRegex(Denied, "github-token"):
            self.push("factory/bin")
        self.assert_not_on_origin("factory/bin")

    def test_binary_files_count_toward_max_diff_bytes(self):
        self.branch("factory/big", {"blob.bin": b"\x00" + os.urandom(20_000)})
        self.bundle("factory/big")
        policy = {**POLICY, "filters": {"max_diff_bytes": 10_000}}
        with self.assertRaisesRegex(Denied, r"max_diff_bytes \(10000 bytes"):
            self.push("factory/big", policy=policy)
        self.assert_not_on_origin("factory/big")

    def test_secret_in_a_deleted_binary_file_counts_too(self):
        self.branch("factory/binrm", {"k.bin": b"\x00" + FAKE_GH.encode()}, {"k.bin": None})
        self.bundle("factory/binrm")
        with self.assertRaisesRegex(Denied, "github-token"):
            self.push("factory/binrm")

    def test_bundle_size_is_capped(self):
        self.branch("factory/huge", {"a.txt": os.urandom(30_000).hex().encode()})
        self.bundle("factory/huge")
        policy = {**POLICY, "filters": {"max_bundle_bytes": 10_000}}
        with self.assertRaisesRegex(Denied, "larger than 10000 bytes; bundle only the new commits"):
            self.push("factory/huge", policy=policy)
        self.assert_not_on_origin("factory/huge")

    # --- finding 6: the bundle is read once, into broker-private space -------

    def test_git_only_ever_sees_a_private_copy_of_the_bundle(self):
        self.branch("factory/copy", {"a.txt": b"a\n"})
        self.bundle("factory/copy")
        real_git, seen = git.git, []

        def spy(*args, **kw):
            seen.append(args)
            return real_git(*args, **kw)
        with mock.patch.object(git, "git", side_effect=spy):
            self.assertIn("Pushed 1 commit(s)", self.push("factory/copy"))
        ws = str(config.WORKSPACE.resolve())
        bundle_args = [a for args in seen for a in args if a.endswith(".bundle")]
        self.assertTrue(bundle_args, seen)
        for a in bundle_args:
            self.assertFalse(os.path.realpath(a).startswith(ws), f"git read the workspace bundle {a}")
        self.assertEqual(len(set(bundle_args)), 1, "every git step must use the same copy")

    def test_push_rejection_wraps_remote_stderr_as_untrusted(self):
        # Finding 9: a rejection from GitHub (forwarded through "git push"'s
        # stderr) is remote content and must be wrapped, not just sanitized.
        self.branch("factory/rej", {"a.txt": b"a\n"})
        self.bundle("factory/rej")
        fake_push = subprocess.CompletedProcess(args=[], returncode=1, stdout="",
                                                 stderr="remote: hook declined\n")
        real_git = git.git

        def spy(*args, **kw):
            return fake_push if args[:1] == ("push",) else real_git(*args, **kw)
        with mock.patch.object(git, "git", side_effect=spy):
            out = self.push("factory/rej")
        self.assertIn("Push rejected by GitHub", out)
        self.assertIn("UNTRUSTED CONTENT", out)
        self.assertIn("hook declined", out)
        self.assert_not_on_origin("factory/rej")

    def test_bundle_must_be_a_regular_file(self):
        self.branch("factory/dir", {"a.txt": b"a\n"})
        # A directory git would happily treat as a local repository.
        out = self.ws / ".factory/outgoing/d.bundle"
        out.parent.mkdir(parents=True, exist_ok=True)
        self.sh("git", "clone", "-q", "--bare", str(self.ws), str(out), cwd=self.ws)
        with self.assertRaisesRegex(Denied, "not a regular file"):
            self.push("factory/dir", "d.bundle")
        self.assert_not_on_origin("factory/dir")

    def test_bundle_symlink_is_refused(self):
        self.branch("factory/link", {"a.txt": b"a\n"})
        self.bundle("factory/link", "real.bundle")
        os.symlink("real.bundle", self.ws / ".factory/outgoing/l.bundle")
        with self.assertRaisesRegex(Denied, "symlink"):
            self.push("factory/link", "l.bundle")

    # --- finding 4: .factory symlinks never redirect broker writes -----------

    def test_fetch_refuses_a_symlinked_factory_dir(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.ws / ".factory")
        with self.assertRaisesRegex(Denied, "'.factory' is a symlink"):
            git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.assertEqual(list(outside.iterdir()), [])

    def test_fetch_refuses_a_symlinked_incoming_dir(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.ws / ".factory").mkdir()
        os.symlink(outside, self.ws / ".factory/incoming")
        with self.assertRaisesRegex(Denied, "symlink"):
            git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.assertEqual(list(outside.iterdir()), [])

    def test_fetch_replaces_a_symlinked_bundle_instead_of_writing_through_it(self):
        target = Path(self.tmp.name) / "victim"
        target.write_text("precious\n")
        (self.ws / ".factory/incoming").mkdir(parents=True)
        os.symlink(target, self.ws / ".factory/incoming/acme__widgets__main.bundle")
        git.tool_git_fetch({"repo": "acme/widgets", "branch": "main"}, POLICY)
        self.assertEqual(target.read_text(), "precious\n")
        self.assertFalse((self.ws / ".factory/incoming/acme__widgets__main.bundle").is_symlink())


if __name__ == "__main__":
    unittest.main()
