"""Tests for factory/policy.py. Ported from policy_value (scripts/lib/common.sh) and
gitlab_needs_token / gitlab_mr_unheld (bin/factory); the bash copies stay until `start` and
`status` are ported. Each check runs both on the same policy files and requires the same answer.
"""

import os
import subprocess
import unittest

from factory import policy
from factory.fixtures import REPO, CommandTest

POLICIES = [
    "",
    "[gitlab]\nurl = \"https://gitlab.example\"\n",
    "[gitlab]\nurl = 'https://gitlab.example'\n",
    "[gitlab]\nurl = \"\"\n",
    "[gitlab]\nurl=\"x\" # comment\n[other]\nurl = \"no\"\n",
    "[other]\nurl = \"no\"\n[gitlab]\n# url = \"commented\"\n",
    "[gitlab]\n  url   =   \"indented\"\n",
    "[gitlab]\nurl = unquoted\n",
    "[gitlab-extra]\nurl = \"wrong section\"\n",
    "[approvals]\ntools = [\"git_push\"]\n",
    "[approvals]\ntools = [\"gitlab_create_mr\", \"git_push\"]\n",
    "[approvals]\ntools = []\n",
    "[approvals]\ntools = [ ]  # none\n",
    "[approvals]\ntools = [\n  \"git_push\", # a\n  \"jira\"\n]\n",
    "[approvals]\ntools = [\n  \"gitlab_create_mr\",\n]\n",
    "[approvals]\ntools = [\n]\n",
    "[approvals]\ntools = [\n  \"never closed\"\n",
    "[approvals]\ntools = [\"git_push\"]\ntools = [\"later\"]\n",
    "[approvals]\nother = 1\n[approvals]\ntools = [\"two sections\"]\n",
    "[web]\nallow_hosts = [\"a\", \"b\"]\n[approvals]\ntools = [\"x\"]",
    "[gitlab]\r\nurl = \"crlf\"\r\n",
]


class PolicyTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.write("repo/broker/tools.txt", "git_fetch\ngitlab_create_mr\n")
        self.write("repo/roles/developer/role.conf", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="git_fetch gitlab_create_mr"\n')
        self.write("repo/roles/reviewer/role.conf", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nBROKER_TOOLS="git_fetch"\n')

    def bash(self, snippet):
        env = dict(self.environ, XDG_CONFIG_HOME=os.path.dirname(self.config), XDG_STATE_HOME=os.path.dirname(self.state), LC_ALL="C")
        cp = subprocess.run(["bash", "-c", 'ROOT="$1"; . "$2/scripts/lib/common.sh"; . "$2/scripts/lib/roles.sh"; '
                             'eval "$(sed -n \'/^gitlab_needs_token() {/,/^}/p;/^gitlab_mr_unheld() {/,/^}/p\' "$2/bin/factory")"; '
                             + snippet, "_", self.root, REPO],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, cwd=self.tmp)
        return cp.returncode, cp.stdout.decode(), cp.stderr.decode()

    def write_policy(self, base, local):
        for path, text in ((os.path.join(self.root, "broker", "policy.toml"), base), (os.path.join(self.config, "policy.toml"), local)):
            if os.path.exists(path):
                os.remove(path)
            if text is not None:
                with open(path, "w", newline="") as f:
                    f.write(text)


class PolicyValueTest(PolicyTest):
    def both(self, section, key):
        _, out, err = self.bash('policy_value %s %s' % (section, key))
        value = policy.policy_value(self.paths, section, key)
        self.assertEqual(err, "")
        self.assertEqual(out, "" if value is None else value + "\n", (section, key))
        return value

    def test_every_fixture_reads_the_same(self):
        os.makedirs(self.config)
        os.makedirs(os.path.join(self.root, "broker"), exist_ok=True)
        for text in POLICIES:
            for base, local in ((text, None), (None, text), (text, "[approvals]\ntools = [\"mine\"]\n")):
                self.write_policy(base, local)
                for section, key in (("gitlab", "url"), ("approvals", "tools"), ("web", "allow_hosts"), ("other", "url")):
                    self.both(section, key)

    def test_the_local_overlay_wins_only_where_it_sets_the_key(self):
        os.makedirs(self.config)
        self.write_policy("[approvals]\ntools = [\"base\"]\n[gitlab]\nurl = \"https://base\"\n", "[approvals]\ntools = [\"mine\"]\n")
        self.assertEqual(self.both("approvals", "tools"), '["mine"]')
        self.assertEqual(self.both("gitlab", "url"), '"https://base"')

    def test_an_empty_value_is_set_but_empty(self):
        self.write_policy("[gitlab]\nurl =\n", None)
        self.assertEqual(policy.policy_value(self.paths, "gitlab", "url"), "")

    def test_nothing_set_is_none(self):
        self.write_policy(None, None)
        self.assertIsNone(policy.policy_value(self.paths, "gitlab", "url"))


class GitlabTest(PolicyTest):
    def test_needs_token_and_unheld_agree_with_bash(self):
        os.makedirs(self.config)
        for text in POLICIES + ["[gitlab]\nurl = \"https://g\"\n[approvals]\ntools = [\"git_push\"]\n",
                                "[gitlab]\nurl = \"https://g\"\n[approvals]\ntools = [\"gitlab_create_mr\"]\n",
                                "[gitlab]\nurl = \"https://g\"\n[approvals]\ntools = []\n",
                                "[gitlab]\nurl = \"https://g\"\n[approvals]\ntools = [\n \"git_push\"\n]\n"]:
            for base, local in ((text, None), (None, text)):
                self.write_policy(base, local)
                status, _, _ = self.bash("gitlab_needs_token")
                self.assertEqual(policy.gitlab_needs_token(self.paths), status == 0, text)
                status, out, _ = self.bash("gitlab_mr_unheld")
                why = policy.gitlab_mr_unheld(self.paths)
                self.assertEqual((status == 0, out.rstrip("\n")), (why is not None, why or ""), text)

    def test_it_warns_only_when_the_tool_is_granted(self):
        os.makedirs(self.config)
        self.write_policy('[gitlab]\nurl = "https://g"\n[approvals]\ntools = ["git_push"]\n', None)
        self.assertEqual(policy.gitlab_mr_unheld(self.paths), '[approvals].tools is set without "gitlab_create_mr"')
        self.write("repo/roles/developer/role.conf", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="git_fetch"\n')
        self.assertIsNone(policy.gitlab_mr_unheld(self.paths))


if __name__ == "__main__":
    unittest.main()
