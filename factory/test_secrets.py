"""Tests for factory/secrets.py, the port of the scripts/lib/secrets.sh helpers `factory secret`
uses. The bash stays while scripts/start-broker.sh sources it, so each helper runs in bash and in
Python against the same fake sbx, secret-tool, security and powershell.exe, and must give the
same status, value, messages and the same argv, byte for byte. Ported from parts 4c, 5, 8 and 9
of scripts/test_factory_secret.sh, which also covered secret_clear_all_manual and
keychain_warn_missing: they have no Python copy yet (start-broker.sh and `factory status` call
them), so they are tested here in bash alone.

Nothing here needs sbx, a keychain or a network: every program is a fake on the PATH that logs
its argv and environment and keeps what it reads on stdin in a file of its own.
"""

import os
import subprocess

from factory import secrets
from factory.fixtures import REPO, CommandTest

# Records "$*" (one line a call) and the exact argv (NUL after each argument, \1 after each
# call), and its environment; keeps the stdin of `exec -i` and `secret set`. Its broker is
# running unless FAKE_SBX_STATE says otherwise; FAKE_SBX_*_FAIL make calls fail.
FAKE_SBX = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG/sbx-args.log"
{ printf '%s\0' "$@"; printf '\001'; } >> "$FAKE_LOG/sbx-argv.bin"
env >> "$FAKE_LOG/sbx-env.log"
case "$1" in
  version) echo "sbx version: v${FAKE_SBX_VERSION:-0.45.1} testcommit" ;;
  ls) echo "factory-broker   template   ${FAKE_SBX_STATE:-running}" ;;
  exec)
    if [ "$2" = -i ]; then
      cat > "$FAKE_LOG/sbx-stdin"
      [ -z "${FAKE_SBX_DELIVER_FAIL:-}" ] || exit 1
      [ -z "${FAKE_SBX_SAYS:-}" ] || echo "$FAKE_SBX_SAYS"
    else
      [ -z "${FAKE_SBX_RM_FAIL:-}" ] || exit 1
    fi ;;
  secret)
    case "${2:-}" in
      set) cat > "$FAKE_LOG/sbx-secret-stdin"; [ -z "${FAKE_SBX_SET_FAIL:-}" ] || exit 1 ;;
      rm) [ -z "${FAKE_SBX_RM_FAIL:-}" ] || exit 1 ;;
      ls) printf '%s\n' "${FAKE_SBX_SECRETS:-}" ;;
    esac ;;
esac
exit 0
"""

# FAKE_KEYRING picks what secret-tool and security report:
#   found          the entry exists and is readable (the value is in FAKE_KEYRING_FILE: not
#                  in the environment, which the leak checks read);
#   absent         no such entry (secret-tool: silent exit 1, empty search;
#                  security: exit 44 "could not be found");
#   locked         an error (secret-tool: D-Bus message on stderr, exit 1;
#                  security: exit 36, "User interaction is not allowed");
#   locked-silent  secret-tool only: the unlock prompt was dismissed, so lookup exits 1
#                  silently but search lists the locked item;
#   search-error   secret-tool only: lookup exits 1 silently, then search fails with a
#                  D-Bus error after printing a "secret = <value>" line;
#   search-silent  secret-tool only: lookup and search both exit 1 silently.
# `store` keeps its stdin; `clear` and `delete-generic-password` do nothing, so a deleted
# entry is still "found" (a failed delete) unless the test changes FAKE_KEYRING.
FAKE_SECRET_TOOL = r"""#!/usr/bin/env bash
{ printf '%s\0' "$@"; printf '\001'; } >> "$FAKE_LOG/keyring-argv.bin"
printf '%s\n' "$*" >> "$FAKE_LOG/keyring-args.log"
env >> "$FAKE_LOG/keyring-env.log"
case "$1:$FAKE_KEYRING" in
  store:*) cat > "$FAKE_LOG/keyring-stdin"; exit "${FAKE_STORE_STATUS:-0}" ;;
  clear:*) exit 0 ;;
  lookup:found) cat "$FAKE_KEYRING_FILE" ;;
  lookup:absent|lookup:locked-silent|lookup:search-error|lookup:search-silent) exit 1 ;;
  lookup:locked|search:locked)
    echo "secret-tool: Cannot autolaunch D-Bus without X11 \$DISPLAY" >&2; exit 1 ;;
  search:found) printf '[/org/freedesktop/secrets/collection/login/7]\nsecret = '; cat "$FAKE_KEYRING_FILE" ;;
  search:absent) ;;
  search:locked-silent) printf '[/org/freedesktop/secrets/collection/login/7]\nlabel = x\n' ;;
  search:search-error)
    printf 'secret = '; cat "$FAKE_KEYRING_FILE"
    echo "secret-tool: org.freedesktop.DBus.Error.NoReply: Did not receive a reply" >&2; exit 1 ;;
  search:search-silent) exit 1 ;;
  *) exit 1 ;;
