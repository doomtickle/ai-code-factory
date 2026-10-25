"""Tests for factory/preflight.py. Ported from the helpers in scripts/lib/preflight.sh (which
stays: scripts/install-prereqs.sh sources it), resolve_python (common.sh) and keychain_backend
(secrets.sh). Each check runs the bash and the Python on the same fixture and requires the
same answer; the Python-only seams (an os-release, /proc/version and /dev/kvm that a test
supplies) stand in for another machine.
"""

import os
import shutil
import subprocess
import sys
import unittest

from factory import preflight
from factory.context import FactoryError
from factory.fixtures import REPO, CommandTest

UBUNTU = 'ID=ubuntu\nVERSION_ID="%s"\nPRETTY_NAME="Ubuntu %s LTS"\n'


class PreflightTest(CommandTest):
    def bash(self, snippet, path=None, env=None):
        """(status, stdout, stderr) of the bash helpers; PATH is the fixture's unless given."""
        e = dict(self.environ, LC_ALL="C", **(env or {}))
        if path is not None:
            e["PATH"] = path
        cp = subprocess.run(["bash", "-c", 'ROOT="$1"; . "$1/scripts/lib/common.sh"; '
                             '. "$1/scripts/lib/preflight.sh"; . "$1/scripts/lib/secrets.sh"; ' + snippet, "_", REPO],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, stdin=subprocess.DEVNULL, cwd=self.tmp)
        return cp.returncode, cp.stdout.decode(), cp.stderr.decode()

    def private_path(self):
        """PATH = an empty folder for stubs, then a folder linking only what the bash helpers need
        (so no python3, secret-tool or powershell.exe of this machine shows through)."""
        self.bindir = os.path.join(self.tmp, "bin")
        sysdir = os.path.join(self.tmp, "sys")
        os.makedirs(self.bindir, exist_ok=True)
        os.makedirs(sysdir, exist_ok=True)
        for name in ("bash", "uname", "grep", "cat", "sed", "head", "tr", "id"):
            found = shutil.which(name)
            if found and not os.path.exists(os.path.join(sysdir, name)):
                os.symlink(found, os.path.join(sysdir, name))
        self.environ["PATH"] = self.bindir + os.pathsep + sysdir

    def release(self, text):
        return self.write("os-release", text)


class ConstantsTest(PreflightTest):
    def test_the_pins_match_common_sh(self):
        names = ["SBX_VERSION", "SBX_DEB_UBUNTU2404_URL", "SBX_DEB_UBUNTU2404_SHA256", "SBX_DEB_UBUNTU2604_URL",
                 "SBX_DEB_UBUNTU2604_SHA256"]
        status, out, err = self.bash("".join('printf "%%s\\n" "$%s"; ' % n for n in names))
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out.split("\n")[:-1], [getattr(preflight, n) for n in names])

    def test_the_wsl_kvm_text_matches_preflight_sh(self):
        self.assertEqual(preflight.KVM_UNDER_WSL, preflight_sh_wsl_text())


def preflight_sh_wsl_text():
    with open(os.path.join(REPO, "scripts", "lib", "preflight.sh")) as f:
        text = f.read()
    return text.split("<<'EOF'\n", 1)[1].split("EOF\n", 1)[0].rstrip("\n")


class IsWslTest(PreflightTest):
    def test_the_same_files_read_the_same(self):
        for text in ("Linux version 5.15 microsoft-standard-WSL2", "Linux version 6.8 generic", "MICROSOFT", "wsl",
                     "", "Linux version 6.8 (buildd) Ubuntu"):
            path = self.write("proc-version", text)
            status = self.bash('is_wsl "%s"' % path)[0]
            self.assertEqual(preflight.is_wsl(path), status == 0, text)
        self.assertEqual(self.bash('is_wsl "%s/none"' % self.tmp)[0] == 0, preflight.is_wsl(self.tmp + "/none"))
        self.assertFalse(preflight.is_wsl(self.tmp + "/none"))


class OsReleaseTest(PreflightTest):
    def test_fields_read_the_same(self):
        path = self.release('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\nVERSION_ID="99"\n'
                            'PRETTY_NAME="Ubuntu 24.04 LTS"\nQUOTED="a"b"\nEMPTY=\n')
        for field in ("ID", "VERSION_ID", "PRETTY_NAME", "NAME", "QUOTED", "EMPTY", "MISSING", "ID_LIKE"):
            status, out, _ = self.bash('os_release_field "%s" "%s"' % (field, path))
            self.assertEqual(preflight.os_release_field(field, path), out.rstrip("\n"), field)
        self.assertEqual(preflight.os_release_field("ID", self.tmp + "/none"), "")


