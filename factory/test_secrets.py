"""Tests for factory/secrets.py, the helpers `factory secret` and the broker start
(factory/broker_start.py) use, for the keychain lookups and for the missing-token warning of
`status` and `start`. Their bash copies (scripts/lib/secrets.sh, and bin/factory's
keychain_warn_missing) are gone; these are tests of the Python alone, against the outputs the bash
gave: the same statuses, values, messages and argv, byte for byte. Ported from parts 4c, 5, 8 and
9 of scripts/test_factory_secret.sh.

Nothing here needs sbx, a keychain or a network: every program is a fake on the PATH that logs
its argv and environment and keeps what it reads on stdin in a file of its own.
"""

import os
import shutil
import subprocess
import tempfile

from factory import keychain, secrets
from factory.commands import status
from factory.context import FactoryError
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

# Distinctive, so no file can hold it by chance (the leak checks scan every file).
VALUE = "kr-secret-Wg5Ra"
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
        # No Python under test writes bytecode into the scratch folder, which is its HOME and which
        # the leak checks scan: macOS's system Python caches under ~/Library/Caches/com.apple.python.
        pycache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, pycache)
        self.environ.update({"FAKE_LOG": self.log, "FACTORY_KEYCHAIN": "none", "FACTORY_POWERSHELL": self.powershell,
                             "FAKE_KEYRING_FILE": os.path.join(self.tmp, "keyring-value"), "LC_ALL": "C",
                             "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": pycache})
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


# The Credential Manager lookup script the bash wincred_run ran for SERVICE, byte for byte.
LOOKUP_SCRIPT = ("# factory-keychain op=lookup\n$ErrorActionPreference = 'Stop'\n"
                 "[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false\n"
                 "[void][Windows.Security.Credentials.PasswordVault, Windows.Security.Credentials, "
                 "ContentType = WindowsRuntime]\n$v = New-Object Windows.Security.Credentials.PasswordVault\n"
                 "try { $c = $v.Retrieve('%s', 'factory') } catch { exit 0 }\n"
                 "$c.RetrievePassword(); [Console]::Out.Write($c.Password)")


class Parity(SecretFakes):
    """Runs a helper in Python against the fakes and returns what it answered and printed, and
    the calls the fakes logged (the bash helpers it was compared with are gone)."""

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

    def run_py(self, fn, env=None):
        """(fn's answer, stdout, stderr) of FN(ctx)."""
        return self.py(fn, env)[0]

    def calls(self):
        return {name: self.logged(name) for name in ("sbx-argv.bin", "sbx-stdin", "sbx-secret-stdin", "keyring-argv.bin",
                                                       "keyring-stdin", "ps-argv.bin", "ps-stdin")}

    @staticmethod
    def argv_log(*calls):
        """The bytes a fake logs for CALLS (argv lists): NUL after each argument, \\1 after each call."""
        return b"".join(b"".join(a.encode() + b"\0" for a in call) + b"\1" for call in calls)


class ConstantsTest(Parity):
    def test_the_names_are_the_bash_ones(self):
        # FACTORY_SECRET_NAMES and FACTORY_KEYCHAIN_NAMES of the deleted secrets.sh, in their order.
        self.assertEqual(secrets.SECRET_NAMES, ("jira", "gemini", "npm-registry", "gitlab", "approvals-webhook"))
        self.assertEqual(keychain.KEYCHAIN_NAMES, ("jira", "gemini", "npm-registry", "gitlab", "approvals-webhook", "github"))

    def test_names_are_matched_whole(self):
        for name in ("jira", "github"):
            self.assertTrue(keychain.name_valid(name))
        self.assertFalse(secrets.secret_name_valid("github"))
        for bad in ("", "jira gemini", " jira", "jira ", "../jira", "jira/x", "JIRA", "passdesk-license", "factory-jira"):
            self.assertFalse(secrets.secret_name_valid(bad), bad)
            self.assertFalse(keychain.name_valid(bad), bad)  # as the bash keychain_name_valid refused them

    def test_the_bash_is_gone(self):
        # The last of scripts/lib/secrets.sh (keychain_has and what it called) went with `start`.
        self.assertFalse(os.path.exists(os.path.join(REPO, "scripts", "lib", "secrets.sh")))


