"""Tests for factory/commands/role.py (factory role new). Ported from the new-role.sh part
of the old shell test for role overrides, plus the rest of what the script did."""

import io
import os
import shutil
import unittest
from unittest import mock

from factory.cli import COMMANDS, main
from factory.commands import role
from factory.conf import read_conf
from factory.fixtures import REPO, CommandTest
from factory.roles import valid_role_name


class RoleNewTest(CommandTest):
    def setUp(self):
        super().setUp()
        # The real roles and the real tool list.
        for sub in ("reviewer", "researcher", "developer"):
            shutil.copytree(os.path.join(REPO, "roles", sub), os.path.join(self.root, "roles", sub))
        os.makedirs(os.path.join(self.root, "broker"))
        shutil.copy(os.path.join(REPO, "broker", "tools.txt"), os.path.join(self.root, "broker", "tools.txt"))
        self.sbx_log = os.path.join(self.tmp, "sbx-calls")
        self.tool("sbx", 'echo "$*" >> "%s"\n' % self.sbx_log)

    def new(self, *args):
        rc = COMMANDS["role"](self.ctx(), ["new"] + list(args))
        return rc, self.out.getvalue(), self.err.getvalue()

    def path(self, *parts):
        return os.path.join(self.root, "roles", *parts)

    def read(self, *parts):
        with open(self.path(*parts)) as f:
            return f.read()

    def test_it_copies_reviewer_by_default_and_says_what_to_do_next(self):
        rc, out, err = self.new("designer")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "Created roles/designer (copied from reviewer). Next:\n"
                         "  1. Edit roles/designer/role.conf: DESCRIPTION, WORKSPACE (rw/ro), BROKER_TOOLS, LANES, "
                         "MAX_TURNS, MODEL and MODELS (optional).\n"
                         "  2. Edit its instructions in roles/designer/instructions.md.\n"
                         "  3. Run 'factory start' again.\n")
        self.assertEqual(self.read("designer", "instructions.md"), self.read("reviewer", "instructions.md"))
        conf = read_conf(self.path("designer", "role.conf"))
        self.assertEqual(conf["DESCRIPTION"], "TODO: one line describing what the designer does")
        self.assertEqual(conf["WORKSPACE"], "ro")

    def test_a_checkout_without_the_tool_list_creates_nothing(self):
        os.remove(os.path.join(self.root, "broker", "tools.txt"))
        before = sorted(os.listdir(self.path()))
        with self.assertRaises(role.FactoryError) as cm:
            self.new("designer")
        self.assertIn("broker/tools.txt is missing", str(cm.exception))
        self.assertEqual(sorted(os.listdir(self.path())), before)
        os.makedirs(os.path.join(self.root, "broker"), exist_ok=True)
        shutil.copy(os.path.join(REPO, "broker", "tools.txt"), os.path.join(self.root, "broker", "tools.txt"))
        self.assertEqual(self.new("designer")[0], 0)

    def test_the_kit_is_renamed(self):
        self.new("designer")
        spec = self.read("designer", "kit", "spec.yaml")
        self.assertIn("\nname: factory-designer-kit\n", spec)
        self.assertIn("\ndisplayName: Factory designer\n", spec)
        self.assertIn("name: factory-reviewer-kit", self.read("reviewer", "kit", "spec.yaml"))

    def test_the_new_kit_is_checked_with_sbx(self):
        self.new("designer")
        with open(self.sbx_log) as f:
            self.assertEqual(f.read(), "kit validate %s\n" % self.path("designer", "kit"))

    def test_it_copies_the_named_role(self):
        rc, out, _ = self.new("coder", "developer")
        self.assertEqual(rc, 0)
        self.assertIn("(copied from developer)", out)
        self.assertEqual(read_conf(self.path("coder", "role.conf"))["WORKSPACE"], "rw")

    def test_a_copy_of_an_optional_role_is_an_ordinary_one(self):
        rc, _, err = self.new("designer", "researcher")
        self.assertEqual((rc, err), (0, ""))
        conf = self.read("designer", "role.conf")
        self.assertNotIn("OPTIONAL", conf)
        self.assertNotIn("Off by default", conf)
        self.assertEqual(read_conf(self.path("designer", "role.conf"))["WORKSPACE"], "ro")
        self.assertEqual(read_conf(self.path("researcher", "role.conf"))["OPTIONAL"], "1")

    def test_the_original_is_untouched(self):
        before = self.read("reviewer", "role.conf")
        self.new("designer")
        self.assertEqual(self.read("reviewer", "role.conf"), before)

    def test_the_wrong_number_of_arguments_is_a_usage_error(self):
        for args in ([], ["a", "b", "c"]):
            with self.assertRaises(role.FactoryError) as cm:
                COMMANDS["role"](self.ctx(), ["new"] + args)
            self.assertEqual(str(cm.exception), "usage: factory role new <name> [like]")

    def test_a_missing_or_unknown_subcommand_is_a_usage_error(self):
        for args in ([], ["frob"], ["--help"]):
            with self.assertRaises(role.FactoryError) as cm:
                COMMANDS["role"](self.ctx(), args)
            self.assertEqual(str(cm.exception), "usage: factory role new <name> [like]")

    def test_invalid_names_are_refused_and_nothing_is_created(self):
        for name in ("Designer", "d", "9lives", "has_underscore", "broker", "coordinator", "dispatch", "x-lane2",
                     "a" * 32, "../evil"):
            with self.assertRaises(role.FactoryError) as cm:
                self.new(name)
            self.assertEqual(str(cm.exception), "invalid role name '%s' (lowercase letters, digits, dashes; "
                             "not ending in -lane<N>; not: broker coordinator dispatch)" % name)
        self.assertEqual(sorted(os.listdir(self.path())), ["developer", "researcher", "reviewer"])

    def test_an_existing_role_is_not_overwritten(self):
        with self.assertRaises(role.FactoryError) as cm:
            self.new("developer")
        self.assertEqual(str(cm.exception), "roles/developer already exists")

    def test_an_unknown_role_to_copy_is_refused(self):
        with self.assertRaises(role.FactoryError) as cm:
            self.new("designer", "nobody")
        self.assertEqual(str(cm.exception), "no role named 'nobody' to copy")
        self.assertFalse(os.path.exists(self.path("designer")))

    def test_a_source_role_that_does_not_validate_fails_with_validate_roles_message(self):
        os.remove(self.path("reviewer", "instructions.md"))
        err = io.StringIO()
        rc = main(["role", "new", "designer"], root=self.root, environ=self.environ, stdout=io.StringIO(), stderr=err)
        self.assertEqual((rc, err.getvalue()), (1, "error: role designer: missing or empty roles/designer/instructions.md\n"))
        self.assertFalse(os.path.exists(self.sbx_log), "sbx is not asked about a role that failed validation")

    def test_a_failing_sbx_kit_validate_is_the_exit_status(self):
        self.tool("sbx", "echo 'bad kit' >&2\nexit 3\n")
        rc, out, err = self.new("designer")
        self.assertEqual((rc, out, err), (3, "", "bad kit\n"))

    def test_sbx_missing_is_an_error(self):
        with mock.patch.object(role.shutil, "which", return_value=None), self.assertRaises(role.FactoryError) as cm:
            self.new("designer")
        self.assertIn("sbx not found", str(cm.exception))


