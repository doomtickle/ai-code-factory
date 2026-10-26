"""Tests for factory/launch.py (`factory _launch`, what scripts/02-launch-factory.sh runs and
`factory start` calls). Ported from scripts/test_claude_md_exclude.sh (the Coordinator's
claudeMdExcludes), plus the launch itself: preflight refusals, the .factory/outgoing folder, the
sandboxes created (exact argv) and recorded, and the closing notes. Before the bash was deleted,
`factory start` and the script were compared with it on 83 fixtures (docs/py-cli-migration.md,
step 7 part C2): the same status, stdout, stderr, sbx calls (with stdin) and files.

sbx is a stateful fake (FAKE_SBX below); the broker and dispatch starts (their own tests are
test_broker_start.py and test_dispatch_start.py) are replaced by stubs that record the call.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from factory import broker_start, dispatch_start, fingerprints, launch
from factory.cli import main
from factory.context import FactoryError
from factory.fixtures import REPO, CommandTest

# A stateful sbx: sandboxes (names, workspaces, the files under each one's /etc/claude-code),
# secrets and MCP registrations live in $FAKE_SBX; every call's argv (and an `exec -i` stdin)
# goes to $FAKE_SBX/log. FAKE_FAIL names calls that fail: ls, create; nosettings and nobriefing
# make `create` leave out the kit's managed settings or briefing.
FAKE_SBX = r'''import json, os, sys
S = os.environ["FAKE_SBX"]
a = sys.argv[1:]
data = sys.stdin.buffer.read() if a[:1] == ["exec"] and "-i" in a[:4] else b""
with open(os.path.join(S, "log"), "a") as f:
    f.write(json.dumps({"argv": a, "stdin": data.decode()}) + "\n")
def rd(name, default=""):
    try:
        with open(os.path.join(S, name)) as f:
            return f.read()
    except OSError:
        return default
names = [n for n in rd("names").split("\n") if n]
fail = os.environ.get("FAKE_FAIL", "")
if a[:1] == ["version"]:
    print("sbx version: v%s test" % os.environ.get("FAKE_VERSION", "0.45.1"))
elif a[:3] == ["settings", "get", "mcp.forceLocalGateway"]:
    print(rd("settings", "true").strip())
elif a[:2] == ["template", "ls"]:
    sys.stdout.write(rd("templates", "factory-developer:v3\nfactory-reviewer:v3\nfactory-broker:v3\n"))
elif a[:1] == ["ls"]:
    if "ls" in fail:
        sys.exit("sbx: daemon down")
    if "--json" in a:
        print(json.dumps({"sandboxes": [{"name": n, "workspaces": [w for w in rd("ws/" + n).split("\n") if w]}
                                        for n in names]}))
    else:
        sys.stdout.write("".join(n + "\n" for n in names))
elif a[:1] == ["create"]:
    if "create" in fail:
        sys.stderr.write("sbx: create failed\n")
        sys.exit(6)
    n = a[a.index("--name") + 1]
    with open(os.path.join(S, "names"), "a") as f:
        f.write(n + "\n")
    os.makedirs(os.path.join(S, "ws"), exist_ok=True)
    with open(os.path.join(S, "ws", n), "w") as f:
        f.write("".join(w + "\n" for w in a[a.index("claude") + 1:]))
    vm = os.path.join(S, "vm", n)
    os.makedirs(vm, exist_ok=True)
    if "nosettings" not in fail:
        with open(os.path.join(vm, "managed-settings.json"), "w") as f:
            f.write('{"model":"opus"}\n')
    if "nobriefing" not in fail:
        open(os.path.join(vm, "CLAUDE.md"), "w").close()
    print("created " + n)
elif a[:1] == ["exec"]:
    rest = a[1:]
    while rest[0].startswith("-"):
        rest = rest[2:] if rest[0] == "-u" else rest[1:]
    vm = os.path.join(S, "vm", rest[0])
    cmd = rest[1:]
    if cmd[:1] == ["cat"]:
        try:
            sys.stdout.write(open(os.path.join(vm, "managed-settings.json")).read())
        except OSError:
            sys.exit("cat: %s: No such file or directory" % cmd[1])
    elif cmd[:2] == ["sh", "-c"]:
        with open(os.path.join(vm, "managed-settings.json"), "w") as f:
            f.write(data.decode())
    elif cmd[:2] == ["test", "-f"]:
        sys.exit(0 if os.path.exists(os.path.join(vm, "CLAUDE.md")) else 1)
elif a[:2] == ["secret", "ls"]:
    sys.stdout.write(rd("secrets"))
elif a[:2] == ["mcp", "ls"]:
    sys.stdout.write(rd("mcp"))
elif a[:1] == ["rm"]:
    with open(os.path.join(S, "names"), "w") as f:
        f.write("".join(n + "\n" for n in names if n not in a[1:]))
'''

LOGIN = "(global)  oauth  anthropic  configured\n"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class LaunchTest(CommandTest):
    """A checkout with the real roles/ and kits/, a project that is a git repo with a commit, the
    fake sbx, and the broker and dispatch starts stubbed."""

    def setUp(self):
        super().setUp()
        for folder in ("roles", "kits"):
            shutil.copytree(os.path.join(REPO, folder), os.path.join(self.root, folder),
                            ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(self.root, "broker"))
        shutil.copy(os.path.join(REPO, "broker", "tools.txt"), os.path.join(self.root, "broker", "tools.txt"))
        self.sbx_state = os.path.join(self.tmp, "sbx")
        os.makedirs(self.sbx_state)
        self.write("bin/sbx", "#!%s\n%s" % (sys.executable, FAKE_SBX), 0o755)
        self.environ.update(FAKE_SBX=self.sbx_state, FACTORY_KEYCHAIN="none")
        self.project = os.path.join(self.tmp, "project")
        self.git("init", "-q", self.project)
        self.git("-C", self.project, "commit", "-q", "--allow-empty", "-m", "first")
        self.starts = []
        self.start_status = {"broker": 0, "dispatch": 0}
        for name, module in (("broker", broker_start), ("dispatch", dispatch_start)):
            patcher = mock.patch.object(module, "main", self.stub_start(name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args):
        subprocess.run(["git"] + list(args), check=True, env=dict(os.environ, **GIT_ENV))

    def stub_start(self, name):
        def start(ctx, args):
            self.starts.append((name, args))
            ctx.say("[%s start]" % name)
            return self.start_status[name]
        return start

    def login(self):
        self.write("sbx/secrets", LOGIN)

    def sandbox(self, name, *workspaces, settings='{"model":"opus"}\n', briefing=True):
        with open(os.path.join(self.sbx_state, "names"), "a") as f:
            f.write(name + "\n")
        self.write("sbx/ws/" + name, "".join(w + "\n" for w in workspaces))
        if settings is not None:
            self.write("sbx/vm/%s/managed-settings.json" % name, settings)
        if briefing:
            self.write("sbx/vm/%s/CLAUDE.md" % name)

    def scratch(self, role):
        return os.path.join(self.state, "roles", role)

    def calls(self):
        try:
            with open(os.path.join(self.sbx_state, "log")) as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    def argvs(self, first=None):
        return [c["argv"] for c in self.calls() if first is None or c["argv"][:1] == [first]]

    def names(self):
        with open(os.path.join(self.sbx_state, "names")) as f:
            return f.read().split()

    def settings(self, name="factory-coordinator"):
        with open(os.path.join(self.sbx_state, "vm", name, "managed-settings.json")) as f:
            return f.read()

    def launch(self, target=None, no_ui=False):
        ctx = self.ctx()
        rc = launch.run(ctx, target or self.project, no_ui)
        return rc, self.out.getvalue(), self.err.getvalue()

    def refused(self, message):
        with self.assertRaises(FactoryError) as caught:
            self.launch()
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.starts, [])
        self.assertEqual(self.argvs("create"), [])


def bash_major():
    cp = subprocess.run(["bash", "-c", 'echo "${BASH_VERSINFO[0]}"'], stdout=subprocess.PIPE)
    return int(cp.stdout.decode().strip() or 0)


class ShellQuoteTest(unittest.TestCase):
    """shell_quote is bash 5's printf %q (the briefing note prints the project that way)."""

    def test_common_paths(self):
        env = {"LC_ALL": "C"}
        self.assertEqual(launch.shell_quote("/home/u/code", env), "/home/u/code")
        self.assertEqual(launch.shell_quote("/home/u/my app", env), "/home/u/my\\ app")
        self.assertEqual(launch.shell_quote("/a$b'c", env), "/a\\$b\\'c")
        self.assertEqual(launch.shell_quote("", env), "''")
        self.assertEqual(launch.shell_quote("/\u00e9", env), "$'/\\303\\251'")
        self.assertEqual(launch.shell_quote("/\u00e9", {"LANG": "en_US.UTF-8"}), "/\u00e9")
    CASES = ["/home/u/code", "/home/u/my app", "/a'b", '/a"b', "/a~b", "~/x", "/a=~b", "/a:~b", "/x#y", "#x",
             "/a,b", "/a!b", "/a@b%c+d:e", "", "/a]b[c{d}e(f)g<h>i|j&k;l$n`o*p?q^r\\s", "/é/ü", "/a\tb", "/a\nb",
             "/é\tb", "/a\x7fb", "/a\x1bb", "/a\x01b", "-x"]

    @unittest.skipIf(bash_major() < 5, "printf %q differs before bash 5 (macOS has 3.2)")
    def test_it_writes_what_bash_writes(self):
        for locale in ("C", "C.UTF-8"):
            env = {"LC_ALL": locale, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            for text in self.CASES:
                want = subprocess.run(["bash", "-c", 'printf %q "$1"', "_", text], env=env, stdout=subprocess.PIPE,
                                      check=True).stdout.decode("utf-8", "surrogateescape")
                self.assertEqual(launch.shell_quote(text, env), want, (locale, text))


class ClaudeMdExcludeTest(LaunchTest):
    """Ported from scripts/test_claude_md_exclude.sh. Here the fake sbx runs `exec` for real,
    with the sandbox's /etc/claude-code mapped to a folder, so the in-sandbox script and the
    file's mode are the real ones."""

    def setUp(self):
        super().setUp()
        self.vm = os.path.join(self.tmp, "vm", "etc", "claude-code")
        os.makedirs(self.vm)
        self.log = os.path.join(self.tmp, "exec.log")
        self.write("bin/sbx", r'''#!/usr/bin/env bash
set -euo pipefail
printf '%%s\n' "$*" >> "%s"
[ "$1" = exec ] || exit 2
shift
while [ "${1#-}" != "$1" ]; do case "$1" in -u) shift 2 ;; *) shift ;; esac; done
shift
args=()
for a in "$@"; do args+=("${a//\/etc\/claude-code/%s}"); done
exec "${args[@]}"
''' % (self.log, self.vm), 0o755)
        self.file = os.path.join(self.vm, "managed-settings.json")

    def exclude(self, workspace="/home/u/code"):
        return launch.exclude_template_claude_md(self.ctx(), "factory-coordinator", workspace)

    def writes(self):
        with open(self.log) as f:
            return sum(1 for line in f if " -i " in line)

    def put(self, text):
        if os.path.exists(self.file):
            os.chmod(self.file, 0o644)
        with open(self.file, "w") as f:
            f.write(text)
        os.chmod(self.file, 0o444)

    def test_the_path_is_the_workspaces_parent(self):
        self.assertEqual(launch.template_claude_md("/home/u/code"), "/home/u/CLAUDE.md")
        self.assertEqual(launch.template_claude_md("/Users/u/src/proj"), "/Users/u/src/CLAUDE.md")
        self.assertEqual(launch.template_claude_md("/code"), "//CLAUDE.md")  # dirname /code is /

    def test_the_exclude_is_added_other_keys_kept_and_the_file_stays_0444(self):
        self.put('{"model":"opus","permissions":{"deny":["Bash"]}}\n')
        self.assertTrue(self.exclude())
        with open(self.file) as f:
            self.assertEqual(f.read(), '{"model":"opus","permissions":{"deny":["Bash"]},'
                                       '"claudeMdExcludes":["/home/u/CLAUDE.md"]}\n')
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o444)
        self.assertEqual(self.writes(), 1)
        self.assertTrue(self.exclude())
        self.assertEqual(self.writes(), 1, "a second run writes nothing")

    def test_an_existing_list_is_kept_without_a_duplicate(self):
        self.put('{"claudeMdExcludes":["/other/CLAUDE.md","/home/u/CLAUDE.md"]}\n')
        self.assertTrue(self.exclude())
        self.assertEqual(self.writes(), 0)
        self.put('{"claudeMdExcludes":["/home/u/CLAUDE.md","/other/CLAUDE.md"]}\n')
        self.assertTrue(self.exclude())
        with open(self.file) as f:
            self.assertEqual(json.load(f)["claudeMdExcludes"], ["/other/CLAUDE.md", "/home/u/CLAUDE.md"])

    def test_an_odd_path_crosses_only_as_data(self):
        self.put("{}\n")
        odd = '/tmp/a b/$(touch pwned);`id`"x'
        self.assertTrue(self.exclude(odd + "/proj"))
        with open(self.file) as f:
            self.assertEqual(json.load(f)["claudeMdExcludes"][-1], odd + "/CLAUDE.md")
        self.assertFalse(os.path.exists("pwned") or os.path.exists(os.path.join(self.tmp, "pwned")))
        with open(self.log) as f:
            self.assertNotIn("pwned", f.read())

    def test_no_managed_settings_fails_and_writes_nothing(self):
        self.assertFalse(self.exclude())
        self.assertFalse(os.path.exists(self.file))
        self.assertIn("No such file", self.err.getvalue())

    def test_settings_that_are_not_an_object_with_a_list_fail(self):
        for text in ("nope\n", "[1]\n", '{"claudeMdExcludes":"x"}\n', '{"claudeMdExcludes":0}\n', "{} {}\n"):
            self.put(text)
            self.assertFalse(self.exclude(), text)
        self.assertEqual(self.writes(), 0)

    def test_null_or_false_reads_as_no_list(self):
        for text in ('{"claudeMdExcludes":null}\n', '{"claudeMdExcludes":false}\n'):
            self.put(text)
            self.assertTrue(self.exclude(), text)
            with open(self.file) as f:
                self.assertEqual(f.read(), '{"claudeMdExcludes":["/home/u/CLAUDE.md"]}\n')


