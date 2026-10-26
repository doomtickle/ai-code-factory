"""Tests for factory/preflight.py. Ported from the helpers in scripts/lib/preflight.sh (deleted in
step 8 part A, once factory/prereqs.py replaced install-prereqs.sh's use of it), resolve_python
(common.sh, still there) and keychain_backend (secrets.sh, now gone). resolve_python runs the bash
and the Python on the same fixture and requires the same answer; the others assert the answers the
bash gave (the two were compared on every fixture here while it existed). The Python-only seams (an
os-release, /proc/version and /dev/kvm that a test supplies) stand in for another machine.
"""

import os
import shutil
import subprocess
import sys
import unittest

from factory import pins, preflight
from factory.context import FactoryError
from factory.fixtures import REPO, CommandTest

UBUNTU = 'ID=ubuntu\nVERSION_ID="%s"\nPRETTY_NAME="Ubuntu %s LTS"\n'


class PreflightTest(CommandTest):
    platform = None

    def bash(self, snippet, path=None, env=None):
        """(status, stdout, stderr) of the bash helpers; PATH is the fixture's unless given."""
        e = dict(self.environ, LC_ALL="C", **(env or {}))
        if path is not None:
            e["PATH"] = path
        cp = subprocess.run(["bash", "-c", 'ROOT="$1"; . "$1/scripts/lib/common.sh"; ' + snippet, "_", REPO],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, stdin=subprocess.DEVNULL, cwd=self.tmp)
        return cp.returncode, cp.stdout.decode(), cp.stderr.decode()

    def private_path(self):
        """PATH = an empty folder for stubs, then a folder linking only what the bash helpers need
        (so no python3, secret-tool or powershell.exe of this machine shows through). `uname` is
        a stub too, Linux until set_platform says otherwise, so the answer does not depend on
        the host the tests run on."""
        self.bindir = os.path.join(self.tmp, "bin")
        sysdir = os.path.join(self.tmp, "sys")
        os.makedirs(self.bindir, exist_ok=True)
        os.makedirs(sysdir, exist_ok=True)
        for name in ("bash", "grep", "cat", "sed", "head", "tr", "id"):
            found = shutil.which(name)
            if found and not os.path.exists(os.path.join(sysdir, name)):
                os.symlink(found, os.path.join(sysdir, name))
        self.environ["PATH"] = self.bindir + os.pathsep + sysdir
        self.set_platform("Linux")

    def set_platform(self, name):
        """Make `uname -s` (the stub on the PATH) and the Python's ctx.uname both say NAME."""
        self.platform = name
        self.tool("uname", 'case "$1" in -m) echo x86_64 ;; *) echo %s ;; esac\n' % name)

    proc_version = "Linux version 6.8.0 generic\n"

    def ctx(self, tty=False):
        ctx = super().ctx(tty)
        if self.platform:
            ctx.uname = (self.platform, "x86_64")
        # Never the host's own /proc/version: what the Python sees is the test's to say.
        ctx.host_files["proc_version"] = self.write("proc-version", self.proc_version)
        return ctx

    def release(self, text):
        return self.write("os-release", text)


class ConstantsTest(PreflightTest):
    def test_the_sbx_version_matches_common_sh(self):
        self.assertEqual(preflight.SBX_VERSION, pins.read(REPO, "SBX_VERSION")["SBX_VERSION"])

    def test_the_deb_pins_are_by_exact_version_and_checksum(self):
        for release in ("2404", "2604"):
            url, sha = getattr(preflight, "SBX_DEB_UBUNTU%s_URL" % release), getattr(preflight, "SBX_DEB_UBUNTU%s_SHA256" % release)
            self.assertEqual(url, "https://github.com/docker/sbx-releases/releases/download/v%s/DockerSandboxes-linux-amd64-"
                                  "ubuntu%s.deb" % (preflight.SBX_VERSION, release))
            self.assertRegex(sha, r"^[0-9a-f]{64}$")

    def test_the_wsl_kvm_text(self):
        self.assertTrue(preflight.KVM_UNDER_WSL.startswith("Under WSL2, turn on nested virtualization"))
        self.assertIn("windows/enable-wsl.ps1", preflight.KVM_UNDER_WSL)
        self.assertTrue(os.path.isfile(os.path.join(REPO, "windows", "enable-wsl.ps1")))