class ScaffoldEditsTest(CommandTest):
    def test_spec_names_are_rewritten_on_every_matching_line_only(self):
        spec = b"schemaVersion: \"2\"\nname: factory-reviewer-kit\ndisplayName: Factory Reviewer\n  name: keep\nname:no-space\n"
        self.assertEqual(role.rewrite_spec(spec, "designer"),
                         b"schemaVersion: \"2\"\nname: factory-designer-kit\ndisplayName: Factory designer\n  name: keep\nname:no-space\n")

    def test_role_conf_description_is_replaced(self):
        self.assertEqual(role.rewrite_role_conf(b'A=1\nDESCRIPTION="x \\ & y"\nB=2', "qa"),
                         b'A=1\nDESCRIPTION="TODO: one line describing what the qa does"\nB=2')

    def test_the_optional_comment_through_optional_is_dropped(self):
        conf = b'# head\n\n# Off by default (why).\n# more\nOPTIONAL="1"\nWORKSPACE="ro"\n'
        self.assertEqual(role.rewrite_role_conf(conf, "qa"), b'# head\n\nWORKSPACE="ro"\n')

    def test_an_unterminated_off_by_default_comment_runs_to_the_end_like_sed(self):
        self.assertEqual(role.rewrite_role_conf(b'A=1\n# Off by default\nB=2\nC=3\n', "qa"), b"A=1\n")

    def test_a_role_without_the_comment_is_unchanged_but_for_the_description(self):
        self.assertEqual(role.rewrite_role_conf(b'WORKSPACE="rw"\nOPTIONAL="1"\n', "qa"), b'WORKSPACE="rw"\nOPTIONAL="1"\n')


class ValidRoleNameTest(unittest.TestCase):
    def test_names(self):
        good = ["ab", "reviewer", "my-role", "a1", "r2-d2", "a" + "b" * 30]
        bad = ["", "a", "A", "1a", "a_b", "a b", "a\n", "a" * 32, "broker", "coordinator", "dispatch", "x-lane2",
               "x-lane12", "-ab", "ab\nc"]
        for name in good:
            self.assertTrue(valid_role_name(name), name)
        for name in bad:
            self.assertFalse(valid_role_name(name), name)
        self.assertTrue(valid_role_name("lane2"))
        self.assertTrue(valid_role_name("x-lane"))


if __name__ == "__main__":
    unittest.main()