esac
"""

FAKE_SECURITY = r"""#!/usr/bin/env bash
{ printf '%s\0' "$@"; printf '\001'; } >> "$FAKE_LOG/keyring-argv.bin"
printf '%s\n' "$*" >> "$FAKE_LOG/keyring-args.log"
case "$1" in
  delete-generic-password) exit 0 ;;
  add-generic-password) exit "${FAKE_STORE_STATUS:-0}" ;;
esac
case "$FAKE_KEYRING" in
  found) case " $* " in *" -w "*) cat "$FAKE_KEYRING_FILE" ;; esac ;;
  absent)
    echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2
    exit 44 ;;
  *) echo "security: SecKeychainItemCopyContent: User interaction is not allowed." >&2; exit 36 ;;
esac
"""

# Reads the operation and service from the script it is given and keeps a store of its own
# under wincred/<service>; FAKE_PS_FAIL makes it fail to start.
FAKE_POWERSHELL = r"""#!/usr/bin/env bash
{ printf '%s\0' "$@"; printf '\001'; } >> "$FAKE_LOG/ps-argv.bin"
printf '%s\n' "$*" >> "$FAKE_LOG/ps-args.log"
env >> "$FAKE_LOG/ps-env.log"
if [ -n "${FAKE_PS_FAIL:-}" ]; then echo "powershell.exe: cannot start" >&2; exit 1; fi
script="${*: -1}"
op="$(printf '%s' "$script" | sed -n 's/.*op=\([a-z]*\).*/\1/p' | head -n1)"
svc="$(printf '%s' "$script" | grep -o "Retrieve('factory-[a-z-]*'" | head -n1 | sed "s/Retrieve('//; s/'//")"
case "$op" in
  lookup) [ ! -f "$FAKE_LOG/wincred/$svc" ] || cat "$FAKE_LOG/wincred/$svc" ;;
  store) cat > "$FAKE_LOG/ps-stdin"; cp "$FAKE_LOG/ps-stdin" "$FAKE_LOG/wincred/$svc" ;;
  delete) [ -n "${FAKE_PS_KEEP:-}" ] || rm -f "$FAKE_LOG/wincred/$svc" ;;
