"""Tests for factory/prereqs.py (`factory _install-prereqs`, what scripts/install-prereqs.sh runs).
Before the bash was replaced, the script and its Python were compared on fixtures
(docs/py-cli-migration.md, step 8 part A): the same status, stdout, stderr and calls.

curl, sudo, sw_vers and sbx are stubs that log their calls. The download is a local file whose
checksum the fixture checkout's scripts/lib/common.sh pins (macOS) or the test patches in (Linux's
.deb pins are constants in factory/preflight.py).
"""

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import unittest
from unittest import mock

from factory import preflight, prereqs
from factory.context import FactoryError
from factory.fixtures import CommandTest

VERSION = preflight.SBX_VERSION

SBX_BODY = r'''import os, sys
v = os.environ.get("FAKE_INSTALLED_VERSION") or "%s"
print("sbx version: v%%s stub" %% v)
''' % VERSION

STUB_CURL = r'''import json, os, shutil, sys
a = sys.argv[1:]
with open(os.path.join(os.environ["STUBS"], "log"), "a") as f:
    f.write(json.dumps(["curl"] + a) + "\n")
if os.environ.get("CURL_FAIL"):
    sys.exit(22)
shutil.copy(os.environ["DOWNLOAD"], a[a.index("-o") + 1])
'''

STUB_LOGGER = r'''import json, os, sys
with open(os.path.join(os.environ["STUBS"], "log"), "a") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\n")
if os.path.basename(sys.argv[0]) == "sw_vers":
    print(os.environ["MACOS"])
if os.path.basename(sys.argv[0]) == "sudo":
    open(os.path.join(os.environ["STUBS"], "installed"), "w").close()
'''

# The sbx a `sudo apt-get install` "installs": it reports the pinned version only after sudo ran.
STATEFUL_SBX = r'''import os, sys
if sys.argv[1:] == ["version"]:
    installed = os.path.exists(os.path.join(os.environ["STUBS"], "installed"))
    v = os.environ["AFTER"] if installed else os.environ.get("BEFORE", "0.1.0")
    print("sbx version: v%s stub" % v)
'''


class PrereqsTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(self.stubs)
        self.prefix = os.path.join(self.tmp, "prefix")
        self.environ.update(STUBS=self.stubs, PREFIX=self.prefix, AFTER=VERSION)
        for name in ("sudo", "apt-get", "sw_vers"):
            self.write("bin/" + name, "#!%s\n%s" % (sys.executable, STUB_LOGGER), 0o755)
        self.write("bin/curl", "#!%s\n%s" % (sys.executable, STUB_CURL), 0o755)
        self.uname = ("Linux", "x86_64")
        self.download = self.write("download.bin", "the sbx package")
        self.environ["DOWNLOAD"] = self.download
        self.write("os-release", 'ID=ubuntu\nVERSION_ID="24.04"\n')
        self.write("proc-version", "Linux version 6.8.0 (buildd)\n")
        self.write("kvm", "")
        self.write("scripts/lib/common.sh", "")

    def sha(self, path=None):
        with open(path or self.download, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def install(self, sha=None, expect_error=False):
        ctx = self.ctx()
        ctx.uname = self.uname
        ctx.host_files = {"os_release": os.path.join(self.tmp, "os-release"),
                          "proc_version": os.path.join(self.tmp, "proc-version"), "kvm": os.path.join(self.tmp, "kvm")}
        deb = ("https://example.invalid/sbx.deb", sha or self.sha())
        error = ""
        with mock.patch.object(preflight, "sbx_deb_pin", return_value=deb):
            try:
                status = prereqs.main(ctx, [])
            except FactoryError as e:
                status, error = 1, str(e)
        return status, self.out.getvalue(), self.err.getvalue(), error

    def calls(self):
        try:
            with open(os.path.join(self.stubs, "log")) as f:
                return [c for c in (json.loads(line) for line in f) if c != ["version"]]
        except OSError:
            return []

    def stateful_sbx(self):
        self.write("bin/sbx", "#!%s\n%s" % (sys.executable, STATEFUL_SBX), 0o755)

    def on_path(self):
        self.environ["PATH"] = os.path.join(self.prefix, "bin") + os.pathsep + self.environ["PATH"]


class LinuxTest(PrereqsTest):
    def test_the_pinned_deb_is_downloaded_checked_and_installed_with_sudo(self):
        self.stateful_sbx()
        self.on_path()
        status, out, err, _ = self.install()
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, "Installing sbx %s (Docker's package; apt asks for your password)...\n"
                              "Prerequisites OK: sbx %s\n" % (VERSION, VERSION))
        curl, sudo = self.calls()
        self.assertEqual(curl[:5], ["curl", "-fsSL", "--retry", "3", "-o"])
        self.assertEqual(curl[-1], "https://example.invalid/sbx.deb")
        self.assertEqual(sudo[:6], ["sudo", "apt-get", "install", "-y", "--no-install-recommends",
                                    "--allow-downgrades"])
        self.assertEqual(sudo[6], curl[5])
        self.assertTrue(os.path.isdir(os.path.join(self.prefix, "bin")))

    def test_a_checksum_mismatch_installs_nothing(self):
        self.stateful_sbx()
        status, out, _, error = self.install(sha="0" * 64)
        self.assertEqual((status, error), (1, "checksum mismatch for https://example.invalid/sbx.deb"))
        self.assertEqual([c[0] for c in self.calls()], ["curl"])

    def test_a_failed_download_installs_nothing(self):
        self.stateful_sbx()
        self.environ["CURL_FAIL"] = "1"
        status, _, _, error = self.install()
        self.assertEqual((status, error), (1, "download failed: https://example.invalid/sbx.deb"))
        self.assertEqual([c[0] for c in self.calls()], ["curl"])

    def test_the_pinned_version_already_installed_is_left_alone(self):
        self.write("bin/sbx", "#!%s\n%s" % (sys.executable, SBX_BODY), 0o755)
        status, out, _, _ = self.install()
        self.assertEqual(status, 0)
        self.assertEqual(out, "sbx %s already installed: %s\nnote: add %s/bin to your PATH, ahead of any other copy of "
                              "sbx\nPrerequisites OK: sbx %s\n" % (VERSION, os.path.join(self.tmp, "bin", "sbx"),
                                                                  self.prefix, VERSION))
        self.assertEqual(self.calls(), [])

    def test_another_version_is_replaced(self):
        self.stateful_sbx()
        self.on_path()
        self.install()
        self.assertEqual([c[0] for c in self.calls()], ["curl", "sudo"])

    def test_an_install_that_leaves_the_wrong_version_fails_the_check(self):
        self.stateful_sbx()
        self.environ["AFTER"] = "0.0.1"
        status, _, _, error = self.install()
        self.assertEqual(status, 1)
        self.assertIn("required, found '0.0.1'", error)

    def test_a_failing_apt_get_stops_with_its_status(self):
        self.stateful_sbx()
        ctx = self.ctx()
        ctx.uname = self.uname
        real = ctx.run
        ctx.run = lambda argv, **kw: 7 if argv[0] == "sudo" else real(argv, **kw)
        ctx.host_files = {"os_release": os.path.join(self.tmp, "os-release"),
                          "proc_version": os.path.join(self.tmp, "proc-version"), "kvm": os.path.join(self.tmp, "kvm")}
        with mock.patch.object(preflight, "sbx_deb_pin", return_value=("https://x/y.deb", self.sha())):
            self.assertEqual(prereqs.main(ctx, []), 7)

    def test_no_kvm_is_a_problem_on_stderr_and_nothing_is_run(self):
        os.remove(os.path.join(self.tmp, "kvm"))
        status, out, err, _ = self.install()
        self.assertEqual((status, out), (1, ""))
        self.assertIn("kvm not found: sbx needs KVM", err)
        self.assertEqual(self.calls(), [])

    def test_a_release_older_than_ubuntu_24_is_refused(self):
        self.write("os-release", 'ID=ubuntu\nVERSION_ID="22.04"\n')
        ctx = self.ctx()
        ctx.uname = self.uname
        ctx.host_files = {"os_release": os.path.join(self.tmp, "os-release"),
                          "proc_version": os.path.join(self.tmp, "proc-version"), "kvm": os.path.join(self.tmp, "kvm")}
        self.assertEqual(prereqs.main(ctx, []), 1)
        self.assertIn("sbx on Linux needs Ubuntu 24.04 or later; this is 'ubuntu' 22.04.", self.err.getvalue())
        self.assertEqual(self.calls(), [])

    def test_missing_programs_are_named(self):
        self.environ["PATH"] = os.path.join(self.tmp, "bin")
        for name in ("sudo", "curl"):
            os.rename(os.path.join(self.tmp, "bin", name), os.path.join(self.tmp, name))
            self.assertEqual(self.install()[3], "'%s' not found in PATH (see README.md)" % name)
            os.rename(os.path.join(self.tmp, name), os.path.join(self.tmp, "bin", name))


