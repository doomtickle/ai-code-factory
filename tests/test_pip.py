"""pip_download: requirements parsing and the broker-private requirements file."""

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core import config
from passdesk.core.errors import Denied
from passdesk.tools import pip

from .base import POLICY, Base

H1, H2 = "a" * 64, "b" * 64


class RequirementsTests(Base):
    def test_pinned_ok(self):
        pins = pip.parse_requirements(
            f"# comment\nrequests==2.32.3 \\\n    --hash=sha256:{H1} \\\n    --hash=sha256:{H2}\n"
            f"idna[x]==3.7 --hash=sha256:{H1}  # trailing\n"
            f"tomli==2.0.1 --hash=sha256:{H2} ; python_version < \"3.11\"\n")
        self.assertEqual(pins, [pip.Pin("requests", "2.32.3", (H1, H2), ""),
                                pip.Pin("idna", "3.7", (H1,), ""),
                                pip.Pin("tomli", "2.0.1", (H2,), 'python_version < "3.11"')])

    def test_rejects_unpinned_and_options(self):
        for bad in ["requests>=2", "requests", f"--index-url https://evil/simple\nx==1 --hash=sha256:{H1}",
                    "-e git+https://github.com/x/y", "git+https://x/y.git", "./local", "x==1 --no-binary :all:",
                    f"x==1 --hash=sha256:{H1}\n--no-binary :all:", f"x==1 --hash=sha256:{H1}\n--no-binary=:all:",
                    f"x==1 --hash=sha256:{H1}\n--find-links /ws/evil", f"x==1 --hash=sha256:{H1}\n-r other.txt",
                    f"x==1 --hash=sha256:{H1}\n-c constraints.txt", f"x==1 --hash=sha256:{H1} --no-binary :all:",
                    f"x==1 --hash=sha256:{H1} ; os_name == 'x' --no-binary :all:", f"x===1 --hash=sha256:{H1}"]:
            with self.assertRaises(Denied, msg=bad):
                pip.parse_requirements(bad)

    def test_hash_is_required(self):
        with self.assertRaisesRegex(Denied, "line 2 has no --hash.*--generate-hashes"):
            pip.parse_requirements(f"a==1 --hash=sha256:{H1}\nb==2\n")
        with self.assertRaises(Denied):
            pip.parse_requirements("a==1 --hash=sha512:" + "c" * 128)

    def test_option_lines_say_why(self):
        with self.assertRaisesRegex(Denied, "line 1 is a pip option, which pip_download refuses"):
            pip.parse_requirements(f"--no-binary :all:\nx==1 --hash=sha256:{H1}")

    def test_render_has_only_the_parsed_pins(self):
        pins = pip.parse_requirements(f"Foo[bar]==1.0 --hash=sha256:{H1} --hash=sha256:{H2} ; sys_platform == 'linux'")
        self.assertEqual(pip.render_requirements(pins),
                         f"Foo==1.0; sys_platform == 'linux' --hash=sha256:{H1} --hash=sha256:{H2}\n")


class PipDownloadTests(Base):
    REQS = f"requests==2.32.3 --hash=sha256:{H1}\n"

    def setUp(self):
        super().setUp()
        self.req = config.WORKSPACE / "requirements.txt"
        self.req.write_text(self.REQS)
        self.calls = []

    def fake_run(self, cmd, cwd=None, timeout=None, extra_env=None):
        """Stands in for pip: records what it was given, swaps the workspace file
        the way a racing agent would, and 'downloads' one wheel."""
        req = Path(cmd[cmd.index("-r") + 1])
        self.calls.append({"cmd": cmd, "cwd": cwd, "req": req, "req_text": req.read_text()})
        self.req.write_text("--no-binary :all:\n--no-index\n--find-links .\nrequests==2.32.3\n")
        dest = Path(cmd[cmd.index("-d") + 1])
        (dest / "requests-2.32.3-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(cmd, 0, "Saved requests-2.32.3-py3-none-any.whl\n", "")

    def download(self, **args) -> str:
        with mock.patch.object(pip, "run", side_effect=self.fake_run):
            return pip.tool_pip_download(args, POLICY)

    def test_pip_reads_a_broker_private_copy_never_the_workspace_file(self):
        out = self.download()
        self.assertIn("Downloaded 1 wheel(s) to .factory/wheels", out)
        self.assertIn("-r requirements.txt", out)
        call, = self.calls
        ws = os.path.realpath(config.WORKSPACE)
        for p in (call["req"], Path(call["cmd"][call["cmd"].index("-d") + 1]), Path(call["cwd"])):
            self.assertFalse(os.path.realpath(p).startswith(ws + os.sep), f"pip was given workspace path {p}")
        self.assertEqual(call["req_text"], f"requests==2.32.3 --hash=sha256:{H1}\n")
        for flag in ("--isolated", "--only-binary=:all:", "--no-deps", "--require-hashes"):
            self.assertIn(flag, call["cmd"])
        self.assertFalse(call["req"].exists(), "the private requirements file outlives the call")
        wheel = config.WORKSPACE / ".factory/wheels/requests-2.32.3-py3-none-any.whl"
        self.assertEqual(wheel.read_bytes(), b"wheel")

    def test_unhashed_requirements_are_refused_before_pip_runs(self):
        self.req.write_text("requests==2.32.3\n")
        with self.assertRaisesRegex(Denied, "no --hash"):
            self.download()
        self.assertEqual(self.calls, [])

    def test_symlinked_requirements_file_is_refused(self):
        (config.WORKSPACE / "real.txt").write_text(self.REQS)
        os.symlink("real.txt", config.WORKSPACE / "linked.txt")
        with self.assertRaisesRegex(Denied, "symlink"):
            self.download(requirements="linked.txt")
        self.assertEqual(self.calls, [])

    def test_requirements_must_be_a_regular_file(self):
        os.mkfifo(config.WORKSPACE / "fifo.txt")
        with self.assertRaisesRegex(Denied, "not a regular file"):
            self.download(requirements="fifo.txt")

    def test_symlinked_wheels_dir_is_refused(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (config.WORKSPACE / ".factory").mkdir()
        os.symlink(outside, config.WORKSPACE / ".factory/wheels")
        with self.assertRaisesRegex(Denied, "symlink"):
            self.download()
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
