"""Tests for factory/commands/doctor.py (factory doctor). Ported from parts 1, 2, 4, 7 and 8 of
scripts/test_factory_doctor.sh, plus the output contract: the exact text, the styles on a
terminal, the problem count and the exit status.

The machine under test is made up: the Context's uname and host files (os-release,
/proc/version, /dev/kvm) are fixtures, and docker, sbx and the rest are stubs on a private PATH,
so nothing here needs the real programs, a network or a keychain.
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from factory import context, preflight, sbx
from factory.commands import doctor
from factory.fixtures import REPO, CommandTest

OS_RELEASE = 'ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04 LTS"\n'
STAMP = "# sbx CLI\n<!-- sbx-reference: version=%s generated=2026-10-14 -->\n" % sbx.SBX_VERSION
KIT = "network:\n  allowedDomains:\n    - github.com\n    - \"api.github.com\"  # comment\n    -   registry.npmjs.org\n"
RESEARCHER = 'OPTIONAL="1"\nTEMPLATE="developer"\nWORKSPACE="ro"\n'
HEAD = "\n==> %s\n"


def policy_table(*rows):
    """What `sbx policy ls --wide` prints: left-aligned columns, a continuation line for a
    rule's extra resources (rows: (source, applies_to, rule_id, decision, resources))."""
    head = ["SOURCE", "CREATED VIA", "APPLIES TO", "POLICY", "POLICY_ID", "RULE", "RULE_ID", "TYPE", "STATUS", "DECISION",
            "PROTOCOLS", "RESOURCES"]
    table = [head] + [[src, "added", to, "-", "local-policy", "-", rid, "network", "active", dec, "tcp", res]
                      for src, to, rid, dec, res in rows]
    widths = [max(len(r[i]) for r in table) + 2 for i in range(len(head))]
    return "".join("".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() + "\n" for r in table)


CLEAN_POLICY = policy_table(("local", "all", "a1413895-0000", "allow", "api.anthropic.com"))


class DoctorTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        self.sysdir = os.path.join(self.tmp, "sys")
        os.makedirs(self.bin)
        os.makedirs(self.sysdir)
        self.environ["PATH"] = self.bin + os.pathsep + self.sysdir
        for name in ("curl", "jq", "openssl", "git", "column"):
            self.write(os.path.join("sys", name), "#!/bin/sh\nexit 0\n", 0o755)
        os.symlink(sys.executable, os.path.join(self.sysdir, "python3"))
        for name in ("sleep", "cat"):
            os.symlink(shutil.which(name), os.path.join(self.sysdir, name))
        self.write("repo/broker/tools.txt", "git_fetch\n")
        self.write("repo/docs/reference/sbx-cli.md", STAMP)
        self.host = {"os_release": self.write("host/os-release", OS_RELEASE), "proc_version": self.write("host/proc-version", "Linux version 6.8\n"),
                     "kvm": self.write("host/kvm", "")}
        self.uname = ("Linux", "x86_64")
        self.environ["FACTORY_KEYCHAIN"] = "none"

    def run_doctor(self, tty=False):
        ctx = self.ctx(tty)
        ctx.uname = self.uname
        ctx.host_files = dict(self.host)
        rc = doctor.run(ctx, [])
        return rc, self.out.getvalue(), self.err.getvalue()

    def docker(self, info="exit 0", buildx="exit 0"):
        self.tool("docker", 'case "$1" in info) %s;; version) echo 27.0.1;; buildx) %s;; esac\n' % (info, buildx))

    def sbx_stub(self, running=True, hosts="github.com api.github.com *.npmjs.org", gateway='echo "==> Policy"; echo "  fine"; echo "No problems found."',
                 setup="true", templates="factory-developer:v3 factory-reviewer:v3 factory-broker:v3", login="global anthropic x",
                 sandboxes="factory-broker", policy=None):
        table = self.write("policy-ls.txt", CLEAN_POLICY if policy is None else policy)
        if not os.path.exists(os.path.join(self.root, "kits", "broker", "spec.yaml")):
            self.write("repo/kits/broker/spec.yaml", KIT + "    - \"*.npmjs.org\"\n")
        self.tool("sbx", '''case "$1 $2" in
"version ") echo "sbx version: v%s";;
"ls -q") for n in %s; do echo $n; done;;
"ls ") echo "NAME AGENT STATUS"; echo "factory-broker x %s";;
"settings get") echo %s;;
"template ls") for t in %s; do echo $t; done;;
"secret ls") echo "%s";;
"policy ls") if [ "$3 $4" = "--wide --source" ]; then cat "%s"; fi; test -s "%s";;
"exec factory-broker") case "$*" in *--egress-hosts*) echo "%s";; *) %s;; esac;;
esac
''' % (sbx.SBX_VERSION, sandboxes, "running" if running else "stopped", setup, templates, login,
           table, table, hosts, gateway))

    def ready_machine(self):
        self.docker()
        self.sbx_stub()


