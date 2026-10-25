"""Tests for factory/commands/secret.py (factory secret save, forget, set, rm, ls). Ported from
parts 1 to 4b, 8 and 9 of scripts/test_factory_secret.sh (the helpers it also covered are in
factory/test_secrets.py), plus the output contract: the exact stdout, stderr and exit status
the bash gave.

Each test runs the real entry point as a subprocess, with the value piped on stdin (or typed
into a pseudo-terminal), against the fakes of factory/test_secrets.py. Every test that hands
over a value checks that it reached the right program's stdin and nothing else: no argv, no
environment, no output, no file under the scratch folder.
"""

import fcntl
import os
import pty
import select
import shutil
import subprocess
import sys
import termios
import time

from factory.fixtures import REPO
from factory.test_secrets import GITHUB_LISTED, VALUE, SecretFakes

FACTORY = [sys.executable, os.path.join(REPO, "factory")]
NAMES = "jira gemini npm-registry gitlab approvals-webhook"
SET_OK = "  ✓ delivered '%s' to the broker sandbox; 'factory start' will no longer touch it from the keychain\n"


class SecretTest(SecretFakes):
    def factory(self, *args, stdin=b"", **env):
        """(status, stdout, stderr) of `factory secret ARGS`; the fakes' logs are cleared first."""
        self.reset()
        if isinstance(stdin, str):
            stdin = stdin.encode()
        cp = subprocess.run(FACTORY + ["secret"] + list(args), input=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=dict(self.environ, **env), cwd=self.tmp)
        return cp.returncode, cp.stdout.decode(), cp.stderr.decode()

    def sbx_calls(self):
        return self.text("sbx-args.log")


class NamesTest(SecretTest):
    def test_a_bad_name_is_refused_before_sbx_or_a_keychain_is_asked(self):
        for verb in ("set", "rm", "save", "forget"):
            for name in ("../etc-passwd", "not-a-real-secret", "jira gemini", "passdesk-license", "", "-x", "--help"):
                with self.subTest(verb=verb, name=name):
                    status, out, err = self.factory(verb, name, stdin="lic-key-xyz\n", FACTORY_KEYCHAIN="wincred")
                    names = NAMES + (" github" if verb in ("save", "forget") else "")
                    self.assertEqual((status, out, err), (1, "", "error: unknown secret '%s' (one of: %s)\n" % (name, names)))
                    self.assertEqual(self.sbx_calls() + self.text("ps-args.log"), "")
        self.assert_not_leaked("lic-key-xyz")

    def test_github_is_not_a_file_set_can_deliver(self):
        status, _, err = self.factory("set", "github", stdin="x\n")
        self.assertEqual((status, self.sbx_calls()), (1, ""))
        self.assertIn("unknown secret 'github'", err)

    def test_usage(self):
        usage = "error: usage: factory secret <save|forget|set|rm|ls> <name>\n"
        self.assertEqual(self.factory(), (1, "", usage))
        self.assertEqual(self.factory("show", "jira"), (1, "", usage))
        for verb in ("set", "rm", "save", "forget"):
            self.assertEqual(self.factory(verb), (1, "", "error: usage: factory secret %s <name>\n" % verb))
            self.assertEqual(self.factory(verb, "jira", "gemini"),
                             (1, "", "error: usage: factory secret %s <name>\n" % verb))
        self.assertEqual(self.sbx_calls(), "")


