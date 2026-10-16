"""Policy loading, the local overlay, and the shared policy checks."""

import unittest
from pathlib import Path

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import policy
from passdesk.core.errors import Denied
from passdesk.tools import git

from .base import POLICY, Base


class PolicyTests(Base):
    def test_host_allowed(self):
        allow = POLICY["web"]["allow_hosts"]
        self.assertTrue(policy.host_allowed("docs.python.org", allow))
        self.assertTrue(policy.host_allowed("api.example.com", allow))
        self.assertFalse(policy.host_allowed("example.com", allow))
        self.assertFalse(policy.host_allowed("docs.python.org.evil.com", allow))
        self.assertFalse(policy.host_allowed("evilexample.com", allow))

    def test_branch_names(self):
        for ok in ["factory/fix-1", "feature.x", "a"]:
            git.check_branch_name(ok)
        for bad in ["../x", "-x", "a..b", "a//b", "x.lock", "x/", "a@{1}", "a b", "a;rm"]:
            with self.assertRaises(Denied, msg=bad):
                git.check_branch_name(bad)

    def test_push_branch_policy(self):
        git.check_push_branch("factory/feature", POLICY)
        for bad in ["main", "release/1.0", "feature/x"]:
            with self.assertRaises(Denied, msg=bad):
                git.check_push_branch(bad, POLICY)

    def test_repo_allowlist(self):
        git.check_repo("acme/widgets", POLICY)
        git.check_repo("ACME/Widgets", POLICY)
        for bad in ["acme/other", "acme", "a/b/c", "../x"]:
            with self.assertRaises(Denied, msg=bad):
                git.check_repo(bad, POLICY)

    def test_packages(self):
        policy.check_package("npm", "left-pad", POLICY)
        with self.assertRaises(Denied):
            policy.check_package("npm", "evil-pkg", POLICY)
        with self.assertRaises(Denied):
            policy.check_package("pypi", "Bad_Lib", POLICY)  # PEP 503 normalization
        allow_only = {"packages": {"npm_allow": ["react"]}}
        with self.assertRaises(Denied):
            policy.check_package("npm", "vue", allow_only)


class LocalPolicyTests(Base):
    def write(self, name: str, text: str) -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(text)
        return path

    def test_local_overlay_merges_table_by_table(self):
        base = self.write("policy.toml", '[packages]\nnpm_deny = ["x"]\nnpm_private_registry = ""\n'
                                         '[jira]\nsite = ""\nmax_results = 25\n')
        self.assertEqual(policy.load_policy(base)["packages"]["npm_private_registry"], "")
        self.write("policy.local.toml", '[packages]\nnpm_private_registry = "https://r.example/npm/"\n'
                                        '[jira]\nsite = "acme.atlassian.net"\n[extra]\nk = 1\n')
        pol = policy.load_policy(base)
        self.assertEqual(pol["packages"], {"npm_deny": ["x"], "npm_private_registry": "https://r.example/npm/"})
        self.assertEqual(pol["jira"], {"site": "acme.atlassian.net", "max_results": 25})
        self.assertEqual(pol["extra"], {"k": 1})

    def test_overlay_lists_replace_rather_than_append(self):
        self.assertEqual(policy.merge_policy({"git": {"repos": ["a/b"]}}, {"git": {"repos": ["c/d"]}}),
                         {"git": {"repos": ["c/d"]}})

    def test_repo_policy_is_org_neutral(self):
        pol = policy.load_policy(Path(__file__).parents[1] / "policy.toml")
        self.assertEqual(pol["packages"]["npm_private_registry"], "")
        self.assertEqual((pol["jira"]["site"], pol["jira"]["cloud_id"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