class OutputTest(DoctorTest):
    def test_a_machine_with_everything_but_sbx(self):
        self.docker()
        rc, out, err = self.run_doctor()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, (
            HEAD % "Platform" + "  ✓ Ubuntu 24.04 LTS\n  ✓ KVM available (/dev/kvm)\n"
            + HEAD % "Tools" + "".join("  ✓ %s\n" % t for t in ("curl", "jq", "openssl", "git", "column"))
            + "  ✓ python3 (%s/python3)\n" % self.sysdir
            + HEAD % "Gateway source (broker/)" + "  ✓ broker/ is present\n"
            + HEAD % "Docker (builds the sandbox images)" + "  ✓ docker 27.0.1\n  ✓ docker buildx\n"
            + HEAD % "Factory tools" + "  ! sbx 0.45.1 not installed yet ('factory setup' installs it)\n"
            + HEAD % "Passdesk (the gateway)"
            + "  ! checked once sbx is installed and the broker sandbox exists ('factory setup', then 'factory start')\n"
            + HEAD % "sbx CLI reference (docs/reference/sbx-cli.md)" + "  ✓ matches sbx 0.45.1\n"
            + "\nNo problems found.\n"))

    def test_a_fully_ready_machine(self):
        self.ready_machine()
        rc, out, err = self.run_doctor()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(HEAD % "Factory tools" + "  ✓ sbx 0.45.1\n  ✓ setup done (security baseline applied, images loaded)\n"
                      "  ✓ Claude login stored\n", out)
        self.assertIn(HEAD % "Passdesk (the gateway)" + "    ==> Policy\n    fine\n  ✓ gateway checks passed\n"
                      "  ✓ every host the policy can reach is in the broker kit\n", out)
        self.assertTrue(out.endswith("\nNo problems found.\n"))

    def test_on_a_terminal_the_styles_are_used(self):
        self.docker()
        rc, out, _ = self.run_doctor(tty=True)
        self.assertIn("\n\033[1m==> Platform\033[0m\n  \033[32m✓\033[0m Ubuntu 24.04 LTS\n", out)
        self.assertIn("  \033[33m!\033[0m sbx 0.45.1 not installed yet", out)
        self.assertTrue(out.endswith("\n\033[32mNo problems found.\033[0m\n"))

    def test_problems_are_counted_and_make_the_exit_status_1(self):
        os.remove(os.path.join(self.root, "broker", "tools.txt"))
        rc, out, _ = self.run_doctor(tty=True)
        self.assertEqual(rc, 1)
        self.assertIn("  \033[31m✗\033[0m docker not found\n", out)
        self.assertTrue(out.endswith("\n\033[31m2 problem(s) found.\033[0m Fix them, then run \033[1mfactory doctor\033[0m again.\n"), out)

    def test_wsl_adds_its_advice_and_the_platform_label(self):
        self.write("host/proc-version", "Linux version 5.15 microsoft-standard-WSL2\n")
        self.docker()
        rc, out, _ = self.run_doctor()
        self.assertIn("  ✓ Ubuntu 24.04 LTS (WSL2)\n", out)
        self.assertIn("  Keep projects in the Linux filesystem (e.g. ~/code), not under /mnt/c: sandboxes mount\n"
                      "  the project folder, and Windows drives are slow and lose file permissions there.\n", out)

    def test_wsl_styles_the_advice_dim(self):
        self.write("host/proc-version", "WSL")
        self.docker()
        self.assertIn("  \033[2mKeep projects in the Linux filesystem", self.run_doctor(tty=True)[1])