class SetTest(SecretTest):
    def test_the_value_goes_to_the_broker_on_stdin_only(self):
        status, out, err = self.factory("set", "jira", stdin="topsecret123\n")
        self.assertEqual((status, out, err), (0, SET_OK % "jira", ""))
        self.assertEqual(self.logged("sbx-stdin"), b"topsecret123")  # one trailing newline dropped
        self.assertIn("exec -i factory-broker sh -c", self.sbx_calls())
        self.assertEqual(self.argv("sbx-argv.bin")[-1][-2:], ["_", "jira"])
        self.assert_not_leaked("topsecret123", out, err)

    def test_only_one_trailing_newline_is_dropped(self):
        self.factory("set", "npm-registry", stdin="line1\nline2\n\n")
        self.assertEqual(self.logged("sbx-stdin"), b"line1\nline2\n")
        self.factory("set", "npm-registry", stdin="line1\nline2")
        self.assertEqual(self.logged("sbx-stdin"), b"line1\nline2")
        self.assert_not_leaked("line1")

    def test_an_empty_value_is_refused(self):
        for value in ("", "\n"):
            with self.subTest(value=value):
                self.assertEqual(self.factory("set", "gemini", stdin=value), (1, "", "error: empty value for 'gemini'\n"))
                self.assertNotIn("exec -i", self.sbx_calls())
                self.assertFalse(os.path.exists(os.path.join(self.markers, "gemini")))

    def test_set_marks_the_name_and_rm_clears_it(self):
        self.assertEqual(self.factory("set", "gitlab", stdin="gl-tok-xyz\n")[0], 0)
        marker = os.path.join(self.markers, "gitlab")
        self.assertEqual(os.path.getsize(marker), 0)  # never the value
        self.assertEqual(os.stat(self.markers).st_mode & 0o777, 0o700)
        self.assert_not_leaked("gl-tok-xyz")
        self.assertEqual(self.factory("ls"), (0, "  gitlab\n", ""))
        self.assertEqual(self.factory("rm", "gitlab"),
                         (0, "  ✓ removed 'gitlab' from the broker sandbox\n", ""))
        self.assertIn("exec factory-broker rm -f /etc/factory-broker/secrets/gitlab", self.sbx_calls())
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.factory("ls"),
                         (0, "No secrets are managed with 'factory secret set' (see: factory secret).\n", ""))

    def test_a_failed_delivery_leaves_no_marker(self):
        status, out, err = self.factory("set", "gemini", stdin="willnotarrive\n", FAKE_SBX_DELIVER_FAIL="1")
        self.assertEqual((status, out, err), (1, "", "error: could not deliver 'gemini' to the broker sandbox\n"))
        self.assertFalse(os.path.exists(os.path.join(self.markers, "gemini")))
        self.assert_not_leaked("willnotarrive", out, err)

    def test_a_marker_that_cannot_be_written_is_an_error(self):
        self.write("state/ai-code-factory/secrets-manual", "")
        status, out, err = self.factory("set", "jira", stdin="v\n")
        self.assertEqual((status, out), (1, ""))
        self.assertEqual(err, "error: delivered 'jira' but could not record it as managed by 'factory secret'; "
                              "the next 'factory start' may remove it\n")

    def test_the_broker_must_be_running(self):
        for env, err in (({"FAKE_SBX_STATE": "stopped"}, "the broker sandbox is not running; run 'factory start' first"),
                         ({"FAKE_SBX_VERSION": "0.1.0"}, "sbx 0.45.1 required, found '0.1.0' (see README.md)")):
            for verb in ("set", "rm"):
                with self.subTest(verb=verb, env=env):
                    self.assertEqual(self.factory(verb, "jira", stdin="v\n", **env), (1, "", "error: %s\n" % err))
                    self.assertNotIn("exec", self.sbx_calls())
        os.remove(os.path.join(self.tmp, "bin", "sbx"))
        sysdir = os.path.join(self.tmp, "sys")
        os.makedirs(sysdir)
        self.assertEqual(self.factory("set", "jira", stdin="v\n", PATH=os.path.join(self.tmp, "bin") + os.pathsep + sysdir),
                         (1, "", "error: 'sbx' not found in PATH (see README.md)\n"))

    def test_rm_without_a_marker_and_rm_failing(self):
        self.assertEqual(self.factory("rm", "approvals-webhook")[0], 0)
        self.assertIn("exec factory-broker rm -f /etc/factory-broker/secrets/approvals-webhook", self.sbx_calls())
        self.mark("jira")
        self.assertEqual(self.factory("rm", "jira", FAKE_SBX_RM_FAIL="1"),
                         (1, "", "error: could not remove 'jira' from the broker sandbox; it may still be active\n"))
        self.assertTrue(os.path.exists(os.path.join(self.markers, "jira")))

    def test_rm_warns_when_the_keychain_would_deliver_it_again(self):
        status, out, err = self.factory("rm", "jira", FACTORY_KEYCHAIN="secret-tool", FAKE_KEYRING="found")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, "  ✓ removed 'jira' from the broker sandbox\n"
                              "  ! a keychain entry for 'factory-jira' still exists; the next 'factory start' will "
                              "deliver it again. To prevent that: factory secret forget jira\n")

    def test_ls_ignores_dotfiles(self):
        self.mark("npm-registry", "gemini", ".hidden")
        self.assertEqual(self.factory("ls"), (0, "  gemini\n  npm-registry\n", ""))