class SbxDebPinTest(PreflightTest):
    def both(self, text, wsl=False):
        path = self.release(text) if text is not None else self.tmp + "/none"
        status, out, err = self.bash('sbx_deb_pin "%s"' % path)
        try:
            url, sha = preflight.sbx_deb_pin(path, wsl)
            self.assertEqual((status, out.strip(), err), (0, url + " " + sha, ""))
            return url, sha
        except preflight.Problem as e:
            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            return str(e), err

    def test_ubuntu_2404_and_later_get_their_deb(self):
        self.assertEqual(self.both(UBUNTU % ("24.04", "24.04")), (preflight.SBX_DEB_UBUNTU2404_URL, preflight.SBX_DEB_UBUNTU2404_SHA256))
        self.assertEqual(self.both(UBUNTU % ("25.10", "25.10")), (preflight.SBX_DEB_UBUNTU2404_URL, preflight.SBX_DEB_UBUNTU2404_SHA256))
        self.assertEqual(self.both(UBUNTU % ("26.04", "26.04")), (preflight.SBX_DEB_UBUNTU2604_URL, preflight.SBX_DEB_UBUNTU2604_SHA256))
        self.assertEqual(self.both(UBUNTU % ("24", "24")), (preflight.SBX_DEB_UBUNTU2404_URL, preflight.SBX_DEB_UBUNTU2404_SHA256))

    def test_anything_else_is_refused_with_the_same_text(self):
        for text, said in ((UBUNTU % ("22.04", "22.04"), "this is 'ubuntu' 22.04."),
                           ('ID=debian\nVERSION_ID="12"\n', "this is 'debian' 12."),
                           ('ID=linuxmint\nVERSION_ID="24.04"\n', "this is 'linuxmint' 24.04."),
                           ('ID=ubuntu\nVERSION_ID="abc"\n', "this is 'ubuntu' abc."),
                           ('ID=ubuntu\n', "this is 'ubuntu' (unknown version)."),
                           ("", "this is 'unknown' (unknown version)."),
                           (None, "this is 'unknown' (unknown version).")):
            message, err = self.both(text)
            self.assertEqual(message, "sbx on Linux needs Ubuntu 24.04 or later; " + said)
            self.assertEqual(err, message + "\n")

    def test_under_wsl_it_adds_how_to_install_ubuntu(self):
        # bash reads /proc/version itself, so only the Python side is exercised with wsl=True
        path = self.release(UBUNTU % ("22.04", "22.04"))
        with self.assertRaises(preflight.Problem) as caught:
            preflight.sbx_deb_pin(path, True)
        self.assertEqual(str(caught.exception).split("\n"), [
            "sbx on Linux needs Ubuntu 24.04 or later; this is 'ubuntu' 22.04.",
            "Install it from PowerShell: wsl --install -d Ubuntu-24.04 (see docs/setup-wsl.md)"])


class KvmTest(PreflightTest):
    def both(self, dev):
        status, out, err = self.bash('kvm_check "%s"' % dev)
        try:
            preflight.kvm_check(dev, False, preflight.user_name())
            self.assertEqual((status, err), (0, ""))
            return None
        except preflight.Problem as e:
            self.assertEqual((status, err), (1, str(e) + "\n"))
            return str(e)

    def test_missing_device_names_the_bios(self):
        message = self.both(self.tmp + "/kvm")
        self.assertEqual(message, "%s/kvm not found: sbx needs KVM to run its microVMs.\n"
                         "Turn on virtualization (Intel VT-x / AMD-V) in your BIOS/UEFI settings and reboot." % self.tmp)

    def test_a_device_the_user_can_use_passes(self):
        self.assertIsNone(self.both(self.write("kvm", "")))

    @unittest.skipIf(os.geteuid() == 0, "root can open anything")
    def test_a_device_the_user_cannot_use_names_the_group(self):
        path = self.write("kvm", "")
        os.chmod(path, 0)
        message = self.both(path)
        self.assertEqual(message, "%s exists but %s can't use it. Join the kvm group:\n  sudo usermod -aG kvm $USER\n"
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

    def test_macos_uses_the_system_python(self):
        ctx = self.ctx()
        ctx.uname = ("Darwin", "arm64")
        if os.access("/usr/bin/python3", os.X_OK):
            self.assertEqual(preflight.resolve_python(ctx), "/usr/bin/python3")
        else:
            with self.assertRaises(FactoryError) as caught:
                preflight.resolve_python(ctx)
            self.assertEqual(str(caught.exception), "python3 not found in PATH (install the Xcode Command Line Tools: xcode-select --install)")


class KeychainBackendTest(PreflightTest):
    def setUp(self):
        super().setUp()
        self.private_path()

    def both(self, **env):
        self.environ.update(env)
        status, out, err = self.bash("keychain_backend", path=self.environ["PATH"], env=env)
        self.assertEqual((status, err), (0, ""))
        ctx = self.ctx()
        ctx.uname = (os.uname().sysname, os.uname().machine)
        self.assertEqual(preflight.keychain_backend(ctx), out)
        return out

    def test_the_override_wins(self):
        self.assertEqual(self.both(FACTORY_KEYCHAIN="wincred"), "wincred")

    def test_secret_tool_when_installed(self):
        self.tool("secret-tool", "exit 1\n")
        self.assertEqual(self.both(), "secret-tool")

    def test_wincred_through_the_powershell_override(self):
        self.assertEqual(self.both(FACTORY_POWERSHELL="/some/powershell.exe"), "wincred")

    def test_none_without_any(self):
        if os.path.exists("/proc/version") and preflight.is_wsl():
            self.skipTest("this machine is WSL")
        self.assertEqual(self.both(), "none")


if __name__ == "__main__":
    unittest.main()