class FreshTest(LaunchTest):
    def test_without_a_login_only_the_coordinator_is_created(self):
        rc, out, err = self.launch()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "[broker start]\n[dispatch start]\nCreating sandbox factory-coordinator...\n"
                              "note: no Claude login is stored yet, so these role sandboxes were not created: developer "
                              "reviewer\n  Type /login in the Coordinator (POWER ON on the dashboard), then run 'factory "
                              "start' again (or press RESTART on the dashboard).\nFactory ready: the dashboard ('factory "
                              "dashboard') is the Coordinator's terminal.\nRoles: developer reviewer \n")
        self.assertEqual(self.starts, [("broker", [self.project]), ("dispatch", [self.project])])
        self.assertEqual(self.argvs("create"), [[
            "create", "--name", "factory-coordinator", "--pull", "never", "-t", "factory-developer:v3",
            "--kit", os.path.join(self.root, "kits", "coordinator"), "--static-mcp", "factory-dispatch",
            "claude", self.project]])
        self.assertEqual(self.settings(), '{"model":"opus","claudeMdExcludes":["%s/CLAUDE.md"]}\n' % self.tmp)
        with open(fingerprints.fingerprint_file(self.paths, "factory-coordinator")) as f:
            self.assertEqual(f.read(), fingerprints.sandbox_fingerprint(self.paths, "factory-coordinator") + "\n")

    def test_the_outgoing_folder_is_made_git_ignored(self):
        self.launch()
        folder = os.path.join(self.project, ".factory")
        self.assertTrue(os.path.isdir(os.path.join(folder, "outgoing")))
        with open(os.path.join(folder, ".gitignore")) as f:
            self.assertEqual(f.read(), "*\n")

    def test_with_a_login_every_role_is_created_with_its_layout(self):
        self.login()
        rc, out, err = self.launch()
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn("note:", out)
        p = self.project
        tools = lambda role: ["--static-mcp", "factory-broker-" + role]  # noqa: E731
        kit = lambda role: ["--kit", os.path.join(self.root, "roles", role, "kit")]  # noqa: E731
        self.assertEqual(self.argvs("create")[1:], [
            ["create", "--name", "factory-developer", "--pull", "never", "-t", "factory-developer:v3"] + kit("developer")
            + tools("developer") + ["claude", self.scratch("developer"), p + ":ro", p + "/.factory/outgoing"],
            ["create", "--name", "factory-reviewer", "--pull", "never", "-t", "factory-reviewer:v3"] + kit("reviewer")
            + tools("reviewer") + ["claude", self.scratch("reviewer"), p + ":ro"]])
        self.assertTrue(os.path.isdir(self.scratch("developer")) and os.path.isdir(self.scratch("reviewer")))
        self.assertEqual(fingerprints.stale_sandboxes(self.ctx()), [])

    def test_no_ui_changes_only_the_closing_line(self):
        self.login()
        rc, out, _ = self.launch(no_ui=True)
        self.assertTrue(out.endswith("Factory ready (no UI): talk to the Coordinator with 'factory chat'; watch every "
                                     "lane with 'factory watch'.\nRoles: developer reviewer \n"), out)

    def test_a_role_without_broker_tools_gets_no_mcp_and_the_coordinator_gets_the_broker_with_tools(self):
        self.login()
        self.write("repo/roles/reviewer/role.conf", 'TEMPLATE="reviewer"\nWORKSPACE="ro"\nBROKER_TOOLS=""\n')
        self.write("repo/kits/coordinator/broker.conf", 'BROKER_TOOLS="jira_my_issues"\n')
        self.launch()
        creates = {a[2]: a for a in self.argvs("create")}
        self.assertNotIn("--static-mcp", creates["factory-reviewer"])
        coordinator = creates["factory-coordinator"]
        self.assertEqual([coordinator[i + 1] for i, x in enumerate(coordinator) if x == "--static-mcp"],
                         ["factory-dispatch", "factory-broker-coordinator"])

    def test_the_relative_project_is_resolved(self):
        os.symlink(self.project, os.path.join(self.tmp, "link"))
        self.launch(os.path.join(self.tmp, "link"))
        self.assertEqual(self.starts[0], ("broker", [self.project]))


