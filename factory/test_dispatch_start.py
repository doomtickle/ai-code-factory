"""Tests for factory/dispatch_start.py (`factory _start-dispatch`, run by scripts/start-dispatch.sh).
Ported from the start-dispatch parts of scripts/test_factory_state.sh and scripts/test_factory_config.sh;
the output, exit status, sbx calls and files written were also compared with the bash this replaced,
on the same stubs, before that was deleted.

sbx, ps and lsof are stubs on a private PATH, the bridge is a small stand-in server
(dispatch/server.py in the scratch checkout) on an ephemeral 127.0.0.1 port, and the machine is
made up (ctx.uname), so nothing here touches a real sandbox, HOME or fixed port.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

from factory import dispatch_start, policy, state
from factory.cli import INTERNAL
from factory.context import FactoryError
from factory.fixtures import REPO, CommandTest
from factory.preflight import SBX_VERSION

SERVER = '''
import http.server, json, os, sys, time
with open(os.environ["FAKE_ENV_OUT"], "w") as f:
    json.dump({k: v for k, v in os.environ.items() if k.startswith("DISPATCH_")}, f)
mode = os.environ.get("FAKE_SERVER_MODE", "ok")
if mode == "exit":
    sys.exit(3)
if mode == "silent":
    time.sleep(100)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(401 if mode == "ok" else 200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", int(os.environ["DISPATCH_PORT"])), Handler).serve_forever()
'''

SBX = r'''printf '%s\n' "$*" >> "$FAKE_SBX_ARGS"
case "$1" in
  version) echo "sbx version: v''' + SBX_VERSION + r''' testcommit" ;;
  mcp) case "$2" in
    ls) [ -z "${FAKE_MCP:-}" ] || printf '%s\n' "$FAKE_MCP" ;;
    add) [ "${FAKE_MCP_FAIL:-}" != 1 ] || { echo "sbx: mcp add boom" >&2; exit 7; } ;;
    esac ;;
  secret) case "$2" in
    ls) [ -z "${FAKE_SECRETS:-}" ] || printf '%s\n' "$FAKE_SECRETS" ;;
    set) cat > "$FAKE_SECRET_VALUE"; [ "${FAKE_SET_FAIL:-}" != 1 ] || { echo "sbx: not signed in" >&2; exit 1; } ;;
    esac ;;
esac
exit 0
'''

SECRET_NAME = "mcp:factory-dispatch:token"
STORED = "  %s  x" % SECRET_NAME


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def umask():
    old = os.umask(0)
    os.umask(old)
    return old


def fake_sleep(seconds):
    time.sleep(0.15)


class StartDispatchTest(CommandTest):
    def setUp(self):
        super().setUp()
        for name in ("roles",):
            shutil.copytree(os.path.join(REPO, name), os.path.join(self.root, name))
        os.makedirs(os.path.join(self.root, "broker"))
        shutil.copy(os.path.join(REPO, "broker", "tools.txt"), os.path.join(self.root, "broker", "tools.txt"))
        self.write("repo/README.md", "readme\n")
        self.write("repo/docs/setup-wsl.md", "wsl\n")
        self.write("repo/docs/guides/01-first.md", "one\n")
        self.write("repo/docs/guides/02-second.md", "two\n", 0o600)
        self.write("repo/docs/guides/notes.txt", "not markdown\n")
        self.write("repo/docs/guides/.hidden.md", "hidden\n")
        os.makedirs(os.path.join(self.root, "docs", "guides", "dir.md"))
        self.write("repo/dispatch/server.py", SERVER)
        self.write("project/file", "")
        self.project = os.path.join(self.tmp, "project")
        self.dstate = os.path.join(self.state, "dispatch")
        self.args_log = os.path.join(self.tmp, "sbx-args.log")
        self.secret_value = os.path.join(self.tmp, "secret-value")
        self.env_out = os.path.join(self.tmp, "bridge-env.json")
        self.environ.update(FAKE_SBX_ARGS=self.args_log, FAKE_SECRET_VALUE=self.secret_value,
                            FAKE_ENV_OUT=self.env_out, PYTHONDONTWRITEBYTECODE="1")
        self.tool("sbx", SBX)
        self.tool("ps", 'echo "python3 %s/dispatch/server.py"\n' % self.root)
        os.symlink(sys.executable, os.path.join(self.tmp, "bin", "python3"))
        self.procs = []
        real = dispatch_start._start_bridge

        def start_bridge(*args):
            proc = real(*args)
            self.procs.append(proc)
            return proc

        port = free_port()
        for patcher in (mock.patch.object(dispatch_start, "DISPATCH_PORT", port),
                        mock.patch.object(dispatch_start, "_start_bridge", start_bridge),
                        mock.patch.object(state, "sleep", fake_sleep)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_bridges)
        self.port = port

    def stop_bridges(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        try:
            with open(os.path.join(self.dstate, "pid")) as f:
                os.kill(int(f.read()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def ctx(self, tty=False):
        ctx = super().ctx(tty)
        ctx.uname = ("Linux", "x86_64")
        return ctx

    def go(self, *args):
        """Run the command: (exit status, stdout). A failure raises FactoryError."""
        ctx = self.ctx()
        rc = dispatch_start.main(ctx, list(args))
        return rc, self.out.getvalue()

    def error(self, *args):
        """The message the command fails with."""
        with self.assertRaises(FactoryError) as caught:
            self.go(*args)
        return str(caught.exception)

    def start(self, **env):
        self.environ.update(env)
        return self.go(self.project)

    def sbx_calls(self):
        try:
            with open(self.args_log) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def put_config(self, name, text, mode=None):
        return self.write(os.path.join("config", "ai-code-factory", name), text, mode)

    def read(self, *parts):
        with open(os.path.join(self.dstate, *parts), "rb") as f:
            return f.read().decode()

    def pid(self):
        return int(self.read("pid"))

    def tree(self):
        found = {}
        for d, _, files in os.walk(self.dstate):
            for n in files:
                p = os.path.join(d, n)
                with open(p, "rb") as f:
                    found[os.path.relpath(p, self.dstate)] = f.read().decode()
        return found


class CommandLineTest(StartDispatchTest):
    def test_it_is_a_hidden_command_not_a_listed_one(self):
        from factory.cli import COMMANDS
        self.assertIs(INTERNAL["_start-dispatch"], dispatch_start.main)
        self.assertNotIn("_start-dispatch", COMMANDS)

    def test_the_shim_runs_the_python(self):
        shim = os.path.join(REPO, "scripts", "start-dispatch.sh")
        self.assertEqual(subprocess.run(["bash", "-n", shim]).returncode, 0)
        with open(shim) as f:
            text = f.read()
        self.assertIn('exec "$(resolve_python)" "$ROOT/factory" _start-dispatch "$@"', text)
        env = dict(os.environ, HOME=self.tmp, XDG_CONFIG_HOME=os.path.join(self.tmp, "config"),
                   XDG_STATE_HOME=os.path.join(self.tmp, "state"), PYTHONDONTWRITEBYTECODE="1")
        cp = subprocess.run([shim], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=self.tmp)
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr.decode()),
                         (1, b"", "error: usage: %s <project-dir>\n" % os.path.join(REPO, "scripts", "start-dispatch.sh")))

    def test_the_port_and_mcp_name_are_common_shs(self):
        with open(os.path.join(REPO, "scripts", "lib", "common.sh")) as f:
            text = f.read()
        self.assertEqual(re.search(r'^readonly DISPATCH_MCP="(.*)"$', text, re.M).group(1), "factory-dispatch")
        self.assertEqual(re.search(r'^readonly DISPATCH_PORT="(.*)"$', text, re.M).group(1), "18766")
        with mock.patch.object(dispatch_start, "DISPATCH_PORT", 18766):
            self.assertEqual(dispatch_start._url(), "http://127.0.0.1:18766/mcp")
        self.assertEqual(dispatch_start.DISPATCH_MCP, "factory-dispatch")

    def test_it_takes_exactly_one_directory_that_exists(self):
        usage = "usage: %s <project-dir>" % os.path.join(self.root, "scripts", "start-dispatch.sh")
        self.assertEqual(self.error(), usage)
        self.assertEqual(self.error("a", "b"), usage)
        self.assertEqual(self.error(os.path.join(self.tmp, "nope")), "project directory not found: %s" % os.path.join(self.tmp, "nope"))
        self.assertEqual(self.error(""), "project directory not found: ")
        self.assertEqual(self.sbx_calls(), [])

    def test_it_needs_the_pinned_sbx(self):
        self.environ["PATH"] = "/usr/bin:/bin"
        self.assertEqual(self.error(self.project), "'sbx' not found in PATH (see README.md)")
        self.environ["PATH"] = os.path.join(self.tmp, "bin") + ":/usr/bin:/bin"
        self.tool("sbx", 'echo "sbx version: v0.0.1 x"\n')
        self.assertEqual(self.error(self.project), "sbx %s required, found '0.0.1' (see README.md)" % SBX_VERSION)

    def test_it_needs_the_broker_tool_list_before_starting_anything(self):
        os.unlink(os.path.join(self.root, "broker", "tools.txt"))
        self.assertIn("tools.txt", self.error(self.project))
        self.assertFalse(os.path.exists(self.dstate))


class TokenTest(StartDispatchTest):
    def test_a_failing_set_names_the_secret_and_leaves_no_hash(self):
        self.environ["FAKE_SET_FAIL"] = "1"
        message = self.error(self.project)
        self.assertIn(SECRET_NAME, message)
        self.assertIn("token hash was not changed", message)
        self.assertFalse(os.path.exists(os.path.join(self.dstate, "token.sha256")))
        self.assertFalse(any(c.startswith("mcp add") for c in self.sbx_calls()))

    def test_a_failing_set_leaves_an_older_hash_unchanged(self):
        os.makedirs(self.dstate)
        self.write("state/ai-code-factory/dispatch/token.sha256", "oldhash\n")
        self.environ["FAKE_SET_FAIL"] = "1"
        self.error(self.project)
        self.assertEqual(self.read("token.sha256"), "oldhash\n")

    def test_the_token_goes_to_sbx_on_stdin_and_its_hash_to_the_file(self):
        rc, out = self.start()
        self.assertEqual(rc, 0)
        self.assertIn("Minting dispatch token...\n", out)
        with open(self.secret_value) as f:
            value = f.read()
        self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertEqual(self.read("token.sha256"), hashlib.sha256(value.encode()).hexdigest() + "\n")
        self.assertIn("secret set -f " + SECRET_NAME, self.sbx_calls())
        self.assertFalse([c for c in self.sbx_calls() if value in c])
        self.assertNotIn(value, out + self.err.getvalue())
        self.assertEqual(os.stat(os.path.join(self.dstate, "token.sha256")).st_mode & 0o777, 0o666 & ~umask())
        self.assertEqual(os.stat(self.dstate).st_mode & 0o777, 0o700)

    def test_a_stored_secret_and_a_hash_are_not_minted_again(self):
        self.start()
        first = self.read("token.sha256")
        rc, out = self.start(FAKE_SECRETS=STORED)
        self.assertNotIn("Minting", out)
        self.assertEqual(self.read("token.sha256"), first)
        self.assertEqual([c for c in self.sbx_calls() if c.startswith("secret set")], ["secret set -f " + SECRET_NAME])

    def test_a_missing_secret_or_an_empty_hash_is_minted_again(self):
        self.start(FAKE_SECRETS=STORED)
        self.assertEqual([c for c in self.sbx_calls() if c.startswith("secret set")], ["secret set -f " + SECRET_NAME])
        self.write("state/ai-code-factory/dispatch/token.sha256", "")
        _, out = self.start(FAKE_SECRETS=STORED)
        self.assertIn("Minting dispatch token...", out)
        self.assertNotEqual(self.read("token.sha256"), "")
        _, out = self.start(FAKE_SECRETS="  mcp:factory-dispatch:token-other  x")
        self.assertIn("Minting dispatch token...", out)


class SnapshotTest(StartDispatchTest):
    def test_each_role_gets_its_instructions_and_this_projects_notes(self):
        self.put_config("projects/project.md", "Test with make.\n")
        self.put_config("projects/project.developer.md", "Docs style X.\n")
        _, out = self.start()
        with open(os.path.join(self.root, "roles", "developer", "instructions.md")) as f:
            base = f.read()
        self.assertEqual(self.read("instructions", "developer.md"),
                         base + "\n# This project (project)\nTest with make.\n"
                         "\n# This project (project), for the developer\nDocs style X.\n")
        with open(os.path.join(self.root, "roles", "reviewer", "instructions.md")) as f:
            self.assertEqual(self.read("instructions", "reviewer.md"),
                             f.read() + "\n# This project (project)\nTest with make.\n")
        self.assertIn("Added developer notes from %s to its instructions\n" % os.path.join(
            self.paths.config, "projects", "project.developer.md"), out)
        self.assertIn("Added project notes from %s to the role instructions\n" % os.path.join(
            self.paths.config, "projects", "project.md"), out)

    def test_without_notes_the_instructions_are_copied_as_they_are(self):
        self.start()
        with open(os.path.join(self.root, "roles", "developer", "instructions.md")) as f:
            self.assertEqual(self.read("instructions", "developer.md"), f.read())
        self.assertEqual(os.listdir(os.path.join(self.dstate, "role-overrides")), [])
        self.assertEqual(self.read("enabled-roles"), "")
        self.assertFalse(os.path.exists(os.path.join(self.dstate, "lane-identity.conf")))

    def test_an_override_is_copied_with_its_mode_and_reported(self):
        path = self.put_config("roles/developer.conf", 'MODEL="opus"\nEFFORT="high"\n', 0o640)
        _, out = self.start()
        self.assertEqual(self.read("role-overrides", "developer.conf"), 'MODEL="opus"\nEFFORT="high"\n')
        self.assertEqual(os.stat(os.path.join(self.dstate, "role-overrides", "developer.conf")).st_mode & 0o777,
                         0o640 & ~umask())
        self.assertIn("Applied local overrides for developer from %s\n" % path, out)

    def test_an_invalid_override_stops_before_anything_is_changed(self):
        self.start()
        self.environ.update(FAKE_SECRETS=STORED)
        before = self.tree()
        self.put_config("roles/developer.conf", 'BROKER_TOOLS="git_push"\n')
        message = self.error(self.project)
        self.assertIn("BROKER_TOOLS", message)
        self.assertEqual(self.tree(), before)
        self.put_config("roles/nonesuch.conf", 'MODEL="x"\n')
        os.unlink(os.path.join(self.paths.config, "roles", "developer.conf"))
        self.assertIn("nonesuch", self.error(self.project))
        self.assertEqual(self.tree(), before)

    def test_enabled_roles_are_validated_and_written(self):
        self.put_config("enabled-roles", "# on\nresearcher\n\n")
        self.start()
        self.assertEqual(self.read("enabled-roles"), "researcher\n")
        self.put_config("enabled-roles", "no-such-role\n")
        self.assertIn("no-such-role", self.error(self.project))

    def test_an_old_enabled_list_does_not_outlive_its_file(self):
        self.put_config("enabled-roles", "researcher\n")
        self.start()
        os.unlink(os.path.join(self.paths.config, "enabled-roles"))
        self.start()
        self.assertEqual(self.read("enabled-roles"), "")

    def test_a_snapshot_replaces_the_last_one_and_leftover_temp_folders(self):
        self.write("state/ai-code-factory/dispatch/instructions/stale.md", "stale")
        self.write("state/ai-code-factory/dispatch/instructions.tmp/stale2.md", "stale")
        self.write("state/ai-code-factory/dispatch/role-overrides/old.conf", "old")
        self.write("state/ai-code-factory/dispatch/docs/old.md", "old")
        self.write("state/ai-code-factory/dispatch/docs.tmp/guides/old.md", "old")
        self.start()
        names = sorted(self.tree())
        self.assertNotIn("instructions/stale.md", names)
        self.assertFalse([n for n in names if ".tmp" in n or "old" in n or "stale" in n])
        self.assertTrue(all(not name.endswith(".tmp") for name in os.listdir(self.dstate)))

    def test_the_docs_snapshot_has_the_readme_the_wsl_guide_and_visible_markdown_guides(self):
        self.start()
        self.assertEqual(sorted(n for n in self.tree() if n.startswith("docs/")),
                         ["docs/README.md", "docs/guides/01-first.md", "docs/guides/02-second.md", "docs/setup-wsl.md"])
        self.assertEqual(os.stat(os.path.join(self.dstate, "docs", "guides", "02-second.md")).st_mode & 0o777,
                         0o600 & ~umask())

    def test_without_the_wsl_guide_or_guides_the_docs_are_the_readme_alone(self):
        os.unlink(os.path.join(self.root, "docs", "setup-wsl.md"))
        shutil.rmtree(os.path.join(self.root, "docs", "guides"))
        self.start()
        self.assertEqual(sorted(n for n in self.tree() if n.startswith("docs/")), ["docs/README.md"])
        self.assertTrue(os.path.isdir(os.path.join(self.dstate, "docs", "guides")))


class IdentityTest(StartDispatchTest):
    def identity(self, policy_text):
        self.put_config("policy.toml", policy_text + "\n")
        self.write("state/ai-code-factory/dispatch/lane-identity.conf", "stale\n")
        try:
            self.go(self.project)
        except FactoryError as e:
            return "1:" + str(e), self.snapshot()
        return "0", self.snapshot()

    def snapshot(self):
        try:
            return self.read("lane-identity.conf")
        except OSError:
            return None

    def test_unset_writes_no_snapshot_and_removes_a_stale_one(self):
        self.assertEqual(self.identity('[git]\nrepos = ["a/b"]'), ("0", None))

    def test_the_copied_example_writes_no_snapshot(self):
        with open(os.path.join(REPO, "config.example", "policy.toml")) as f:
            self.assertEqual(self.identity(f.read()), ("0", None))

    def test_name_and_email_are_written_and_reported(self):
        got = self.identity('[git]\nauthor_name = "Acme Bot ({lane})"  # a comment\nauthor_email = \'bot+{lane}@example.com\'')
        self.assertEqual(got, ("0", 'AUTHOR_NAME="Acme Bot ({lane})"\nAUTHOR_EMAIL="bot+{lane}@example.com"\n'))
        self.assertIn("Lane commits use the git identity from %s\n" % os.path.join(self.paths.config, "policy.toml"),
                      self.out.getvalue())

    def test_an_email_alone_leaves_the_name_to_the_default(self):
        self.assertEqual(self.identity('[git]\nauthor_email = "team@example.com"'),
                         ("0", 'AUTHOR_NAME=""\nAUTHOR_EMAIL="team@example.com"\n'))

    def test_a_key_in_another_table_is_ignored(self):
        self.assertEqual(self.identity('[jira]\nauthor_name = "x"'), ("0", None))

    def test_not_a_one_line_string_is_refused_and_leaves_no_snapshot(self):
        for line in ('author_name = "a\\nb"', 'author_name = "a\\"b"', "author_name = 42", 'author_name = "open'):
            got = self.identity("[git]\n" + line)
            self.assertEqual(got, ("1:[git] author_name in policy.toml must be a one-line string "
                                   "(\"...\" without escapes, or '...')", None), line)
        got = self.identity("[git]\nauthor_email = 7")
        self.assertEqual(got, ("1:[git] author_email in policy.toml must be a one-line string "
                               "(\"...\" without escapes, or '...')", None))

    def test_an_invalid_name_or_email_is_refused_and_leaves_no_snapshot(self):
        for line in ('author_name = "<root>"', 'author_name = "-x"', 'author_name = "   "',
                     'author_name = "a\033[31m"', "author_name = 'a\tb'"):
            got = self.identity("[git]\n" + line)
            self.assertEqual(got[1], None, line)
            self.assertTrue(got[0].startswith('1:[git] in policy.toml: author_name must be 1-128 characters'), line)
        for line in ('author_email = "a b@example.com"', 'author_email = "no-at"', 'author_email = "a@b@c"',
                     'author_email = "-a@b"', 'author_email = "<a@b>"'):
            got = self.identity("[git]\n" + line)
            self.assertEqual(got[1], None, line)
            self.assertTrue(got[0].startswith('1:[git] in policy.toml: author_email must look like name@domain'), line)

    def test_the_rules_in_isolation(self):
        problem = policy.lane_identity_problem
        self.assertIsNone(problem("", ""))
        self.assertIsNone(problem("x" * 128, ""))
        self.assertIsNotNone(problem("x" * 129, ""))
        self.assertIsNone(problem("", "a@" + "b" * 252))
        self.assertIsNotNone(problem("", "a@" + "b" * 253))
        self.assertIsNotNone(problem("\x7f", ""))
        self.assertIsNone(problem("{lane}", "{lane}@factory.invalid"))
        self.assertIsNotNone(problem("café" * 40, ""))  # 200 bytes: length is in bytes, as in bash's C locale


class BridgeTest(StartDispatchTest):
    def bridge_env(self):
        with open(self.env_out) as f:
            return json.load(f)

    def test_a_fresh_start_runs_the_bridge_and_registers_it(self):
        rc, out = self.start()
        url = "http://127.0.0.1:%d/mcp" % self.port
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[-2:], [
            "Starting dispatch bridge for %s..." % self.project,
            "Dispatch ready: %s (audit log: %s/audit.jsonl, transcripts: %s/logs/)" % (url, self.dstate, self.dstate)])
        self.assertEqual(self.read("project"), self.project + "\n")
        self.assertEqual(self.pid(), self.procs[0].pid)
        self.assertEqual(self.bridge_env(), {
            "DISPATCH_ROOT": self.root, "DISPATCH_PROJECT": self.project, "DISPATCH_STATE": self.dstate,
            "DISPATCH_ROLES_SCRATCH": os.path.join(self.state, "roles"), "DISPATCH_PORT": str(self.port),
            "DISPATCH_SBX": os.path.join(self.tmp, "bin", "sbx"),
            "DISPATCH_BROKER_AUDIT": os.path.join(self.state, "broker", "audit.jsonl")})
        self.assertEqual([c for c in self.sbx_calls() if c.startswith("mcp")],
                         ["mcp ls -q", "mcp add factory-dispatch --url %s --header Authorization: Bearer ${token} "
                          "--skip-auth" % url])
        self.assertTrue(os.path.exists(os.path.join(self.dstate, "dispatch.log")))

    def test_the_bridge_is_its_own_session_and_the_project_is_resolved(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.project, link)
        self.go(link)
        self.assertEqual(self.read("project"), self.project + "\n")
        self.assertEqual(os.getsid(self.pid()), self.pid())

    def test_a_running_bridge_for_the_same_project_is_kept(self):
        self.start()
        pid = self.pid()
        _, out = self.start(FAKE_MCP="factory-dispatch")
        self.assertEqual(self.pid(), pid)
        self.assertNotIn("Starting dispatch bridge", out)
        self.assertNotIn("restarting", out)
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(len([c for c in self.sbx_calls() if c.startswith("mcp add")]), 1)

    def test_a_bridge_for_another_project_is_stopped_and_started_again(self):
        self.start()
        pid = self.pid()
        other = self.write("other/file")
        other = os.path.dirname(other)
        self.environ["FAKE_MCP"] = "factory-dispatch"
        _, out = self.go(other)
        self.assertIn("Dispatch is running for another project; restarting it...\n", out)
        self.assertIn("Starting dispatch bridge for %s...\n" % other, out)
        self.assertNotEqual(self.pid(), pid)
        self.assertEqual(self.read("project"), other + "\n")
        self.assertIsNotNone(self.procs[0].wait(5))

    def test_a_pid_that_is_not_this_checkouts_bridge_is_ignored(self):
        self.write("state/ai-code-factory/dispatch/pid", "%d\n" % os.getpid())
        self.tool("ps", 'echo "vim notes.txt"\n')
        self.start()
        self.assertEqual(self.pid(), self.procs[0].pid)

    def test_something_else_on_the_port_stops_the_start(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", self.port))
        srv.listen(5)
        self.addCleanup(srv.close)
        import threading

        def answer():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                conn.recv(1024)
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n")
                conn.close()

        threading.Thread(target=answer, daemon=True).start()
        self.tool("lsof", "echo 4242\n")
        message = self.error(self.project)
        self.assertEqual(message, (
            "port %d is already in use by another process (pid 4242: python3 %s/dispatch/server.py).\n"
            "It is not this checkout's dispatch bridge; it may be one started from another copy of the factory repo.\n"
            "Stop it (e.g. run that copy's scripts/03-stop-factory.sh, or kill the process), then re-run."
            % (self.port, self.root)))
        self.assertEqual(self.procs, [])

    def test_the_port_message_without_lsof_has_no_holder(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", self.port))
        srv.listen(5)
        self.addCleanup(srv.close)
        import threading

        def answer():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                conn.recv(1024)
                conn.sendall(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                conn.close()

        threading.Thread(target=answer, daemon=True).start()
        self.tool("lsof", "exit 1\n")
        self.assertTrue(self.error(self.project).startswith("port %d is already in use by another process.\n" % self.port))

    def test_a_bridge_that_exits_is_reported_with_its_log(self):
        self.environ["FAKE_SERVER_MODE"] = "exit"
        self.assertEqual(self.error(self.project), "dispatch bridge exited; see %s/dispatch.log" % self.dstate)

    def test_a_bridge_that_never_answers_401_is_reported(self):
        self.environ["FAKE_SERVER_MODE"] = "200"
        self.assertEqual(self.error(self.project),
                         "dispatch did not become ready (last HTTP status: 200); see %s/dispatch.log" % self.dstate)
        self.environ["FAKE_SERVER_MODE"] = "silent"
        self.stop_bridges()
        self.assertEqual(self.error(self.project),
                         "dispatch did not become ready (last HTTP status: 000); see %s/dispatch.log" % self.dstate)

    def test_a_failing_mcp_add_returns_its_status_and_no_ready_line(self):
        rc, out = self.start(FAKE_MCP_FAIL="1")
        self.assertEqual(rc, 7)
        self.assertNotIn("Dispatch ready", out)

    def test_the_registered_name_is_looked_up_as_a_whole_line(self):
        self.start(FAKE_MCP="factory-dispatch-other")
        self.assertTrue([c for c in self.sbx_calls() if c.startswith("mcp add")])


class HelperTest(StartDispatchTest):
    def test_status_is_none_when_nothing_answers(self):
        self.assertIsNone(dispatch_start._status("GET"))

    def test_alive_tells_a_finished_child_from_a_running_one(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(dispatch_start._alive(proc.pid, proc))
        running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(running.wait)
        self.addCleanup(running.kill)
        self.assertTrue(dispatch_start._alive(running.pid, running))
        self.assertTrue(dispatch_start._alive(os.getpid()))

    def test_a_file_error_is_an_error_line_not_a_traceback(self):
        self.start()
        os.unlink(os.path.join(self.root, "README.md"))
        self.assertEqual(self.error(self.project), "%s: No such file or directory" % os.path.join(self.root, "README.md"))


if __name__ == "__main__":
    unittest.main()