class DeliverTest(Parity):
    def test_a_found_value_goes_to_the_broker_on_stdin(self):
        env = {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found", "FAKE_SBX_SAYS": "sbx said this"}
        py = self.run_py(lambda c: secrets.sync_from_keychain(c, "jira"), env)
        self.assertEqual(py, (secrets.SYNCED, "sbx said this\n", ""))
        self.assertEqual(self.argv("sbx-argv.bin"),
                         [["exec", "-i", "factory-broker", "sh", "-c", secrets.DELIVER_SCRIPT, "_", "jira"]])
        self.assertEqual(self.logged("sbx-stdin"), VALUE.encode())
        self.assert_not_leaked(VALUE, py[1], py[2])

    def test_the_delivery_script_is_the_one_the_bash_ran(self):
        self.assertEqual(secrets.DELIVER_SCRIPT,
                         'umask 077 && dir="/etc/factory-broker/secrets" && mkdir -p "$dir" \\\n'
                         '     && { [ "$(id -u)" != 0 ] || chown agent:agent "$dir"; } \\\n'
                         '     && dest="$dir/$1" && cat > "$dest.tmp" && mv "$dest.tmp" "$dest" \\\n'
                         '     && { [ "$(id -u)" != 0 ] || chown agent:agent "$dest"; }')

    def test_quiet_drops_what_sbx_says_and_the_note(self):
        env = {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found", "FAKE_SBX_SAYS": "sbx said this"}
        self.assertEqual(self.run_py(lambda c: secrets.sync_from_keychain(c, "jira", quiet=True), env),
                         (secrets.SYNCED, "", ""))
        self.mark("jira")
        self.assertEqual(self.run_py(lambda c: secrets.sync_from_keychain(c, "jira", quiet=True), env),
                         (secrets.SYNCED, "", ""))

    def test_the_delivery_script_writes_the_file_atomically(self):
        # Run it for real in a local sh, with the secrets folder moved into the scratch folder.
        script = secrets.DELIVER_SCRIPT.replace("/etc/factory-broker/secrets", os.path.join(self.tmp, "secrets"))
        cp = subprocess.run(["sh", "-c", script, "_", "jira"], input=b"line1-q7Zk3\nline2-Yt6Lb", env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(cp.returncode, 0)
        path = os.path.join(self.tmp, "secrets", "jira")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"line1-q7Zk3\nline2-Yt6Lb")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_a_failed_delivery_warns_and_is_not_delivered(self):
        env = {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found", "FAKE_SBX_DELIVER_FAIL": "1"}
        self.assertEqual(self.run_py(lambda c: secrets.sync_from_keychain(c, "gemini"), env),
                         (secrets.NOT_DELIVERED, "", "warning: could not deliver the gemini credential to the broker "
                          "sandbox (an old broker image? see README, Upgrading, for the rebuild steps)\n"))
        self.assertEqual(self.logged("sbx-stdin"), VALUE.encode())
        self.assertEqual(len({secrets.SYNCED, secrets.NOT_DELIVERED, secrets.REVOKE_FAILED}), 3)

    def test_a_name_that_is_not_ours_is_never_delivered_or_revoked(self):
        ctx = self.ctx()
        self.assertFalse(secrets.deliver_file(ctx, "../x", b"v"))
        self.assertFalse(secrets.revoke_file(ctx, "github"))
        self.assertEqual(self.logged("sbx-argv.bin"), b"")


class SyncTest(Parity):
    """sync_from_keychain: what the broker start does for each name on every start."""

    def test_a_marked_name_is_left_alone(self):
        self.mark("jira")
        py = self.run_py(lambda c: secrets.sync_from_keychain(c, "jira"),
                         {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "absent"})
        self.assertEqual(py, (secrets.SYNCED, "note: jira is managed by 'factory secret set/rm'; leaving it as is\n", ""))
        self.assertEqual(self.calls(), dict.fromkeys(self.calls(), b""))  # no lookup, no sbx

    def test_not_stored_revokes_the_file(self):
        for backend in ("none", "secret-tool", "macos", "wincred"):
            with self.subTest(backend=backend):
                env = {"FACTORY_KEYCHAIN": backend, "FAKE_KEYRING": "absent"}
                self.assertEqual(self.run_py(lambda c: secrets.sync_from_keychain(c, "gemini"), env),
                                 (secrets.SYNCED, "", ""))
                self.assertEqual(self.argv("sbx-argv.bin"),
                                 [["exec", "factory-broker", "rm", "-f", "/etc/factory-broker/secrets/gemini"]])

    def test_a_failed_revocation_is_status_2(self):
        py = self.run_py(lambda c: secrets.sync_from_keychain(c, "npm-registry"), {"FAKE_SBX_RM_FAIL": "1"})
        self.assertEqual(py, (secrets.REVOKE_FAILED, "", "error: could not remove the npm-registry credential file "
                              "from the broker sandbox; it may still be active even though its keychain entry is gone\n"))
        self.assertEqual(secrets.REVOKE_FAILED, 2)

    def test_an_unreadable_keychain_keeps_the_file_and_warns_once(self):
        cases = [("secret-tool", m) for m in ("locked", "locked-silent", "search-error", "search-silent")]
        cases += [("macos", "locked"), ("wincred", "ps-fail")]
        for backend, mode in cases:
            with self.subTest(backend=backend, mode=mode):
                env = {"FACTORY_KEYCHAIN": backend, "FAKE_KEYRING": mode}
                if mode == "ps-fail":
                    env["FAKE_PS_FAIL"] = "1"
                    self.store_wincred("factory-gitlab", VALUE)
                py = self.run_py(lambda c: secrets.sync_from_keychain(c, "gitlab"), env)
                self.assertEqual(py[:2], (secrets.NOT_DELIVERED, ""))
                self.assertEqual(py[2].count("\n"), 1)
                self.assertIn("could not read factory-gitlab from the keychain", py[2])
                self.assertIn("To fix: ", py[2])
                for noise in ("interaction is not allowed", "D-Bus", "NoReply", "cannot start", VALUE):
                    self.assertNotIn(noise, py[2])
                self.assertEqual(self.logged("sbx-argv.bin"), b"")  # the credential is kept
                self.assertNotIn(b"--unlock", self.logged("keyring-argv.bin"))

    def test_the_warning_names_the_fix_for_each_backend(self):
        hints = {"macos": "unlock the Keychain (security unlock-keychain) and allow access when asked",
                 "secret-tool": "unlock your login keyring (log in to the desktop session, or open Passwords and Keys) "
                                "and check that a Secret Service such as gnome-keyring-daemon is running "
                                "(try: secret-tool lookup service factory-jira)",
                 "wincred": "check that Windows PowerShell runs from WSL (powershell.exe) and can reach the "
                            "Credential Manager",
                 "other": "check this machine's keychain"}
        for backend, hint in hints.items():
            with self.subTest(backend=backend):
                py = self.run_py(lambda c: secrets.keychain_unavailable_warning(c, "factory-jira",
                                                                               "the broker's jira credential"),
                                 {"FACTORY_KEYCHAIN": backend})
                self.assertEqual(py[1:], ("", "warning: could not read factory-jira from the keychain (locked or "
                                              "unavailable); leaving the broker's jira credential as it is. To fix: "
                                              "%s, then run 'factory start' again\n" % hint))


class LookupTest(Parity):
    def lookup(self, env, service="factory-jira", lookup_calls=(), has_calls=None):
        """lookup and has of SERVICE: (status, value), as the bash keychain_lookup gave them (has
        must give the same status). The fakes must see exactly the calls the bash made: LOOKUP_CALLS
        for lookup and HAS_CALLS (the same unless given) for has, nothing on any stdin."""
        log = "ps-argv.bin" if env.get("FACTORY_KEYCHAIN") == "wincred" else "keyring-argv.bin"
        py, calls = self.py(lambda c: keychain.lookup(c, service), env)
        self.assertEqual(calls, dict(dict.fromkeys(calls, b""), **{log: self.argv_log(*lookup_calls)}))
        has, has_log = self.py(lambda c: keychain.has(c, service), env)
        self.assertEqual(has_log, dict(dict.fromkeys(calls, b""),
                                       **{log: self.argv_log(*(lookup_calls if has_calls is None else has_calls))}))
        self.assertEqual(has[0], py[0][0])
        self.assertEqual((py[1], py[2], has[1], has[2]), ("", "", "", ""))
        return py[0][0], py[0][1].decode()

    def test_secret_tool_and_macos(self):
        lookup = ["lookup", "service", "factory-jira"]
        search = ["search", "service", "factory-jira"]
        expected = {"found": (0, VALUE, [lookup]), "absent": (1, "", [lookup, search]), "locked": (2, "", [lookup]),
                    "locked-silent": (2, "", [lookup, search]), "search-error": (2, "", [lookup, search]),
                    "search-silent": (2, "", [lookup, search])}
        for mode, (status, value, calls) in expected.items():
            with self.subTest(backend="secret-tool", mode=mode):
                self.assertEqual(self.lookup({"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": mode}, lookup_calls=calls),
                                 (status, value))
        for mode, want in (("found", (0, VALUE)), ("absent", (1, "")), ("locked", (2, ""))):
            with self.subTest(backend="macos", mode=mode):
                # has asks only whether it exists (no -w: the value is not read)
                self.assertEqual(self.lookup({"FACTORY_KEYCHAIN": "macos", "FAKE_KEYRING": mode},
                                             lookup_calls=[["find-generic-password", "-s", "factory-jira", "-w"]],
                                             has_calls=[["find-generic-password", "-s", "factory-jira"]]), want)

    def test_every_trailing_newline_is_dropped_as_a_shell_does(self):
        env = {"FACTORY_KEYCHAIN": "secret-tool", "FAKE_KEYRING": "found"}
        calls = [["lookup", "service", "factory-jira"]]
        self.keyring_value("a\nb\n\n")
        self.assertEqual(self.lookup(env, lookup_calls=calls), (0, "a\nb"))
        self.keyring_value("\n")
        self.assertEqual(self.lookup(env, lookup_calls=calls), (2, ""))

    def test_wincred(self):
        env = {"FACTORY_KEYCHAIN": "wincred"}
        calls = [["-NoProfile", "-NonInteractive", "-Command", LOOKUP_SCRIPT % "factory-gemini"]]
        self.assertEqual(self.lookup(env, "factory-gemini", calls), (1, ""))
        self.store_wincred("factory-gemini", "wc-secret-2")
        self.assertEqual(self.lookup(env, "factory-gemini", calls), (0, "wc-secret-2"))
        self.assertEqual(self.lookup(dict(env, FAKE_PS_FAIL="1"), "factory-gemini", calls), (2, ""))

    def test_a_service_that_is_not_ours_never_reaches_powershell(self):
        self.store_wincred("factory-jira", "x")
        for service in ("factory-jira'); Remove-Item C:\\ ; ('", "jira", "factory-", "factory-jira gemini", "other-jira"):
            with self.subTest(service=service):
                self.assertEqual(self.lookup({"FACTORY_KEYCHAIN": "wincred"}, service), (2, ""))
                self.assertEqual(self.logged("ps-argv.bin"), b"")
                self.assertIsNone(keychain.wincred_script("store", service))

    def test_none_has_nothing(self):
        self.assertEqual(self.lookup({"FACTORY_KEYCHAIN": "none"}), (1, ""))


class StoreDeleteTest(Parity):
    def test_store_sends_the_value_on_stdin_only(self):
        for backend in ("secret-tool", "wincred"):
            with self.subTest(backend=backend):
                env = {"FACTORY_KEYCHAIN": backend}
                py, calls = self.py(lambda c: keychain.store(c, "factory-jira", b"wc-secret-1"), env)
                self.assertEqual(py, (True, "", ""))
                stdin = "keyring-stdin" if backend == "secret-tool" else "ps-stdin"
                self.assertEqual(calls[stdin], b"wc-secret-1")
                self.assertNotIn(b"wc-secret-1", calls["keyring-argv.bin"] + calls["ps-argv.bin"])
                if backend == "secret-tool":
                    self.assertEqual(self.argv("keyring-argv.bin"),
                                     [["store", "--label", "AI Code Factory: factory-jira", "service", "factory-jira"]])
                else:
                    self.assertEqual(self.argv("ps-argv.bin"), [["-NoProfile", "-NonInteractive", "-Command",
                                                                 keychain.wincred_script("store", "factory-jira")]])
                    self.assertIn(b"Retrieve('factory-jira'", calls["ps-argv.bin"])

    def test_a_failed_store_is_false(self):
        ctx = self.ctx()
        self.environ.update(FACTORY_KEYCHAIN="secret-tool", FAKE_STORE_STATUS="1")
        self.assertFalse(keychain.store(ctx, "factory-jira", b"x"))
        for backend in ("macos", "none"):
            self.environ["FACTORY_KEYCHAIN"] = backend
            self.assertFalse(keychain.store(ctx, "factory-jira", b"x"))

    def test_delete(self):
        self.store_wincred("factory-jira", "x")
        expected = {"secret-tool": ("keyring-argv.bin", [["clear", "service", "factory-jira"]]),
                    "macos": ("keyring-argv.bin", [["delete-generic-password", "-s", "factory-jira"]]),
                    "wincred": ("ps-argv.bin", [["-NoProfile", "-NonInteractive", "-Command",
                                                 keychain.wincred_script("delete", "factory-jira")]]),
                    "none": ("keyring-argv.bin", [])}
        for backend, (log, argv) in expected.items():
            with self.subTest(backend=backend):
                py = self.run_py(lambda c: keychain.delete(c, "factory-jira"), {"FACTORY_KEYCHAIN": backend})
                self.assertEqual(py[1:], ("", ""))
                self.assertEqual(self.argv(log), argv)
        self.assertFalse(os.path.exists(os.path.join(self.log, "wincred", "factory-jira")))


class GithubSyncTest(Parity):
    def sync(self, env):
        py = self.run_py(secrets.github_sync_from_keychain, env)
        self.assertIsNone(py[0])
        return py

    def test_found_is_registered_broker_scoped_on_stdin(self):
        for backend in ("secret-tool", "macos"):
            with self.subTest(backend=backend):
                out = self.sync({"FACTORY_KEYCHAIN": backend, "FAKE_KEYRING": "found"})
                self.assertEqual(out[1:], ("GitHub token for factory-broker restored from the keychain (factory-github)\n",
                                           ""))
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
        self.assertEqual(out[1:], ("", "warning: could not register the GitHub token from the keychain for "
                                       "factory-broker; run 'factory secret save github' again\n"))


class ManualMarkersTest(Parity):
    def test_mark_list_and_unmark(self):
        self.assertEqual(secrets.manual_names(self.paths), [])
        self.assertTrue(secrets.mark_manual(self.paths, "gitlab"))
        self.assertEqual(os.stat(self.markers).st_mode & 0o777, 0o700)
        self.assertEqual(os.path.getsize(os.path.join(self.markers, "gitlab")), 0)
        self.assertTrue(secrets.is_manual(self.paths, "gitlab"))
        self.assertTrue(secrets.mark_manual(self.paths, "jira"))
        self.assertEqual(secrets.manual_names(self.paths), ["gitlab", "jira"])
        secrets.unmark_manual(self.paths, "jira")
        secrets.unmark_manual(self.paths, "jira")
        self.assertFalse(secrets.is_manual(self.paths, "jira"))

    def test_a_marker_that_cannot_be_written_is_false(self):
        self.write("state/ai-code-factory/secrets-manual", "not a folder")
        self.assertFalse(secrets.mark_manual(self.paths, "jira"))

    def test_clear_all_manual(self):
        # Only the broker start calls it, when it creates a new broker sandbox.
        self.mark("npm-registry", "jira", ".hidden")
        self.assertEqual(secrets.clear_all_manual(self.paths), ["jira", "npm-registry"])
        self.assertEqual(os.listdir(self.markers), [".hidden"])  # as the bash glob, which skips dotfiles
        self.assertEqual(self.logged("sbx-argv.bin"), b"")
        shutil.rmtree(self.markers)
        self.assertEqual(secrets.clear_all_manual(self.paths), [])

    def test_a_marker_that_cannot_be_cleared_stops_the_start(self):
        os.makedirs(os.path.join(self.markers, "gemini"))
        with self.assertRaises(FactoryError) as cm:
            secrets.clear_all_manual(self.paths)
        self.assertTrue(str(cm.exception).startswith(
            "could not remove the 'factory secret set' marker %s: " % os.path.join(self.markers, "gemini")))


class WarnMissingTest(SecretFakes):
    """status.warn_missing, for `status` and `start` (bin/factory's keychain_warn_missing before):
    a locked keychain is reported as locked, not as a missing token. The expected lines are the
    ones the bash printed (no colour: not a terminal)."""

    def warn_missing(self, mode):
        self.environ.update(FACTORY_KEYCHAIN="secret-tool", FAKE_KEYRING=mode)
        ctx = self.ctx()
        self.assertIsNone(status.warn_missing(ctx, "factory-jira", "no token is stored"))
        self.assertEqual(self.err.getvalue(), "")
        return self.out.getvalue()

    def test_warn_missing(self):
        self.assertEqual(self.warn_missing("absent"), "  ! no token is stored\n")
        self.assertEqual(self.warn_missing("locked"), "  ! the keychain is locked or unavailable, so factory-jira couldn't "
                                                      "be checked; unlock it and run this again\n")
        self.assertEqual(self.warn_missing("found"), "")
