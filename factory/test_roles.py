"""Tests for factory/roles.py and `factory roles` (factory/commands/roles.py)."""

import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from factory import roles
from factory.cli import main
from factory.context import Context, Paths

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEVELOPER = '''# comment
DESCRIPTION="Writes code"
WORKSPACE="rw"
LANES="3"
BROKER_TOOLS="npm_fetch git_push"
MODEL="sonnet"
MODELS="haiku opus"
EFFORT=""
'''
REVIEWER = 'DESCRIPTION="Reviews"\nWORKSPACE="ro"\nBROKER_TOOLS=""\n'
RESEARCHER = 'OPTIONAL="1"\nDESCRIPTION="Looks things up"\nWORKSPACE="ro"\nBROKER_TOOLS="web_fetch"\nLANES="1"\n'


class Fixture(unittest.TestCase):
    """A fake checkout (roles/ and broker/tools.txt) and a fake config folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "repo")
        self.config = os.path.join(self.tmp, "config", "ai-code-factory")
        self.state = os.path.join(self.tmp, "state")
        self.paths = Paths(self.root, self.config, self.state)
        os.makedirs(os.path.join(self.root, "broker"))
        self.write(os.path.join(self.root, "broker", "tools.txt"), "# tools\nnpm_fetch\ngit_push\nweb_fetch\n")
        for name, text in (("developer", DEVELOPER), ("reviewer", REVIEWER), ("researcher", RESEARCHER)):
            self.write_role(name, text)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def write_role(self, name, text):
        self.write(os.path.join(self.root, "roles", name, "role.conf"), text)

    def override(self, role, text):
        self.write(roles.role_override_file(self.paths, role), text)

    def enable(self, text):
        self.write(roles.enabled_roles_file(self.paths), text)

    def run_roles(self, tty=False):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: tty
        ctx = Context(self.paths, {}, out, err)
        from factory.commands import roles as cmd
        self.assertEqual(cmd.run(ctx, []), 0)
        self.assertEqual(err.getvalue(), "")
        return out.getvalue()


class RolesTest(Fixture):
    def test_list_all_roles_is_every_folder_with_a_role_conf_in_byte_order(self):
        self.write_role("Zed", "")
        os.makedirs(os.path.join(self.root, "roles", "no-conf"))
        os.makedirs(os.path.join(self.root, "roles", ".hidden"))
        self.write_role(".hidden", "")
        self.write(os.path.join(self.root, "roles", "a-file"), "")
        self.assertEqual(roles.list_all_roles(self.paths), ["Zed", "developer", "researcher", "reviewer"])

    def test_no_roles_folder(self):
        self.assertEqual(roles.list_all_roles(Paths(os.path.join(self.tmp, "nowhere"), "", "")), [])

    def test_optional_roles_are_off_unless_enabled_roles_names_them(self):
        self.assertTrue(roles.role_optional(self.paths, "researcher"))
        self.assertFalse(roles.role_optional(self.paths, "developer"))
        self.assertEqual(roles.list_roles(self.paths), ["developer", "reviewer"])
        self.enable("")
        self.assertEqual(roles.list_roles(self.paths), ["developer", "reviewer"])
        self.enable("# researcher\n\n   researcher  \t\n")
        self.assertEqual(roles.list_roles(self.paths), ["developer", "researcher", "reviewer"])
        self.enable("developer\n")
        self.assertEqual(roles.list_roles(self.paths), ["developer", "reviewer"])

    def test_optional_only_when_exactly_1(self):
        self.write_role("researcher", 'OPTIONAL="2"\n')
        self.assertFalse(roles.role_optional(self.paths, "researcher"))
        self.write_role("researcher", 'OPTIONAL=""\n')
        self.assertFalse(roles.role_optional(self.paths, "researcher"))

    def test_optional_in_a_local_override_does_not_count(self):
        self.override("developer", 'OPTIONAL="1"\n')
        self.assertFalse(roles.role_optional(self.paths, "developer"))

    def test_parse_enabled_roles(self):
        text = "a\n  b-2  \n#c\nBad\n9x\nd e\n\nfoo_bar\nlast"
        self.assertEqual(roles.parse_enabled_roles(text), ["a", "b-2", "last"])

    def test_enabled_roles_that_is_not_a_file_lists_nothing(self):
        os.makedirs(roles.enabled_roles_file(self.paths))
        self.assertEqual(roles.enabled_roles_listed(self.paths), [])

    def test_role_get_reads_role_conf(self):
        self.assertEqual(roles.role_get(self.paths, "developer", "MODEL"), "sonnet")
        self.assertEqual(roles.role_get(self.paths, "developer", "NOPE"), "")
        self.assertEqual(roles.role_get(self.paths, "developer", "EFFORT"), "")

    def test_override_layers_on_the_tuning_keys(self):
        self.override("developer", '# cheaper\nMODELS="x y"\n  # indented\nLANES="5"\nEFFORT=""\n')
        self.assertEqual(roles.role_get(self.paths, "developer", "MODELS"), "x y")
        self.assertEqual(roles.role_get(self.paths, "developer", "LANES"), "5")
        self.assertEqual(roles.role_get(self.paths, "developer", "MODEL"), "sonnet")

    def test_an_empty_override_value_wins(self):
        self.override("developer", 'MODEL=""\n')
        self.assertEqual(roles.role_get(self.paths, "developer", "MODEL"), "")

    def test_override_cannot_move_the_security_boundary(self):
        self.override("developer", 'BROKER_TOOLS="web_fetch"\nWORKSPACE="ro"\nTEMPLATE="reviewer"\nDESCRIPTION="x"\n')
        self.assertEqual(roles.role_get(self.paths, "developer", "BROKER_TOOLS"), "npm_fetch git_push")
        self.assertEqual(roles.role_get(self.paths, "developer", "WORKSPACE"), "rw")
        self.assertEqual(roles.role_get(self.paths, "developer", "DESCRIPTION"), "Writes code")

    def test_override_that_is_not_a_file_is_ignored(self):
        os.makedirs(roles.role_override_file(self.paths, "developer"))
        self.assertEqual(roles.role_get(self.paths, "developer", "MODEL"), "sonnet")

    def test_role_lanes(self):
        self.assertEqual(roles.role_lanes(self.paths, "developer"), "3")
        self.assertEqual(roles.role_lanes(self.paths, "reviewer"), "1")
        self.override("developer", 'LANES="5"\n')
        self.assertEqual(roles.role_lanes(self.paths, "developer"), "5")
        self.override("developer", 'LANES=""\n')
        self.assertEqual(roles.role_lanes(self.paths, "developer"), "1")

    def test_override_keys_match_bash(self):
        with open(os.path.join(REPO, "scripts", "lib", "roles.sh")) as f:
            m = re.search(r'^readonly ROLE_OVERRIDE_KEYS="([^"]*)"', f.read(), re.M)
        self.assertEqual(tuple(m.group(1).split()), roles.ROLE_OVERRIDE_KEYS)


def locales_installed():
    try:
        out = subprocess.run(["locale", "-a"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    except OSError:
        return []
    return out.decode(errors="replace").split()


class BashParityTest(Fixture):
    """The bash helpers (scripts/lib/roles.sh) still serve every other command; both
    sides must answer alike on one fixture, edge cases included."""

    def bash(self, snippet, locale=None):
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(self.config), XDG_STATE_HOME=self.state)
        if locale:
            env["LC_ALL"] = locale
        cp = subprocess.run(
            ["bash", "-c", 'ROOT="$1"; . "$2/scripts/lib/common.sh"; . "$2/scripts/lib/roles.sh"; ' + snippet,
             "_", self.root, REPO],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.decode()

    def test_lookups_and_lists_match(self):
        self.write_role("odd", 'DESCRIPTION="a  b"\nWORKSPACE="rw"\nLANES="x"\nBROKER_TOOLS="a  b"\nBROKER_TOOLS="c d"\n')
        self.override("developer", 'LANES="5"\nMODEL=""\nBROKER_TOOLS="evil"\n')
        self.override("odd", "# nothing\n")
        self.enable("  researcher\nodd\n#x\n")
        self.assertEqual(self.bash("list_all_roles").split(), roles.list_all_roles(self.paths))
        self.assertEqual(self.bash("list_roles").split(), roles.list_roles(self.paths))
        self.assertEqual(self.bash("enabled_roles_listed").split(), roles.enabled_roles_listed(self.paths))
        for role in roles.list_all_roles(self.paths):
            self.assertEqual(self.bash('role_optional "%s" && echo y || echo n' % role).strip() == "y",
                             roles.role_optional(self.paths, role), role)
            self.assertEqual(self.bash('role_lanes "%s"' % role), roles.role_lanes(self.paths, role), role)
            for key in ("DESCRIPTION", "WORKSPACE", "BROKER_TOOLS", "MODEL", "MODELS", "EFFORT", "LANES", "OPTIONAL"):
                self.assertEqual(self.bash('role_get "%s" %s' % (role, key)).rstrip("\n"),
                                 roles.role_get(self.paths, role, key), (role, key))

    def test_list_all_roles_is_in_byte_order_under_any_locale(self):
        # In en_US.UTF-8 "developer" sorts before "dev-ops" (the dash is ignored); bytes put the dash first.
        for name in ("dev-ops", "Zed", "alpha"):
            self.write_role(name, 'WORKSPACE="ro"\n')
        expected = roles.list_all_roles(self.paths)
        self.assertEqual(expected[:2], ["Zed", "alpha"])
        self.assertLess(expected.index("dev-ops"), expected.index("developer"))
        other = [l for l in locales_installed() if not re.match(r"(c|posix)(\.|$)", l, re.I)]
        if not other:
            self.skipTest("no locale other than C is installed")
        for locale in ["C"] + other[:3]:
            self.assertEqual(self.bash("list_all_roles", locale).split(), expected, locale)

    def test_valid_role_name_and_the_reserved_names_match(self):
        with open(os.path.join(REPO, "scripts", "lib", "roles.sh")) as f:
            m = re.search(r'^readonly RESERVED_ROLE_NAMES="([^"]*)"', f.read(), re.M)
        self.assertEqual(tuple(m.group(1).split()), roles.RESERVED_ROLE_NAMES)
        names = ["", "a", "ab", "reviewer", "my-role", "r2-d2", "A", "1a", "a_b", "a b", "-ab", "a" * 31, "a" * 32,
                 "broker", "coordinator", "dispatch", "broker2", "x-lane2", "x-lane12", "x-lane", "x-lane2a", "lane2",
                 "ab-", "a--b", "é1", "a.b"]
        for name in names:
            self.assertEqual(self.bash("valid_role_name '%s' && echo y || echo n" % name).strip() == "y",
                             roles.valid_role_name(name), repr(name))

    def test_roles_output_matches_the_old_bash_command(self):
        # bash's cmd_roles, kept here verbatim as the reference until bash stops using these helpers.
        reference = r'''
lanes_label() { local n; n="$(role_lanes "$1")"; if [ "$n" = 1 ]; then echo "1 lane"; else echo "$n lanes"; fi; }
B="" D="" N=""
for r in $(list_roles); do
  printf '%s%-18s%s %-9s %-8s %s\n' "$B" "$r" "$N" \
    "$( [ "$(role_get "$r" WORKSPACE)" = rw ] && echo "branches" || echo "read-only")" \
    "$(lanes_label "$r")" "$(role_get "$r" DESCRIPTION)"
  printf '  %snetwork tools: %s%s\n' "$D" "$(role_get "$r" BROKER_TOOLS | sed 's/ /, /g; s/^$/none/')" "$N"
  [ ! -f "$(role_override_file "$r")" ] || printf '  %slocal overrides: %s%s\n' "$D" "$(role_override_file "$r")" "$N"
done
for r in $(list_all_roles); do
  role_optional "$r" && ! role_listed_enabled "$r" || continue
  printf '%s%-18s%s off (optional)  enable: add "%s" to %s, then factory start\n' "$D" "$r" "$N" "$r" "$(enabled_roles_file)"
done'''
        self.write_role("odd", 'DESCRIPTION="a  b"\nWORKSPACE="rw"\nLANES="x"\nBROKER_TOOLS="a  b"\n')
        self.override("developer", 'LANES="5"\n')
        self.assertEqual(self.run_roles(), self.bash(reference))
        self.enable("researcher\n")
        self.assertEqual(self.run_roles(), self.bash(reference))


class CommandTest(Fixture):
    def test_output(self):
        self.override("developer", 'LANES="1"\n')
        lines = self.run_roles().split("\n")
        self.assertEqual(lines, [
            "developer          branches  1 lane   Writes code",
            "  network tools: npm_fetch, git_push",
            "  local overrides: " + roles.role_override_file(self.paths, "developer"),
            "reviewer           read-only 1 lane   Reviews",
            "  network tools: none",
            'researcher         off (optional)  enable: add "researcher" to %s, then factory start'
            % roles.enabled_roles_file(self.paths),
            "",
        ])

    def test_enabled_optional_role_is_listed_not_flagged_off(self):
        self.enable("researcher\n")
        out = self.run_roles()
        self.assertIn("researcher         read-only 1 lane   Looks things up\n  network tools: web_fetch\n", out)
        self.assertNotIn("off (optional)", out)

    def test_lane_count_label(self):
        self.assertIn("branches  3 lanes  Writes code", self.run_roles())

    def test_colour_only_on_a_terminal(self):
        self.assertNotIn("\033", self.run_roles(tty=False))
        coloured = self.run_roles(tty=True)
        self.assertIn("\033[1mdeveloper         \033[0m", coloured)
        self.assertIn("  \033[2mnetwork tools: npm_fetch, git_push\033[0m", coloured)

    def test_no_roles_prints_nothing(self):
        shutil.rmtree(os.path.join(self.root, "roles"))
        self.assertEqual(self.run_roles(), "")

    def test_needs_neither_broker_tools_txt_nor_a_config_folder(self):
        shutil.rmtree(os.path.join(self.root, "broker"))
        self.assertIn("developer", self.run_roles())

    def test_main_runs_it_with_the_checkout_and_environment_it_is_given(self):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: False
        rc = main(["roles"], root=self.root, environ={"HOME": self.tmp, "XDG_CONFIG_HOME": "/" + "nonexistent"},
                  stdout=out, stderr=err)
        self.assertEqual((rc, err.getvalue()), (0, ""))
        self.assertIn('enable: add "researcher" to /nonexistent/ai-code-factory/enabled-roles', out.getvalue())


if __name__ == "__main__":
    unittest.main()
