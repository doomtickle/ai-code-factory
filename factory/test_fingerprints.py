"""Tests for the Coordinator's and the roles' sandbox fingerprints and stale_sandboxes in
factory/fingerprints.py (the broker's case is also in factory/test_broker_start.py). Each check
runs the bash (sandbox_fingerprint and stale_sandboxes in scripts/lib/roles.sh) and the Python on
the same fixtures and requires the same digest, the same file and the same list of stale
sandboxes: bash `update` compares against what the Python records until it is ported. The bash
record_sandbox_fingerprint is gone; RECORD_SH is what it did, for files it recorded before.
"""

import os
import shutil
import subprocess

from factory import fingerprints
from factory.fixtures import REPO, CommandTest

BROKER, COORDINATOR = "factory-broker", "factory-coordinator"
SANDBOXES = [BROKER, COORDINATOR, "factory-dev", "factory-rev", "factory-opt"]

# The body of the deleted bash record_sandbox_fingerprint, for sandbox "$3".
RECORD_SH = 'mkdir -p "$FACTORY_STATE/sandboxes"; sandbox_fingerprint "$3" > "$(sandbox_fingerprint_file "$3")"'

# Lists the sandboxes in $FAKE_SANDBOXES; FAKE_FAIL makes `ls -q` print what it has and fail.
FAKE_SBX = """#!/bin/sh
[ "$1 $2" = "ls -q" ] || exit 0
cat "$FAKE_SANDBOXES" 2>/dev/null
[ -z "${FAKE_FAIL:-}" ] || exit 1
"""


