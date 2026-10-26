"""Tests for factory/commands/config.py (factory config init), and that config.example/ is inert.
Ported from scripts/test_factory_config.sh (its cloud_id checks are in factory/test_policy.py)."""

import filecmp
import io
import os
import stat
import subprocess
import unittest

from factory import cli
from factory.commands import config
from factory.fixtures import REPO, CommandTest

EXAMPLE = {"policy.toml": "# policy\n[git]\nrepos = []\n", "broker-kit/spec.yaml": "# kit\n",
           "roles/developer.conf": "# LANES=3\n", "projects/my-app.md": "# notes\n"}


class ConfigInitTest(CommandTest):
    def setUp(self):
        super().setUp()
        for rel, text in EXAMPLE.items():
            self.write(os.path.join("repo", "config.example", rel), text)

    def init(self, *args):
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(["config"] + list(args), root=self.root, environ=self.environ, stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    def dest(self, rel):
        return os.path.join(self.config, rel)

    def test_init_writes_every_example_file_and_points_at_doctor(self):
        rc, out, err = self.init("init")
        self.assertEqual((rc, err), (0, ""))
        for rel, text in EXAMPLE.items():
            with open(self.dest(rel)) as f:
                self.assertEqual(f.read(), text)
        self.assertEqual(out, "\n==> Config init (%s)\n" % self.config + "".join(
            "  ✓ wrote %s\n" % self.dest(rel) for rel in sorted(EXAMPLE)) +
            "Every value in them is commented out: uncomment and fill in only what you need.\n"
            "Next: factory doctor\n")

    def test_the_files_come_in_byte_order(self):
        self.write("repo/config.example/a-b", "")
        self.write("repo/config.example/a/b", "")
        self.assertEqual(config.example_files(os.path.join(self.root, "config.example")),
                         ["a-b", "a/b", "broker-kit/spec.yaml", "policy.toml", "projects/my-app.md", "roles/developer.conf"])

    def test_it_refuses_to_overwrite_and_writes_nothing(self):
        self.write(os.path.join("config", "ai-code-factory", "policy.toml"), "# mine\n")
        rc, out, err = self.init("init")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "  ✗ exists: %s\n" % self.dest("policy.toml"))
        self.assertEqual(err, "error: nothing written; re-run with --force to overwrite the files above\n")
        with open(self.dest("policy.toml")) as f:
            self.assertEqual(f.read(), "# mine\n")
        self.assertFalse(os.path.exists(self.dest("roles/developer.conf")), "all or nothing")

    def test_force_overwrites_and_writes_the_missing_files(self):
        self.write(os.path.join("config", "ai-code-factory", "policy.toml"), "# mine\n")
        rc, out, err = self.init("init", "--force")
        self.assertEqual((rc, err), (0, ""))
        with open(self.dest("policy.toml")) as f:
            self.assertEqual(f.read(), EXAMPLE["policy.toml"])
        self.assertTrue(os.path.exists(self.dest("roles/developer.conf")))

    def test_a_dangling_symlink_counts_as_existing(self):
        os.makedirs(self.config)
        target = os.path.join(self.tmp, "nowhere", "target.toml")
        os.symlink(target, self.dest("policy.toml"))
        rc, out, err = self.init("init")
        self.assertEqual(rc, 1)
        self.assertIn("exists: %s\n" % self.dest("policy.toml"), out)
        self.assertEqual([f for _, _, fs in os.walk(self.config) for f in fs if f != "policy.toml"], [])
        self.assertEqual(os.readlink(self.dest("policy.toml")), target)
        self.assertFalse(os.path.exists(os.path.dirname(target)))

    def test_force_replaces_a_symlink_instead_of_writing_through_it(self):
        os.makedirs(self.config)
        target = os.path.join(self.tmp, "nowhere", "target.toml")
        os.symlink(target, self.dest("policy.toml"))
        rc, _, _ = self.init("init", "--force")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.islink(self.dest("policy.toml")))
        self.assertFalse(os.path.exists(os.path.dirname(target)))

    def test_a_new_file_gets_the_examples_mode_minus_the_umask(self):
        os.chmod(os.path.join(self.root, "config.example", "policy.toml"), 0o755)
        old = os.umask(0o027)
        try:
            self.init("init")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.dest("policy.toml")).st_mode), 0o750)

    def test_bad_arguments_are_a_usage_error(self):
        for args in ([], ["init", "--bogus"], ["init", "--force", "x"], ["list"], ["--force"]):
            rc, out, err = self.init(*args)
            self.assertEqual((rc, out, err), (1, "", "error: usage: factory config init [--force]\n"), args)
        self.assertFalse(os.path.exists(self.config))

    def test_no_example_folder_is_an_error(self):
        subprocess.check_call(["rm", "-r", os.path.join(self.root, "config.example")])
        self.assertEqual(self.init("init")[1:], ("", "error: no config.example/ in %s\n" % self.root))

    def test_an_empty_example_folder_is_an_error(self):
        subprocess.check_call(["rm", "-r", os.path.join(self.root, "config.example")])
        os.makedirs(os.path.join(self.root, "config.example"))
        self.assertEqual(self.init("init")[1:], ("", "error: config.example/ in %s is empty\n" % self.root))

    def test_an_unwritable_config_folder_is_reported(self):
        self.write("config", "a file where the folder should be")
        rc, out, err = self.init("init")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: cannot write %s" % self.dest("broker-kit/spec.yaml")), err)


class RealExampleTest(CommandTest):
    def test_the_copy_of_the_real_example_is_identical_and_inert(self):
        env = dict(self.environ, PATH=os.environ["PATH"])
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(["config", "init"], root=REPO, environ=env, stdout=out, stderr=err), 0)
        example = os.path.join(REPO, "config.example")
        for folder, _, names in os.walk(example):
            for name in names:
                rel = os.path.relpath(os.path.join(folder, name), example)
                self.assertTrue(filecmp.cmp(os.path.join(example, rel), os.path.join(self.config, rel), shallow=False), rel)
        with open(os.path.join(self.config, "policy.toml")) as f:
            active = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual(active, ["[git]", "repos = []"])
        self.assertEqual(err.getvalue(), "")

    def test_the_example_allows_no_host(self):
        example = os.path.join(REPO, "config.example")
        with open(os.path.join(example, "broker-kit", "spec.yaml")) as f:
            active = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual([l for l in active if "example" in l or "permissions" in l or "allow" in l], [])
        uncommented = []
        for folder, _, names in os.walk(example):
            for name in names:
                with open(os.path.join(folder, name), errors="replace") as f:
                    uncommented += [l for l in f if not l.lstrip().startswith("#") and "example." in l]
        self.assertEqual(uncommented, [])

    def test_help_lists_config_init_and_docs_print(self):
        out = subprocess.run([os.path.join(REPO, "bin", "factory"), "help"], stdout=subprocess.PIPE,
                             env=dict(self.environ, PATH=os.environ["PATH"]), stdin=subprocess.DEVNULL,
                             check=True).stdout.decode()
        self.assertIn("factory config init", out)
        self.assertIn("factory docs [--print]", out)


if __name__ == "__main__":
    unittest.main()
