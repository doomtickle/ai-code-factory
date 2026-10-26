"""Tests for factory/sbx.py. Ported from sbx_installed_version, sandbox_exists, sandbox_running,
claude_login_stored and sbx_reference_problem (scripts/lib/common.sh) and setup_done
(bin/factory); the bash copies stay until their callers are ported. Each check runs both on the
same `sbx` stub and requires the same answer. require_sbx (common.sh) is compared on whether it dies and what it says.
"""

import os
import shutil
import subprocess
import sys
import unittest

from factory import sbx
from factory.context import FactoryError
from factory.fixtures import REPO, CommandTest


class SbxTest(CommandTest):
    def setUp(self):
        super().setUp()
        sysdir = os.path.join(self.tmp, "sys")
        os.makedirs(sysdir)
        for name in ("bash", "sed", "grep", "awk", "cat", "head", "tr", "uname", "dirname", "readlink", "env", "id", "mkdir"):
            if shutil.which(name):
                os.symlink(shutil.which(name), os.path.join(sysdir, name))
        self.environ["PATH"] = os.path.join(self.tmp, "bin") + os.pathsep + sysdir
        os.makedirs(os.path.join(self.tmp, "bin"))

    def sbx_stub(self, body):
        self.tool("sbx", body)

    def bash(self, snippet):
        cp = subprocess.run(["bash", "-c", 'ROOT="$1"; . "$1/bin/factory"; ' + snippet, "_", REPO], env=dict(self.environ, LC_ALL="C"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, cwd=self.tmp)
        return cp.returncode, cp.stdout.decode(), cp.stderr.decode()


class ConstantsTest(SbxTest):
    def test_names_match_common_sh(self):
        status, out, err = self.bash('printf "%s\\n%s\\n" "$BROKER_SANDBOX" "$TEMPLATE_TAG"')
        self.assertEqual((status, err, out.split()), (0, "", [sbx.BROKER_SANDBOX, sbx.TEMPLATE_TAG]))


class InstalledVersionTest(SbxTest):
    def test_reads_what_sbx_version_says(self):
        for said in ("sbx version: v0.45.1 (abc)", "sbx version: 0.45.1", "sbx version: v1.2.3", "something else",
                     "", "Client:\nsbx version: v9.9.9\nother\n", "sbx version: v1\nsbx version: v2"):
            self.sbx_stub("cat <<'EOF'\n%s\nEOF\n" % said)
            status, out, _ = self.bash("sbx_installed_version")
            self.assertEqual(sbx.installed_version(self.ctx()), out.rstrip("\n"), said)

    def test_a_missing_or_failing_sbx_reads_as_nothing(self):
        self.assertEqual(sbx.installed_version(self.ctx()), "")
        self.assertEqual(self.bash("sbx_installed_version")[1], "")
        self.sbx_stub("echo boom >&2; exit 1\n")
        self.assertEqual(sbx.installed_version(self.ctx()), "")


class RequireSbxTest(SbxTest):
    def both(self):
        status, _, err = self.bash("require_sbx")
        try:
            sbx.require_sbx(self.ctx())
            problem = None
        except FactoryError as e:
            problem = str(e)
        self.assertEqual(status != 0, problem is not None, err)
        if problem:
            self.assertIn(problem, err)
        return problem

    def test_the_pinned_version_passes(self):
        self.sbx_stub('echo "sbx version: v%s (abc)"\n' % sbx.SBX_VERSION)
        self.assertIsNone(self.both())

    def test_a_missing_sbx_and_a_wrong_version_stop_with_the_bash_messages(self):
        self.assertEqual(self.both(), "'sbx' not found in PATH (see README.md)")
        self.sbx_stub('echo "sbx version: v0.1.0"\n')
        self.assertEqual(self.both(), "sbx %s required, found '0.1.0' (see README.md)" % sbx.SBX_VERSION)

    def test_an_sbx_that_cannot_say_its_version_is_named_unknown(self):
        # bash exits silently here (set -e inside sbx_installed_version's pipeline); the Python says why.
        self.sbx_stub("exit 1\n")
        with self.assertRaises(FactoryError) as caught:
            sbx.require_sbx(self.ctx())
        self.assertEqual(str(caught.exception), "sbx %s required, found 'unknown' (see README.md)" % sbx.SBX_VERSION)


class SandboxStateTest(SbxTest):
    LISTING = ("NAME AGENT STATUS\nfactory-broker x running\nfactory-developer x stopped\n"
               "factory-reviewer x\nfactory-broker-2 x running\n")

    def stub_listing(self):
        self.sbx_stub('if [ "$1 $2" = "ls -q" ]; then printf "factory-broker\\nfactory-developer\\nfactory-broker-2\\n"; '
                      'else cat <<\'EOF\'\n%s\nEOF\nfi\n' % self.LISTING.rstrip("\n"))

    def test_exists_and_running_agree_with_bash(self):
        self.stub_listing()
        ctx = self.ctx()
        for name in ("factory-broker", "factory-developer", "factory-reviewer", "factory-broker-2", "factory-none", "factory", "NAME"):
            self.assertEqual(sbx.sandbox_exists(ctx, name), self.bash('sandbox_exists "%s"' % name)[0] == 0, name)
            self.assertEqual(sbx.sandbox_running(ctx, name), self.bash('sandbox_running "%s"' % name)[0] == 0, name)
        self.assertTrue(sbx.sandbox_running(ctx, "factory-broker"))
        self.assertFalse(sbx.sandbox_running(ctx, "factory-developer"))
        self.assertFalse(sbx.sandbox_running(ctx, "factory-reviewer"))

    def test_without_sbx_nothing_exists(self):
        ctx = self.ctx()
        self.assertFalse(sbx.sandbox_exists(ctx, "factory-broker"))
        self.assertFalse(sbx.sandbox_running(ctx, "factory-broker"))
        self.assertEqual(self.bash('sandbox_exists factory-broker')[0], 1)

    def test_names_lists_what_sbx_prints(self):
        self.stub_listing()
        self.assertEqual(sbx.sandbox_names(self.ctx()), ["factory-broker", "factory-developer", "factory-broker-2", ""])


class CoordinatorAttachedTest(SbxTest):
    """The security rule of scripts/CLAUDE.md: `factory chat` (sbx.runs_sandbox) and the dashboard's
    Terminal 0 (dashboard_term.runs_sandbox) must see the same `sbx run` command lines as naming the
    Coordinator. The fixtures are the forms the bash coordinator_attached (deleted with `cmd_chat`)
    was checked on, plus odd ones; both answers are required on every one."""

    NAME = "factory-coordinator"
    LINES = [
        ("sbx run --name factory-coordinator", True),
        ("/usr/local/bin/sbx run --name factory-coordinator --resume", True),
        ("sbx run --name=factory-coordinator", True),
        ("sbx run factory-coordinator", True),
        ("sbx run claude --name factory-coordinator", True),
        ("sbx -D run --name factory-coordinator", True),
        ("sbx --debug run --name factory-coordinator", True),
        ("/opt/bin/sbx run -e X=1 factory-coordinator", True),
        ("sbx run -- --name factory-coordinator", False),
        ("sbx run claude -- factory-coordinator", False),
        ("sbx ls factory-coordinator", False),
        ("sbx --debug run factory-coordinator-old", False),
        ("sbx run --name factory-coordinator-old", False),
        ("sbx run --name= factory-coordinator-old", False),
        ("grep sbx run --name factory-coordinator", False),
        ("sbx exec factory-coordinator true", False),
        ("/usr/bin/sbx daemon start", False),
        ("sbx/ run factory-coordinator", False),
        ("mysbx run factory-coordinator", False),
        ("sbx run", False),
        ("sbx -", False),
        ("sbx", False),
        ("", False),
    ]

    @classmethod
    def dashboard_term(cls):
        sys.path.insert(0, os.path.join(REPO, "scripts"))
        try:
            import dashboard_term
        finally:
            sys.path.remove(os.path.join(REPO, "scripts"))
        return dashboard_term

    def test_both_rules_agree_on_every_command_line(self):
        dt = self.dashboard_term()
        for line, want in self.LINES:
            self.assertEqual(sbx.runs_sandbox(line.split(), self.NAME), want, line)
            self.assertEqual(dt.runs_sandbox(line.split(), self.NAME), want, line)

    def test_both_rules_agree_on_a_name_of_another_sandbox(self):
        dt = self.dashboard_term()
        for line, _ in self.LINES:
            for name in ("factory-developer", "factory-coordinator-old", "run"):
                self.assertEqual(sbx.runs_sandbox(line.split(), name), dt.runs_sandbox(line.split(), name), (line, name))

    def test_attached_reads_ps_and_ignores_a_ps_that_fails(self):
        for line, want in self.LINES:
            self.tool("ps", "cat <<'EOF'\n/sbin/init\n%s\nEOF\n" % line)
            self.assertEqual(sbx.coordinator_attached(self.ctx(), self.NAME), want, line)
        self.tool("ps", "exit 1\n")
        self.assertFalse(sbx.coordinator_attached(self.ctx(), self.NAME))
        os.remove(os.path.join(self.tmp, "bin", "ps"))
        self.assertFalse(sbx.coordinator_attached(self.ctx(), self.NAME))


class LoginTest(SbxTest):
    def test_the_anthropic_secret_is_found_like_bash(self):
        for listing in ("global anthropic x", "SCOPE SERVICE\nglobal github\n", "anthropic", " anthropic ", "x anthropic", ""):
            self.sbx_stub("cat <<'EOF'\n%s\nEOF\n" % listing)
            self.assertEqual(sbx.claude_login_stored(self.ctx()), self.bash("claude_login_stored")[0] == 0, listing)


class SetupDoneTest(SbxTest):
    def stub(self, forced="true", templates="factory-developer:v3\nfactory-reviewer:v3\nfactory-broker:v3"):
        self.sbx_stub('case "$1 $2" in "settings get") echo "%s";; "template ls") cat <<\'EOF\'\n%s\nEOF\n;; esac\n' % (forced, templates))

    def both(self, **kw):
        self.stub(**kw)
        answer = sbx.setup_done(self.ctx())
        self.assertEqual(answer, self.bash("setup_done")[0] == 0, kw)
        return answer

    def test_all_three_images_of_the_current_tag_and_the_baseline(self):
        self.assertTrue(self.both())
        self.assertTrue(self.both(templates="factory-developer:v3 x\nfactory-reviewer:v3\t1\nfactory-broker:v3\nother"))

    def test_the_baseline_must_be_on(self):
        self.assertFalse(self.both(forced="false"))
        self.assertFalse(self.both(forced=""))

    def test_an_old_tag_or_a_missing_image_is_not_done(self):
        self.assertFalse(self.both(templates="factory-developer:v2\nfactory-reviewer:v2\nfactory-broker:v2"))
        self.assertFalse(self.both(templates="factory-developer:v3\nfactory-reviewer:v3"))
        self.assertFalse(self.both(templates="factory-developer:v3\nfactory-reviewer:v3\nfactory-broker:v30"))
        self.assertFalse(self.both(templates="factory-developer:v3\nfactory-reviewer:v3\nxfactory-broker:v3x"))

    def test_without_sbx_it_is_not_done(self):
        self.assertFalse(sbx.setup_done(self.ctx()))


class ReferenceProblemTest(SbxTest):
    def both(self, text):
        path = os.path.join(self.tmp, "ref.md")
        if text is not None:
            self.write("ref.md", text)
        status, out, _ = self.bash('sbx_reference_problem "%s"' % path)
        problem = sbx.reference_problem(path)
        if problem is None:
            self.assertEqual((status, out), (1, ""), text)
        else:
            self.assertEqual((status, out.rstrip("\n")), (0, problem), text)
        return problem

    def test_current_stamp_is_fine(self):
        self.assertIsNone(self.both("# sbx\n<!-- sbx-reference: version=%s generated=2026-10-14 -->\nbody\n" % sbx.SBX_VERSION))

    def test_every_other_state_is_described_like_bash(self):
        path = os.path.join(self.tmp, "ref.md")
        self.assertEqual(self.both(None), "%s is missing; run scripts/gen-sbx-reference.sh on the host" % path)
        self.assertIn("has not been generated yet", self.both("<!-- sbx-reference: not-generated -->\n"))
        self.assertIn("has no version stamp", self.both("no stamp here\n"))
        self.assertIn("has no version stamp", self.both("<!-- sbx-reference: generated=2026 -->\n"))
        self.assertIn("is for sbx 0.1.0 but the pinned sbx is %s" % sbx.SBX_VERSION,
                      self.both("<!-- sbx-reference: version=0.1.0 generated=2026-10-14 -->\n"))

    def test_the_first_stamp_wins(self):
        self.both("<!-- sbx-reference: version=%s generated=x -->\n<!-- sbx-reference: not-generated -->\n" % sbx.SBX_VERSION)


if __name__ == "__main__":
    unittest.main()
