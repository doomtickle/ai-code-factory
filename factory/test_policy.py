"""Tests for factory/policy.py. Ported from the parity tests against policy_value and policy_string
(scripts/lib/common.sh) and git_push_enabled, github_repos_listed, gitlab_needs_token,
gitlab_mr_unheld, jira_cloud_id_set, any_jira_tool_granted and npm_private_registry_needs_token
(bin/factory), and from scripts/test_factory_gitlab.sh and the cloud_id part of
scripts/test_factory_config.sh. That bash is gone: every expected answer here is the one it gave
on the same policy files.
"""

import os
import re
import unittest

from factory import policy
from factory.fixtures import REPO, CommandTest

# Each fixture, with what policy_value gave for [gitlab] url, [approvals] tools, [web] allow_hosts
# and [other] url (None: unset, nothing printed).
POLICIES = [
    ("", None, None, None, None),
    ("[gitlab]\nurl = \"https://gitlab.example\"\n", '"https://gitlab.example"', None, None, None),
    ("[gitlab]\nurl = 'https://gitlab.example'\n", "'https://gitlab.example'", None, None, None),
    ("[gitlab]\nurl = \"\"\n", '""', None, None, None),
    ("[gitlab]\nurl=\"x\" # comment\n[other]\nurl = \"no\"\n", '"x" # comment', None, None, '"no"'),
    ("[other]\nurl = \"no\"\n[gitlab]\n# url = \"commented\"\n", None, None, None, '"no"'),
    ("[gitlab]\n  url   =   \"indented\"\n", '"indented"', None, None, None),
    ("[gitlab]\nurl = unquoted\n", "unquoted", None, None, None),
    ("[gitlab-extra]\nurl = \"wrong section\"\n", None, None, None, None),
    ("[approvals]\ntools = [\"git_push\"]\n", None, '["git_push"]', None, None),
    ("[approvals]\ntools = [\"gitlab_create_mr\", \"git_push\"]\n", None, '["gitlab_create_mr", "git_push"]', None, None),
    ("[approvals]\ntools = []\n", None, "[]", None, None),
    ("[approvals]\ntools = [ ]  # none\n", None, "[ ]  # none", None, None),
    ("[approvals]\ntools = [\n  \"git_push\", # a\n  \"jira\"\n]\n", None, '[   "git_push",    "jira" ]', None, None),
    ("[approvals]\ntools = [\n  \"gitlab_create_mr\",\n]\n", None, '[   "gitlab_create_mr", ]', None, None),
    ("[approvals]\ntools = [\n]\n", None, "[ ]", None, None),
    ("[approvals]\ntools = [\n  \"never closed\"\n", None, None, None, None),
    ("[approvals]\ntools = [\"git_push\"]\ntools = [\"later\"]\n", None, '["git_push"]', None, None),
    ("[approvals]\nother = 1\n[approvals]\ntools = [\"two sections\"]\n", None, '["two sections"]', None, None),
    ("[web]\nallow_hosts = [\"a\", \"b\"]\n[approvals]\ntools = [\"x\"]", None, '["x"]', '["a", "b"]', None),
    ("[gitlab]\r\nurl = \"crlf\"\r\n", '"crlf"\r', None, None, None),
]
TEXTS = [p[0] for p in POLICIES]
KEYS = (("gitlab", "url"), ("approvals", "tools"), ("web", "allow_hosts"), ("other", "url"))
UNHELD = '[approvals].tools is set without "gitlab_create_mr"'


class PolicyTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.write("repo/broker/tools.txt", "git_fetch\ngitlab_create_mr\n")
        self.write("repo/roles/developer/role.conf", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="git_fetch gitlab_create_mr"\n')
        self.write("repo/roles/reviewer/role.conf", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nBROKER_TOOLS="git_fetch"\n')

    def write_policy(self, base, local):
        os.makedirs(self.config, exist_ok=True)
        os.makedirs(os.path.join(self.root, "broker"), exist_ok=True)
        for path, text in ((os.path.join(self.root, "broker", "policy.toml"), base), (os.path.join(self.config, "policy.toml"), local)):
            if os.path.exists(path):
                os.remove(path)
            if text is not None:
                with open(path, "w", newline="") as f:
                    f.write(text)

    def either_file(self, text):
        """TEXT as the repo's policy, then as this machine's overlay: the answers are the same."""
        for base, local in ((text, None), (None, text)):
            self.write_policy(base, local)
            yield

    def grant(self, tools):
        self.write("repo/roles/developer/role.conf", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="%s"\n' % tools)


class PolicyValueTest(PolicyTest):
    def test_every_fixture_reads_as_bash_did(self):
        for text, *want in POLICIES:
            for _ in self.either_file(text):
                self.assertEqual([policy.policy_value(self.paths, s, k) for s, k in KEYS], want, repr(text))
            self.write_policy(text, "[approvals]\ntools = [\"mine\"]\n")
            self.assertEqual([policy.policy_value(self.paths, s, k) for s, k in KEYS], [want[0], '["mine"]', want[2], want[3]],
                             repr(text))

    def test_the_local_overlay_wins_only_where_it_sets_the_key(self):
        self.write_policy("[approvals]\ntools = [\"base\"]\n[gitlab]\nurl = \"https://base\"\n", "[approvals]\ntools = [\"mine\"]\n")
        self.assertEqual(policy.policy_value(self.paths, "approvals", "tools"), '["mine"]')
        self.assertEqual(policy.policy_value(self.paths, "gitlab", "url"), '"https://base"')

    def test_the_source_names_the_file_the_value_came_from(self):
        self.write_policy("[approvals]\ntools = [\"base\"]\n[gitlab]\nurl = \"https://base\"\n", "[approvals]\ntools = [\"mine\"]\n")
        self.assertEqual(policy.policy_value_source(self.paths, "approvals", "tools"), ('["mine"]', "local"))
        self.assertEqual(policy.policy_value_source(self.paths, "gitlab", "url"), ('"https://base"', "repo"))
        self.assertEqual(policy.policy_value_source(self.paths, "git", "repos"), (None, None))

    def test_an_empty_value_is_set_but_empty(self):
        self.write_policy("[gitlab]\nurl =\n", None)
        self.assertEqual(policy.policy_value(self.paths, "gitlab", "url"), "")

    def test_nothing_set_is_none(self):
        self.write_policy(None, None)
        self.assertIsNone(policy.policy_value(self.paths, "gitlab", "url"))


class GitlabTest(PolicyTest):
    # (fixture, gitlab_needs_token, gitlab_mr_unheld) with developer granted gitlab_create_mr.
    CASES = [(t, t in (TEXTS[1], TEXTS[2], TEXTS[4], TEXTS[6], TEXTS[20]), None) for t in TEXTS] + [
        ("[gitlab]\nurl = \"https://g\"\n[approvals]\ntools = [\"git_push\"]\n", True, UNHELD),
        ("[gitlab]\nurl = \"https://g\"\n[approvals]\ntools = [\"gitlab_create_mr\"]\n", True, None),
        ("[gitlab]\nurl = \"https://g\"\n[approvals]\ntools = []\n", True, None),
        ("[gitlab]\nurl = \"https://g\"\n[approvals]\ntools = [\n \"git_push\"\n]\n", True, UNHELD),
    ]

    def test_needs_token_and_unheld_answer_as_bash_did(self):
        for text, needs, why in self.CASES:
            for _ in self.either_file(text):
                self.assertEqual(policy.gitlab_needs_token(self.paths), needs, repr(text))
                self.assertEqual(policy.gitlab_mr_unheld(self.paths), why, repr(text))

    def test_it_warns_only_when_the_tool_is_granted(self):
        self.write_policy('[gitlab]\nurl = "https://g"\n[approvals]\ntools = ["git_push"]\n', None)
        self.assertEqual(policy.gitlab_mr_unheld(self.paths), UNHELD)
        self.grant("git_fetch")
        self.assertIsNone(policy.gitlab_mr_unheld(self.paths))


class JiraAndNpmTest(PolicyTest):
    # (fixture, policy_string of [jira] cloud_id, [packages] npm_private_registry and [gitlab] url,
    # jira_cloud_id_set)
    STRINGS = [
        (TEXTS[1], "", "", "https://gitlab.example", False),
        (TEXTS[4], "", "", "x", False),
        (TEXTS[6], "", "", "indented", False),
        (TEXTS[20], "", "", "crlf", False),
        ('[jira]\ncloud_id = "5c1f"\n', "5c1f", "", "", True),
        ('[jira]\ncloud_id = "00000000-0000-0000-0000-000000000000"\n', "00000000-0000-0000-0000-000000000000", "", "", False),
        ('[jira]\ncloud_id = "0-0"\n', "0-0", "", "", False),
        ('[jira]\ncloud_id = ""\n', "", "", "", False),
        ("[jira]\ncloud_id = 'single'\n", "", "", "", False),
        ('[jira]\ncloud_id = unquoted\n', "", "", "", False),
        ('[jira]\ncloud_id = "a" "b"\n', "a", "", "", True),
        ('[jira]\ncloud_id = "0a"\n', "0a", "", "", True),
        ('[packages]\nnpm_private_registry = "https://r.example"\n', "", "https://r.example", "", False),
        ('[packages]\nnpm_private_registry = ""\n', "", "", "", False),
    ]
    # Every other fixture of POLICIES reads as "" for all three, with no cloud_id set.
    STRINGS += [(t, "", "", "", False) for t in TEXTS if t not in (TEXTS[1], TEXTS[4], TEXTS[6], TEXTS[20])]
    # Which of these grants hold npm_fetch (a whole word).
    GRANTS = (("git_fetch", False), ("npm_fetch", True), ("jira_my_issues npm_fetch", True),
              ("web_fetch xnpm_fetch", False), ("xjira_a", False))

    def test_policy_string_jira_and_npm_answer_as_bash_did(self):
        self.assertEqual(len(self.STRINGS), len(POLICIES) + 10)
        for text, cloud_id, registry, url, cloud_id_set in self.STRINGS:
            for _ in self.either_file(text):
                self.assertEqual([policy.policy_string(self.paths, s, k) for s, k in
                                  (("jira", "cloud_id"), ("packages", "npm_private_registry"), ("gitlab", "url"))],
                                 [cloud_id, registry, url], repr(text))
                self.assertEqual(policy.jira_cloud_id_set(self.paths), cloud_id_set, repr(text))
                for tools, npm in self.GRANTS:
                    self.grant(tools)
                    self.assertEqual(policy.npm_private_registry_needs_token(self.paths), npm and registry != "",
                                     (text, tools))

    def test_any_jira_tool_granted_answers_as_bash_did(self):
        # (Coordinator's BROKER_TOOLS, the developer's, granted) -- only a whole word starting jira_ counts.
        for coordinator, role, want in (
                ("", "git_fetch", False), ("", "jira_get_issue", True), ("", "xjira_a", False),
                ("web_fetch", "git_fetch", False), ("web_fetch", "jira_get_issue", True), ("web_fetch", "xjira_a", False),
                ("jira_my_issues", "git_fetch", True), ("jira_my_issues", "jira_get_issue", True),
                ("jira_my_issues", "xjira_a", True),
                ("xjira_a jira", "git_fetch", False), ("xjira_a jira", "jira_get_issue", True), ("xjira_a jira", "xjira_a", False),
                ("git_fetch jira_x", "git_fetch", True), ("git_fetch jira_x", "jira_get_issue", True),
                ("git_fetch jira_x", "xjira_a", True)):
            self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS="%s"\n' % coordinator)
            self.grant(role)
            self.assertEqual(policy.any_jira_tool_granted(self.paths), want, (coordinator, role))

    def test_the_all_zero_placeholder_is_not_a_cloud_id(self):
        self.write_policy('[jira]\ncloud_id = "00000000-0000-0000-0000-000000000000"\n', None)
        self.assertFalse(policy.jira_cloud_id_set(self.paths))
        self.write_policy('[jira]\ncloud_id = "00000000-0000-0000-0000-000000000001"\n', None)
        self.assertTrue(policy.jira_cloud_id_set(self.paths))


class GitReposTest(PolicyTest):
    # (fixture, git_push_enabled, github_repos_listed), as the bash gave them.
    CASES = [
        ("", False, False),
        ('[other]\nrepos = ["a/b"]\n', False, False),
        ("[git]\nrepos = []\n", False, False),
        ("[git]\nrepos = [ ]\n", False, False),
        ("[git]\nrepos = [\t]\n", False, False),
        ("[git]\nrepos = [\n]\n", False, False),
        ('[git]\nrepos = ["a/b"]\n', True, True),
        ('[git]\nrepos = [\n  "a/b",\n]\n', True, True),
        ("[git]\nrepos = [\n  # none yet\n]\n", False, False),
        ('[git]\nrepos = ["gitlab.acme.net/platform/api", "gitlab.acme.net:8443/g/p"]\n', True, False),
        ('[git]\nrepos = ["gitlab.acme.net/platform/api", "my-org/my-project"]\n', True, True),
        ('[git]\nrepos = ["noslash"]\n', True, False),
        ('[git]\nrepos = ["/lead"]\n', True, True),
        ('[git]\nrepos = ["a/"]\n', True, True),
        ('[git]\nrepos = [""]\n', True, False),
        ("[git]\nrepos = ['a/b']\n", True, False),
        ('[git]\nrepos = "a/b"\n', False, True),
        ("[git]\nrepos = [[]]\n", True, False),  # only "]" and spaces are not content
        ("[git]\nrepos = [ ] x ]\n", True, False),
        ('[git]\nrepos = ["a/b"] # mine\n', True, True),
        ('[git]\nrepos = ["a/b/c"]\n', True, True),
        ('[git]\nrepos = ["a:b/c"]\n', True, False),
        ('[git]\nrepos = ["a.b", "c/d"]\n', True, True),
        ('[git]\nrepos = ["a\\"/b"]\n', True, False),
        ('[git]\nrepos = [\r\n  "x/y"\r\n]\r\n', True, True),
        ('[git]\nrepos = ["a/b"\n', False, False),
        ('[git]\n  repos = ["o/r"]\n', True, True),
        ('[git]\r\nrepos = ["o/r"]\r\n', True, True),
        ("[git]\nrepos =\n", False, False),
        ("[git]\nrepos = x\n", False, False),
    ]

    def test_push_enabled_and_github_listed_answer_as_bash_did(self):
        for text, enabled, github in self.CASES:
            for _ in self.either_file(text):
                self.assertEqual((policy.git_push_enabled(self.paths), policy.github_repos_listed(self.paths)),
                                 (enabled, github), repr(text))


class GitlabChecksTest(PolicyTest):
    """Ported from scripts/test_factory_gitlab.sh (doomtickle/passdesk-ops#122): the repo's policy
    has an empty [git] repos list and an empty [gitlab] url, and this machine's overlay varies."""

    GITLAB = '[gitlab]\nurl = "https://gitlab.acme.net/"\n'

    def setUp(self):
        super().setUp()
        self.grant("npm_fetch git_push gitlab_create_mr")
        os.remove(os.path.join(self.root, "roles", "reviewer", "role.conf"))
        self.write_policy('[git]\nrepos = []\n\n[gitlab]\nurl = ""\n', None)

    def overlay(self, text):
        with open(os.path.join(self.config, "policy.toml"), "w") as f:
            f.write(text + "\n")

    def test_policy_value(self):
        self.overlay('[approvals]\ntools = [\n  "git_push",   # pushes\n  "github_create_pr",\n]\nhold_seconds = 45')
        self.assertEqual(re.sub(" +", " ", policy.policy_value(self.paths, "approvals", "tools")),
                         '[ "git_push", "github_create_pr", ]')  # a multi-line array comes back joined
        self.assertEqual(policy.policy_value(self.paths, "approvals", "hold_seconds"), "45")  # the key after it
        self.overlay('[git]\nrepos = ["a/b"]')
        self.assertEqual(policy.policy_value(self.paths, "git", "repos"), '["a/b"]')  # one line: unchanged
        self.assertEqual(policy.policy_value(self.paths, "gitlab", "url"), '""')  # from broker/policy.toml

    def test_gitlab_needs_token(self):
        for text, want in ((self.GITLAB, True), ("[gitlab]\nurl = 'https://gitlab.acme.net/'", True),
                           ('[gitlab]\nurl = ""', False),
                           ('# [gitlab]\n# url = "https://gitlab.example.com/"', False)):  # base url = ""
            self.overlay(text)
            self.assertEqual(policy.gitlab_needs_token(self.paths), want, text)

    def test_github_repos_listed(self):
        for text, want in (('[git]\nrepos = ["gitlab.acme.net/platform/api", "gitlab.acme.net:8443/g/p"]', False),
                           ('[git]\nrepos = ["gitlab.acme.net/platform/api", "my-org/my-project"]', True),
                           ("[git]\nrepos = []", False)):
            self.overlay(text)
            self.assertEqual(policy.github_repos_listed(self.paths), want, text)

    GL = "gitlab.acme.net"

    def test_git_repos_reads_every_quoted_entry(self):
        self.overlay('[git]\nrepos = [\n  "acme/widgets",  # one\n  \'%s/g/*\',\n  "%s/a/b/svc"\n]' % (self.GL, self.GL))
        self.assertEqual(policy.git_repos(self.paths), ["acme/widgets", "%s/g/*" % self.GL, "%s/a/b/svc" % self.GL])
        self.overlay("[git]\nrepos = []")
        self.assertEqual(policy.git_repos(self.paths), [])

    def test_group_entries_the_broker_would_accept_have_no_problem(self):
        for entry in ("%s/g/*" % self.GL, "%s/G/Sub/*" % self.GL, "%s:8443/g/*" % self.GL.upper(), "10.0.0.5/g/*",
                      "%s/%s/*" % (self.GL, "/".join("g%d" % i for i in range(21))),
                      "acme/widgets", "%s/g/p" % self.GL):
            netloc = (self.GL + ":8443") if ":8443" in entry else ("10.0.0.5" if entry.startswith("10.") else self.GL)
            self.assertIsNone(policy.repo_entry_problem(entry, netloc.lower()), entry)

    def test_malformed_group_entries_are_refused(self):
        for entry, fragment in (("acme/*", "GitLab only"), ("*/*", '"*" is allowed only'), ("*", '"*" is allowed only'),
                                ("%s/*" % self.GL, "whole instance"), ("%s/g/**" % self.GL, '"*" is allowed only'),
                                ("%s/*/g/*" % self.GL, '"*" is allowed only'), ("%s/g*" % self.GL, '"*" is allowed only'),
                                ("%s/g/*/x" % self.GL, '"*" is allowed only'), ("%s/g/x*/*" % self.GL, '"*" is allowed only'),
                                ("%s//g/*" % self.GL, "not a valid GitLab group"), ("%s/g//*" % self.GL, "not a valid GitLab group"),
                                ("%s/g/../*" % self.GL, "not a valid GitLab group"), ("%s/g/.x/*" % self.GL, "not a valid GitLab group"),
                                ("%s/g/-x/*" % self.GL, "not a valid GitLab group"), ("%s/g/x.git/*" % self.GL, "not a valid GitLab group"),
                                ("%s/g/x y/*" % self.GL, "not a valid GitLab group"), ("-.net/g/*", "not a valid GitLab group"),
                                ("%s:99999/g/*" % self.GL, "not a valid GitLab group"), ("%s:0/g/*" % self.GL, "not a valid GitLab group"),
                                ("https://%s/g/*" % self.GL, "not a valid GitLab group"),
                                ("%s/%s/*" % (self.GL, "/".join("g%d" % i for i in range(22))), "not a valid GitLab group")):
            problem = policy.repo_entry_problem(entry, self.GL)
            self.assertIsNotNone(problem, entry)
            self.assertIn(fragment, problem, entry)
            self.assertIn(entry, problem)

    def test_a_group_must_be_on_the_configured_gitlab(self):
        self.assertIn("not on the configured GitLab (gitlab.acme.net)",
                      policy.repo_entry_problem("gitlab.evil.example/g/*", self.GL))
        self.assertIn("not on the configured GitLab", policy.repo_entry_problem("%s:8443/g/*" % self.GL, self.GL))
        self.assertIn("[gitlab].url is not set", policy.repo_entry_problem("%s/g/*" % self.GL, None))

    def test_git_repos_problems_reads_the_policy(self):
        self.overlay(self.GITLAB + '[git]\nrepos = ["acme/widgets", "%s/g/*", "%s/*", "acme/*", "other.example/g/*"]' % (self.GL, self.GL))
        problems = policy.git_repos_problems(self.paths)
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("whole instance" in p for p in problems))
        self.assertTrue(any("GitLab only" in p for p in problems))
        self.assertTrue(any("other.example/g/*" in p and "not on the configured" in p for p in problems))
        self.overlay(self.GITLAB + '[git]\nrepos = ["%s/g/*"]' % self.GL)
        self.assertEqual(policy.git_repos_problems(self.paths), [])
        self.overlay('[git]\nrepos = ["%s/g/*"]' % self.GL)  # no [gitlab].url
        self.assertIn("[gitlab].url is not set", policy.git_repos_problems(self.paths)[0])
        self.overlay(self.GITLAB.replace("acme.net/", "acme.net:443/") + '[git]\nrepos = ["%s/g/*"]' % self.GL)
        self.assertEqual(policy.git_repos_problems(self.paths), [])  # :443 is the default port
        self.overlay('[git]\nrepos = []')
        self.assertEqual(policy.git_repos_problems(self.paths), [])

    def test_describe_repo_entry(self):
        self.assertEqual(policy.describe_repo_entry("%s/g/*" % self.GL), "%s/g/* (any project in the group)" % self.GL)
        self.assertEqual(policy.describe_repo_entry("acme/widgets"), "acme/widgets")

    def test_a_group_entry_enables_push_and_is_not_a_github_repo(self):
        self.overlay('[git]\nrepos = ["%s/g/*"]' % self.GL)
        self.assertTrue(policy.git_push_enabled(self.paths))
        self.assertFalse(policy.github_repos_listed(self.paths))

    def test_gitlab_mr_unheld(self):
        for text, want in (
                (self.GITLAB, None),  # no [approvals].tools: holds off
                (self.GITLAB + '[approvals]\ntools = ["git_push", "github_create_pr"]', UNHELD),
                (self.GITLAB + '[approvals]\ntools = [\n  "git_push",\n  "github_create_pr",\n]', UNHELD),
                (self.GITLAB + '[approvals]\ntools = [\n  "git_push",\n  "gitlab_create_mr",\n]', None),
                (self.GITLAB + '[approvals]\ntools = ["git_push", "gitlab_create_mr"]', None),
                (self.GITLAB + "[approvals]\ntools = []", None),  # holds off on purpose
                (self.GITLAB + "[approvals]\ntools = [\n]", None),
                ('[approvals]\ntools = ["git_push"]', None)):  # no GitLab configured
            self.overlay(text)
            self.assertEqual(policy.gitlab_mr_unheld(self.paths), want, text)
        self.overlay(self.GITLAB + '[approvals]\ntools = ["git_push"]')
        self.grant("npm_fetch git_push")  # no role granted gitlab_create_mr
        self.assertIsNone(policy.gitlab_mr_unheld(self.paths))


class CloudIdTest(PolicyTest):
    """Ported from the jira_cloud_id_set part of scripts/test_factory_config.sh."""

    def test_cloud_id(self):
        self.write_policy('[jira]\ncloud_id = ""\n', None)
        with open(os.path.join(REPO, "config.example", "policy.toml")) as f:
            example = f.read().rstrip("\n")
        for text, want in (('[jira]\ncloud_id = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"', True),
                           ('[jira]\ncloud_id = ""', False),
                           ('[jira]\ncloud_id = "00000000-0000-0000-0000-000000000000"', False),
                           ('[jira]\ncloud_id = "00000000-0000-0000-0000-000000000001"', True),
                           (example, False)):  # the commented-out example
            with open(os.path.join(self.config, "policy.toml"), "w") as f:
                f.write(text + "\n")
            self.assertEqual(policy.jira_cloud_id_set(self.paths), want, text)
        os.remove(os.path.join(self.config, "policy.toml"))
        self.assertFalse(policy.jira_cloud_id_set(self.paths))  # falls back to the empty repo policy


if __name__ == "__main__":
    unittest.main()