class PlatformTest(DoctorTest):
    def test_unsupported_platform(self):
        self.uname = ("Linux", "aarch64")
        rc, out, _ = self.run_doctor()
        self.assertIn("  ✗ unsupported platform Linux/aarch64: macOS on Apple silicon or Ubuntu 24.04+ x86_64 (including WSL2) only\n", out)

    def test_old_ubuntu_gets_its_fix_text_indented(self):
        self.write("host/os-release", 'ID=ubuntu\nVERSION_ID="22.04"\n')
        self.write("host/proc-version", "microsoft")
        rc, out, _ = self.run_doctor()
        self.assertIn("  ✗ sbx on Linux needs Ubuntu 24.04 or later; this is 'ubuntu' 22.04.\n"
                      "      Install it from PowerShell: wsl --install -d Ubuntu-24.04 (see docs/setup-wsl.md)\n", out)

    def test_missing_kvm_is_a_problem_with_a_multi_line_fix(self):
        self.host["kvm"] = os.path.join(self.tmp, "host", "no-kvm")
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("  ✗ %s not found: sbx needs KVM to run its microVMs.\n"
                      "      Turn on virtualization (Intel VT-x / AMD-V) in your BIOS/UEFI settings and reboot.\n" % self.host["kvm"], out)

    def test_macos_14_on_apple_silicon(self):
        self.uname = ("Darwin", "arm64")
        self.tool("sw_vers", 'echo 14.5\n')
        self.tool("python3", "exit 0\n")
        rc, out, _ = self.run_doctor()
        self.assertIn(HEAD % "Platform" + "  ✓ macOS 14.5, Apple silicon\n", out)
        self.assertIn("  ✗ docker not found\n      Install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/\n", out)

    def test_macos_before_14_is_a_problem(self):
        self.uname = ("Darwin", "arm64")
        self.tool("sw_vers", 'echo 13.6\n')
        self.assertIn("  ✗ macOS 14 (Sonoma) or later required\n", self.run_doctor()[1])


class ToolsTest(DoctorTest):
    def test_missing_tools_are_listed_with_the_packages_to_install(self):
        for name in ("curl", "column"):
            os.remove(os.path.join(self.sysdir, name))
        rc, out, _ = self.run_doctor()
        self.assertIn("  ✗ curl not found\n", out)
        self.assertIn("  ✗ column not found\n  ✓ python3 (%s/python3)\n"
                      "      Install them: sudo apt-get update && sudo apt-get install -y curl bsdextrautils\n" % self.sysdir, out)

    def test_homebrew_advice_on_macos_skips_the_linux_only_package(self):
        self.uname = ("Darwin", "arm64")
        self.tool("sw_vers", "echo 15.0\n")
        os.remove(os.path.join(self.sysdir, "column"))
        os.remove(os.path.join(self.sysdir, "jq"))
        out = self.run_doctor()[1]
        self.assertIn("      Install them with Homebrew: brew install jq \n", out)  # the trailing space is bash's too

    def test_a_missing_python_is_reported_and_installable_on_linux(self):
        os.remove(os.path.join(self.sysdir, "python3"))
        out = self.run_doctor()[1]
        self.assertIn("  ✗ python3 not found in PATH (install python3 3.9+ (e.g. `sudo apt install python3`))\n"
                      "      Install them: sudo apt-get update && sudo apt-get install -y python3\n", out)


class DockerTest(DoctorTest):
    def test_no_docker_on_linux(self):
        out = self.run_doctor()[1]
        self.assertIn("  ✗ docker not found\n      Install Docker Engine in this Linux system (docs/setup-wsl.md, step 3):\n"
                      "        curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER\n"
                      "      then restart the shell (under WSL: 'wsl --shutdown' in PowerShell, reopen the terminal).\n", out)

    def test_permission_denied(self):
        self.docker(info='echo "permission denied while trying" >&2; exit 1')
        out = self.run_doctor()[1]
        self.assertIn("  ✗ docker is installed but %s can't use it\n      sudo usermod -aG docker $USER\n"
                      "      then restart the shell.\n" % preflight.user_name(), out.replace("      usermod", "      sudo usermod").replace("        sudo", "      sudo"))

    def test_permission_denied_under_wsl_says_how_to_restart(self):
        self.write("host/proc-version", "microsoft")
        self.docker(info='echo "permission denied" >&2; exit 1')
        self.assertIn("      then restart the shell (under WSL: 'wsl --shutdown' in PowerShell).\n", self.run_doctor()[1])

    def test_daemon_not_running(self):
        self.docker(info='echo "Cannot connect" >&2; exit 1')
        self.assertIn("  ✗ the Docker daemon isn't running\n        sudo systemctl enable --now docker\n", self.run_doctor()[1])

    def test_daemon_not_running_on_wsl_without_systemd(self):
        self.write("host/proc-version", "microsoft")
        self.docker(info="exit 1")
        self.tool("ps", "echo init\n")
        self.assertIn("  ✗ the Docker daemon isn't running, and WSL started without systemd\n"
                      "      Add to /etc/wsl.conf (sudo nano /etc/wsl.conf):\n        [boot]\n        systemd=true\n"
                      "      then run 'wsl --shutdown' in PowerShell and reopen the terminal.\n", self.run_doctor()[1])

    def test_daemon_not_running_on_wsl_with_systemd_is_the_linux_advice(self):
        self.write("host/proc-version", "microsoft")
        self.docker(info="exit 1")
        self.tool("ps", "echo systemd\n")
        self.assertIn("the Docker daemon isn't running\n        sudo systemctl", self.run_doctor()[1])

    def test_daemon_not_running_on_macos(self):
        self.uname = ("Darwin", "arm64")
        self.tool("sw_vers", "echo 15.0\n")
        self.docker(info="exit 1")
        self.assertIn("  ✗ the Docker daemon isn't running\n      Start Docker Desktop.\n", self.run_doctor()[1])

    def test_missing_buildx(self):
        self.docker(buildx="exit 1")
        out = self.run_doctor()[1]
        self.assertIn("  ✓ docker 27.0.1\n  ✗ docker buildx not found (the broker image build needs it)\n"
                      "        sudo apt-get install -y docker-buildx-plugin\n", out)


