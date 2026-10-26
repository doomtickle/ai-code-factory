"""Tests for factory/build_templates.py (`factory _build-templates`, what scripts/00-build-templates.sh
runs) and factory/pins.py. Ported from scripts/test_factory_update.sh (Parts 3 and 4: the broker image
is built from an export of the committed broker/, never the working tree) and scripts/test_lint.sh (the
ShellCheck pins reach the developer and reviewer builds as --build-arg). Before the bash was replaced,
the script and its Python were compared on fixtures (docs/py-cli-migration.md, step 8 part A): the same
status, stdout, stderr, docker and sbx calls and files.

docker and sbx are stubs that log every call; docker also copies the broker's build context aside.
"""

import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from factory import build_templates, pins
from factory.context import FactoryError
from factory.fixtures import REPO, CommandTest

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

STUB_DOCKER = r'''import json, os, shutil, sys
a = sys.argv[1:]
S = os.environ["STUBS"]
with open(os.path.join(S, "log"), "a") as f:
    f.write(json.dumps(["docker"] + a) + "\n")
if a[:1] == ["build"] and a[2].startswith("factory-broker:"):
    ctx = a[-1]
    cap = os.path.join(S, "capture")
    shutil.rmtree(cap, ignore_errors=True)
    shutil.copytree(ctx, cap)
    with open(os.path.join(S, "broker-build"), "w") as f:
        json.dump({"dockerfile": a[a.index("-f") + 1], "context": ctx}, f)
if a[:1] == ["build"] and os.environ.get("FAKE_FAIL") == a[2].split(":")[0]:
    sys.exit(3)
if a[:2] == ["image", "inspect"]:
    print("sha256:stub")
if a[:2] == ["image", "save"]:
    open(a[a.index("-o") + 1], "w").close()
'''

STUB_SBX = r'''import json, os, sys
a = sys.argv[1:]
with open(os.path.join(os.environ["STUBS"], "log"), "a") as f:
    f.write(json.dumps(["sbx"] + a) + "\n")
if a[:1] == ["version"]:
    print("sbx version: v%s stub" % os.environ["SBX_VERSION"])
'''


def tree_same(a, b):
    """True when directories A and B hold the same files with the same bytes."""
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    return not mismatch and not errors and all(tree_same(os.path.join(a, d), os.path.join(b, d))
                                              for d in cmp.common_dirs)