class RerunTest(LaunchTest):
    def setUp(self):
        super().setUp()
        self.login()
        p = self.project
        self.sandbox("factory-broker", self.paths.broker_state, p)
        self.sandbox("factory-coordinator", p)
        self.sandbox("factory-developer", self.scratch("developer"), p + ":ro", p + "/.factory/outgoing")
        self.sandbox("factory-reviewer", self.scratch("reviewer"), p + ":ro")

    def test_existing_sandboxes_are_reused(self):
        rc, out, err = self.launch()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "[broker start]\n[dispatch start]\nFactory ready: the dashboard ('factory dashboard') is "
                              "the Coordinator's terminal.\nRoles: developer reviewer \n")
        self.assertEqual(self.argvs("create"), [])

    def test_a_coordinator_without_the_briefing_gets_a_note_with_the_project_quoted(self):
        os.remove(os.path.join(self.sbx_state, "vm", "factory-coordinator", "CLAUDE.md"))
        odd = os.path.join(self.tmp, "my proj$x")
        os.rename(self.project, odd)
        self.project = odd
        self.write("sbx/ws/factory-coordinator", odd + "\n")
        for name in ("factory-developer", "factory-reviewer", "factory-broker"):
            self.write("sbx/ws/" + name, "")
        _, out, _ = self.launch()
        self.assertIn("note: factory-coordinator predates the Coordinator briefing; to install it run:\n"
                      "  sbx rm --force factory-coordinator && %s/scripts/02-launch-factory.sh %s/my\\ proj\\$x\n"
                      % (self.root, self.tmp), out)

    def test_sandboxes_of_removed_roles_are_pointed_out(self):
        self.sandbox("factory-designer", "/x")
        self.sandbox("other", "/y")
        _, out, _ = self.launch()
        self.assertIn("note: factory-designer belongs to a removed role; delete it with: sbx rm --force "
                      "factory-designer\n", out)
        self.assertNotIn("other", out)
        self.assertNotIn("broker belongs", out)

    def test_an_existing_gitignore_is_left_alone(self):
        self.write("project/.factory/.gitignore", "custom\n")
        self.launch()
        with open(os.path.join(self.project, ".factory", ".gitignore")) as f:
            self.assertEqual(f.read(), "custom\n")