class FactoryToolsTest(DoctorTest):
    def test_unfinished_setup_and_no_login_are_warnings(self):
        self.docker()
        self.sbx_stub(setup="false", login="global github x")
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("  ! setup not finished: run 'factory setup'\n  ! no Claude login yet: 'factory start' walks you through it\n", out)

    def test_an_sbx_of_another_version_counts_as_not_installed(self):
        self.docker()
        self.tool("sbx", 'echo "sbx version: v0.1.0"\n')
        self.assertIn("  ! sbx 0.45.1 not installed yet", self.run_doctor()[1])


class GlobalPolicyTest(DoctorTest):
    FIX = "sbx policy rm network --id %s"

    def policy_lines(self, **kw):
        self.docker()
        self.sbx_stub(**kw)
        rc, out, err = self.run_doctor()
        self.assertEqual((rc, err), (0, ""))
        return out

    def test_only_the_anthropic_allow_is_clean(self):
        self.assertIn("  \u2713 Claude login stored\n  \u2713 global sbx network policy allows only api.anthropic.com\n",
                      self.policy_lines())

    def test_the_anthropic_allow_with_its_port_is_clean(self):
        out = self.policy_lines(policy=policy_table(("local", "all", "id-1", "allow", "api.anthropic.com:443")))
        self.assertIn("allows only api.anthropic.com\n", out)

    def test_extra_hosts_are_warned_about_with_their_rule_ids_and_the_fix(self):
        table = policy_table(("local", "all", "a1413895-0000", "allow", "api.anthropic.com"),
                             ("local", "all", "72d6f7e9-1111", "allow", "*.npmjs.org"),
                             ("local", "all", "fd5d5a7b-2222", "allow", "github.com"))
        out = self.policy_lines(policy=table)
        for host, rule in (("*.npmjs.org", "72d6f7e9-1111"), ("github.com", "fd5d5a7b-2222")):
            self.assertIn("  ! the global sbx policy allows %s (rule %s): every sandbox reaches it without the broker; "
                          "remove it with: %s\n" % (host, rule, self.FIX % rule), out)
        self.assertIn("  A host the broker needs belongs in kits/broker/spec.yaml, not in the global policy.\n", out)
        self.assertNotIn("allows only", out)
        self.assertNotIn("a1413895", out)
        self.assertTrue(out.endswith("\nNo problems found.\n"))

    def test_other_scopes_sandbox_rules_and_denies_are_not_flagged(self):
        table = policy_table(("kit", "sandbox:factory-reviewer", "kit-1", "allow", "registry.npmjs.org"),
                             ("local", "sandbox:other", "loc-2", "allow", "example.com"),
                             ("local", "all", "deny-3", "deny", "evil.example"),
                             ("local", "all", "a1413895-0000", "allow", "api.anthropic.com"))
        self.assertIn("allows only api.anthropic.com\n", self.policy_lines(policy=table))

    def test_a_rule_with_several_resources_names_each(self):
        table = policy_table(("local", "all", "multi-1", "allow", "a.example,b.example"))
        out = self.policy_lines(policy=table)
        self.assertIn("allows a.example (rule multi-1)", out)
        self.assertIn("allows b.example (rule multi-1)", out)

    def test_a_continuation_line_adds_resources_to_the_rule_above(self):
        table = policy_table(("local", "all", "multi-1", "allow", "a.example"))
        table += " " * table.index("RESOURCES") + "b.example\n"
        out = self.policy_lines(policy=table)
        self.assertIn("allows a.example (rule multi-1)", out)
        self.assertIn("allows b.example (rule multi-1)", out)

    def test_filesystem_rules_are_not_in_the_table_read(self):
        table = policy_table(("local", "all", "a1413895-0000", "allow", "api.anthropic.com"))
        table += "local  default  all  -  local-policy  -  default-fs-read-allow-all  filesystem:read  allow  -  **\n"
        self.assertIn("allows only api.anthropic.com\n", self.policy_lines(policy=table))

    def test_a_failing_sbx_skips_the_check_without_a_problem(self):
        out = self.policy_lines(policy="")
        self.assertIn("  ! could not read the global sbx network policy; extra-host check skipped\n", out)
        self.assertTrue(out.endswith("\nNo problems found.\n"))

    def test_a_table_that_cannot_be_read_skips_the_check(self):
        out = self.policy_lines(policy="something else entirely\n")
        self.assertIn("extra-host check skipped\n", out)

    def test_it_asks_sbx_with_a_read_only_argv_list(self):
        asked = []
        ctx = mock.Mock()
        ctx.capture = lambda argv, **kw: asked.append(argv) or context.Captured(1, "", "")
        self.assertIsNone(sbx.extra_global_allows(ctx))
        self.assertEqual(asked, [["sbx", "policy", "ls", "--wide", "--source", "local", "--type", "network",
                                  "--decision", "allow"]])

    def test_it_gives_sbx_a_timeout_so_a_hang_becomes_a_skip(self):
        seen = []
        ctx = mock.Mock()
        ctx.capture = lambda argv, **kw: seen.append(kw) or context.Captured(124, "", "")
        self.assertIsNone(sbx.extra_global_allows(ctx))
        self.assertEqual(seen, [{"timeout": 15}])

    def test_an_unrecognised_scope_skips_the_check_instead_of_passing_it(self):
        table = policy_table(("local", "everything", "odd-1", "allow", "evil.example"))
        out = self.policy_lines(policy=table)
        self.assertIn("extra-host check skipped\n", out)
        self.assertNotIn("allows only", out)

    def test_without_sbx_nothing_is_checked(self):
        self.docker()
        out = self.run_doctor()[1]
        self.assertNotIn("global sbx", out)