class BuildTest(CommandTest):
    """A checkout that is a git repo with a committed broker/, the real scripts/lib/common.sh (the
    pins), and the stub docker and sbx."""

    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "main", self.root)
        os.makedirs(os.path.join(self.root, "broker", "passdesk"))
        os.makedirs(os.path.join(self.root, "scripts", "lib"))
        shutil.copy(os.path.join(REPO, "scripts", "lib", "common.sh"), os.path.join(self.root, "scripts", "lib"))
        self.put("broker/tools.txt", "git_fetch\n")
        self.put("broker/Dockerfile", "FROM scratch\n")
        self.put("broker/.gitignore", "__pycache__/\n*.pyc\n")
        self.put("broker/passdesk/app.py", "print('reviewed')\n")
        self.put("templates/developer/Dockerfile", "FROM scratch\n")
        self.git("-C", self.root, "add", "-A")
        self.git("-C", self.root, "commit", "-q", "-m", "broker v1")
        self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(self.stubs)
        self.write("bin/docker", "#!%s\n%s" % (sys.executable, STUB_DOCKER), 0o755)
        self.write("bin/sbx", "#!%s\n%s" % (sys.executable, STUB_SBX), 0o755)
        self.environ.update(STUBS=self.stubs, SBX_VERSION=pins.read(REPO, "SBX_VERSION")["SBX_VERSION"])

    def git(self, *args):
        subprocess.run(["git"] + list(args), check=True, env=dict(os.environ, **GIT_ENV))

    def put(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def build(self, *argv):
        ctx = self.ctx()
        try:
            status = build_templates.main(ctx, list(argv))
        except FactoryError as e:
            return 1, self.out.getvalue(), self.err.getvalue() + "error: %s\n" % e
        return status, self.out.getvalue(), self.err.getvalue()

    def calls(self):
        try:
            with open(os.path.join(self.stubs, "log")) as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    def committed_tree(self):
        dest = os.path.join(self.tmp, "committed-tree")
        os.makedirs(dest, exist_ok=True)
        archive = subprocess.Popen(["git", "-C", self.root, "archive", "HEAD:broker"], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-f", "-", "-C", dest], stdin=archive.stdout, check=True)
        archive.stdout.close()
        archive.wait()
        return dest

    def captured(self):
        return os.path.join(self.stubs, "capture")


class ExportTest(BuildTest):
    def export(self):
        dest = os.path.join(self.tmp, "ctx-%d" % len(os.listdir(self.tmp)))
        os.mkdir(dest)
        build_templates.broker_export(self.ctx(), self.root, dest)
        return dest

    def test_a_clean_checkout_exports_the_committed_tree_exactly(self):
        self.assertTrue(tree_same(self.committed_tree(), self.export()))
        self.assertTrue(os.path.isfile(os.path.join(self.export(), "Dockerfile")))

    def test_planted_files_do_not_reach_the_export(self):
        pycache = os.path.join(self.root, "broker", "passdesk", "__pycache__")
        os.makedirs(pycache)
        with open(os.path.join(pycache, "app.cpython-312.pyc"), "w") as f:
            f.write("planted bytecode")
        self.put("broker/passdesk/app.py", "print('planted')\n")
        self.put("broker/planted.txt", "planted\n")
        self.assertTrue(tree_same(self.committed_tree(), self.export()))

    def test_a_deleted_dockerfile_is_still_exported(self):
        os.remove(os.path.join(self.root, "broker", "Dockerfile"))
        self.assertTrue(os.path.isfile(os.path.join(self.export(), "Dockerfile")))

    def test_a_staged_edit_is_not_exported(self):
        self.put("broker/passdesk/app.py", "print('staged')\n")
        self.git("-C", self.root, "add", "broker/passdesk/app.py")
        with open(os.path.join(self.export(), "passdesk", "app.py")) as f:
            self.assertEqual(f.read(), "print('reviewed')\n")

    def test_a_checkout_without_a_committed_broker_is_refused_and_nothing_is_written(self):
        bare = os.path.join(self.tmp, "nobroker")
        self.git("init", "-q", bare)
        self.git("-C", bare, "commit", "-q", "--allow-empty", "-m", "no broker")
        os.makedirs(os.path.join(bare, "broker"))
        with open(os.path.join(bare, "broker", "tools.txt"), "w") as f:
            f.write("git_fetch\n")
        dest = os.path.join(self.tmp, "ctx-nobroker")
        os.mkdir(dest)
        with self.assertRaises(FactoryError) as caught:
            build_templates.broker_export(self.ctx(), bare, dest)
        self.assertIn("broker/tools.txt is not in the committed tree of %s" % bare, str(caught.exception))
        self.assertEqual(os.listdir(dest), [])

    def test_a_tar_that_fails_is_reported(self):
        dest = os.path.join(self.tmp, "ctx-missing")
        with self.assertRaises(FactoryError) as caught:
            build_templates.broker_export(self.ctx(), self.root, dest)
        self.assertEqual(str(caught.exception), "exporting the committed broker/ from %s failed" % self.root)


class PrepareTest(BuildTest):
    def prepare(self):
        dest = os.path.join(self.tmp, "prepared-%d" % len(os.listdir(self.tmp)))
        os.mkdir(dest)
        ctx = self.ctx()
        build_templates.prepare_broker_context(ctx, self.root, dest)
        return dest

    def test_a_clean_checkout_prints_nothing(self):
        self.assertTrue(tree_same(self.committed_tree(), self.prepare()))
        self.assertEqual((self.out.getvalue(), self.err.getvalue()), ("", ""))

    def test_uncommitted_work_is_noted_on_stderr_and_not_exported(self):
        self.put("broker/passdesk/app.py", "print('edited')\n")
        self.put("broker/planted.txt", "planted\n")
        self.assertTrue(tree_same(self.committed_tree(), self.prepare()))
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.err.getvalue(), "note: building the broker image from the committed broker/ (HEAD); "
                                              "uncommitted changes in %s/broker are not included\n" % self.root)

    def test_a_committed_broker_without_a_dockerfile_is_refused(self):
        self.git("-C", self.root, "rm", "-q", "broker/Dockerfile")
        self.git("-C", self.root, "commit", "-q", "-m", "no dockerfile")
        with self.assertRaises(FactoryError) as caught:
            self.prepare()
        self.assertEqual(str(caught.exception), "the committed broker/ has no Dockerfile")


class MainTest(BuildTest):
    def test_the_broker_is_built_from_exactly_the_committed_tree_outside_the_checkout(self):
        pycache = os.path.join(self.root, "broker", "passdesk", "__pycache__")
        os.makedirs(pycache)
        with open(os.path.join(pycache, "app.cpython-312.pyc"), "w") as f:
            f.write("planted bytecode")
        status, out, err = self.build("--build-only")
        self.assertEqual((status, err), (0, ""))
        self.assertTrue(tree_same(self.committed_tree(), self.captured()))
        with open(os.path.join(self.stubs, "broker-build")) as f:
            build = json.load(f)
        self.assertFalse(build["context"].startswith(self.root + os.sep), build)
        self.assertEqual(build["dockerfile"], build["context"] + "/Dockerfile")
        self.assertNotIn("submodule", out + err)

    def test_uncommitted_work_in_the_broker_still_builds_with_a_note(self):
        self.put("broker/passdesk/app.py", "print('edited')\n")
        self.put("broker/planted.txt", "planted\n")
        status, out, err = self.build("--build-only")
        self.assertEqual(status, 0)
        self.assertIn("uncommitted changes in %s/broker are not included" % self.root, err)
        self.assertTrue(tree_same(self.committed_tree(), self.captured()))

    def test_a_checkout_without_a_committed_broker_builds_nothing(self):
        self.git("-C", self.root, "rm", "-q", "-r", "--cached", "broker")
        self.git("-C", self.root, "commit", "-q", "-m", "drop the broker")
        status, out, err = self.build("--build-only")
        self.assertEqual(status, 1)
        self.assertIn("not in the committed tree", err)
        self.assertEqual(self.calls(), [])

    def test_the_shellcheck_pins_reach_the_developer_and_reviewer_builds(self):
        self.build("--build-only")
        want = pins.read(REPO, *build_templates.SHELLCHECK_PINS)
        builds = [c for c in self.calls() if c[1] == "build"]
        for image in ("developer", "reviewer"):
            argv = [c for c in builds if c[3] == "factory-%s:v3" % image][0]
            args = [argv[i + 1] for i, word in enumerate(argv) if word == "--build-arg"]
            self.assertEqual(args, ["%s=%s" % (name, want[name]) for name in build_templates.SHELLCHECK_PINS], image)
            self.assertEqual(argv[-1], self.root)
            self.assertEqual(argv[5], os.path.join(self.root, "templates", image, "Dockerfile"))
        for value in want.values():
            self.assertTrue(value, "a pin is empty")

    def test_build_only_builds_three_images_and_touches_neither_sbx_nor_the_state_dir(self):
        status, out, err = self.build("--build-only")
        self.assertEqual(status, 0)
        self.assertEqual(out, "Building factory-developer:v3...\nBuilding factory-reviewer:v3...\n"
                              "Building factory-broker:v3...\n")
        self.assertEqual([c[0] for c in self.calls()], ["docker"] * 3)
        self.assertFalse(os.path.exists(self.state))

    def test_the_broker_build_gets_the_policy_and_plugin_contexts(self):
        self.build("--build-only")
        argv = [c for c in self.calls() if c[1] == "build" and c[3] == "factory-broker:v3"][0]
        contexts = [argv[i + 1] for i, w in enumerate(argv) if w == "--build-context"]
        self.assertEqual([c.split("=")[0] for c in contexts], ["factory-config", "plugins"])

    def test_a_local_policy_and_plugins_are_included_and_named(self):
        self.write("config/ai-code-factory/policy.toml", "[web]\n", 0o600)
        self.write("config/ai-code-factory/plugins/zeta/plugin.py", "x = 1\n")
        self.write("config/ai-code-factory/plugins/alpha/plugin.py", "x = 2\n")
        self.write("config/ai-code-factory/plugins/alpha/__pycache__/plugin.pyc", "junk")
        self.write("config/ai-code-factory/plugins/.hidden/x", "x")
        seen = {}

        def capture_contexts(argv, env=None, **kw):
            if argv[:2] == ["docker", "build"] and "factory-broker:v3" in argv:
                for word in argv:
                    if word.startswith(("factory-config=", "plugins=")):
                        name, path = word.split("=", 1)
                        seen[name] = {os.path.relpath(os.path.join(d, f), path) for d, _, fs in os.walk(path)
                                      for f in fs}
                        if name == "factory-config":
                            seen["mode"] = os.stat(os.path.join(path, "policy.toml")).st_mode & 0o777
            return 0

        ctx = self.ctx()
        real = ctx.run
        ctx.run = lambda argv, **kw: (capture_contexts(argv), real(argv, **kw))[1]
        build_templates.main(ctx, ["--build-only"])
        plugins = os.path.join(self.config, "plugins")
        self.assertIn("Including local broker policy from %s\n" % os.path.join(self.config, "policy.toml"),
                      self.out.getvalue())
        self.assertIn("Including broker plugins from %s: alpha zeta \n" % plugins, self.out.getvalue())
        self.assertEqual(seen["factory-config"], {"policy.toml"})
        self.assertEqual(seen["plugins"], {"alpha/plugin.py", "zeta/plugin.py", ".hidden/x"})
        self.assertEqual(seen["mode"], 0o600 & ~self.umask())

    @staticmethod
    def umask():
        mask = os.umask(0)
        os.umask(mask)
        return mask

    def test_a_failing_docker_build_stops_with_its_status(self):
        self.environ["FAKE_FAIL"] = "factory-reviewer"
        status, out, err = self.build("--build-only")
        self.assertEqual(status, 3)
        self.assertEqual(out, "Building factory-developer:v3...\nBuilding factory-reviewer:v3...\n")

    def test_the_full_build_loads_each_image_records_its_id_and_lists_the_templates(self):
        status, out, err = self.build()
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(re.findall(r"^(Building|Loading) factory-(\w+):v3", out, re.M),
                         [("Building", "developer"), ("Loading", "developer"), ("Building", "reviewer"),
                          ("Loading", "reviewer"), ("Building", "broker"), ("Loading", "broker")])
        for image in ("developer", "reviewer", "broker"):
            with open(os.path.join(self.state, "images", "factory-%s-v3.id" % image)) as f:
                self.assertEqual(f.read(), "sha256:stub\n")
        loads = [c for c in self.calls() if c[:3] == ["sbx", "template", "load"]]
        self.assertEqual(len(loads), 3)
        self.assertTrue(all(re.search(r"/(developer|reviewer|broker)\.tar$", c[3]) for c in loads))
        self.assertEqual(self.calls()[-1], ["sbx", "template", "ls"])

    def test_the_temporary_folder_is_removed(self):
        seen = []
        real = build_templates.tempfile.mkdtemp
        build_templates.tempfile.mkdtemp = lambda: seen.append(real()) or seen[-1]
        self.addCleanup(setattr, build_templates.tempfile, "mkdtemp", real)
        self.build("--build-only")
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]))

    def test_usage_and_missing_programs(self):
        status, _, err = self.build("--nope")
        self.assertEqual((status, err), (1, "error: usage: %s/scripts/00-build-templates.sh [--build-only]\n" % self.root))
        status, _, err = self.build("--build-only", "x")
        self.assertEqual(status, 1)
        self.environ["PATH"] = "/nonexistent"
        self.assertEqual(self.build("--build-only")[2], "error: 'docker' not found in PATH (see README.md)\n")
        self.environ["PATH"] = os.path.join(self.tmp, "bin") + os.pathsep + "/usr/bin:/bin"
        os.remove(os.path.join(self.tmp, "bin", "sbx"))
        self.assertEqual(self.build()[2], "error: 'sbx' not found in PATH (see README.md)\n")
        self.assertEqual(self.build("--build-only")[0], 0)

    def test_the_wrong_sbx_version_stops_a_full_build(self):
        self.environ["SBX_VERSION"] = "0.0.1"
        status, _, err = self.build()
        self.assertEqual((status, err), (1, "error: sbx %s required, found '0.0.1' (see README.md)\n"
                                         % pins.read(REPO, "SBX_VERSION")["SBX_VERSION"]))
        self.assertEqual([c for c in self.calls() if c[0] == "docker"], [])


