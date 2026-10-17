"""The approval point and plugin loading in core."""

import sys
import types
import unittest
from unittest import mock

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import approvals, config, plugins, protocol
from passdesk.core.errors import Denied
from passdesk.tools import git

from . import test_git
from .base import POLICY, Base


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

    def test_a_tampered_mirror_ref_after_approval_is_refused(self):
        def hook(card):
            mirror = git.ensure_mirror("acme/widgets")
            git.git("update-ref", "refs/factory/factory/feature", "refs/remotes/origin/main", cwd=mirror)
        approvals.set_hook(hook)
        self.make_bundle()
        with self.assertRaises(Denied) as cm:
            git.tool_git_push({"repo": "acme/widgets", "branch": "factory/feature", "bundle": "f.bundle"}, POLICY)
        self.assertIn("changed in the broker's mirror", str(cm.exception))


class ApprovalPointTests(Base):
    def tearDown(self):
        approvals.set_hook(None)
        super().tearDown()

    def test_no_hook_means_pass_through(self):
        self.assertFalse(approvals.hook_installed())
        approvals.require({"tool": "git_push"})  # returns at once

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