class GatewayTest(DoctorTest):
    def test_no_broker_sandbox_yet(self):
        self.docker()
        self.sbx_stub(sandboxes="")
        self.assertIn("  ! broker sandbox not created yet ('factory start' creates it)\n", self.run_doctor()[1])

    def test_a_stopped_broker_sandbox(self):
        self.docker()
        self.sbx_stub(running=False)
        self.assertIn("  ! broker sandbox is not running ('factory start' starts it); its checks are skipped\n", self.run_doctor()[1])

    def test_gateway_problems_fail_and_its_own_summary_is_dropped(self):
        self.docker()
        self.sbx_stub(gateway='echo "==> Policy"; echo "  bad thing"; echo ""; echo "    deep"; echo "2 problem(s) found."; exit 1')
        self.write("repo/kits/broker/spec.yaml", KIT)
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn(HEAD % "Passdesk (the gateway)" + "    ==> Policy\n    bad thing\n      deep\n  ✗ the gateway reported problems (above)\n", out)
        self.assertNotIn("2 problem(s) found.\n  ", out)
        self.assertTrue(out.endswith("\n1 problem(s) found. Fix them, then run factory doctor again.\n"), out)

    def test_an_image_that_predates_the_gateway_doctor(self):
        self.docker()
        self.sbx_stub(gateway='echo "/usr/bin/python3: No module named passdesk.doctor" >&2; exit 1')
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("  ! the broker image predates the gateway doctor; run 'factory update' to rebuild it\n", out)
        self.assertNotIn("kit", out.split("Passdesk")[1].split("sbx CLI")[0])

    def test_hosts_missing_from_the_kit_are_a_problem_and_the_local_kit_counts(self):
        self.docker()
        self.sbx_stub(hosts="github.com extra.example other.example *.wild.example")
        self.write("repo/kits/broker/spec.yaml", KIT)
        rc, out, _ = self.run_doctor()
        local = os.path.join(self.config, "broker-kit", "spec.yaml")
        self.assertEqual(rc, 1)
        self.assertIn("  ✗ in the broker policy but not in the broker kit: extra.example other.example\n"
                      "      add them to kits/broker/spec.yaml (or %s), then 'factory update'\n" % local, out)
        self.write("config/ai-code-factory/broker-kit/spec.yaml", "hosts:\n  - extra.example\n  - other.example # x\n")
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("  ✓ every host the policy can reach is in the broker kit\n", out)

    def test_a_host_list_that_cannot_be_read_skips_the_kit_check(self):
        self.docker()
        self.sbx_stub(hosts="")
        self.assertIn("  ✓ gateway checks passed\n  ! could not list the policy's hosts; kit check skipped\n", self.run_doctor()[1])