class MacosTest(PrereqsTest):
    def setUp(self):
        super().setUp()
        self.uname = ("Darwin", "arm64")
        self.environ["MACOS"] = "14.5"
        buf = io.BytesIO()
        script = ("#!%s\n%s" % (sys.executable, SBX_BODY)).encode()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("Sbx.app/Contents/MacOS/sbx")
            info.size, info.mode = len(script), 0o755
            tar.addfile(info, io.BytesIO(script))
        self.download = self.write("sbx.tar.gz", "")
        with open(self.download, "wb") as f:
            f.write(buf.getvalue())
        self.environ["DOWNLOAD"] = self.download
        self.write("scripts/lib/common.sh", 'readonly SBX_VERSION="%s"\nreadonly SBX_URL="https://example.invalid/'
                                            'sbx-${SBX_VERSION}.tar.gz"\nreadonly SBX_SHA256="%s"\n'
                   % (VERSION, self.sha()))
        os.rename(os.path.join(self.tmp, "scripts"), os.path.join(self.root, "scripts"))
        self.on_path()

    def test_the_archive_is_extracted_under_the_prefix_and_linked(self):
        status, out, err, _ = self.install()
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, "Installing sbx %s...\nPrerequisites OK: sbx %s\n" % (VERSION, VERSION))
        target = os.path.join(self.prefix, "opt", "sbx", VERSION, "Sbx.app", "Contents", "MacOS", "sbx")
        self.assertEqual(os.readlink(os.path.join(self.prefix, "bin", "sbx")), target)
        self.assertTrue(os.stat(target).st_mode & stat.S_IXUSR)
        self.assertEqual(self.calls()[1][-1], "https://example.invalid/sbx-%s.tar.gz" % VERSION)

    def test_a_checksum_mismatch_extracts_nothing(self):
        self.write("scripts/lib/common.sh", 'readonly SBX_URL="https://example.invalid/x"\nreadonly SBX_SHA256="%s"\n'
                   % ("1" * 64))
        os.replace(os.path.join(self.tmp, "scripts", "lib", "common.sh"),
                   os.path.join(self.root, "scripts", "lib", "common.sh"))
        status, _, _, error = self.install()
        self.assertEqual((status, error), (1, "checksum mismatch for https://example.invalid/x"))
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "opt")))

    def test_macos_13_is_refused(self):
        self.environ["MACOS"] = "13.6.1"
        self.assertEqual(self.install()[3], "macOS 14 (Sonoma) or later required")
        self.assertEqual([c[0] for c in self.calls()], ["sw_vers"])

    def test_the_reinstall_replaces_the_old_copy(self):
        self.install()
        stale = os.path.join(self.prefix, "opt", "sbx", VERSION, "stale")
        open(stale, "w").close()
        os.remove(os.path.join(self.prefix, "bin", "sbx"))
        self.assertEqual(self.install()[0], 0)
        self.assertFalse(os.path.exists(stale))


class PlatformTest(PrereqsTest):
    def test_other_machines_are_refused(self):
        for uname, text in ((("Linux", "aarch64"), "Linux/aarch64"), (("Darwin", "x86_64"), "Darwin/x86_64")):
            self.uname = uname
            self.assertEqual(self.install()[3], "this installer supports macOS (Apple silicon) or Ubuntu Linux "
                                                "(x86_64, including WSL2) only; found %s" % text)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