class PinsTest(unittest.TestCase):
    def test_the_pins_are_read_from_common_sh_with_references_expanded(self):
        got = pins.read(REPO, "SBX_VERSION", "SBX_URL", "SBX_SHA256")
        self.assertRegex(got["SBX_VERSION"], r"^\d+\.\d+\.\d+$")
        self.assertIn(got["SBX_VERSION"], got["SBX_URL"])
        self.assertNotIn("${", got["SBX_URL"])
        self.assertRegex(got["SBX_SHA256"], r"^[0-9a-f]{64}$")

    def test_what_bash_sees_is_what_python_reads(self):
        names = ("SBX_VERSION", "SBX_URL", "SBX_SHA256") + build_templates.SHELLCHECK_PINS
        script = '. "$1/scripts/lib/common.sh"; for n in "${@:2}"; do printf "%s\\n" "${!n}"; done'
        cp = subprocess.run(["bash", "-c", script, "_", REPO] + list(names), stdout=subprocess.PIPE, check=True)
        want = pins.read(REPO, *names)
        self.assertEqual(cp.stdout.decode().split("\n")[:-1], [want[n] for n in names])

    def test_the_sbx_pins_match_the_copies_in_preflight_and_sbx(self):
        from factory import preflight, sbx
        self.assertEqual(pins.read(REPO, "SBX_VERSION")["SBX_VERSION"], sbx.SBX_VERSION)
        self.assertEqual(pins.read(REPO, "SBX_VERSION")["SBX_VERSION"], preflight.SBX_VERSION)

    def test_the_shellcheck_pins_are_pinned_by_exact_version_and_digest(self):
        got = pins.read(REPO, *build_templates.SHELLCHECK_PINS)
        self.assertRegex(got["SHELLCHECK_VERSION"], r"^\d+\.\d+\.\d+$")
        for name in build_templates.SHELLCHECK_PINS[1:]:
            self.assertRegex(got[name], r"^[0-9a-f]{64}$", name)

    def test_a_missing_pin_names_it_and_the_file(self):
        with self.assertRaises(FactoryError) as caught:
            pins.read(REPO, "NOT_A_PIN")
        self.assertEqual(str(caught.exception), "NOT_A_PIN is not pinned in %s" %
                         os.path.join(REPO, "scripts", "lib", "common.sh"))

    def test_a_missing_file_is_a_factory_error(self):
        with self.assertRaises(FactoryError):
            pins.read(os.path.join(REPO, "nowhere"), "SBX_VERSION")

    def test_the_dockerfiles_take_each_pin_as_an_arg_with_no_default(self):
        for image in ("developer", "reviewer"):
            with open(os.path.join(REPO, "templates", image, "Dockerfile")) as f:
                lines = f.read().split("\n")
            for name in build_templates.SHELLCHECK_PINS:
                self.assertIn("ARG " + name, lines, image)


if __name__ == "__main__":
    unittest.main()