class SecretToolTest(DoctorTest):
    def test_a_quiet_not_found_passes_the_probe(self):
        self.tool("secret-tool", "exit 1\n")
        self.assertEqual(doctor.secret_tool_probe(self.ctx()), "")

    def test_a_message_on_stderr_is_the_reason(self):
        self.tool("secret-tool", 'echo "secret-tool: Cannot autolaunch D-Bus without X11 \\$DISPLAY" >&2\nexit 1\n')
        self.assertEqual(doctor.secret_tool_probe(self.ctx()), "secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY")

    def test_a_probe_that_hangs_is_cut_off(self):
        self.tool("secret-tool", "sleep 30\n")
        with mock.patch.object(doctor, "SECRET_TOOL_TIMEOUT", 1):
            self.assertEqual(doctor.secret_tool_probe(self.ctx()), "timed out after 1 seconds (is a keyring prompt waiting?)")

    def test_the_probe_asks_for_a_name_nothing_stores_and_reads_only(self):
        self.tool("secret-tool", 'echo "$*" > "%s/argv"\ncat > "%s/stdin"\nexit 1\n' % (self.tmp, self.tmp))
        doctor.secret_tool_probe(self.ctx())
        with open(os.path.join(self.tmp, "argv")) as f:
            self.assertEqual(f.read(), "lookup service factory-doctor-probe\n")
        with open(os.path.join(self.tmp, "stdin")) as f:
            self.assertEqual(f.read(), "")

    def test_doctor_warns_with_the_reason_and_the_way_around(self):
        self.environ["FACTORY_KEYCHAIN"] = "secret-tool"
        self.tool("secret-tool", 'echo "secret-tool: Cannot autolaunch D-Bus" >&2\nexit 1\n')
        self.docker()
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn(HEAD % "Credential store (secret-tool)"
                      + "  ! secret-tool is installed but can't reach a keyring: secret-tool: Cannot autolaunch D-Bus\n"
                      "      'factory secret save' will fail until it can. Start a keyring (e.g. gnome-keyring) in a desktop\n"
                      "      session, or skip the keychain: 'factory secret set <name>' delivers a credential without one.\n", out)

    def test_doctor_says_so_when_it_answers(self):
        self.environ["FACTORY_KEYCHAIN"] = "secret-tool"
        self.tool("secret-tool", "exit 1\n")
        self.assertIn("  ✓ secret-tool answers\n", self.run_doctor()[1])

    def test_no_step_without_secret_tool(self):
        self.assertNotIn("Credential store", self.run_doctor()[1])


class BrokerSourceTest(DoctorTest):
    def setUp(self):
        super().setUp()
        self.docker()
        os.remove(os.path.join(self.root, "broker", "tools.txt"))

    def test_an_incomplete_broker_is_reported_and_the_checks_carry_on(self):
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("==> Platform", out)
        self.assertIn(HEAD % "Gateway source (broker/)" + "  ✗ %s/broker/tools.txt is missing: broker/ is incomplete in this checkout\n"
                      "      restore it from the committed tree:\n        git -C '%s' checkout HEAD -- broker\n" % (self.root, self.root), out)
        self.assertNotIn("submodule", out)
        self.assertIn("==> Docker", out)
        self.assertIn("problem(s) found.", out)

    def test_the_printed_command_restores_it(self):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        self.write("repo/broker/tools.txt", "git_fetch\n")
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", self.root] + argv, check=True, env=env)
        os.remove(os.path.join(self.root, "broker", "tools.txt"))
        out = self.run_doctor()[1]
        line = [l.strip() for l in out.split("\n") if l.strip().startswith("git -C ")][0]
        subprocess.run(shlex.split(line), check=True, env=env, cwd=self.tmp, stdout=subprocess.DEVNULL)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "broker", "tools.txt")))

    def test_role_overrides_cannot_be_checked_without_it(self):
        os.makedirs(os.path.join(self.config, "roles"))
        out = self.run_doctor()[1]
        self.assertIn(HEAD % "Role overrides (%s/roles)" % self.config + "  ! skipped: needs broker/ (see above)\n", out)

    def test_optional_roles_and_gitlab_checks_are_left_out(self):
        self.write("config/ai-code-factory/enabled-roles", "nonsense\n")
        self.assertNotIn("Optional roles", self.run_doctor()[1])