class IsWslTest(PreflightTest):
    def test_files_read_as_the_bash_read_them(self):
        for text, wsl in (("Linux version 5.15 microsoft-standard-WSL2", True), ("Linux version 6.8 generic", False),
                          ("MICROSOFT", True), ("wsl", True), ("", False), ("Linux version 6.8 (buildd) Ubuntu", False)):
            self.assertEqual(preflight.is_wsl(self.write("proc-version", text)), wsl, text)
        self.assertFalse(preflight.is_wsl(self.tmp + "/none"))


class OsReleaseTest(PreflightTest):
    def test_fields_read_as_the_bash_read_them(self):
        path = self.release('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\nVERSION_ID="99"\n'
                            'PRETTY_NAME="Ubuntu 24.04 LTS"\nQUOTED="a"b"\nEMPTY=\n')
        for field, want in (("ID", "ubuntu"), ("VERSION_ID", "24.04"), ("PRETTY_NAME", "Ubuntu 24.04 LTS"),
                            ("NAME", "Ubuntu"), ("QUOTED", "ab"), ("EMPTY", ""), ("MISSING", ""),
                            ("ID_LIKE", "debian")):
            self.assertEqual(preflight.os_release_field(field, path), want, field)
        self.assertEqual(preflight.os_release_field("ID", self.tmp + "/none"), "")


class SbxDebPinTest(PreflightTest):
    def pin(self, text, wsl=False):
        path = self.release(text) if text is not None else self.tmp + "/none"
        try:
            return preflight.sbx_deb_pin(path, wsl)
        except preflight.Problem as e:
            return str(e)

    def test_ubuntu_2404_and_later_get_their_deb(self):
        old = (preflight.SBX_DEB_UBUNTU2404_URL, preflight.SBX_DEB_UBUNTU2404_SHA256)
        new = (preflight.SBX_DEB_UBUNTU2604_URL, preflight.SBX_DEB_UBUNTU2604_SHA256)
        self.assertEqual(self.pin(UBUNTU % ("24.04", "24.04")), old)
        self.assertEqual(self.pin(UBUNTU % ("25.10", "25.10")), old)
        self.assertEqual(self.pin(UBUNTU % ("26.04", "26.04")), new)
        self.assertEqual(self.pin(UBUNTU % ("24", "24")), old)

    def test_anything_else_is_refused_with_the_same_text(self):
        for text, said in ((UBUNTU % ("22.04", "22.04"), "this is 'ubuntu' 22.04."),
                           ('ID=debian\nVERSION_ID="12"\n', "this is 'debian' 12."),
                           ('ID=linuxmint\nVERSION_ID="24.04"\n', "this is 'linuxmint' 24.04."),
                           ('ID=ubuntu\nVERSION_ID="abc"\n', "this is 'ubuntu' abc."),
                           ('ID=ubuntu\n', "this is 'ubuntu' (unknown version)."),
                           ("", "this is 'unknown' (unknown version)."),
                           (None, "this is 'unknown' (unknown version).")):
            self.assertEqual(self.pin(text), "sbx on Linux needs Ubuntu 24.04 or later; " + said)

    def test_under_wsl_it_adds_how_to_install_ubuntu(self):
        self.assertEqual(self.pin(UBUNTU % ("22.04", "22.04"), True).split("\n"), [
            "sbx on Linux needs Ubuntu 24.04 or later; this is 'ubuntu' 22.04.",
            "Install it from PowerShell: wsl --install -d Ubuntu-24.04 (see docs/setup-wsl.md)"])


