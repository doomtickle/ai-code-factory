"""git_fetch / git_push end to end against a local bare repo."""

import unittest
from pathlib import Path

from agentbroker import tools  # noqa: F401 - registers every tool
from agentbroker.core import config, workspace
from agentbroker.core.errors import Denied
from agentbroker.tools import git

from .base import FAKE_GH, POLICY, Base


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
        workspace.factory_dir("outgoing")
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
        workspace.factory_dir("outgoing")
        self.sh("git", "bundle", "create", "-q", ".factory/outgoing/l.bundle",
                "refs/remotes/origin/main..factory/leak", cwd=self.ws)
        with self.assertRaisesRegex(Denied, "github-token"):
            git.tool_git_push({"repo": "acme/widgets", "branch": "factory/leak", "bundle": "l.bundle"}, POLICY)
        cp = workspace.run(["git", "rev-parse", "--verify", "-q", "refs/heads/factory/leak"], cwd=self.origin)
        self.assertNotEqual(cp.returncode, 0, "secret-bearing branch reached origin")


if __name__ == "__main__":
    unittest.main()