class RoleOverridesTest(DoctorTest):
    def test_no_step_without_the_folder(self):
        self.assertNotIn("Role overrides", self.run_doctor()[1])

    def test_an_empty_folder_is_valid(self):
        os.makedirs(os.path.join(self.config, "roles"))
        self.docker()
        out = self.run_doctor()[1]
        self.assertIn(HEAD % "Role overrides (%s/roles)" % self.config + "  ✓ role overrides are valid\n", out)
        self.assertNotIn("is incomplete", out)
        self.assertIn("✓ broker/ is present", out)

    def make_developer(self, conf):
        self.write("repo/roles/developer/role.conf", conf)
        self.write("repo/roles/developer/kit/spec.yaml", "name: developer\n")
        self.write("repo/roles/developer/instructions.md", "Do the work.\n")

    def test_a_bad_override_fails_with_its_message_and_the_fix(self):
        self.make_developer('TEMPLATE="developer"\nWORKSPACE="rw"\n')
        self.write("config/ai-code-factory/roles/developer.conf", 'LANES="x"\n')
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("  ✗ role developer: LANES must be a whole number from 1 to 8 (or empty) (set in %s/roles/developer.conf)\n"
                      "      fix or remove the override, then 'factory start' applies it (docs/guides/04-customize.md)\n" % self.config, out)

    def test_a_bad_role_is_caught_too(self):
        self.make_developer('TEMPLATE="developer"\nWORKSPACE="sideways"\n')
        os.makedirs(os.path.join(self.config, "roles"))
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("  ✗ role developer: WORKSPACE", out)


class ApprovalsTest(DoctorTest):
    def summary(self, line, status=0):
        os.makedirs(os.path.join(self.paths.broker_state, "approvals", "pending"), exist_ok=True)
        self.write("repo/scripts/approvals.py", "import sys\nprint(%r)\nsys.exit(%d)\n" % (line, status))

    def test_decided_cards_are_not_a_warning(self):
        self.summary("0 -1 0 3")
        self.assertNotIn("expired approval", self.run_doctor()[1])

    def test_an_expired_undecided_card_is_a_warning_not_a_problem(self):
        self.summary("1 40 1 0")
        self.docker()
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("  ! 1 expired approval(s) with no decision: nothing was written for them; run 'factory approvals' "
                      "(the agent's next call of each stages a fresh card)\n", out)

    def test_nothing_when_holds_are_not_in_use_or_the_summary_fails(self):
        self.assertNotIn("expired approval", self.run_doctor()[1])
        self.summary("0 -1 5", status=1)
        self.assertNotIn("expired approval", self.run_doctor()[1])

    def test_a_short_or_odd_summary_is_read_like_bash(self):
        for line in ("1 3", "", "1 3 x"):
            self.summary(line)
            self.assertNotIn("expired approval", self.run_doctor()[1], line)


class OptionalRolesTest(DoctorTest):
    def setUp(self):
        super().setUp()
        self.write("repo/roles/researcher/role.conf", RESEARCHER)
        self.write("repo/roles/researcher/kit/spec.yaml", "name: researcher\n")
        self.enabled = os.path.join(self.config, "enabled-roles")
        self.docker()

    def leftover(self):
        self.tool("sbx", 'echo "$*" >> "%s/calls"\n[ "$1 $2" = "ls -q" ] && echo factory-researcher\nexit 0\n' % self.tmp)

    def test_a_disabled_roles_leftover_sandbox_is_noted_and_never_removed(self):
        self.leftover()
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn(HEAD % "Optional roles"
                      + "  ! factory-researcher exists but its role (researcher) is disabled, so it is not started or used\n"
                      "      To remove it:  sbx rm --force factory-researcher\n"
                      "      To use it again: add 'researcher' to %s, then 'factory start'\n" % self.enabled, out)
        with open(os.path.join(self.tmp, "calls")) as f:
            self.assertNotIn("rm", f.read())

    def test_an_enabled_role_is_listed_and_not_a_leftover(self):
        self.leftover()
        self.write("config/ai-code-factory/enabled-roles", "researcher\n")
        out = self.run_doctor()[1]
        self.assertIn("  ✓ enabled-roles is valid (researcher)\n", out)
        self.assertNotIn("is disabled", out)

    def test_an_unknown_name_is_a_problem(self):
        self.write("config/ai-code-factory/enabled-roles", "reseacher\n")
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("no role named 'reseacher'", out)
        self.assertIn("      fix the file (%s): one role name per line\n" % self.enabled, out)

    def test_no_step_when_there_is_nothing_to_say(self):
        self.tool("sbx", "exit 0\n")
        self.assertNotIn("Optional roles", self.run_doctor()[1])