class FingerprintFixture(CommandTest):
    def setUp(self):
        super().setUp()
        for role, conf in (("dev", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="git_fetch npm_fetch"\n'),
                           ("rev", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nBROKER_TOOLS=""\n'),
                           ("opt", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nOPTIONAL="1"\nBROKER_TOOLS="web_fetch"\n')):
            self.write("repo/roles/%s/role.conf" % role, conf)
            self.write("repo/roles/%s/instructions.md" % role, "Do the work.\n")
            self.write("repo/roles/%s/kit/spec.yaml" % role, "name: %s\n" % role)
        self.write("repo/kits/coordinator/spec.yaml", "name: coordinator\n")
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS="github_create_pr"\n')
        self.write("repo/broker/tools.txt", "web_fetch\ngit_fetch\ngit_push\ngithub_create_pr\nnpm_fetch\n")
        self.write("repo/kits/broker/spec.yaml", "name: broker\n")
        self.write("bin/sbx", FAKE_SBX, 0o755)
        self.environ.update({"LC_ALL": "C", "FAKE_SANDBOXES": os.path.join(self.tmp, "sandboxes")})

    def exist(self, *names):
        self.write("sandboxes", "".join(n + "\n" for n in names))

    def bash(self, snippet, *args, quiet=True, **env):
        cp = subprocess.run(["bash", "-c", 'ROOT="$1"; . "$2/scripts/lib/common.sh"; . "$2/scripts/lib/roles.sh"; '
                             + snippet, "_", self.root, REPO] + list(args), env=dict(self.environ, **env),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp)
        if quiet:
            self.assertEqual(cp.stderr, b"")
        return cp.stdout.decode()

    def check(self, names=SANDBOXES, quiet=True):
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(fingerprints.sandbox_fingerprint(self.paths, name) + "\n",
                                 self.bash('sandbox_fingerprint "$3"', name, quiet=quiet))

    def stale(self, **env):
        """The stale sandboxes, from bash and from Python (which must agree)."""
        self.environ.update(env)
        got = fingerprints.stale_sandboxes(self.ctx())
        self.assertEqual(got, self.bash("stale_sandboxes", **env).split())
        return got

    def record_all(self, names=SANDBOXES):
        for name in names:
            fingerprints.record_sandbox_fingerprint(self.paths, name)


class FingerprintTest(FingerprintFixture):
    def test_the_same_as_bash(self):
        self.check()

    def test_the_layout_is_bash_s(self):
        self.assertEqual(self.bash('printf %s "$RW_LAYOUT"'), fingerprints.RW_LAYOUT)

    def test_only_an_rw_role_records_its_layout(self):
        dev, rev = (fingerprints.sandbox_fingerprint(self.paths, "factory-" + r) for r in ("dev", "rev"))
        self.assertEqual(len({dev, rev}), 2)
        self.write("repo/roles/dev/role.conf", 'TEMPLATE="developer"\nWORKSPACE="ro"\nBROKER_TOOLS="git_fetch npm_fetch"\n')
        ro = fingerprints.sandbox_fingerprint(self.paths, "factory-dev")
        self.assertNotEqual(ro, dev)
        self.check()

    def test_the_coordinator_with_and_without_broker_tools(self):
        with_tools = fingerprints.sandbox_fingerprint(self.paths, COORDINATOR)
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS=""\n')
        self.check()
        empty = fingerprints.sandbox_fingerprint(self.paths, COORDINATOR)
        os.remove(os.path.join(self.root, "kits", "coordinator", "broker.conf"))
        self.check()
        self.assertEqual(fingerprints.sandbox_fingerprint(self.paths, COORDINATOR), empty)
        self.assertNotEqual(with_tools, empty)
        # Which tools it holds is not an input (the broker re-reads grants on every call).
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS="web_fetch git_push"\n')
        self.check()
        self.assertEqual(fingerprints.sandbox_fingerprint(self.paths, COORDINATOR), with_tools)

    def test_a_role_with_overrides(self):
        before = fingerprints.sandbox_fingerprint(self.paths, "factory-dev")
        self.write("config/ai-code-factory/roles/dev.conf", 'MODEL="opus"\nLANES="4"\nMAX_TURNS="9"\n')
        self.check()
        self.assertEqual(fingerprints.sandbox_fingerprint(self.paths, "factory-dev"), before)

    def test_what_changes_the_digest(self):
        for name in SANDBOXES:
            before = fingerprints.sandbox_fingerprint(self.paths, name)
            kit = {BROKER: "kits/broker", COORDINATOR: "kits/coordinator"}.get(name, "roles/%s/kit" % name[len("factory-"):])
            self.write("repo/%s/extra.txt" % kit, name)
            self.check([name])
            self.assertNotEqual(fingerprints.sandbox_fingerprint(self.paths, name), before, name)

    def test_the_image_id(self):
        for text in ("sha256:abc\n", "sha256:abc\n\n", ""):
            self.write("state/ai-code-factory/images/factory-developer-v3.id", text)
            self.write("state/ai-code-factory/images/factory-reviewer-v3.id", "sha256:def\n")
            self.check()
        # The Coordinator uses the developer image, a role the image of its TEMPLATE.
        self.write("state/ai-code-factory/images/factory-developer-v3.id", "sha256:new\n")
        self.check()
        os.remove(os.path.join(self.state, "images", "factory-developer-v3.id"))
        self.check()

    def test_no_kit(self):
        for kit in ("kits/coordinator", "kits/broker", "roles/rev/kit"):
            shutil.rmtree(os.path.join(self.root, kit))
        self.check()

    def test_a_name_that_is_no_role(self):
        # Bash's sed complains about the missing role.conf; the digest is the same.
        self.check(["factory-nope", "nope", "factory-", "factory-factory-dev"], quiet=False)

    def test_the_optional_role_has_a_fingerprint_either_way(self):
        self.write("config/ai-code-factory/enabled-roles", "opt\n")
        self.check()

    def test_a_role_conf_that_is_not_utf8(self):
        with open(os.path.join(self.root, "roles", "rev", "role.conf"), "wb") as f:
            f.write(b'TEMPLATE="reviewer"\nWORKSPACE="r\xffo"\nBROKER_TOOLS=""\n')
        self.check()


class RecordTest(FingerprintFixture):
    def test_the_file_is_the_one_bash_wrote(self):
        for name in SANDBOXES:
            fingerprints.record_sandbox_fingerprint(self.paths, name)
            with open(fingerprints.fingerprint_file(self.paths, name), "rb") as f:
                ours = f.read()
            self.assertEqual(self.bash('printf %s "$(sandbox_fingerprint_file "$3")"', name),
                             fingerprints.fingerprint_file(self.paths, name))
            self.assertEqual(ours.decode(), self.bash('sandbox_fingerprint "$3"', name), name)
            self.assertRegex(ours.decode(), "^[0-9a-f]{64}\n$")
            os.remove(fingerprints.fingerprint_file(self.paths, name))
            self.bash(RECORD_SH, name)
            with open(fingerprints.fingerprint_file(self.paths, name), "rb") as f:
                self.assertEqual(ours, f.read(), name)
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "sandboxes"))), sorted(SANDBOXES))


