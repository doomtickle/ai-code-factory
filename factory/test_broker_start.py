"""Tests for factory/broker_start.py (`factory _start-broker`, what scripts/start-broker.sh runs)
and factory/fingerprints.py. Before the bash was deleted, its output was compared with this on
eleven scenarios (fresh, existing, failures, bound, a failing `sbx run`, a failing token set, not
ready, a bad Coordinator tool, usage, no project folder, state inside the project): the same
status, stdout, stderr, sbx calls, stdin of every call, state files and modes.

Every program is a fake on the PATH: sbx keeps its sandboxes, secrets, MCP registrations and
ports in files, logs each call's argv and environment, and keeps each call's stdin in a file of
its own; secret-tool answers per service from a folder of values; curl prints a status code.
Nothing needs sbx, a keychain, a network, a terminal or this machine's HOME.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile

from factory import broker_start, cli, fingerprints, secrets
from factory.fixtures import REPO, CommandTest

FAKE_SBX = r"""#!/usr/bin/env bash
F="$FAKE_LOG"
n=$(( $(cat "$F/n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$F/n"
{ printf '%s\0' "$@"; printf '\001'; } >> "$F/sbx-argv.bin"
env > "$F/env.$n"
case "$1" in
  version) echo "sbx version: v0.45.1 testcommit" ;;
  ls) cat "$F/sandboxes" 2>/dev/null ;;
  run)
    if [ -n "${FAKE_RUN_FAIL:-}" ]; then
      i=1; while [ "$i" -le 25 ]; do echo "run line $i"; i=$((i + 1)); done
      echo "sbx: no such image" >&2; exit 3
    fi
    grep -qx factory-broker "$F/sandboxes" 2>/dev/null || echo factory-broker >> "$F/sandboxes" ;;
  exec)
    if [ "$2" = -i ]; then cat > "$F/stdin.$n"; [ -z "${FAKE_DELIVER_FAIL:-}" ] || exit 1
    elif [ "$3" = rm ]; then [ -z "${FAKE_RM_FAIL:-}" ] || { echo "sbx: rm refused" >&2; exit 1; }
    else echo "server checked"; [ -z "${FAKE_EXEC_FAIL:-}" ] || exit 5; fi ;;
  ports)
    if [ "$3" = --json ]; then cat "$F/ports" 2>/dev/null || exit 1
    else echo '[{"host_port":18765}]' > "$F/ports"; echo published; fi ;;
  mcp)
    case "$2" in
      ls) cat "$F/mcp" 2>/dev/null ;;
      add) echo "$3" >> "$F/mcp"; echo added ;;
      rm) grep -vx "$4" "$F/mcp" > "$F/mcp.tmp"; mv "$F/mcp.tmp" "$F/mcp"; echo removed ;;
    esac ;;
  secret)
    case "$2" in
      ls) cat "$F/secrets" 2>/dev/null ;;
      set)
        cat > "$F/stdin.$n"
        [ -z "${FAKE_SET_FAIL:-}" ] || { echo "sbx: not signed in" >&2; exit 1; }
        echo "(global)  -  $4  " >> "$F/secrets"; echo stored ;;
      rm) echo removed ;;
    esac ;;
esac
exit 0
"""

# A value per service in FAKE_KEYS/<service>; none = definitely not stored; FAKE_LOCKED names a
# service whose lookup fails with an error (a locked keyring).
FAKE_SECRET_TOOL = r"""#!/usr/bin/env bash
env > "$FAKE_LOG/keyring-env.$$"
case "$1" in
  lookup)
    [ "$3" != "${FAKE_LOCKED:-}" ] || { echo "secret-tool: the keyring is locked" >&2; exit 1; }
    [ -f "$FAKE_KEYS/$3" ] || exit 1
    cat "$FAKE_KEYS/$3" ;;
  search) [ ! -f "$FAKE_KEYS/$3" ] || echo "[/org/freedesktop/secrets/collection/login/1]" ;;
  *) exit 1 ;;