class GitlabTest(DoctorTest):
    POLICY = '[gitlab]\nurl = "https://gitlab.example"\n[approvals]\ntools = ["git_push"]\n'

    def setUp(self):
        super().setUp()
        self.write("repo/roles/developer/role.conf", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="git_fetch gitlab_create_mr"\n')

    def test_unheld_merge_requests_are_a_warning_not_a_problem(self):
        self.write("config/ai-code-factory/policy.toml", self.POLICY)
        self.docker()
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn('  ! GitLab merge requests open without approval: [approvals].tools is set without "gitlab_create_mr" '
                      "(see README: Enabling approvals)\n", out)

    def test_no_warning_without_the_policy(self):
        self.assertNotIn("GitLab", self.run_doctor()[1])


class ReferenceTest(DoctorTest):
    def test_a_stale_reference_is_a_warning(self):
        self.write("repo/docs/reference/sbx-cli.md", "<!-- sbx-reference: version=0.1.0 generated=x -->\n")
        self.docker()
        rc, out, _ = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("  ! the sbx CLI reference is for sbx 0.1.0 but the pinned sbx is 0.45.1; regenerate it with scripts/gen-sbx-reference.sh\n", out)

    def test_a_missing_reference_names_the_file(self):
        os.remove(os.path.join(self.root, "docs", "reference", "sbx-cli.md"))
        self.assertIn("  ! %s/docs/reference/sbx-cli.md is missing; run scripts/gen-sbx-reference.sh on the host\n" % self.root, self.run_doctor()[1])


class ShimTest(unittest.TestCase):
    """bin/factory runs doctor through the Python package, and `setup` stops after a failing one."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.checkout = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.checkout, "bin"))
        shutil.copy(os.path.join(REPO, "bin", "factory"), os.path.join(self.checkout, "bin", "factory"))
        shutil.copytree(os.path.join(REPO, "scripts", "lib"), os.path.join(self.checkout, "scripts", "lib"))
        shutil.copytree(os.path.join(REPO, "factory"), os.path.join(self.checkout, "factory"),
                        ignore=shutil.ignore_patterns("test_*", "__pycache__", "fixtures.py"))
        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        for name in ("sbx", "docker"):
            path = os.path.join(stubs, name)
            with open(path, "w") as f:
                f.write('#!/bin/sh\necho "%s $*" >> "%s/forbidden"\nexit 1\n' % (name, self.tmp))
            os.chmod(path, 0o755)
        self.env = dict(os.environ, PATH=stubs + os.pathsep + os.environ["PATH"], HOME=os.path.join(self.tmp, "home"),
                        XDG_STATE_HOME=os.path.join(self.tmp, "state"), XDG_CONFIG_HOME=os.path.join(self.tmp, "config"))

    def factory(self, *argv):
        cp = subprocess.run([os.path.join(self.checkout, "bin", "factory")] + list(argv), env=self.env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return cp.returncode, cp.stdout.decode()

    def forbidden(self):
        try:
            with open(os.path.join(self.tmp, "forbidden")) as f:
                return f.read()
        except OSError:
            return ""

    def test_doctor_runs_without_broker_and_reports_it(self):
        rc, out = self.factory("doctor")
        self.assertEqual(rc, 1)
        self.assertIn("==> Gateway source (broker/)", out)
        self.assertIn("broker/tools.txt is missing: broker/ is incomplete in this checkout", out)
        self.assertIn("problem(s) found.", out)
        self.assertNotIn("command not found", out)
        self.assertNotIn("build", self.forbidden())

    def test_setup_stops_after_a_failing_doctor_and_builds_nothing(self):
        rc, out = self.factory("setup")
        self.assertEqual(rc, 1)
        self.assertIn("==> Gateway source (broker/)", out)
        self.assertIn("fix the problems above, then run 'factory setup' again", out)
        self.assertNotIn("build", self.forbidden())
        self.assertNotIn("login", self.forbidden())


if __name__ == "__main__":
    unittest.main()