class TerminalTest(SecretTest):
    def test_a_typed_value_is_not_echoed(self):
        """The value typed at the hidden prompt: the prompt on stderr, nothing echoed back to
        the terminal, a newline after it, and the value on sbx's stdin."""
        master, slave = pty.openpty()
        self.reset()
        proc = subprocess.Popen(FACTORY + ["secret", "set", "jira"], stdin=slave, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=dict(self.environ), cwd=self.tmp)
        os.close(slave)
        prompt = b"Value for jira (input hidden): "
        got = b""
        deadline = time.time() + 20
        while not got.endswith(prompt) and time.time() < deadline:
            ready, _, _ = select.select([proc.stderr], [], [], 1)
            if ready:
                got += os.read(proc.stderr.fileno(), 1)
        self.assertEqual(got, prompt)
        os.write(master, b"typed-secret-7\n")
        out, err = proc.communicate(timeout=20)
        echoed = b""
        while select.select([master], [], [], 0.2)[0]:
            try:
                chunk = os.read(master, 1024)
            except OSError:
                break
            if not chunk:
                break
            echoed += chunk
        os.close(master)
        self.assertEqual((proc.returncode, out.decode(), err), (0, SET_OK % "jira", b"\n"))
        self.assertNotIn(b"typed-secret-7", echoed)
        self.assertEqual(self.logged("sbx-stdin"), b"typed-secret-7")
        self.assert_not_leaked("typed-secret-7")

    def test_ctrl_c_at_the_prompt_restores_echo_and_exits_130(self):
        """Ctrl-C typed at the hidden prompt (the pty is the command's controlling terminal, so
        the terminal driver sends SIGINT): echo is on again, the status is 130, the prompt line
        is ended, and nothing is delivered."""
        master, slave = pty.openpty()
        self.assertTrue(termios.tcgetattr(slave)[3] & termios.ECHO)
        self.reset()
        proc = subprocess.Popen(FACTORY + ["secret", "set", "jira"], stdin=slave, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=dict(self.environ), cwd=self.tmp, start_new_session=True,
                                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        prompt = b"Value for jira (input hidden): "
        got = b""
        deadline = time.time() + 20
        while not got.endswith(prompt) and time.time() < deadline:
            ready, _, _ = select.select([proc.stderr], [], [], 1)
            if ready:
                got += os.read(proc.stderr.fileno(), 1)
        self.assertEqual(got, prompt)
        self.assertFalse(termios.tcgetattr(slave)[3] & termios.ECHO)  # off while it waits
        os.write(master, b"half-typed\x03")
        out, err = proc.communicate(timeout=20)
        restored = termios.tcgetattr(slave)[3] & termios.ECHO
        os.close(slave)
        os.close(master)
        self.assertTrue(restored)
        self.assertEqual((proc.returncode, out, err), (130, b"", b"\n"))
        self.assertNotIn("exec", self.sbx_calls())
        self.assert_not_leaked("half-typed")


class SaveTest(SecretTest):
    def test_wincred_save_delivers_to_the_running_broker(self):
        self.mark("jira")
        status, out, err = self.factory("save", "jira", stdin="wc-secret-1\n", FACTORY_KEYCHAIN="wincred")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, "  ✓ saved factory-jira in the keychain (wincred); every broker start delivers it from now on\n"
                              "  ✓ delivered 'jira' to the running broker\n")
        self.assertEqual(self.logged("ps-stdin"), b"wc-secret-1")
        self.assertIn("Retrieve('factory-jira'", self.text("ps-args.log"))
        self.assertEqual(self.logged("sbx-stdin"), b"wc-secret-1")
        self.assertFalse(os.path.exists(os.path.join(self.markers, "jira")))  # the keychain manages it now
        self.assert_not_leaked("wc-secret-1", out, err)

    def test_secret_tool_save(self):
        status, out, err = self.factory("save", "gitlab", stdin="st-secret-3\n", FACTORY_KEYCHAIN="secret-tool",
                                        FAKE_KEYRING="found")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(self.argv("keyring-argv.bin")[0],
                         ["store", "--label", "AI Code Factory: factory-gitlab", "service", "factory-gitlab"])
        self.assertEqual(self.logged("keyring-stdin"), b"st-secret-3")
        self.assert_not_leaked("st-secret-3", out, err)

    def test_github_becomes_the_broker_scoped_sbx_secret(self):
        status, out, err = self.factory("save", "github", stdin="gh-token-abc\n", FACTORY_KEYCHAIN="wincred")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, "  ✓ saved factory-github in the keychain (wincred); every broker start delivers it from now on\n"
                              "GitHub token for factory-broker restored from the keychain (factory-github)\n")
        self.assertIn("secret set -f github --sandbox factory-broker", self.sbx_calls())
        self.assertEqual(self.logged("sbx-secret-stdin"), b"gh-token-abc")
        set_calls = [c for c in self.argv("sbx-argv.bin") if c[:2] == ["secret", "set"]]
        self.assertTrue(set_calls)
        self.assertTrue(all(c[-2:] == ["--sandbox", "factory-broker"] for c in set_calls))  # never global
        self.assert_not_leaked("gh-token-abc", out, err)

    def test_with_the_broker_stopped(self):
        status, out, err = self.factory("save", "gemini", stdin="v-1\n", FACTORY_KEYCHAIN="wincred", FAKE_SBX_STATE="stopped")
        self.assertEqual((status, err), (0, ""))
        self.assertTrue(out.endswith("The broker isn't running; 'factory start' will deliver it.\n"))
        self.assertNotIn("exec", self.sbx_calls())

    def test_an_empty_value_is_never_stored(self):
        self.assertEqual(self.factory("save", "gemini", stdin="", FACTORY_KEYCHAIN="wincred"),
                         (1, "", "error: empty value for 'gemini'\n"))
        self.assertEqual(self.text("ps-args.log"), "")

    def test_failures(self):
        self.assertEqual(self.factory("save", "jira", stdin="v\n", FACTORY_KEYCHAIN="wincred", FAKE_PS_FAIL="1"),
                         (1, "", "powershell.exe: cannot start\nerror: could not save factory-jira in the keychain (wincred)\n"))
        self.assertEqual(self.factory("save", "jira", stdin="v\n", FACTORY_KEYCHAIN="secret-tool", FAKE_KEYRING="locked"),
                         (1, "", "error: saved factory-jira, but reading it back failed\n"))

    def test_no_keychain(self):
        status, out, err = self.factory("save", "jira", stdin="x", FACTORY_KEYCHAIN="none")
        self.assertEqual((status, out), (1, ""))
        self.assertEqual(err, "error: this machine has no keychain the factory can use (macOS Keychain, secret-tool, or "
                              "Windows Credential Manager on WSL2); use 'factory secret set jira' after each broker "
                              "start instead\n")
        self.assertEqual(self.text("ps-args.log"), "")

    def test_macos_needs_a_terminal(self):
        self.assertEqual(self.factory("save", "jira", stdin="x", FACTORY_KEYCHAIN="macos"),
                         (1, "", "error: on macOS run 'factory secret save jira' in a terminal: "
                                 "Keychain prompts for the value itself\n"))
        self.assertEqual(self.text("keyring-args.log"), "")

    def test_macos_lets_security_prompt_for_the_value(self):
        master, slave = pty.openpty()
        self.reset()
        proc = subprocess.run(FACTORY + ["secret", "save", "jira"], stdin=slave, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=dict(self.environ, FACTORY_KEYCHAIN="macos", FAKE_KEYRING="found"),
                              cwd=self.tmp, timeout=30)
        os.close(slave)
        os.close(master)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertTrue(proc.stdout.decode().startswith("Keychain will ask for the value of jira twice.\n"
                                                        "  ✓ saved factory-jira in the keychain (macos)"))
        self.assertEqual(self.argv("keyring-argv.bin")[0],
                         ["add-generic-password", "-U", "-s", "factory-jira", "-a", "factory", "-w"])