class KvmTest(PreflightTest):
    def check(self, dev):
        try:
            preflight.kvm_check(dev, False, preflight.user_name())
            return None
        except preflight.Problem as e:
            return str(e)

    def test_missing_device_names_the_bios(self):
        self.assertEqual(self.check(self.tmp + "/kvm"), "%s/kvm not found: sbx needs KVM to run its microVMs.\n"
                         "Turn on virtualization (Intel VT-x / AMD-V) in your BIOS/UEFI settings and reboot." % self.tmp)

    def test_a_device_the_user_can_use_passes(self):
        self.assertIsNone(self.check(self.write("kvm", "")))

    @unittest.skipIf(os.geteuid() == 0, "root can open anything")
    def test_a_device_the_user_cannot_use_names_the_group(self):
        path = self.write("kvm", "")
        os.chmod(path, 0)
        self.assertEqual(self.check(path), "%s exists but %s can't use it. Join the kvm group:\n  sudo usermod -aG kvm $USER\n"
                         "then log out and back in." % (path, preflight.user_name()))

    def test_under_wsl_missing_kvm_points_at_nested_virtualization(self):
        with self.assertRaises(preflight.Problem) as caught:
            preflight.kvm_check(self.tmp + "/kvm", True)
        self.assertEqual(str(caught.exception).split("\n", 1)[1], preflight.KVM_UNDER_WSL)

    def test_under_wsl_an_unusable_device_says_to_restart_wsl(self):
        path = self.write("kvm", "")
        os.chmod(path, 0)
        if os.geteuid() == 0:
            self.skipTest("root can open anything")
        with self.assertRaises(preflight.Problem) as caught:
            preflight.kvm_check(path, True, "me")
        self.assertTrue(str(caught.exception).endswith("then run 'wsl --shutdown' in PowerShell and reopen your WSL terminal."))


class ResolvePythonTest(PreflightTest):
    def setUp(self):
        super().setUp()
        self.private_path()

    def both(self):
        status, out, err = self.bash("resolve_python", path=self.environ["PATH"])
        try:
            found = preflight.resolve_python(self.ctx())
            self.assertEqual((status, out, err), (0, found, ""))
            return found
        except FactoryError as e:
            self.assertEqual((status, out, err), (1, "", "error: %s\n" % e))
            return str(e)

    def test_a_python_on_the_path_is_found(self):
        os.symlink(sys.executable, os.path.join(self.bindir, "python3"))
        self.assertEqual(self.both(), os.path.join(self.bindir, "python3"))

    def test_none_on_the_path_is_refused_with_a_hint(self):
        self.assertEqual(self.both(), "python3 not found in PATH (install python3 3.9+ (e.g. `sudo apt install python3`))")

    def test_one_that_is_too_old_is_refused_by_name(self):
        self.tool("python3", "exit 1\n")
        self.assertEqual(self.both(), "%s/python3 3.9+ is required (install python3 3.9+ (e.g. `sudo apt install python3`))" % self.bindir)

    def test_macos_ignores_the_path_and_uses_the_system_python(self):
        self.set_platform("Darwin")
        os.symlink(sys.executable, os.path.join(self.bindir, "python3"))
        found = self.both()
        if os.access("/usr/bin/python3", os.X_OK):
            self.assertEqual(found, "/usr/bin/python3")
        else:
            self.assertEqual(found, "python3 not found in PATH (install the Xcode Command Line Tools: xcode-select --install)")


class KeychainBackendTest(PreflightTest):
    def setUp(self):
        super().setUp()
        self.private_path()

    def both(self, **env):
        """The backend the Python picks on the stubbed host; each test asserts the one the bash
        keychain_backend picked there."""
        self.environ.update(env)
        return preflight.keychain_backend(self.ctx())

    def test_the_override_wins(self):
        self.assertEqual(self.both(FACTORY_KEYCHAIN="wincred"), "wincred")

    def test_secret_tool_when_installed(self):
        self.tool("secret-tool", "exit 1\n")
        self.assertEqual(self.both(), "secret-tool")

    def test_wincred_through_the_powershell_override(self):
        self.assertEqual(self.both(FACTORY_POWERSHELL="/some/powershell.exe"), "wincred")

    def test_macos_uses_its_keychain_whatever_else_is_installed(self):
        self.set_platform("Darwin")
        self.tool("secret-tool", "exit 1\n")
        self.assertEqual(self.both(FACTORY_POWERSHELL="/some/powershell.exe"), "macos")

    def test_none_without_any(self):
        self.assertEqual(self.both(), "none")

    def test_wincred_under_wsl_when_powershell_is_on_the_path(self):
        self.proc_version = "Linux version 5.15 microsoft-standard-WSL2\n"
        self.tool("powershell.exe", "exit 0\n")
        self.assertEqual(self.both(), "wincred")

    def test_powershell_on_the_path_means_nothing_outside_wsl(self):
        self.tool("powershell.exe", "exit 0\n")
        self.assertEqual(self.both(), "none")


if __name__ == "__main__":
    unittest.main()
