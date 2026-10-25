"""Tests for role validation in factory/roles.py: validate_role, validate_role_override(s),
validate_enabled_roles, role_lane_names, coordinator_broker_tools, known_broker_tools.

Ported from scripts/test_factory_role_overrides.sh. Every check runs the Python and the
bash helper (scripts/lib/roles.sh, still called by start-broker.sh, start-dispatch.sh,
02-launch-factory.sh and bin/factory) on the same fixture and requires the same answer:
the same refusal with the same message, or both pass. Where Python is deliberately
stricter, a test says so and asserts only the Python.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from factory import roles
from factory.context import FactoryError, Paths
from factory.test_roles import locales_installed

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEVELOPER = '''DESCRIPTION="Writes code"
TEMPLATE="developer"
WORKSPACE="rw"
LANES="3"
BROKER_TOOLS="npm_fetch"
MODEL="sonnet"
MODELS="haiku opus"
EFFORT=""
MAX_TURNS="200"
'''
RESEARCHER = '''# Off by default (test fixture).
OPTIONAL="1"
DESCRIPTION="Looks things up"
TEMPLATE="developer"
WORKSPACE="ro"
BROKER_TOOLS="npm_fetch"
'''


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "repo")
        self.config = os.path.join(self.tmp, "config", "ai-code-factory")
        self.state = os.path.join(self.tmp, "state")
        self.cwd = os.path.join(self.tmp, "cwd")  # empty, so a stray glob in bash matches nothing
        os.makedirs(self.cwd)
        self.paths = Paths(self.root, self.config, self.state)
        self.write(os.path.join(self.root, "broker", "tools.txt"), "# tools\nnpm_fetch\ngit_push\n")
        self.make_role("developer", DEVELOPER)
        self.make_role("researcher", RESEARCHER)
        self.overrides = os.path.join(self.config, "roles")
        self.override_file = os.path.join(self.overrides, "developer.conf")
        self.enabled = roles.enabled_roles_file(self.paths)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode())

    def make_role(self, name, conf):
        base = os.path.join(self.root, "roles", name)
        self.write(os.path.join(base, "kit", "spec.yaml"), "name: %s\n" % name)
        self.write(os.path.join(base, "instructions.md"), "Do the work.\n")
        self.write(os.path.join(base, "role.conf"), conf)

    def role_conf(self, name, text):
        self.write(os.path.join(self.root, "roles", name, "role.conf"), text)

    def override(self, text, role="developer"):
        self.write(os.path.join(self.overrides, role + ".conf"), text)

    def bash(self, snippet, locale="C"):
        """(status, stdout, stderr) of the bash helpers on this fixture (an empty working folder)."""
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(self.config), XDG_STATE_HOME=self.state,
                   LC_ALL=locale)
        cp = subprocess.run(
            ["bash", "-c", 'ROOT="$1"; . "$2/scripts/lib/common.sh"; . "$2/scripts/lib/roles.sh"; ' + snippet,
             "_", self.root, REPO],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self.cwd)
        return cp.returncode, cp.stdout.decode(errors="surrogateescape"), cp.stderr.decode(errors="surrogateescape")

    def both(self, call, snippet, locale="C"):
        """Run the Python `call` and the bash `snippet` (a die-ing helper); require the same
        outcome and return the refusal message, or None if both passed."""
        try:
            call()
            message = None
        except FactoryError as e:
            message = str(e)
        status, out, err = self.bash(snippet, locale)
        if message is None:
            self.assertEqual((status, out, err), (0, "", ""), "bash refused what Python accepts")
        else:
            self.assertEqual((status, out, err), (1, "", "error: %s\n" % message), "bash and Python disagree")
        return message

    def role_check(self, role="developer", locale="C"):
        return self.both(lambda: roles.validate_role(self.paths, role), 'validate_role "%s"' % role, locale)

    def overrides_check(self, locale="C"):
        return self.both(lambda: roles.validate_role_overrides(self.paths), "validate_role_overrides", locale)

    def enabled_check(self, locale="C"):
        return self.both(lambda: roles.validate_enabled_roles(self.paths), "validate_enabled_roles", locale)

    def bash_get(self, role, key):
        return self.bash('role_get "%s" %s' % (role, key))[1].rstrip("\n")


class NoOverrideTest(Fixture):
    def test_role_conf_alone_is_read_and_validated(self):
        self.assertEqual(roles.role_get(self.paths, "developer", "MODEL"), "sonnet")
        self.assertEqual(roles.role_lanes(self.paths, "developer"), "3")
        self.assertIsNone(self.role_check())
        self.assertIsNone(self.overrides_check())

    def test_a_role_conf_error_is_reported_without_a_note(self):
        self.role_conf("developer", DEVELOPER.replace('EFFORT=""', 'EFFORT="max"'))
        self.assertEqual(self.role_check(), "role developer: EFFORT must be low, medium, high or xhigh (or empty)")


class MergeTest(Fixture):
    def test_an_override_layers_its_tuning_keys_over_role_conf(self):
        self.override('# Cheaper default, more lanes.\nMODELS="claude-haiku-5-5 opus claude-fable-5-1"\n\n'
                      '  # indented comment\nEFFORT="high"\nLANES="5"\nMAX_TURNS=""\n')
        get = lambda key: roles.role_get(self.paths, "developer", key)
        self.assertEqual(get("MODELS"), "claude-haiku-5-5 opus claude-fable-5-1")
        self.assertEqual(get("EFFORT"), "high")
        self.assertEqual(roles.role_lanes(self.paths, "developer"), "5")
        self.assertEqual(get("MAX_TURNS"), "")  # an explicitly empty value wins
        self.assertEqual(get("MODEL"), "sonnet")  # unset key: role.conf's
        for key in ("MODELS", "EFFORT", "LANES", "MAX_TURNS", "MODEL"):
            self.assertEqual(self.bash_get("developer", key), get(key), key)
        self.assertIsNone(self.role_check())

    def test_role_get_ignores_keys_outside_the_allowlist(self):
        self.override('BROKER_TOOLS="npm_fetch git_push"\nWORKSPACE="ro"\n')
        self.assertEqual(roles.role_get(self.paths, "developer", "BROKER_TOOLS"), "npm_fetch")
        self.assertEqual(roles.role_get(self.paths, "developer", "WORKSPACE"), "rw")
        self.assertEqual(self.bash_get("developer", "BROKER_TOOLS"), "npm_fetch")
        self.assertEqual(self.bash_get("developer", "WORKSPACE"), "rw")


class AllowlistTest(Fixture):
    def test_keys_outside_the_allowlist_are_refused_naming_the_file_and_key(self):
        for key in ("BROKER_TOOLS", "WORKSPACE", "TEMPLATE", "DESCRIPTION", "OTHER_KEY", "OPTIONAL"):
            self.override('LANES="2"\n%s="npm_fetch git_push"\n' % key)
            self.assertEqual(
                self.role_check(),
                "%s: %s can't be set in a local override (allowed: MODEL MODELS EFFORT LANES MAX_SESSION_TOKENS "
                "MAX_TURNS MAX_JOB_SECONDS); change it in roles/developer/role.conf" % (self.override_file, key), key)

    def test_a_local_override_cannot_set_optional_on_an_optional_role(self):
        self.write(self.enabled, "researcher\n")
        self.override('OPTIONAL="0"\n', role="researcher")
        self.assertIn("OPTIONAL can't be set in a local override", self.role_check("researcher"))
        self.assertTrue(roles.role_optional(self.paths, "researcher"))

    def test_the_allowed_keys_are_the_ones_bash_allows(self):
        for key in roles.ROLE_OVERRIDE_KEYS:
            self.override('%s=""\n' % key)
            self.assertIsNone(self.role_check(), key)


class ValueTest(Fixture):
    def test_override_values_are_validated_like_role_conf_and_the_error_names_the_override(self):
        cases = (
            ('MODEL="bad model"', "MODEL must contain only"),
            ('MODELS="opus bad;model"', "MODELS entries must contain only"),
            ('EFFORT="max"', "EFFORT must be low, medium, high or xhigh"),
            ('LANES="9"', "LANES must be a whole number from 1 to 8"),
            ('LANES="0"', "LANES must be a whole number from 1 to 8"),
            ('MAX_SESSION_TOKENS="lots"', "MAX_SESSION_TOKENS must be a whole number"),
            ('MAX_TURNS="-1"', "MAX_TURNS must be a whole number"),
            ('MAX_JOB_SECONDS="1h"', "MAX_JOB_SECONDS must be a whole number"),
        )
        for line, expected in cases:
            self.override(line + "\n")
            message = self.role_check()
            self.assertIn(expected, message, line)
            self.assertTrue(message.endswith(" (set in %s)" % self.override_file), line)

    def test_the_exact_messages(self):
        for line, expected in (
            ('MODEL="a b"', "MODEL must contain only letters, digits, dots, dashes, underscores"),
            ('MODELS="a b/c"', "MODELS entries must contain only letters, digits, dots, dashes, underscores"),
            ('MAX_SESSION_TOKENS="x"', "MAX_SESSION_TOKENS must be a whole number of tokens (or empty)"),
            ('MAX_TURNS="x"', "MAX_TURNS must be a whole number of turns (or empty)"),
            ('MAX_JOB_SECONDS="x"', "MAX_JOB_SECONDS must be a whole number of seconds (or empty)"),
            ('LANES="x"', "LANES must be a whole number from 1 to 8 (or empty)"),
        ):
            self.override(line + "\n")
            self.assertEqual(self.role_check(), "role developer: %s (set in %s)" % (expected, self.override_file), line)

    def test_a_bad_value_in_role_conf_has_no_note_even_with_an_unrelated_override(self):
        self.override('MODEL="ok"\n')
        self.role_conf("developer", DEVELOPER.replace('LANES="3"', 'LANES="0"'))
        self.assertEqual(self.role_check(),
                         "role developer: LANES must be a whole number from 1 to 8 (or empty)")

    def test_a_bad_role_conf_value_overridden_by_a_good_one_passes(self):
        self.role_conf("developer", DEVELOPER.replace('LANES="3"', 'LANES="0"'))
        self.override('LANES="2"\n')
        self.assertIsNone(self.role_check())

    def test_values_that_pass(self):
        for conf in ('MODEL="claude-sonnet-5.5_x"', 'MODELS="a  b\tc"', 'MODELS=""', 'EFFORT="xhigh"', 'LANES="8"',
                     'LANES="1"', 'MAX_TURNS="0"', 'MAX_SESSION_TOKENS="0123"', 'MAX_JOB_SECONDS=""'):
            self.override(conf + "\n")
            self.assertIsNone(self.role_check(), conf)

    def test_values_that_do_not_pass(self):
        for conf in ('MODEL="a b"', 'MODEL="é"', 'MODELS="a é"', 'MODELS="a\vb"', 'MODELS="*"', 'MODELS="$(id)"',
                     'MODEL="a$b"', 'EFFORT="High"', 'EFFORT=" high"', 'LANES="01"', 'LANES="٣"', 'LANES="1 "',
                     'LANES=" 1"', 'LANES="1.5"', 'MAX_TURNS="٣"', 'MAX_TURNS="1 2"', 'MAX_TURNS=" 1"',
                     'MAX_TURNS="+1"'):
            self.override(conf + "\n")
            self.assertIsNotNone(self.role_check(), conf)

    def test_the_last_line_for_a_key_wins_in_the_override_and_in_role_conf(self):
        self.override('LANES="9"\nLANES="2"\n')
        self.assertIsNone(self.role_check())
        self.override('LANES="2"\nLANES="9"\n')
        self.assertIn("LANES must be", self.role_check())
        self.override('')
        self.role_conf("developer", DEVELOPER + 'LANES="9"\n')
        self.assertIn("LANES must be", self.role_check())


class RoleConfTest(Fixture):
    def test_template_workspace_and_optional(self):
        for old, new, expected in (
            ('TEMPLATE="developer"', 'TEMPLATE="other"', "role developer: TEMPLATE must be one of: developer reviewer"),
            ('TEMPLATE="developer"', 'TEMPLATE=""', "role developer: TEMPLATE must be one of: developer reviewer"),
            ('TEMPLATE="developer"', '', "role developer: TEMPLATE must be one of: developer reviewer"),
            ('WORKSPACE="rw"', 'WORKSPACE="RW"', "role developer: WORKSPACE must be rw or ro"),
            ('WORKSPACE="rw"', '', "role developer: WORKSPACE must be rw or ro"),
            ('WORKSPACE="rw"', 'WORKSPACE="rw"\nOPTIONAL="2"',
             "role developer: OPTIONAL must be 1 (off unless enabled-roles names it) or empty"),
        ):
            self.role_conf("developer", DEVELOPER.replace(old, new))
            self.assertEqual(self.role_check(), expected, new)
        self.role_conf("developer", DEVELOPER + 'OPTIONAL="0"\n')
        self.assertIsNone(self.role_check())
        self.role_conf("developer", DEVELOPER + 'OPTIONAL=""\n')
        self.assertIsNone(self.role_check())

    def test_unknown_broker_tool(self):
        self.role_conf("developer", DEVELOPER.replace('BROKER_TOOLS="npm_fetch"', 'BROKER_TOOLS="npm_fetch evil git_push"'))
        self.assertEqual(self.role_check(), "role developer: unknown broker tool 'evil' (known: npm_fetch git_push)")
        for tools in ("", "git_push npm_fetch", "  npm_fetch\tgit_push ", "npm_fetch,git_push", "NPM_FETCH", "npm_fetch\vgit_push"):
            self.role_conf("developer", DEVELOPER.replace('BROKER_TOOLS="npm_fetch"', 'BROKER_TOOLS="%s"' % tools))
            self.role_check()  # bash and Python agree, whichever way

    def test_the_kit_and_instructions_must_exist(self):
        os.remove(os.path.join(self.root, "roles", "developer", "instructions.md"))
        self.assertEqual(self.role_check(), "role developer: missing or empty roles/developer/instructions.md")
        self.write(os.path.join(self.root, "roles", "developer", "instructions.md"), "")
        self.assertEqual(self.role_check(), "role developer: missing or empty roles/developer/instructions.md")
        self.write(os.path.join(self.root, "roles", "developer", "instructions.md"), "x")
        self.assertIsNone(self.role_check())
        os.remove(os.path.join(self.root, "roles", "developer", "kit", "spec.yaml"))
        self.assertEqual(self.role_check(), "role developer: missing roles/developer/kit/spec.yaml")
        os.makedirs(os.path.join(self.root, "roles", "developer", "kit", "spec.yaml"))
        self.assertEqual(self.role_check(), "role developer: missing roles/developer/kit/spec.yaml")

    def test_the_instructions_may_be_a_symlink_to_a_file_but_not_to_an_empty_one_or_nothing(self):
        path = os.path.join(self.root, "roles", "developer", "instructions.md")
        os.remove(path)
        self.write(os.path.join(self.tmp, "real.md"), "text")
        os.symlink(os.path.join(self.tmp, "real.md"), path)
        self.assertIsNone(self.role_check())
        os.remove(path)
        os.symlink(os.path.join(self.tmp, "gone.md"), path)
        self.assertIn("missing or empty", self.role_check())

    def test_the_role_name_is_checked_first(self):
        for name in ("Developer", "d", "broker", "coordinator", "dispatch", "x-lane2", "a" * 32, "é1", "", "a b", "../x"):
            self.assertEqual(
                self.role_check(name),
                "invalid role name '%s' (lowercase letters, digits, dashes; not ending in -lane<N>; not: broker "
                "coordinator dispatch)" % name, name)

    def test_a_role_with_no_folder(self):
        self.assertEqual(self.role_check("nobody"), "role nobody: missing roles/nobody/kit/spec.yaml")

    def test_optional_roles_are_valid_roles(self):
        self.assertIsNone(self.role_check("researcher"))

    def test_a_missing_tool_list_is_the_same_error_everywhere(self):
        os.remove(os.path.join(self.root, "broker", "tools.txt"))
        expected = ("%s/broker/tools.txt is missing: broker/ is incomplete in this checkout "
                    "(restore it with 'git -C %s checkout HEAD -- broker')" % (self.root, self.root))
        with self.assertRaises(FactoryError) as cm:
            roles.validate_role(self.paths, "developer")
        self.assertEqual(str(cm.exception), expected)
        self.assertEqual(self.bash(":"), (1, "", "error: " + expected + "\n"))

    def test_the_known_templates_and_tools_are_bash_s(self):
        self.assertEqual(self.bash('printf %s "$KNOWN_TEMPLATES"')[1], " ".join(roles.KNOWN_TEMPLATES))
        for text in ("# tools\nnpm_fetch\ngit_push\n", "npm_fetch\n\n  \n# x\n git_push \n", "a\r\n\r\nb\r\n", "a b\nc",
                     "a\nb", "#only\n", "", "\n\n", "  # indented comment is a tool\n", "a\t\n\tb\n"):
            self.write(os.path.join(self.root, "broker", "tools.txt"), text)
            self.assertEqual(self.bash('printf %s "$KNOWN_BROKER_TOOLS"')[1], roles.known_broker_tools(self.paths),
                             repr(text))

    def test_the_real_tool_list_and_reserved_names_match_bash(self):
        real = Paths(REPO, "", "")
        cp = subprocess.run(["bash", "-c", ('ROOT="$1"; . "$1/scripts/lib/common.sh"; . "$1/scripts/lib/roles.sh"; '
                                            'printf %s "$KNOWN_BROKER_TOOLS"'), "_", REPO],
                            stdout=subprocess.PIPE, check=True)
        self.assertEqual(cp.stdout.decode(), roles.known_broker_tools(real))
        self.assertTrue(roles.known_broker_tools(real))

    def test_every_real_role_validates_in_python_as_in_bash(self):
        real = Paths(REPO, self.config, self.state)
        for name in roles.list_all_roles(real):
            roles.validate_role(real, name)
            subprocess.run(["bash", "-c", ('ROOT="$1"; . "$1/scripts/lib/common.sh"; . "$1/scripts/lib/roles.sh"; '
                                           'validate_role "$2"'), "_", REPO, name],
                           env=dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(self.config)), check=True)

    def test_python_is_stricter_where_bash_s_in_list_matches_adjacent_words(self):
        # bash's in_list is a substring test on word boundaries, so TEMPLATE="developer reviewer" and
        # EFFORT="low medium" pass there. Python wants one exact word.
        for old, new in (('TEMPLATE="developer"', 'TEMPLATE="developer reviewer"'),
                         ('WORKSPACE="rw"', 'WORKSPACE="rw ro"'), ('EFFORT=""', 'EFFORT="low medium"'),
                         ('WORKSPACE="rw"', 'WORKSPACE="rw"\nOPTIONAL="0 1"')):
            self.role_conf("developer", DEVELOPER.replace(old, new))
            with self.assertRaises(FactoryError):
                roles.validate_role(self.paths, "developer")


class StrictLinesTest(Fixture):
    def test_a_line_that_is_not_key_value_is_refused_and_never_run(self):
        pwned = os.path.join(self.tmp, "pwned")
        for line in ('LANES=2', "LANES='2'", 'lanes="2"', 'export LANES="2"', '$(touch %s)' % pwned, 'source /etc/profile',
                     '  LANES="2"', '\tLANES="2"', 'LANES = "2"', 'LANES="2" # c', 'LANES2="2"', '1LANES="2"',
                     'LANES="2', 'LANES=2"', '"LANES"="2"', 'LANES=""x'):
            self.override(line + "\n")
            self.assertEqual(self.role_check(), '%s: not a KEY="value" line: %s' % (self.override_file, line), line)
        self.override('MODEL="$(touch %s)"\n' % pwned)
        self.assertIn("MODEL must contain only", self.role_check())
        self.assertFalse(os.path.exists(pwned), "the override was executed")

    def test_lines_that_are_fine(self):
        for text in ('LANES="2"', 'LANES="2"   ', 'LANES="2"\r', 'LANES="2"\t\v\f', "# c", "   # c", "\t", "", "\r",
                     'LANES="2"\n\n\n# c\nMODEL="m"', 'MODEL="m"\r\nLANES="2"\r\n'):
            self.override(text)
            self.assertIsNone(self.role_check(), repr(text))
        self.override(b'MODEL="m"\nLANES="2"')  # no final newline
        self.assertIsNone(self.role_check())

    def test_non_ascii_whitespace_is_not_whitespace(self):
        for locale in ["C"] + [l for l in locales_installed() if re.search(r"utf-?8", l, re.I)][:3]:
            for prefix in (" ", " ", "　"):
                self.override(prefix + '# comment\n')
                self.assertIn("not a KEY=", self.role_check(locale=locale), (locale, prefix))
            self.override('LANES="2" \n')  # trailing: not [[:space:]] in sed's C-locale view either
            self.role_check(locale=locale)

    def test_the_first_bad_line_is_the_one_reported(self):
        self.override('LANES="2"\nbad\nBROKER_TOOLS="x"\n')
        self.assertIn("not a KEY", self.role_check())
        self.override('LANES="2"\nBROKER_TOOLS="x"\nbad\n')
        self.assertIn("BROKER_TOOLS can't be set", self.role_check())

    def test_file_kinds(self):
        os.makedirs(self.override_file)
        self.assertEqual(self.role_check(), "%s: not a regular file" % self.override_file)
        os.rmdir(self.override_file)
        os.symlink(os.path.join(self.tmp, "nowhere.conf"), self.override_file)  # dangling: -e is false, so none
        self.assertIsNone(self.role_check())
        os.remove(self.override_file)
        self.write(os.path.join(self.tmp, "real.conf"), 'LANES="4"\n')  # bash follows a symlink to a file
        os.symlink(os.path.join(self.tmp, "real.conf"), self.override_file)
        self.assertIsNone(self.role_check())
        self.assertEqual(roles.role_lanes(self.paths, "developer"), "4")
        self.assertEqual(self.bash('role_lanes developer')[1], "4")
        os.remove(self.override_file)
        os.symlink(self.config, self.override_file)  # to a directory
        self.assertEqual(self.role_check(), "%s: not a regular file" % self.override_file)

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_an_unreadable_override_is_an_error_not_a_traceback(self):
        self.override('LANES="2"\n')
        os.chmod(self.override_file, 0)
        self.addCleanup(os.chmod, self.override_file, 0o600)
        with self.assertRaises(FactoryError) as cm:
            roles.validate_role(self.paths, "developer")
        self.assertEqual(str(cm.exception), "%s: can't be read (Permission denied)" % self.override_file)

    def test_bytes_that_are_not_utf8_are_data(self):
        self.override(b'MODEL="\xff\xfe"\n')
        self.assertIn("MODEL must contain only", self.role_check())
        self.override(b'bad \xff line\n')
        self.assertEqual(self.role_check(), '%s: not a KEY="value" line: bad \udcff line' % self.override_file)


class OverridesFolderTest(Fixture):
    def test_an_override_for_a_role_that_does_not_exist_is_refused(self):
        self.override('LANES="2"\n', role="designer")
        self.assertEqual(self.overrides_check(),
                         "%s/designer.conf: no role named 'designer' in %s/roles (an override is named "
                         "<role>.conf; see: factory roles)" % (self.overrides, self.root))
        os.remove(os.path.join(self.overrides, "designer.conf"))
        self.assertIsNone(self.overrides_check())

    def test_what_counts_as_an_override_file(self):
        self.assertIsNone(self.overrides_check())  # no folder at all
        self.override("x", role=".hidden")  # a dotfile is not matched by the glob
        self.write(os.path.join(self.overrides, "notes.txt"), "x")
        self.write(os.path.join(self.overrides, "README"), "x")
        os.symlink(os.path.join(self.tmp, "nowhere"), os.path.join(self.overrides, "dangling.conf"))
        self.assertIsNone(self.overrides_check())
        os.makedirs(os.path.join(self.overrides, "adir.conf"))  # a folder with the name counts
        self.assertIn("no role named 'adir'", self.overrides_check())

    def test_the_override_folder_being_a_file_is_not_an_error(self):
        os.makedirs(self.config)
        self.write(self.overrides, "x")
        self.assertIsNone(self.overrides_check())

    def test_a_good_override_for_a_real_role_passes_without_being_validated_here(self):
        self.override('BROKER_TOOLS="evil"\n')  # validate_role reports this one, not validate_role_overrides
        self.assertIsNone(self.overrides_check())

    def test_the_first_unknown_name_in_byte_order_is_reported(self):
        for name in ("b-x", "a-y", "Zed"):
            self.override("", role=name)
        self.assertIn("/Zed.conf: no role named 'Zed'", self.overrides_check())
        os.remove(os.path.join(self.overrides, "Zed.conf"))
        self.assertIn("/a-y.conf: no role named 'a-y'", self.overrides_check())

    def test_several_bad_overrides_are_reported_in_byte_order_in_every_locale(self):
        for name in ("b-x", "a-y", "Zed", "c-z"):
            self.override("", role=name)
        for locale in ["C"] + [l for l in locales_installed() if not re.match(r"(c|posix)(\.|$)", l, re.I)][:3]:
            self.assertIn("/Zed.conf: no role named 'Zed'", self.overrides_check(locale), locale)

    def test_enabled_roles_are_validated_first(self):
        self.write(self.enabled, "nobody\n")
        self.override("", role="designer")
        self.assertIn("enabled-roles: no role named 'nobody'", self.overrides_check())


class EnabledRolesTest(Fixture):
    def lists(self):
        return roles.list_roles(self.paths)

    def test_optional_roles_are_off_until_named(self):
        self.assertEqual(self.lists(), ["developer"])
        self.assertEqual(roles.list_all_roles(self.paths), ["developer", "researcher"])
        self.assertEqual(self.bash("list_roles")[1].split(), ["developer"])
        self.assertIsNone(self.role_check("researcher"))
        self.assertIsNone(self.overrides_check())
        self.write(self.enabled, "")
        self.assertEqual(self.lists(), ["developer"])
        self.write(self.enabled, "# on for this laptop\n\n  researcher  \n")
        self.assertEqual(self.lists(), ["developer", "researcher"])
        self.assertEqual(self.bash("list_roles")[1].split(), ["developer", "researcher"])
        self.assertIsNone(self.overrides_check())
        self.assertIsNone(self.enabled_check())

    def test_enabling_grants_nothing(self):
        self.write(self.enabled, "researcher\n")
        self.assertEqual(roles.role_get(self.paths, "researcher", "BROKER_TOOLS"), "npm_fetch")
        self.assertEqual(roles.role_get(self.paths, "researcher", "WORKSPACE"), "ro")

    def test_naming_a_non_optional_role_is_harmless(self):
        self.write(self.enabled, "developer\n")
        self.assertEqual(self.lists(), ["developer"])
        self.assertIsNone(self.overrides_check())

    def test_an_unknown_name_is_refused_naming_the_file(self):
        self.write(self.enabled, "reseacher\n")
        self.assertEqual(self.overrides_check(), "%s: no role named 'reseacher' in %s/roles (see: factory roles)"
                         % (self.enabled, self.root))

    def test_a_line_that_is_not_a_role_name_is_refused_and_never_run(self):
        pwned = os.path.join(self.tmp, "pwned")
        for line in ('researcher=1', 'enable researcher', 'Researcher', '$(touch %s)' % pwned, 'a_b', '-a', '9a',
                     'researcher # on', 'é', 'researçher', ' researcher', 'researcher ', '"researcher"',
                     'research*r'):
            self.write(self.enabled, line + "\n")
            self.assertEqual(self.overrides_check(),
                             "%s: not a role name (one per line, e.g. researcher): %s" % (self.enabled, line), line)
            self.assertEqual(self.enabled_check(), self.overrides_check())
        self.assertFalse(os.path.exists(pwned), "enabled-roles was executed")

    def test_the_reported_line_is_as_written_and_checked_trimmed(self):
        self.write(self.enabled, "  \t Bad \t \n")
        self.assertEqual(self.enabled_check(), "%s: not a role name (one per line, e.g. researcher):   \t Bad \t " % self.enabled)

    def test_whitespace_comments_and_a_missing_final_newline(self):
        for text in ("researcher", "researcher\r\n", "\t researcher\t\n", "#c\r\n\r\nresearcher", "\n\n", "  #  x\n",
                     "developer\nresearcher\n", "researcher\nresearcher\n"):
            self.write(self.enabled, text)
            self.assertIsNone(self.enabled_check(), repr(text))
            self.assertEqual(self.bash("list_roles")[1].split(), roles.list_roles(self.paths), repr(text))

    def test_the_names_are_byte_wise_in_every_locale(self):
        self.write(self.enabled, "researcher\né\n")
        for locale in ["C"] + [l for l in locales_installed() if not re.match(r"(c|posix)(\.|$)", l, re.I)][:3]:
            self.assertIn("not a role name", self.enabled_check(locale), locale)

    def test_file_kinds(self):
        os.makedirs(self.enabled)
        self.assertEqual(self.enabled_check(), "%s: not a regular file" % self.enabled)
        os.rmdir(self.enabled)
        self.assertIsNone(self.enabled_check())
        os.symlink(os.path.join(self.tmp, "nowhere"), self.enabled)
        self.assertIsNone(self.enabled_check())

    def test_a_symlink_to_a_file_is_read_by_both(self):
        self.write(os.path.join(self.tmp, "list"), "researcher\n")
        os.makedirs(self.config)
        os.symlink(os.path.join(self.tmp, "list"), self.enabled)
        self.assertIsNone(self.enabled_check())
        self.assertEqual(roles.list_roles(self.paths), ["developer", "researcher"])
        self.assertEqual(self.bash("list_roles")[1].split(), ["developer", "researcher"])

    def test_optional_must_be_exactly_1_in_the_committed_role_conf(self):
        self.role_conf("researcher", RESEARCHER.replace('OPTIONAL="1"', 'OPTIONAL="2"'))
        self.assertEqual(self.role_check("researcher"),
                         "role researcher: OPTIONAL must be 1 (off unless enabled-roles names it) or empty")


class LaneNamesTest(Fixture):
    def test_lane_names(self):
        self.assertEqual(roles.role_lane_names(self.paths, "developer"), ["developer", "developer-lane2", "developer-lane3"])
        self.assertEqual(self.bash("role_lane_names developer")[1].split(), roles.role_lane_names(self.paths, "developer"))
        self.assertEqual(roles.role_lane_names(self.paths, "researcher"), ["researcher"])
        self.override('LANES="5"\n')
        self.assertEqual(roles.role_lane_names(self.paths, "developer")[-1], "developer-lane5")

    def test_every_value_gives_the_lanes_bash_does(self):
        for value in ("1", "2", "8", "9", "", "x", " 3", "3 ", "\t3", "3\t", "+3", "010", "-1", "0", "3.5", "1 2",
                      "0x3", "٣", "9223372036854775808", "-9223372036854775809", " ", "+", "-", "3x", "\v3", "3\v",
                      "00000000000000000000000000000000004"):
            self.role_conf("developer", DEVELOPER.replace('LANES="3"', 'LANES="%s"' % value))
            status, out, _ = self.bash("role_lane_names developer")
            self.assertEqual((status, out.split()), (0, roles.role_lane_names(self.paths, "developer")), repr(value))

    def test_the_override_decides_the_count(self):
        self.override('LANES="2"\n')
        self.assertEqual(self.bash("role_lane_names developer")[1].split(), ["developer", "developer-lane2"])
        self.assertEqual(roles.role_lane_names(self.paths, "developer"), ["developer", "developer-lane2"])


class CoordinatorToolsTest(Fixture):
    def tools(self):
        python = roles.coordinator_broker_tools(self.paths)
        self.assertEqual(self.bash("coordinator_broker_tools")[1].rstrip("\n"), python)
        return python

    def test_absent_file_or_key(self):
        self.assertEqual(self.tools(), "")
        path = os.path.join(self.root, "kits", "coordinator", "broker.conf")
        self.write(path, '# no tools\nOTHER="x"\n')
        self.assertEqual(self.tools(), "")

    def test_the_value_is_parsed_not_sourced_and_the_last_line_wins(self):
        path = os.path.join(self.root, "kits", "coordinator", "broker.conf")
        for text in ('BROKER_TOOLS="git_fetch jira_get"\n', 'BROKER_TOOLS="a"\nBROKER_TOOLS="b  c"\n',
                     'BROKER_TOOLS="x"   \r\n', 'BROKER_TOOLS=x\n', ' BROKER_TOOLS="x"\n', 'BROKER_TOOLS=""\n',
                     'BROKER_TOOLS="$(touch %s/pwned)"\n' % self.tmp, 'BROKER_TOOLS="a"'):
            self.write(path, text)
            self.tools()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned")))
        self.write(path, 'BROKER_TOOLS="git_fetch jira_get"\n')
        self.assertEqual(self.tools(), "git_fetch jira_get")

    def test_a_folder_in_its_place_counts_as_absent(self):
        os.makedirs(os.path.join(self.root, "kits", "coordinator", "broker.conf"))
        self.assertEqual(self.tools(), "")

    def test_the_real_coordinator_grant(self):
        real = Paths(REPO, "", "")
        cp = subprocess.run(["bash", "-c", ('ROOT="$1"; . "$1/scripts/lib/common.sh"; . "$1/scripts/lib/roles.sh"; '
                                            'coordinator_broker_tools'), "_", REPO],
                            stdout=subprocess.PIPE, check=True)
        self.assertEqual(cp.stdout.decode().rstrip("\n"), roles.coordinator_broker_tools(real))


class BashOnlyHelpersTest(Fixture):
    """disabled_role_sandboxes is only used by `factory doctor` (bin/factory) and has no Python
    counterpart until doctor is ported (docs/py-cli-migration.md, step 3); it is checked here, in
    bash, so the shell test it came from can go."""

    def disabled(self, existing):
        snippet = ('sbx() { [ "$1 $2" = "ls -q" ] || { echo "unexpected sbx $*" >&2; return 1; }; '
                   "printf '%s\\n' " + existing + "; }; disabled_role_sandboxes")
        status, out, err = self.bash(snippet)
        self.assertEqual((status, err), (0, ""))
        return out

    def test_a_disabled_roles_existing_sandbox_is_reported_never_removed(self):
        both = "factory-developer factory-researcher"
        self.assertEqual(self.disabled(both), "factory-researcher")
        self.write(self.enabled, "researcher\n")
        self.assertEqual(self.disabled(both), "")
        os.remove(self.enabled)
        self.assertEqual(self.disabled("factory-developer"), "")


if __name__ == "__main__":
    unittest.main()
