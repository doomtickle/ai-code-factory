"""Tests for factory/commands/docs.py (factory docs [--print]). Ported from
scripts/test_factory_docs.sh."""

import io
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

from factory import cli
from factory.cli import COMMANDS
from factory.commands import docs
from factory.fixtures import REPO, CommandTest

STUB_DOCS = '''import os, sys
with open(os.environ["DOCS_STUB_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
html = "<!doctype html><title>stub docs</title>\\n"
if "--out" in sys.argv:
    with open(sys.argv[sys.argv.index("--out") + 1], "w") as f:
        f.write(html)
else:
    sys.stdout.write(html)
'''


class DocsTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.write("repo/scripts/docs.py", STUB_DOCS)
        self.log = os.path.join(self.tmp, "docs-calls")
        self.environ["DOCS_STUB_LOG"] = self.log
        # The opener stubs need mv and printf, but the host's own openers (xdg-open on a CI
        # runner, wslview, open) must stay invisible: PATH is the stub folder plus links to those two.
        tools = os.path.join(self.tmp, "host-tools")
        os.makedirs(tools)
        for name in ("mv", "printf"):
            os.symlink(shutil.which(name), os.path.join(tools, name))
        self.environ["PATH"] = os.path.join(self.tmp, "bin") + os.pathsep + tools
        self.opened = os.path.join(self.tmp, "opened")
        self.index = os.path.join(self.state, "docs", "index.html")
        self.system = mock.patch.object(docs, "system_name", return_value="Linux")
        self.system.start()
        self.addCleanup(self.system.stop)

    def docs_calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def opener(self, name):
        self.tool(name, 'echo "%s $*" > "%s.tmp" && mv "%s.tmp" "%s"\n' % ((name,) + (self.opened,) * 3))

    def wait_opened(self):
        for _ in range(50):
            if os.path.exists(self.opened):
                break
            time.sleep(0.1)
        try:
            with open(self.opened) as f:
                return f.read().strip()
        except OSError:
            return ""

    def run_docs(self, *args, tty=False, wsl=False):
        with mock.patch.object(docs, "is_wsl", return_value=wsl):
            return COMMANDS["docs"](self.ctx(tty=tty), list(args))

    # --- the HTML goes to stdout ---

    def test_print_hands_over_to_docs_py_with_the_root_and_no_out(self):
        self.assertEqual(self.run_docs("--print", tty=True), 0)
        self.assertEqual([a for a, _ in self.execs], [self.script("docs.py") + ["--root", self.root]])
        self.assertFalse(os.path.exists(self.index))

    def test_a_stdout_that_is_not_a_terminal_prints_too(self):
        self.run_docs()
        self.assertEqual([a for a, _ in self.execs], [self.script("docs.py") + ["--root", self.root]])
        self.assertEqual(self.docs_calls(), [])

    def test_an_unknown_argument_is_a_usage_error_and_runs_nothing(self):
        out, err = self.run_cli("docs", "--html")
        self.assertEqual(err, "error: usage: factory docs [--print]\n")
        self.assertEqual((out, self.docs_calls(), self.execs), ("", [], []))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(list(argv), root=self.root, environ=self.environ, stdout=out, stderr=err, execer=self.execer)
        self.assertEqual(rc, 1)
        return out.getvalue(), err.getvalue()

    # --- a terminal: write the page and open it ---

    def test_a_terminal_writes_the_page_and_opens_it_with_xdg_open(self):
        self.opener("xdg-open")
        self.assertEqual(self.run_docs(tty=True), 0)
        self.assertEqual(self.docs_calls(), ["--root %s --out %s" % (self.root, self.index)])
        with open(self.index) as f:
            self.assertIn("<title>stub docs</title>", f.read())
        self.assertEqual(self.wait_opened(), "xdg-open " + self.index)
        self.assertEqual(self.out.getvalue(), "Opening the docs in your browser: %s\n" % self.index)
        self.assertEqual(self.execs, [])

    def test_macos_opens_with_open(self):
        self.system.stop()
        with mock.patch.object(docs, "system_name", return_value="Darwin"):
            for name in ("open", "wslview", "xdg-open"):
                self.opener(name)
            self.run_docs(tty=True)
        self.assertEqual(self.wait_opened(), "open " + self.index)

    def test_wsl_opens_with_wslview_when_there_is_one(self):
        for name in ("open", "wslview", "xdg-open"):
            self.opener(name)
        self.run_docs(tty=True, wsl=True)
        self.assertEqual(self.wait_opened(), "wslview " + self.index)

    def test_wsl_is_also_detected_from_the_environment(self):
        for name in ("wslview", "xdg-open"):
            self.opener(name)
        self.environ["WSL_DISTRO_NAME"] = "Ubuntu"
        self.run_docs(tty=True, wsl=False)
        self.assertEqual(self.wait_opened(), "wslview " + self.index)

    def test_wsl_without_wslview_falls_back_to_xdg_open(self):
        self.opener("xdg-open")
        self.run_docs(tty=True, wsl=True)
        self.assertEqual(self.wait_opened(), "xdg-open " + self.index)

    def test_no_opener_says_where_the_page_is(self):
        self.assertEqual(self.run_docs(tty=True), 0)
        self.assertEqual(self.out.getvalue(), "Docs written to %s\nOpen that file in a web browser.\n" % self.index)
        self.assertTrue(os.path.exists(self.index))
        self.assertFalse(os.path.exists(self.opened))

    def test_the_state_folder_is_private(self):
        self.run_docs(tty=True)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.index)).st_mode), 0o700)

    def test_a_docs_py_failure_is_reported_and_nothing_is_opened(self):
        self.write("repo/scripts/docs.py", "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
        self.opener("xdg-open")
        with self.assertRaises(docs.FactoryError) as cm:
            self.run_docs(tty=True)
        self.assertEqual(str(cm.exception), "could not build the docs")
        self.assertEqual(self.err.getvalue(), "boom\n")
        self.assertFalse(os.path.exists(self.opened))

    # --- WSL with Windows interop ---

    def interop(self):
        """cmd.exe answers %TEMP% (with a CRLF, as on Windows), wslpath maps
        C:\\Users\\me\\Temp <-> <tmp>/winc/Users/me/Temp, and explorer.exe records what it
        was asked to open (exiting 1, as the real one does)."""
        win = os.path.join(self.tmp, "winc", "Users", "me", "Temp")
        os.makedirs(win)
        self.tool("cmd.exe", 'printf "C:\\\\Users\\\\me\\\\Temp\\r\\n"\n')
        self.tool("wslpath", 'case "$1" in\n  -u) echo "%s" ;;\n  -w) printf \'C:\\\\Users\\\\me\\\\Temp\\\\%%s\\n\' "${2##*/}" ;;\nesac\n' % win)
        self.tool("explorer.exe", 'printf "%%s\\n" "explorer.exe $*" > "%s.tmp" && mv "%s.tmp" "%s"\nexit 1\n'
                  % (self.opened, self.opened, self.opened))
        return win

    def test_wsl_with_windows_interop_opens_a_copy_in_the_windows_temp_folder(self):
        win = self.interop()
        self.opener("wslview")
        self.assertEqual(self.run_docs(tty=True, wsl=True), 0)
        self.assertEqual(self.wait_opened(), r"explorer.exe C:\Users\me\Temp\ai-code-factory-docs.html")
        with open(os.path.join(win, "ai-code-factory-docs.html")) as f:
            self.assertIn("<title>stub docs</title>", f.read())
        self.assertEqual(self.out.getvalue(), "Opening the docs in your Windows browser.\n"
                         "If no browser window appears, paste this into your browser's address bar:\n"
                         "  C:\\Users\\me\\Temp\\ai-code-factory-docs.html\n")

    def test_interop_that_does_not_answer_with_a_drive_path_falls_back(self):
        self.interop()
        self.tool("cmd.exe", 'printf "%TEMP%\\r\\n"\n')
        self.opener("wslview")
        self.run_docs(tty=True, wsl=True)
        self.assertEqual(self.wait_opened(), "wslview " + self.index)
        self.assertNotIn("Windows browser", self.out.getvalue())

    def test_a_windows_temp_folder_that_is_not_there_falls_back(self):
        win = self.interop()
        os.rmdir(win)
        self.opener("wslview")
        self.run_docs(tty=True, wsl=True)
        self.assertEqual(self.wait_opened(), "wslview " + self.index)

    def test_without_wsl_the_windows_tools_are_not_used(self):
        self.interop()
        self.opener("xdg-open")
        self.run_docs(tty=True, wsl=False)
        self.assertEqual(self.wait_opened(), "xdg-open " + self.index)


class IsWslTest(CommandTest):
    def test_proc_version(self):
        for text, want in (("Linux version 5.15 (microsoft-standard-WSL2)\n", True), ("Linux version 6.1 (Debian)\n", False),
                           ("Linux version 5.4 Microsoft\n", True)):
            path = self.write("proc-version", text)
            self.assertEqual(docs.is_wsl(path), want, text)
        self.assertFalse(docs.is_wsl(os.path.join(self.tmp, "missing")))


class RealRenderTest(CommandTest):
    def test_factory_docs_print_renders_the_real_docs(self):
        env = dict(os.environ, HOME=self.tmp, XDG_STATE_HOME=os.path.join(self.tmp, "state"),
                   PYTHONDONTWRITEBYTECODE="1")
        cp = subprocess.run([os.path.join(REPO, "bin", "factory"), "docs", "--print"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual((cp.returncode, cp.stderr), (0, b""))
        self.assertIn("<!doctype html", cp.stdout[:200].decode().lower())
        self.assertIn("</html>", cp.stdout[-200:].decode())

    def test_it_works_on_a_fresh_clone_with_no_broker_folder(self):
        # docs needs only python3: no sbx, no docker, no broker/.
        self.assertTrue(os.path.isdir(os.path.join(REPO, "broker")))
        cp = subprocess.run([sys.executable, "-m", "factory", "docs", "--print"], cwd=REPO, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=dict(os.environ, PATH="/nonexistent", HOME=self.tmp,
                                                             PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(cp.returncode, 0, cp.stderr)


if __name__ == "__main__":
    unittest.main()