class StaleSandboxesTest(FingerprintFixture):
    def test_nothing_exists_and_nothing_is_stale(self):
        self.assertEqual(self.stale(), [])

    def test_up_to_date_sandboxes(self):
        self.exist(*SANDBOXES)
        self.record_all()
        self.assertEqual(self.stale(), [])

    def test_recorded_by_the_old_bash_is_up_to_date_for_python(self):
        self.exist(*SANDBOXES)
        for name in SANDBOXES:
            self.bash(RECORD_SH, name)
        self.assertEqual(self.stale(), [])

    def test_a_missing_fingerprint_file_is_stale(self):
        self.exist(*SANDBOXES)
        self.record_all()
        os.remove(fingerprints.fingerprint_file(self.paths, "factory-rev"))
        os.remove(fingerprints.fingerprint_file(self.paths, BROKER))
        self.assertEqual(self.stale(), [BROKER, "factory-rev"])

    def test_a_fingerprint_file_that_is_a_folder_or_wrong_is_stale(self):
        self.exist(*SANDBOXES)
        self.record_all()
        os.remove(fingerprints.fingerprint_file(self.paths, COORDINATOR))
        os.mkdir(fingerprints.fingerprint_file(self.paths, COORDINATOR))
        self.write("state/ai-code-factory/sandboxes/factory-dev", "0123\n")
        self.assertEqual(self.stale(), [COORDINATOR, "factory-dev"])

    def test_trailing_newlines_in_the_file_do_not_matter(self):
        self.exist(*SANDBOXES)
        self.record_all()
        with open(fingerprints.fingerprint_file(self.paths, "factory-dev"), "a") as f:
            f.write("\n\n")
        self.assertEqual(self.stale(), [])

    def test_an_edited_kit_or_image_is_stale(self):
        self.exist(*SANDBOXES)
        self.record_all()
        self.write("repo/roles/dev/kit/new.txt", "x")
        self.write("repo/kits/coordinator/spec.yaml", "name: changed\n")
        self.assertEqual(self.stale(), [COORDINATOR, "factory-dev"])
        self.write("state/ai-code-factory/images/factory-developer-v3.id", "sha256:rebuilt\n")
        self.assertEqual(self.stale(), [COORDINATOR, "factory-dev"])
        self.write("state/ai-code-factory/images/factory-reviewer-v3.id", "sha256:rebuilt\n")
        self.assertEqual(self.stale(), [COORDINATOR, "factory-dev", "factory-rev"])
        self.write("repo/broker/tools.txt", "web_fetch\ngit_fetch\ngit_push\ngithub_create_pr\nnpm_fetch\n")
        self.write("repo/kits/broker/spec.yaml", "name: changed\n")
        self.assertEqual(self.stale(), [BROKER, COORDINATOR, "factory-dev", "factory-rev"])

    def test_losing_or_gaining_broker_access_is_stale(self):
        self.exist(*SANDBOXES)
        self.record_all()
        os.remove(os.path.join(self.root, "kits", "coordinator", "broker.conf"))
        self.write("repo/roles/rev/role.conf", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nBROKER_TOOLS="git_fetch"\n')
        self.assertEqual(self.stale(), [COORDINATOR, "factory-rev"])

    def test_only_existing_sandboxes_count(self):
        self.record_all()
        self.exist(COORDINATOR, "factory-dev-extra", "factory-nope", "other")
        self.write("repo/kits/coordinator/spec.yaml", "name: changed\n")
        self.write("repo/roles/dev/kit/new.txt", "x")
        self.assertEqual(self.stale(), [COORDINATOR])

    def test_the_optional_role_when_enabled_and_disabled(self):
        self.exist(*SANDBOXES)
        self.write("repo/roles/opt/kit/new.txt", "x")
        self.assertEqual(self.stale(), [BROKER, COORDINATOR, "factory-dev", "factory-rev"])
        self.write("config/ai-code-factory/enabled-roles", "# on\nopt\n")
        self.assertEqual(self.stale(), [BROKER, COORDINATOR, "factory-dev", "factory-opt", "factory-rev"])
        self.record_all()
        self.assertEqual(self.stale(), [])
        self.write("repo/roles/opt/kit/newer.txt", "x")
        self.assertEqual(self.stale(), ["factory-opt"])
        os.remove(os.path.join(self.config, "enabled-roles"))
        self.assertEqual(self.stale(), [])

    def test_the_order_is_broker_coordinator_then_roles_by_name(self):
        self.write("repo/roles/aaa/role.conf", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\n')
        self.write("repo/roles/Zed/role.conf", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\n')
        self.exist("factory-dev", "factory-Zed", "factory-aaa", COORDINATOR, BROKER, "factory-rev")
        self.assertEqual(self.stale(), [BROKER, COORDINATOR, "factory-Zed", "factory-aaa", "factory-dev", "factory-rev"])

    def test_a_failing_sbx_ls_keeps_what_it_printed_and_otherwise_reads_as_no_sandboxes(self):
        self.exist(*SANDBOXES)
        self.assertEqual(self.stale(FAKE_FAIL="1"), [BROKER, COORDINATOR, "factory-dev", "factory-rev"])
        self.exist()
        self.assertEqual(self.stale(FAKE_FAIL="1"), [])

    def test_without_sbx_nothing_is_stale(self):
        os.remove(os.path.join(self.tmp, "bin", "sbx"))
        self.assertEqual(self.stale(), [])