class RefusalTest(LaunchTest):
    def test_a_sandbox_of_another_project(self):
        self.sandbox("factory-coordinator", "/elsewhere")
        self.refused("factory-coordinator was created for a different project. To switch projects, see README "
                     "(Switching projects).")

    def test_an_rw_role_with_the_old_layout(self):
        self.sandbox("factory-developer", self.project)
        self.refused("these sandboxes were created with an older layout (the project mounted read-write, or no "
                     ".factory/outgoing) and must be recreated: factory-developer\nRun 'factory update': it recreates "
                     "them (your Claude login, the logs and your project are kept; those roles start a fresh "
                     "conversation).")

    def test_an_old_state_location(self):
        self.sandbox("factory-broker", "/old/.broker", self.project)
        self.sandbox("factory-reviewer", "/old/.roles/reviewer", self.project + ":ro")
        self.refused("these sandboxes mount an old state location and must be recreated (your Claude login and the "
                     "logs are kept; those roles start a fresh conversation):\n  sbx rm --force factory-broker "
                     "factory-reviewer\nthen re-run.")

    def test_the_pre_roles_broker(self):
        self.write("sbx/mcp", "factory-dispatch\nfactory-broker\n")
        self.refused("found the pre-roles broker setup. Migrate once with:\n"
                     "  sbx rm --force factory-developer factory-reviewer\n"
                     "  sbx mcp rm --force factory-broker\n"
                     "  sbx secret rm -f mcp:factory-broker:token; sbx secret rm -f mcp:factory-broker:token:endpoint\n"
                     "  rm -f '%s/token.sha256'\nthen re-run this script." % self.paths.broker_state)

    def test_a_project_that_is_not_a_repo(self):
        shutil.rmtree(os.path.join(self.project, ".git"))
        self.refused("%s is not a git repository or a folder of git repositories; the developer role(s) work in git "
                     "clones of it (each commits to factory/* branches you merge). Run 'git init' and commit, or "
                     "start the factory on the repo itself." % self.project)

    def test_state_inside_the_project(self):
        self.paths.state = os.path.join(self.project, "state")
        with self.assertRaises(FactoryError) as caught:
            self.launch()
        self.assertIn("is inside the project", str(caught.exception))
        self.assertEqual(self.starts, [])

    def test_symlinks_in_the_outgoing_path(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        for rel in (".factory", ".factory/outgoing", ".factory/.gitignore"):
            shutil.rmtree(os.path.join(self.project, ".factory"), ignore_errors=True)
            if rel != ".factory":
                os.makedirs(os.path.join(self.project, ".factory"))
            os.symlink(outside, os.path.join(self.project, rel))
            self.refused("%s/%s is a symlink; refusing to use it (remove it, then re-run)" % (self.project, rel))
            os.unlink(os.path.join(self.project, rel))
        self.assertEqual(os.listdir(outside), [])

    def test_a_bad_role_override_and_no_roles(self):
        self.write("config/ai-code-factory/roles/developer.conf", 'BROKER_TOOLS="x"\n')
        with self.assertRaises(FactoryError) as caught:
            self.launch()
        self.assertIn("BROKER_TOOLS can't be set in a local override", str(caught.exception))
        os.remove(os.path.join(self.config, "roles", "developer.conf"))
        shutil.rmtree(os.path.join(self.root, "roles"))
        os.makedirs(os.path.join(self.root, "roles"))
        self.refused("no roles defined in roles/")

    def test_a_missing_project_folder_and_an_incomplete_checkout(self):
        with self.assertRaises(FactoryError) as caught:
            self.launch(os.path.join(self.tmp, "nope"))
        self.assertEqual(str(caught.exception), "project directory not found: %s" % os.path.join(self.tmp, "nope"))
        os.remove(os.path.join(self.root, "broker", "tools.txt"))
        with self.assertRaises(FactoryError) as caught:
            self.launch()
        self.assertIn("broker/tools.txt is missing", str(caught.exception))

    def test_a_failing_sandbox_list(self):
        self.environ["FAKE_FAIL"] = "ls"
        with self.assertRaises(FactoryError) as caught:
            self.launch()
        self.assertEqual(str(caught.exception), "could not list the sandboxes ('sbx ls --json' failed: exit status 1): "
                                                "sbx: daemon down")


class FailureTest(LaunchTest):
    def test_a_failing_broker_start_stops_with_its_status(self):
        self.start_status["broker"] = 3
        rc, out, _ = self.launch()
        self.assertEqual(rc, 3)
        self.assertEqual([s[0] for s in self.starts], ["broker"])
        self.assertEqual(self.argvs("create"), [])

    def test_a_failing_dispatch_start_stops_with_its_status(self):
        self.start_status["dispatch"] = 4
        self.assertEqual(self.launch()[0], 4)
        self.assertEqual(self.argvs("create"), [])

    def test_a_failing_create_stops_with_sbxs_status(self):
        self.environ["FAKE_FAIL"] = "create"
        rc, out, err = self.launch()
        self.assertEqual((rc, err), (6, "sbx: create failed\n"))
        self.assertFalse(os.path.exists(fingerprints.fingerprint_file(self.paths, "factory-coordinator")))

    def test_a_coordinator_without_managed_settings(self):
        self.environ["FAKE_FAIL"] = "nosettings"
        with self.assertRaises(FactoryError) as caught:
            self.launch()
        self.assertEqual(str(caught.exception), "could not add the sbx template's CLAUDE.md to claudeMdExcludes in "
                                                "factory-coordinator's managed settings")

    def test_a_scratch_folder_that_cannot_be_made(self):
        self.login()
        self.write("state/ai-code-factory/roles", "a file\n")
        rc, out, err = self.launch()
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: %s" % self.scratch("developer")), err)


class ShimTest(LaunchTest):
    def script(self, *args, **env):
        cp = subprocess.run(["bash", os.path.join(REPO, "scripts", "02-launch-factory.sh")] + list(args),
                            env=dict(self.environ, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return cp.returncode, cp.stdout.decode(), cp.stderr.decode()

    def test_the_script_checks_its_usage_and_names_itself(self):
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(REPO, "scripts", "02-launch-factory.sh")]).returncode, 0)
        for args in ((), ("a", "b")):
            self.assertEqual(self.script(*args), (1, "", "error: usage: %s <project-dir>\n"
                                                         % os.path.join(REPO, "scripts", "02-launch-factory.sh")))

    def test_the_entry_point_reads_factory_no_ui(self):
        self.login()
        for value, line in (("1", "Factory ready (no UI)"), ("yes", "Factory ready: the dashboard")):
            self.environ["FACTORY_NO_UI"] = value
            self.ctx()
            rc = main(["_launch", self.project], root=self.root, environ=self.environ, stdout=self.out, stderr=self.err)
            self.assertEqual(rc, 0)
            self.assertIn(line, self.out.getvalue())
        rc = main(["_launch"], root=self.root, environ=self.environ, stdout=self.out, stderr=self.err)
        self.assertEqual(rc, 1)
        self.assertIn("usage: %s/scripts/02-launch-factory.sh <project-dir>" % self.root, self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