esac
"""

FAKE_CURL = r"""#!/bin/sh
echo x >> "$FAKE_LOG/curl-calls"
printf '%s' "${FAKE_HTTP:-401}"
"""

VALUES = {"factory-jira": "jira-Vq3Lp", "factory-gitlab": "gitlab-Hn8Tc", "factory-github": "github-Zr5Wk"}
TOOLS = "web_fetch git_fetch git_push github_create_pr npm_fetch"


class BrokerFakes(CommandTest):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp, "log")
        self.keys = os.path.join(self.tmp, "keys")
        self.project = os.path.join(self.tmp, "project")
        for folder in (self.log, self.keys, self.project):
            os.makedirs(folder)
        self.tool("sbx", FAKE_SBX)
        self.tool("secret-tool", FAKE_SECRET_TOOL)
        self.tool("curl", FAKE_CURL)
        for service, value in VALUES.items():
            self.write(os.path.join("keys", service), value)
        pycache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, pycache)
        self.environ.update({"FAKE_LOG": self.log, "FAKE_KEYS": self.keys, "FACTORY_KEYCHAIN": "secret-tool",
                             "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": pycache})
        self.checkout(self.root)
        self.broker = os.path.join(self.state, "broker")
        self.sleeps = []

    def tool(self, name, body):
        # The fakes carry their own #! line.
        return self.write(os.path.join("bin", name), body, 0o755)

    def checkout(self, root):
        """A small checkout: dev (granted tools), rev (none), opt (optional, off) and a Coordinator
        grant; the factory package, scripts/lib and the shim, so the shim can run too."""
        rel = os.path.relpath(root, self.tmp)
        self.write(os.path.join(rel, "broker", "tools.txt"), "# tools\n" + TOOLS.replace(" ", "\n") + "\n")
        for role, conf in (("dev", 'TEMPLATE="developer"\nWORKSPACE="rw"\nBROKER_TOOLS="git_fetch  npm_fetch"\n'),
                           ("rev", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nBROKER_TOOLS=""\n'),
                           ("opt", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nOPTIONAL="1"\nBROKER_TOOLS="web_fetch"\n')):
            self.write(os.path.join(rel, "roles", role, "role.conf"), conf)
            self.write(os.path.join(rel, "roles", role, "instructions.md"), "Do the work.\n")
            self.write(os.path.join(rel, "roles", role, "kit", "spec.yaml"), "name: %s\n" % role)
        self.write(os.path.join(rel, "kits", "coordinator", "broker.conf"), 'BROKER_TOOLS="github_create_pr"\n')
        self.write(os.path.join(rel, "kits", "broker", "spec.yaml"), "name: broker\n")
        shutil.copytree(os.path.join(REPO, "factory"), os.path.join(root, "factory"),
                        ignore=shutil.ignore_patterns("__pycache__", "test_*"))
        shutil.copytree(os.path.join(REPO, "scripts", "lib"), os.path.join(root, "scripts", "lib"))
        shutil.copy(os.path.join(REPO, "scripts", "start-broker.sh"), os.path.join(root, "scripts"))

    def fake(self, name, text):
        self.write(os.path.join("log", name), text)

    def logged(self, name):
        try:
            with open(os.path.join(self.log, name), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return b""

    def calls(self):
        """The sbx calls, as argv lists, in order (call n is calls()[n - 1])."""
        raw = self.logged("sbx-argv.bin").split(b"\1")[:-1]
        return [[a.decode() for a in call.split(b"\0")[:-1]] for call in raw]

    def stdins(self):
        """{call number: what that sbx call read on stdin}, for the calls that read one."""
        return {int(n.split(".")[1]): self.logged(n) for n in os.listdir(self.log) if n.startswith("stdin.")}

    def start(self, *args, **env):
        """Run `factory _start-broker ARGS` in this process: (status, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()

        def runner(argv, env=None, stdout=None, stderr=None, **kw):
            cp = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if stdout is not subprocess.DEVNULL:
                out.write(cp.stdout.decode())
            if stderr is not subprocess.DEVNULL:
                err.write(cp.stderr.decode())
            return cp.returncode

        saved = broker_start._sleep
        broker_start._sleep = self.sleeps.append
        try:
            status = cli.main(["_start-broker"] + list(args), root=self.root, environ=dict(self.environ, **env),
                              stdout=out, stderr=err, runner=runner)
        finally:
            broker_start._sleep = saved
        return status, out.getvalue(), err.getvalue()

    def shim(self, *args, **env):
        """Run scripts/start-broker.sh ARGS for real: (status, stdout, stderr)."""
        cp = subprocess.run(["bash", os.path.join(self.root, "scripts", "start-broker.sh")] + list(args),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(self.environ, **env), cwd=self.tmp)
        return cp.returncode, cp.stdout.decode(), cp.stderr.decode()

    def read(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()

    def existing_broker(self, mcp="factory-broker-dev\nfactory-broker-coordinator\n"):
        """A broker that exists, bound to the project, with both tokens minted and registered."""
        self.fake("sandboxes", "factory-broker\n")
        self.fake("ports", '[{"host_port": 18765}]')
        self.fake("mcp", mcp)
        os.makedirs(os.path.join(self.broker, "tokens"))
        lines = ""
        for role in ("dev", "coordinator"):
            self.write(os.path.join(self.broker, "tokens", role + ".sha256"), "oldhash-%s\n" % role)
            lines += "(global)  -  mcp:factory-broker-%s:token  \n" % role
        self.fake("secrets", lines)
        self.write(os.path.join(self.broker, "project"), self.project + "\n")

    def assert_not_leaked(self, values, *outputs):
        """No value is in an output, or in any file under the scratch folder but the stdin
        captures and the fake keychain's own store: every argv and environment the fakes
        logged, the state and config folders. Bytecode is left out."""
        hits = []
        for value in values:
            value = value.encode()
            for out in outputs:
                self.assertNotIn(value, out.encode())
            for folder, _, files in os.walk(self.tmp):
                for name in files:
                    path = os.path.join(folder, name)
                    if folder == self.keys or name.startswith("stdin.") or name.endswith(".pyc") or os.path.islink(path):
                        continue
                    with open(path, "rb") as f:
                        if value in f.read():
                            hits.append((value, path))
        self.assertEqual(hits, [])


class FreshStartTest(BrokerFakes):
    """No broker sandbox yet: everything is minted, created, registered and delivered."""

    def test_through_the_shim(self):
        self.write(os.path.join(self.state, "secrets-manual", "jira"))
        self.write(os.path.join(self.state, "secrets-manual", "gemini"))
        status, out, err = self.shim(self.project)
        self.assertEqual((status, err), (0, ""))
        state, url = self.broker, "http://127.0.0.1:18765/mcp"
        self.assertEqual(out, "Minting broker token for dev...\n"
                              "Minting broker token for coordinator...\n"
                              "Creating broker sandbox for %s...\n"
                              "note: this is a new broker sandbox, so the 'factory secret set' marker(s) for: gemini jira "
                              "were cleared; run 'factory secret set <name>' again for each to redeliver it\n"
                              "server checked\n"
                              "Waiting for the broker to answer...\n"
                              "GitHub token for factory-broker restored from the keychain (factory-github)\n"
                              "Broker ready: %s, roles: dev coordinator (audit log: %s/audit.jsonl)\n"
                              % (self.project, url, state))
        deliver = ["exec", "-i", "factory-broker", "sh", "-c", secrets.DELIVER_SCRIPT, "_"]
        revoke = ["exec", "factory-broker", "rm", "-f"]
        header = ["--url", url, "--header", "Authorization: Bearer ${token}", "--skip-auth"]
        self.assertEqual(self.calls(), [
            ["version"],
            ["ls", "-q"],
            ["secret", "ls"],
            ["secret", "set", "-f", "mcp:factory-broker-dev:token"],
            ["secret", "set", "-f", "mcp:factory-broker-coordinator:token"],
            ["ls", "-q"],
            ["run", "-d", "--name", "factory-broker", "--pull", "never", "-t", "factory-broker:v3",
             "-p", "127.0.0.1:18765:8765", "-e", "BROKER_WORKSPACE=" + self.project, "-e", "BROKER_STATE=" + state,
             "shell", "--kit", os.path.join(self.root, "kits", "broker"), self.project, state],
            ["exec", "factory-broker", "sh", "-c", broker_start.SERVER_SCRIPT],
            ["ports", "factory-broker", "--json"],
            ["ports", "factory-broker", "--publish", "127.0.0.1:18765:8765"],
            ["mcp", "ls", "-q"],
            ["mcp", "add", "factory-broker-dev"] + header,
            ["mcp", "add", "factory-broker-coordinator"] + header,
            deliver + ["jira"],
            revoke + ["/etc/factory-broker/secrets/gemini"],
            revoke + ["/etc/factory-broker/secrets/npm-registry"],
            deliver + ["gitlab"],
            revoke + ["/etc/factory-broker/secrets/approvals-webhook"],
            ["secret", "set", "-f", "github", "--sandbox", "factory-broker"],
        ])
        stdins = self.stdins()
        self.assertEqual(sorted(stdins), [4, 5, 14, 17, 19])
        self.assertEqual((stdins[14], stdins[17], stdins[19]),
                         (b"jira-Vq3Lp", b"gitlab-Hn8Tc", b"github-Zr5Wk"))
        tokens = {"dev": stdins[4].decode(), "coordinator": stdins[5].decode()}
        for role, token in tokens.items():
            self.assertRegex(token, r"\A[0-9a-f]{64}\Z")
            self.assertEqual(self.read(state, "tokens", role + ".sha256"),
                             hashlib.sha256(token.encode()).hexdigest() + "\n")
        self.assertNotEqual(tokens["dev"], tokens["coordinator"])
        self.assertEqual(self.read(state, "grants.json"),
                         '{\n  "dev": {\n    "tools": [\n      "git_fetch",\n      "npm_fetch"\n    ],\n'
                         '    "source": "roles/dev/role.conf"\n  },\n  "coordinator": {\n    "tools": [\n'
                         '      "github_create_pr"\n    ],\n    "source": "kits/coordinator/broker.conf"\n  }\n}\n')
        self.assertEqual(json.loads(self.read(state, "grants.json"))["dev"]["tools"], ["git_fetch", "npm_fetch"])
        self.assertEqual(self.read(state, "project"), self.project + "\n")
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        self.assertEqual(self.read(self.state, "sandboxes", "factory-broker"),
                         fingerprints.broker_fingerprint(self.paths) + "\n")
        self.assertEqual(os.listdir(os.path.join(self.state, "secrets-manual")), [])
        self.assertFalse(os.path.exists(os.path.join(state, "grants.json.tmp")))
        self.assertEqual(self.read(self.log, "mcp"), "factory-broker-dev\nfactory-broker-coordinator\n")
        # No value, keychain or minted, anywhere but on a program's stdin.
        self.assert_not_leaked(list(VALUES.values()) + list(tokens.values()), out, err)

    def test_the_github_token_is_never_set_globally(self):
        self.assertEqual(self.start(self.project)[0], 0)
        github = [c for c in self.calls() if c[:2] == ["secret", "set"] and "github" in c]
        self.assertEqual(github, [["secret", "set", "-f", "github", "--sandbox", "factory-broker"]])

    def test_local_kit_and_plugins(self):
        self.write(os.path.join(self.config, "broker-kit", "spec.yaml"), "name: local\n")
        for plugin in ("zeta", "alpha", ".hidden", "__pycache__"):
            os.makedirs(os.path.join(self.config, "plugins", plugin))
        self.write(os.path.join(self.config, "plugins", "notes.txt"), "not a plugin")
        status, out, err = self.start(self.project)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("Including local broker kit from %s\nEnabling broker plugins: alpha,zeta\n"
                      % os.path.join(self.config, "broker-kit"), out)
        run = [c for c in self.calls() if c[0] == "run"][0]
        self.assertEqual(run[run.index("shell"):], ["shell", "--kit", os.path.join(self.root, "kits", "broker"),
                                                    "--kit", os.path.join(self.config, "broker-kit"),
                                                    "-e", "PASSDESK_PLUGINS=alpha,zeta", self.project, self.broker])

    def test_old_in_repo_state_is_moved(self):
        self.write(os.path.join(self.root, ".broker", "audit.jsonl"), "{}\n")
        status, out, _ = self.start(self.project)
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("Moving %s to %s...\n" % (os.path.join(self.root, ".broker"), self.broker)))
        self.assertEqual(self.read(self.broker, "audit.jsonl"), "{}\n")
        os.makedirs(os.path.join(self.root, ".roles"))
        os.makedirs(os.path.join(self.state, "roles"))
        self.assertEqual(self.start(self.project)[1:], ("", "error: state exists both in %s and %s; keep one (move or "
                                                            "delete the other), then re-run\n"
                                                        % (os.path.join(self.root, ".roles"),
                                                           os.path.join(self.state, "roles"))))


class ExistingBrokerTest(BrokerFakes):
    def test_tokens_registrations_and_the_port_are_kept(self):
        self.existing_broker()
        status, out, err = self.start(self.project)
        self.assertEqual((status, err), (0, ""))
        self.assertTrue(out.startswith("Starting existing broker sandbox...\nserver checked\n"))
        self.assertNotIn("Minting", out)
        calls = self.calls()
        self.assertIn(["run", "-d", "--name", "factory-broker"], calls)
        self.assertFalse([c for c in calls if c[:2] in (["mcp", "add"], ["ports", "factory-broker"]) and "--json" not in c])
        self.assertFalse([c for c in calls if c[:3] == ["secret", "set", "-f"] and c[3].startswith("mcp:")])
        self.assertEqual(self.read(self.broker, "tokens", "dev.sha256"), "oldhash-dev\n")
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandboxes", "factory-broker")))

    def test_a_token_whose_secret_is_gone_is_minted_again(self):
        self.existing_broker()
        self.fake("secrets", "(global)  -  mcp:factory-broker-coordinator:token  \n")
        status, out, _ = self.start(self.project)
        self.assertEqual(status, 0)
        self.assertIn("Minting broker token for dev...\n", out)
        self.assertNotIn("Minting broker token for coordinator", out)
        self.assertNotEqual(self.read(self.broker, "tokens", "dev.sha256"), "oldhash-dev\n")

    def test_a_former_role_loses_its_access(self):
        self.existing_broker("factory-broker-dev\nfactory-broker-coordinator\nfactory-broker-gone\nsomething-else\n")
        self.write(os.path.join(self.broker, "tokens", "gone.sha256"), "h\n")
        status, out, err = self.start(self.project)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("Removing broker access for former role gone...\n", out)
        calls = self.calls()
        for argv in (["mcp", "rm", "--force", "factory-broker-gone"],
                     ["secret", "rm", "-f", "mcp:factory-broker-gone:token"],
                     ["secret", "rm", "-f", "mcp:factory-broker-gone:token:endpoint"]):
            self.assertIn(argv, calls)
        self.assertFalse(os.path.exists(os.path.join(self.broker, "tokens", "gone.sha256")))
        self.assertTrue(os.path.exists(os.path.join(self.broker, "tokens", "dev.sha256")))
        self.assertNotIn("something-else", "".join(" ".join(c) for c in calls if c[:2] == ["mcp", "rm"]))

    def test_a_former_role_name_never_reaches_outside_the_tokens_folder(self):
        self.existing_broker("factory-broker-dev\nfactory-broker-coordinator\nfactory-broker-../../victim\n")
        victim = self.write(os.path.join(self.state, "victim.sha256"), "keep me\n")
        self.assertEqual(self.start(self.project)[0], 0)
        self.assertIn(["mcp", "rm", "--force", "factory-broker-../../victim"], self.calls())
        self.assertEqual(self.read(victim), "keep me\n")

    def test_bound_to_another_project(self):
        self.existing_broker()
        self.write(os.path.join(self.broker, "project"), "/somewhere/else\n")
        self.assertEqual(self.start(self.project), (1, "", "error: the broker is bound to /somewhere/else. To switch "
                                                           "projects, see README (Switching projects).\n"))
        self.assertEqual(self.read(self.broker, "project"), "/somewhere/else\n")

    def test_a_bound_project_with_no_sandbox_is_rebound(self):
        os.makedirs(self.broker)
        self.write(os.path.join(self.broker, "project"), "/somewhere/else\n")
        self.assertEqual(self.start(self.project)[0], 0)
        self.assertEqual(self.read(self.broker, "project"), self.project + "\n")

    def test_markers_are_kept_for_an_existing_sandbox(self):
        self.existing_broker()
        self.write(os.path.join(self.state, "secrets-manual", "jira"))
        status, out, _ = self.start(self.project)
        self.assertEqual(status, 0)
        self.assertIn("note: jira is managed by 'factory secret set/rm'; leaving it as is\n", out)
        self.assertNotIn(["exec", "-i", "factory-broker", "sh", "-c", secrets.DELIVER_SCRIPT, "_", "jira"], self.calls())


class CredentialFailureTest(BrokerFakes):
    def test_a_failed_delivery_only_warns(self):
        # The choice for #179: a delivery that fails stays a warning here, so the factory still starts.
        self.existing_broker()
        status, out, err = self.start(self.project, FAKE_DELIVER_FAIL="1")
        self.assertEqual(status, 0)
        self.assertEqual(err, "warning: could not deliver the jira credential to the broker sandbox (an old broker image? "
                              "see README, Upgrading, for the rebuild steps)\n"
                              "warning: could not deliver the gitlab credential to the broker sandbox (an old broker "
                              "image? see README, Upgrading, for the rebuild steps)\n")
        self.assertIn("Broker ready: ", out)

    def test_a_locked_keychain_only_warns(self):
        self.existing_broker()
        status, out, err = self.start(self.project, FAKE_LOCKED="factory-github")
        self.assertEqual(status, 0)
        self.assertIn("warning: could not read factory-github from the keychain (locked or unavailable); "
                      "leaving the broker's GitHub token as it is.", err)
        self.assertNotIn(["secret", "set", "-f", "github", "--sandbox", "factory-broker"], self.calls())

    def test_a_failed_revocation_is_fatal_after_every_name_is_tried(self):
        self.existing_broker()
        status, out, err = self.start(self.project, FAKE_RM_FAIL="1")
        self.assertEqual(status, 1)
        self.assertNotIn("Broker ready", out)
        for name in ("gemini", "npm-registry", "approvals-webhook"):
            self.assertIn("error: could not remove the %s credential file from the broker sandbox; it may still be "
                          "active even though its keychain entry is gone\n" % name, err)
        self.assertTrue(err.endswith("error: a credential revocation failed above; it may still be active in the "
                                     "broker sandbox - see the error(s) above\n"))
        self.assertIn(["secret", "set", "-f", "github", "--sandbox", "factory-broker"], self.calls())
        self.assertEqual(err.count("sbx: rm refused\n"), 3)


class FailureTest(BrokerFakes):
    def test_usage(self):
        usage = "error: usage: %s <project-dir>\n" % os.path.join(self.root, "scripts", "start-broker.sh")
        self.assertEqual(self.start(), (1, "", usage))
        self.assertEqual(self.start("a", "b"), (1, "", usage))
        shim = os.path.join(self.root, "scripts", "start-broker.sh")
        self.assertEqual(self.shim(), (1, "", "error: usage: %s <project-dir>\n" % shim))
        self.assertEqual(self.calls(), [])

    def test_no_project_folder(self):
        missing = os.path.join(self.tmp, "nope")
        self.assertEqual(self.start(missing), (1, "", "error: project directory not found: %s\n" % missing))
        self.assertEqual(self.shim(missing), (1, "", "error: project directory not found: %s\n" % missing))

    def test_state_inside_the_project(self):
        state = os.path.join(self.project, "st")
        self.assertEqual(self.start(self.project, XDG_STATE_HOME=state),
                         (1, "", "error: factory state (%s/ai-code-factory) is inside the project %s, where agents "
                                 "could edit it; set XDG_STATE_HOME elsewhere\n" % (state, self.project)))

    def test_an_unknown_coordinator_tool(self):
        self.write(os.path.join(self.root, "kits", "coordinator", "broker.conf"), 'BROKER_TOOLS="git_push nope"\n')
        self.assertEqual(self.start(self.project), (1, "", "error: kits/coordinator/broker.conf: unknown broker tool "
                                                           "'nope' (known: %s)\n" % TOOLS))

    def test_a_failed_token_set_leaves_the_old_hash(self):
        self.existing_broker()
        self.fake("secrets", "")
        status, out, err = self.start(self.project, FAKE_SET_FAIL="1")
        self.assertEqual((status, out), (1, "Minting broker token for dev...\n"))
        self.assertEqual(err, "error: could not store the broker token secret 'mcp:factory-broker-dev:token' with sbx; "
                              "the token hash was not changed. Check 'sbx secret ls' and that sbx is signed in, then "
                              "run this again\n")
        self.assertEqual(self.read(self.broker, "tokens", "dev.sha256"), "oldhash-dev\n")
        self.assertNotIn(["run", "-d", "--name", "factory-broker"], self.calls())

    def test_a_failed_sandbox_create_shows_its_last_20_lines(self):
        status, out, err = self.start(self.project, FAKE_RUN_FAIL="1")
        self.assertEqual(status, 1)
        self.assertTrue(out.endswith("Creating broker sandbox for %s...\n" % self.project))
        run = [c for c in self.calls() if c[0] == "run"][0]
        self.assertEqual(err, "error: could not create the broker sandbox (exit status 3)\n"
                              "last output of '%s' (up to 20 lines):\n" % " ".join(["sbx"] + run)
                         + "".join("  run line %d\n" % i for i in range(7, 26)) + "  sbx: no such image\n")
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandboxes", "factory-broker")))

    def test_a_failed_server_start_stops_with_its_status(self):
        self.existing_broker()
        status, out, _ = self.start(self.project, FAKE_EXEC_FAIL="1")
        self.assertEqual(status, 5)
        self.assertTrue(out.endswith("Starting existing broker sandbox...\nserver checked\n"))

    def test_not_ready(self):
        self.existing_broker()
        status, out, err = self.start(self.project, FAKE_HTTP="503")
        self.assertEqual((status, err), (1, "error: broker did not become ready (last HTTP status: 503); see %s\n"
                                         % os.path.join(self.broker, "broker.log")))
        self.assertEqual(self.logged("curl-calls").count(b"x"), 30)
        self.assertEqual(self.sleeps, [1] * 30)
        self.assertNotIn(["mcp", "ls", "-q"], self.calls())

    def test_ready_on_a_later_try(self):
        self.existing_broker()
        self.tool("curl", '#!/bin/sh\necho x >> "$FAKE_LOG/curl-calls"\n'
                          '[ "$(wc -l < "$FAKE_LOG/curl-calls")" -ge 3 ] && printf 401 || printf 000\n')
        self.assertEqual(self.start(self.project)[0], 0)
        self.assertEqual(self.sleeps, [1, 1])

    def test_the_port_listing(self):
        ctx = self.ctx()
        for listing, published in (('[{"host_port": 18765}]', True), ('{"a": {"host_port": 18765}}', True),
                                   ('[{"host_port": 18766}]', False), ('[{"host_port": "18765"}]', False),
                                   ('[null, 3]', False), ("not json", False), ("", False)):
            with self.subTest(listing=listing):
                self.fake("ports", listing)
                self.assertEqual(broker_start.port_published(ctx), published)


class FingerprintTest(BrokerFakes):
    """The broker's fingerprint must be the one bash's sandbox_fingerprint computes: bash
    stale_sandboxes compares against it until `start` is ported."""

    def bash(self):
        cp = subprocess.run(["bash", "-c", 'ROOT="$1"; . "$2/scripts/lib/common.sh"; . "$2/scripts/lib/roles.sh"; '
                             'sandbox_fingerprint factory-broker', "_", self.root, REPO],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environ, cwd=self.tmp)
        self.assertEqual(cp.stderr, b"")
        return cp.stdout.decode()

    def check(self):
        self.assertEqual(fingerprints.broker_fingerprint(self.paths) + "\n", self.bash())

    def test_the_same_as_bash(self):
        self.check()
        self.write(os.path.join(self.state, "images", "factory-broker-v3.id"), "sha256:abc123\n\n")
        self.check()
        kit = os.path.join(self.config, "broker-kit")
        self.write(os.path.join(kit, "spec.yaml"), "name: local\n")
        self.write(os.path.join(kit, "sub", "a-b.txt"), "x")
        self.write(os.path.join(kit, "sub", "a.txt"), "y")
        self.write(os.path.join(kit, "sub-z"), "z")
        self.write(os.path.join(kit, ".hidden"), "h")
        self.check()
        self.write(os.path.join(kit, "broker.conf"), "left out")
        os.symlink(os.path.join(kit, "spec.yaml"), os.path.join(kit, "link.yaml"))
        before = fingerprints.dir_hash(kit)
        self.check()
        self.write(os.path.join(kit, "sub", "broker.conf"), "left out too")
        self.assertEqual(fingerprints.dir_hash(kit), before)
        for plugin in ("b", "a", "__pycache__", ".x"):
            os.makedirs(os.path.join(self.config, "plugins", plugin))
        self.check()

    def test_a_kit_that_is_a_link_is_not_descended(self):
        real = os.path.join(self.tmp, "real-kit")
        self.write(os.path.join(real, "spec.yaml"), "name: local\n")
        os.makedirs(self.config)
        os.symlink(real, os.path.join(self.config, "broker-kit"))
        self.check()
        self.assertEqual(fingerprints.dir_hash(os.path.join(self.config, "broker-kit")),
                         hashlib.sha256(b"").hexdigest().encode())

    def test_no_folder_is_none(self):
        self.assertEqual(fingerprints.dir_hash(os.path.join(self.tmp, "absent")), b"none")


class StateWriteFailureTest(BrokerFakes):
    def test_a_state_file_that_cannot_be_written_is_an_error(self):
        os.makedirs(os.path.join(self.broker, "grants.json.tmp"))
        status, out, err = self.start(self.project)
        self.assertEqual((status, out), (1, ""))
        self.assertTrue(err.startswith("error: %s: " % os.path.join(self.broker, "grants.json.tmp")), err)
        self.assertEqual(err.count("\n"), 1)