esac
"""

VALUE = "kr-secret-9"
GITHUB_LISTED = "SCOPE  SECRET  SERVICE\nfactory-broker   -   github"


class SecretFakes(CommandTest):
    """A scratch state folder, the fakes above first on the PATH, and no keychain unless a
    test picks one (FACTORY_KEYCHAIN), so this machine's own is never asked."""

    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp, "log")
        os.makedirs(os.path.join(self.log, "wincred"))
        self.tool("sbx", FAKE_SBX)
        self.tool("secret-tool", FAKE_SECRET_TOOL)
        self.tool("security", FAKE_SECURITY)
        self.powershell = self.tool("powershell.exe", FAKE_POWERSHELL)
        self.environ.update({"FAKE_LOG": self.log, "FACTORY_KEYCHAIN": "none", "FACTORY_POWERSHELL": self.powershell,
                             "FAKE_KEYRING_FILE": os.path.join(self.tmp, "keyring-value"), "LC_ALL": "C"})
        self.keyring_value(VALUE + "\n")
        self.markers = os.path.join(self.state, "secrets-manual")

    def keyring_value(self, value):
        self.write("keyring-value", value)

    def tool(self, name, body):
        # The fakes carry their own #! line.
        return self.write(os.path.join("bin", name), body, 0o755)

    def logged(self, name):
        try:
            with open(os.path.join(self.log, name), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return b""

    def text(self, name):
        return self.logged(name).decode()

    def argv(self, name):
        """The calls a fake logged, as argv lists."""
        return [[a.decode() for a in call.split(b"\0")[:-1]] for call in self.logged(name).split(b"\1")[:-1]]

    def reset(self):
        for name in os.listdir(self.log):
            path = os.path.join(self.log, name)
            if os.path.isfile(path):
                os.remove(path)

    def mark(self, *names):
        os.makedirs(self.markers, exist_ok=True)
        for name in names:
            self.write(os.path.join(self.markers, name))

    def store_wincred(self, service, value):
        self.write(os.path.join(self.log, "wincred", service), value)

    def assert_not_leaked(self, value, *outputs):
        """VALUE is in no output, and in no file under the scratch folder but the ones where a
        fake keeps what it read on stdin (and the fake Credential Manager's store). That covers
        every argv and environment the fakes logged, the marker files and the state folder."""
        if isinstance(value, str):
            value = value.encode()
        for out in outputs:
            self.assertNotIn(value, out.encode() if isinstance(out, str) else out)
        allowed = {os.path.join(self.log, n) for n in ("sbx-stdin", "sbx-secret-stdin", "ps-stdin", "keyring-stdin")}
        allowed.add(self.environ["FAKE_KEYRING_FILE"])
        hits = []
        for folder, _, files in os.walk(self.tmp):
            for name in files:
                path = os.path.join(folder, name)
                if path in allowed or folder == os.path.join(self.log, "wincred") or os.path.islink(path):
                    continue
                with open(path, "rb") as f:
                    if value in f.read():
                        hits.append(path)
        self.assertEqual(hits, [])


class Parity(SecretFakes):
    """Runs a helper in bash and in Python against the same fakes."""

    def bash(self, snippet, env=None, stdin=b""):
        """(status, stdout, stderr) of SNIPPET with secrets.sh sourced, and the calls the fakes
        logged; the logs are cleared first."""
        self.reset()
        cp = subprocess.run(["bash", "-c", 'ROOT="$1"; . "$1/scripts/lib/common.sh"; . "$1/scripts/lib/secrets.sh"; '
                             + snippet, "_", REPO], input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=dict(self.environ, **(env or {})), cwd=self.tmp)
        return (cp.returncode, cp.stdout.decode(), cp.stderr.decode()), self.calls()

    def py(self, fn, env=None):
        """(fn's answer, stdout, stderr) of FN(ctx), and the calls the fakes logged."""
        self.reset()
        saved = dict(self.environ)
        self.environ.update(env or {})
        try:
            ctx = self.ctx()
            answer = fn(ctx)
        finally:
            self.environ.clear()
            self.environ.update(saved)
        return (answer, self.out.getvalue(), self.err.getvalue()), self.calls()

    def calls(self):
        return {name: self.logged(name) for name in ("sbx-argv.bin", "sbx-stdin", "sbx-secret-stdin", "keyring-argv.bin",
                                                       "keyring-stdin", "ps-argv.bin", "ps-stdin")}

    def same(self, snippet, fn, env=None, stdin=None):
        """Run both; require the same calls, and return (bash's, Python's) results."""
        bash, bash_calls = self.bash(snippet, env)
        py, py_calls = self.py(fn, env)
        self.assertEqual(py_calls, bash_calls)
        return bash, py


class ConstantsTest(Parity):
    def test_the_names_match_secrets_sh(self):
        (status, out, _), _ = self.bash('printf "%s\\n" "$FACTORY_SECRET_NAMES" "$FACTORY_KEYCHAIN_NAMES"')
        self.assertEqual(out, "%s\n%s\n" % (" ".join(secrets.SECRET_NAMES), " ".join(secrets.KEYCHAIN_NAMES)))

    def test_names_are_matched_whole(self):
        for name in ("jira", "github"):
            self.assertTrue(secrets.keychain_name_valid(name))
        self.assertFalse(secrets.secret_name_valid("github"))
        for bad in ("", "jira gemini", " jira", "jira ", "../jira", "jira/x", "JIRA", "passdesk-license", "factory-jira"):
            self.assertFalse(secrets.secret_name_valid(bad), bad)
            self.assertFalse(secrets.keychain_name_valid(bad), bad)
            (status, _, _), _ = self.bash('secret_name_valid "$X" || keychain_name_valid "$X"', {"X": bad})
            self.assertEqual(status, 1, bad)


class DeliverTest(Parity):
    def test_a_found_value_goes_to_the_broker_on_stdin_with_the_same_argv(self):
        env = {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found", "FAKE_SBX_SAYS": "sbx said this"}
        bash, py = self.same("secret_sync_from_keychain jira", lambda c: secrets.sync_from_keychain(c, "jira"), env)
        self.assertEqual(bash, (0, "sbx said this\n", ""))
        self.assertEqual(py, (0, "sbx said this\n", ""))
        self.assertEqual(self.argv("sbx-argv.bin"),
                         [["exec", "-i", "factory-broker", "sh", "-c", secrets.DELIVER_SCRIPT, "_", "jira"]])
        self.assertEqual(self.logged("sbx-stdin"), VALUE.encode())
        self.assert_not_leaked(VALUE, bash[1], bash[2], py[1], py[2])

    def test_quiet_drops_what_sbx_says_and_the_note(self):
        env = {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found", "FAKE_SBX_SAYS": "sbx said this"}
        bash, py = self.same("secret_sync_from_keychain jira >/dev/null",
                             lambda c: secrets.sync_from_keychain(c, "jira", quiet=True), env)
        self.assertEqual(bash, (0, "", ""))
        self.assertEqual(py, bash)
        self.mark("jira")
        bash, py = self.same("secret_sync_from_keychain jira >/dev/null",
                             lambda c: secrets.sync_from_keychain(c, "jira", quiet=True), env)
        self.assertEqual(py, bash)

    def test_the_delivery_script_writes_the_file_atomically(self):
        # Run it for real in a local sh, with the secrets folder moved into the scratch folder.
        script = secrets.DELIVER_SCRIPT.replace("/etc/factory-broker/secrets", os.path.join(self.tmp, "secrets"))
        cp = subprocess.run(["sh", "-c", script, "_", "jira"], input=b"line1\nline2", env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(cp.returncode, 0)
        path = os.path.join(self.tmp, "secrets", "jira")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"line1\nline2")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_a_failed_delivery_is_a_warning_only(self):
        env = {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found", "FAKE_SBX_DELIVER_FAIL": "1"}
        bash, py = self.same("secret_sync_from_keychain gemini", lambda c: secrets.sync_from_keychain(c, "gemini"), env)
        self.assertEqual(bash[0], 0)
        self.assertIn("warning: could not deliver the gemini credential", bash[2])
        self.assertEqual(py, bash)

    def test_a_name_that_is_not_ours_is_never_delivered_or_revoked(self):
        ctx = self.ctx()
        self.assertFalse(secrets.deliver_file(ctx, "../x", b"v"))
        self.assertFalse(secrets.revoke_file(ctx, "github"))
        self.assertEqual(self.logged("sbx-argv.bin"), b"")


class SyncTest(Parity):
    """secret_sync_from_keychain: what start-broker.sh does for each name on every start."""

    def test_a_marked_name_is_left_alone(self):
        self.mark("jira")
        bash, py = self.same("secret_sync_from_keychain jira", lambda c: secrets.sync_from_keychain(c, "jira"),
                             {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "absent"})
        self.assertEqual(bash, (0, "note: jira is managed by 'factory secret set/rm'; leaving it as is\n", ""))
        self.assertEqual(py, bash)
        self.assertEqual(self.calls(), dict.fromkeys(self.calls(), b""))  # no lookup, no sbx

    def test_not_stored_revokes_the_file(self):
        for backend in ("none", "secret-tool", "macos", "wincred"):
            with self.subTest(backend=backend):
                env = {"FACTORY_KEYCHAIN": backend, "FAKE_KEYRING": "absent"}
                bash, py = self.same("secret_sync_from_keychain gemini",
                                     lambda c: secrets.sync_from_keychain(c, "gemini"), env)
                self.assertEqual(bash, (0, "", ""))
                self.assertEqual(py, bash)
                self.assertEqual(self.argv("sbx-argv.bin"),
                                 [["exec", "factory-broker", "rm", "-f", "/etc/factory-broker/secrets/gemini"]])

    def test_a_failed_revocation_is_status_2(self):
        env = {"FAKE_SBX_RM_FAIL": "1"}
        bash, py = self.same("secret_sync_from_keychain npm-registry",
                             lambda c: secrets.sync_from_keychain(c, "npm-registry"), env)
        self.assertEqual(bash[0], 2)
        self.assertIn("error: could not remove the npm-registry credential file", bash[2])
        self.assertEqual(py, bash)

    def test_an_unreadable_keychain_keeps_the_file_and_warns_once(self):
        cases = [("secret-tool", m) for m in ("locked", "locked-silent", "search-error", "search-silent")]
        cases += [("macos", "locked"), ("wincred", "ps-fail")]
        for backend, mode in cases:
            with self.subTest(backend=backend, mode=mode):
                env = {"FACTORY_KEYCHAIN": backend, "FAKE_KEYRING": mode}
                if mode == "ps-fail":
                    env["FAKE_PS_FAIL"] = "1"
                    self.store_wincred("factory-gitlab", VALUE)
                bash, py = self.same("secret_sync_from_keychain gitlab",
                                     lambda c: secrets.sync_from_keychain(c, "gitlab"), env)
                self.assertEqual(bash[:2], (0, ""))
                self.assertEqual(bash[2].count("\n"), 1)
                self.assertIn("could not read factory-gitlab from the keychain", bash[2])
                self.assertIn("To fix: ", bash[2])
                for noise in ("interaction is not allowed", "D-Bus", "NoReply", "cannot start", VALUE):
                    self.assertNotIn(noise, bash[2])
                self.assertEqual(py, bash)
                self.assertEqual(self.logged("sbx-argv.bin"), b"")  # the credential is kept
                self.assertNotIn(b"--unlock", self.logged("keyring-argv.bin"))

    def test_the_warning_names_the_fix_for_each_backend(self):
        for backend, hint in (("macos", "unlock the Keychain"), ("secret-tool", "unlock your login keyring"),
                              ("wincred", "powershell.exe"), ("other", "check this machine's keychain")):
            with self.subTest(backend=backend):
                bash, py = self.same("keychain_unavailable_warning factory-jira \"the broker's jira credential\"",
                                     lambda c: secrets.keychain_unavailable_warning(c, "factory-jira",
                                                                                   "the broker's jira credential"),
                                     {"FACTORY_KEYCHAIN": backend})
                self.assertIn(hint, bash[2])
                self.assertEqual(py[1:], bash[1:])


class LookupTest(Parity):
    def lookup(self, env, service="factory-jira"):
        bash, py = self.same('keychain_lookup "$S"', lambda c: secrets.keychain_lookup(c, service),
                             dict(env, S=service))
        has_bash, has_py = self.same('keychain_has "$S"', lambda c: secrets.keychain_has(c, service),
                                     dict(env, S=service))
        self.assertEqual(py[0][0], bash[0])
        self.assertEqual(py[0][1].decode(), bash[1])
        self.assertEqual(has_py[0], has_bash[0])
        self.assertEqual(has_bash[0], bash[0])
        self.assertEqual((py[1], py[2], bash[2], has_bash[1], has_bash[2]), ("", "", "", "", ""))
        return bash[0], bash[1]

    def test_secret_tool_and_macos(self):
        expected = {"found": (0, VALUE), "absent": (1, ""), "locked": (2, ""), "locked-silent": (2, ""),
                    "search-error": (2, ""), "search-silent": (2, "")}
        for backend in ("secret-tool", "macos"):
            for mode, want in expected.items():
                if backend == "macos" and mode not in ("found", "absent", "locked"):
                    continue
                with self.subTest(backend=backend, mode=mode):
                    self.assertEqual(self.lookup({"FACTORY_KEYCHAIN": backend, "FAKE_KEYRING": mode}), want)

    def test_every_trailing_newline_is_dropped_as_a_shell_does(self):
        env = {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found"}
        self.keyring_value("a\nb\n\n")
        self.assertEqual(self.lookup(env), (0, "a\nb"))
        self.keyring_value("\n")
        self.assertEqual(self.lookup(env), (2, ""))

    def test_wincred(self):
        env = {"FACTORY_KEYCHAIN": "wincred"}
        self.assertEqual(self.lookup(env, "factory-gemini"), (1, ""))
        self.store_wincred("factory-gemini", "wc-secret-2")
        self.assertEqual(self.lookup(env, "factory-gemini"), (0, "wc-secret-2"))
        self.assertEqual(self.lookup(dict(env, FAKE_PS_FAIL="1"), "factory-gemini"), (2, ""))

    def test_a_service_that_is_not_ours_never_reaches_powershell(self):
        self.store_wincred("factory-jira", "x")
        for service in ("factory-jira'); Remove-Item C:\\ ; ('", "jira", "factory-", "factory-jira gemini", "other-jira"):
            with self.subTest(service=service):
                self.assertEqual(self.lookup({"FACTORY_KEYCHAIN": "wincred"}, service), (2, ""))
                self.assertEqual(self.logged("ps-argv.bin"), b"")
                self.assertIsNone(secrets.wincred_script("store", service))

    def test_none_has_nothing(self):
        self.assertEqual(self.lookup({"FACTORY_KEYCHAIN": "none"}), (1, ""))


class StoreDeleteTest(Parity):
    def test_store_sends_the_value_on_stdin_only(self):
        for backend in ("secret-tool", "wincred"):
            with self.subTest(backend=backend):
                env = {"FACTORY_KEYCHAIN": backend, "V": "wc-secret-1"}
                bash, bash_calls = self.bash('printf %s "$V" | keychain_store factory-jira', env)
                py, py_calls = self.py(lambda c: secrets.keychain_store(c, "factory-jira", b"wc-secret-1"), env)
                self.assertEqual(py_calls, bash_calls)
                self.assertEqual((py[0], bash[0]), (True, 0))
                stdin = "keyring-stdin" if backend == "secret-tool" else "ps-stdin"
                self.assertEqual(self.logged(stdin), b"wc-secret-1")
                self.assertNotIn(b"wc-secret-1", self.logged("keyring-argv.bin") + self.logged("ps-argv.bin"))
                if backend == "wincred":
                    self.assertIn(b"Retrieve('factory-jira'", self.logged("ps-argv.bin"))

    def test_a_failed_store_is_false(self):
        ctx = self.ctx()
        self.environ.update(FACTORY_KEYCHAIN="secret-tool", FAKE_STORE_STATUS="1")
        self.assertFalse(secrets.keychain_store(ctx, "factory-jira", b"x"))
        for backend in ("macos", "none"):
            self.environ["FACTORY_KEYCHAIN"] = backend
            self.assertFalse(secrets.keychain_store(ctx, "factory-jira", b"x"))

    def test_delete(self):
        self.store_wincred("factory-jira", "x")
        for backend in ("secret-tool", "macos", "wincred", "none"):
            with self.subTest(backend=backend):
                bash, py = self.same("keychain_delete factory-jira", lambda c: secrets.keychain_delete(c, "factory-jira"),
                                     {"FACTORY_KEYCHAIN": backend})
                self.assertEqual(bash, (0, "", ""))
                self.assertEqual(py[1:], bash[1:])
        self.assertFalse(os.path.exists(os.path.join(self.log, "wincred", "factory-jira")))


class GithubSyncTest(Parity):
    def sync(self, env):
        bash, py = self.same("github_sync_from_keychain", secrets.github_sync_from_keychain, env)
        self.assertEqual(bash[0], 0)
        self.assertEqual(py[1:], bash[1:])
        return bash

    def test_found_is_registered_broker_scoped_on_stdin(self):
        for backend in ("secret-tool", "macos"):
            with self.subTest(backend=backend):
                out = self.sync({"FACTORY_KEYCHAIN": backend, "FAKE_KEYRING": "found"})
                self.assertEqual(out[1], "GitHub token for factory-broker restored from the keychain (factory-github)\n")
                self.assertEqual(self.argv("sbx-argv.bin"),
                                 [["secret", "set", "-f", "github", "--sandbox", "factory-broker"]])
                self.assertEqual(self.logged("sbx-secret-stdin"), VALUE.encode())
                self.assert_not_leaked(VALUE, *out[1:])

    def test_absent_or_locked_leaves_sbx_alone(self):
        for mode in ("absent", "locked"):
            with self.subTest(mode=mode):
                out = self.sync({"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": mode})
                self.assertEqual(self.logged("sbx-argv.bin"), b"")
                self.assertEqual(out[1], "")
                self.assertEqual("could not read factory-github" in out[2], mode == "locked")

    def test_a_failed_registration_warns(self):
        out = self.sync({"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found", "FAKE_SBX_SET_FAIL": "1"})
        self.assertIn("warning: could not register the GitHub token", out[2])


class ManualMarkersTest(Parity):
    def test_mark_list_and_unmark_match_the_bash(self):
        self.assertEqual(secrets.manual_names(self.paths), [])
        self.assertTrue(secrets.mark_manual(self.paths, "gitlab"))
        self.assertEqual(os.stat(self.markers).st_mode & 0o777, 0o700)
        self.assertEqual(os.path.getsize(os.path.join(self.markers, "gitlab")), 0)
        (status, _, _), _ = self.bash("secret_is_manual gitlab && secret_mark_manual jira")
        self.assertEqual(status, 0)
        self.assertEqual(secrets.manual_names(self.paths), ["gitlab", "jira"])
        secrets.unmark_manual(self.paths, "jira")
        secrets.unmark_manual(self.paths, "jira")
        self.assertFalse(secrets.is_manual(self.paths, "jira"))

    def test_a_marker_that_cannot_be_written_is_false(self):
        self.write("state/ai-code-factory/secrets-manual", "not a folder")
        self.assertFalse(secrets.mark_manual(self.paths, "jira"))

    def test_secret_clear_all_manual_stays_bash(self):
        # Only start-broker.sh calls it (step 7 ports it), when it creates a new broker sandbox.
        self.mark("jira", "npm-registry")
        (status, out, err), calls = self.bash("secret_clear_all_manual")
        self.assertEqual((status, sorted(out.split()), err), (0, ["jira", "npm-registry"], ""))
        self.assertEqual(os.listdir(self.markers), [])
        self.assertEqual(calls["sbx-argv.bin"], b"")
        os.rmdir(self.markers)
        self.assertEqual(self.bash("secret_clear_all_manual")[0], (0, "", ""))


class WarnMissingTest(SecretFakes):
    """keychain_warn_missing stays in bin/factory for `status` and `start`: a locked keychain
    is reported as locked, not as a missing token."""

    def warn_missing(self, mode):
        cp = subprocess.run(["bash", "-c", '. "$1"; keychain_warn_missing factory-jira "no token is stored"', "_",
                             os.path.join(REPO, "bin", "factory")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=dict(self.environ, FACTORY_KEYCHAIN="secret-tool", FAKE_KEYRING=mode), cwd=self.tmp)
        return cp.stdout.decode()

    def test_warn_missing(self):
        self.assertIn("no token is stored", self.warn_missing("absent"))
        locked = self.warn_missing("locked")
        self.assertIn("keychain is locked or unavailable, so factory-jira couldn't be checked", locked)
        self.assertNotIn("no token is stored", locked)
        self.assertEqual(self.warn_missing("found"), "")