class ForgetTest(SecretTest):
    """forget deletes the keychain entry and the broker's copy, and says truthfully what
    happened to each; the broker's copy goes first even when the entry can't be deleted."""

    def forget(self, name, mode, **env):
        return self.factory("forget", name, FACTORY_KEYCHAIN="secret-tool", FAKE_KEYRING=mode, **env)

    def test_wincred_forget(self):
        self.store_wincred("factory-jira", "x")
        status, out, err = self.factory("forget", "jira", FACTORY_KEYCHAIN="wincred")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, "  ✓ deleted factory-jira from the keychain\n"
                              "  ✓ removed 'jira' from the broker sandbox\n")
        self.assertFalse(os.path.exists(os.path.join(self.log, "wincred", "factory-jira")))
        self.assertIn("exec factory-broker rm -f /etc/factory-broker/secrets/jira", self.sbx_calls())

    def test_locked(self):
        status, out, err = self.forget("jira", "locked")
        self.assertEqual((status, out), (1, "  ✓ removed 'jira' from the broker sandbox\n"))
        self.assertEqual(err, "error: the broker's copy of 'jira' is revoked, but factory-jira could not be deleted from the "
                              "keychain (the keychain is locked or unavailable). Unlock it, then run 'factory secret "
                              "forget jira' again\n")
        status, out, err = self.forget("github", "locked")
        self.assertEqual((status, out), (1, "  ✓ removed the broker's GitHub token\n"))
        self.assertIn("secret rm github --sandbox factory-broker -f", self.sbx_calls())
        self.assertIn("Unlock it, then run 'factory secret forget github' again", err)
        self.assert_not_leaked(VALUE, out, err)

    def test_a_failed_delete(self):
        for name in ("jira", "github"):
            with self.subTest(name=name):
                status, out, err = self.forget(name, "found")
                self.assertEqual(status, 1)
                self.assertEqual(err, "error: the broker's copy of '%s' is revoked, but factory-%s could not be deleted "
                                      "from the keychain (the delete failed, so the next 'factory start' would deliver it "
                                      "again). Delete it by hand (e.g. in your keychain app), then run 'factory secret "
                                      "forget %s' again\n" % (name, name, name))
                self.assertNotIn("deleted factory-%s from" % name, out)
                self.assert_not_leaked(VALUE, out, err)

    def test_the_message_matches_what_happened_to_the_broker_copy(self):
        for mode, problem in (("locked", "the keychain is locked or unavailable"),
                              ("found", "the delete failed, so the next 'factory start' would deliver it again")):
            with self.subTest(mode=mode):
                status, out, err = self.forget("jira", mode, FAKE_SBX_STATE="stopped")
                self.assertEqual((status, out), (1, ""))
                self.assertNotIn("rm -f", self.sbx_calls())
                self.assertIn("the broker isn't running, so its copy of 'jira' was NOT touched and may still be active "
                              "in the stopped broker; factory-jira could not be deleted from the keychain either (%s)"
                              % problem, err)
                self.assertTrue(err.endswith("then run 'factory secret forget jira' again before the next 'factory start'\n"))

                status, out, err = self.forget("jira", mode, FAKE_SBX_RM_FAIL="1")
                self.assertEqual((status, out), (1, ""))
                self.assertTrue(err.startswith("error: could not remove 'jira' from the broker sandbox, so it may still be "
                                               "active; factory-jira could not be deleted from the keychain either (%s)."
                                               % problem))

                status, out, err = self.forget("github", mode, FAKE_SBX_RM_FAIL="1", FAKE_SBX_SECRETS=GITHUB_LISTED)
                self.assertEqual((status, out), (1, ""))
                self.assertIn("may still be active", err)
                self.assertNotIn("is revoked", err)

    def test_deleted(self):
        self.assertEqual(self.forget("jira", "absent"),
                         (0, "  ✓ deleted factory-jira from the keychain\n"
                             "  ✓ removed 'jira' from the broker sandbox\n", ""))
        status, out, err = self.forget("github", "absent", FAKE_SBX_RM_FAIL="1", FAKE_SBX_SECRETS=GITHUB_LISTED)
        self.assertEqual((status, out), (1, "  ✓ deleted factory-github from the keychain\n"))
        self.assertEqual(err, "error: deleted factory-github from the keychain, but could not remove 'github' from the "
                              "broker sandbox; it may still be active\n")
        # sbx refusing because nothing is stored is not a failure.
        self.assertEqual(self.forget("github", "absent", FAKE_SBX_RM_FAIL="1", FAKE_SBX_SECRETS="SCOPE  SECRET  SERVICE"),
                         (0, "  ✓ deleted factory-github from the keychain\n"
                             "  ✓ removed the broker's GitHub token\n", ""))
        self.assertEqual(self.forget("jira", "absent", FAKE_SBX_STATE="stopped"),
                         (0, "  ✓ deleted factory-jira from the keychain\n"
                             "  ! the broker isn't running, so its copy of 'jira' was not touched and may still be active; "
                             "start the broker and run 'factory secret forget jira' again to remove it\n", ""))

    def test_forget_clears_the_marker(self):
        self.mark("jira")
        self.assertEqual(self.forget("jira", "absent")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.markers, "jira")))

    def test_bin_factory_runs_the_python(self):
        bash = shutil.which("bash")
        cp = subprocess.run([bash, os.path.join(REPO, "bin", "factory"), "secret", "ls"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=dict(self.environ, PATH=os.path.dirname(sys.executable) + os.pathsep
                                                             + self.environ["PATH"]), cwd=self.tmp)
        self.assertEqual((cp.returncode, cp.stdout.decode(), cp.stderr.decode()),
                         (0, "No secrets are managed with 'factory secret set' (see: factory secret).\n", ""))
