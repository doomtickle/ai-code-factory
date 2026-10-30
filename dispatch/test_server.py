"""Tests for the dispatch bridge, using a fake `sbx` executable.

Run on the host with the system Python:
  /usr/bin/python3 -m unittest discover -s dispatch -v
"""

import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import server as d

FAKE_SBX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys, time
    args = sys.argv[1:]
    log = os.environ["FAKE_SBX_LOG"]
    with open(log, "a") as f:
        f.write(json.dumps(args) + "\n")
    if args[:2] == ["ls", "-q"]:
        print("factory-developer\nfactory-reviewer\nfactory-opuser\nfactory-tiered\nfactory-builder\nfactory-researcher")
        sys.exit(0)
    if args[0] == "exec" and args[2:3] == ["pkill"]:
        sys.exit(1)  # nothing matched: the test's "claude" is this fake, killed via its process group
    if args[0] == "exec" and args[2:4] == ["factory-lane", "prepare"]:
        project, lane = args[4], args[5]
        mode = os.environ.get("FAKE_PREPARE", "")
        print("Cloning into the lane...", file=sys.stderr)
        if mode == "fail":
            print("fatal: \x1b[31mclone failed\x1b[0m", file=sys.stderr)
            sys.exit(2)
        print("progress noise")
        print("relative/dir" if mode == "badpath" else "/home/agent/lanes/" + lane)
        sys.exit(0)
    if args[0] == "exec" and args[2:4] == ["factory-lane", "export"]:
        import subprocess, tempfile
        project, lane, rel = args[4], args[5], args[6]
        mode = os.environ.get("FAKE_EXPORT", "")
        if mode == "big":
            for _ in range(100):
                sys.stdout.buffer.write(b"x" * 1000)
            sys.exit(0)
        if mode == "fail":
            print("fatal: \x1b[31mexport broke\x1b[0m", file=sys.stderr)
            sys.exit(1)
        root = os.environ.get("FAKE_LANES")
        repo = os.path.normpath(os.path.join(root or "/nonexistent", lane, rel))
        if not os.path.isdir(repo):
            print("nothing to export", file=sys.stderr)
            sys.exit(3)
        tmp = tempfile.mkdtemp()
        bundle = os.path.join(tmp, "b")
        # Like the old factory-lane: trust the lane's origin/* refs for what the
        # host has (so a moved origin/main makes a bundle the host can't import);
        # "full" is the whole history.
        rev = [] if args[7:8] == ["full"] else ["--not", "--remotes=origin"]
        cp = subprocess.run(["git", "-C", repo, "bundle", "create", "--quiet", bundle,
                             "--branches=factory/*", *rev])
        if cp.returncode != 0:
            sys.exit(3)
        with open(bundle, "rb") as f:
            sys.stdout.buffer.write(f.read())
        sys.exit(0)
    if args[0] == "exec" and "claude" in args:
        task = args[args.index("-p") + 1]
        resume = args[args.index("--resume") + 1] if "--resume" in args else None
        if resume == "gone":
            print("No conversation found with session ID: gone", file=sys.stderr)
            sys.exit(1)
        if task == "hang":
            time.sleep(60)
        sid = resume or (args[args.index("--session-id") + 1] if "--session-id" in args
                         else "sess-" + str(os.getpid()))
        # Like claude, total_cost_usd is cumulative for the session (0.25 per run).
        cost_file = os.path.join(os.path.dirname(log), "cost-" + sid)
        total = (float(open(cost_file).read()) if os.path.exists(cost_file) else 0.0) + 0.25
        with open(cost_file, "w") as f:
            f.write(str(total))
        init_event = {"type": "system", "subtype": "init", "session_id": sid}
        if task == "messy-model":
            init_event["model"] = "claude-\x1b[31msonnet\x1b[0m-5\nrogue-line"
        elif task != "no-model":
            init_event["model"] = "claude-sonnet-5"
        events = [
            init_event,
            {"type": "assistant", "message": {"usage": {"input_tokens": 5}, "content": [
                {"type": "text", "text": "Working on it \x1b[31mnow\x1b[0m"}]}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 85000},
                                              "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "assistant", "parent_tool_use_id": "t1",  # a subagent: not the session's context
             "message": {"usage": {"input_tokens": 999999}, "content": []}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a.txt"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text",
                "text": "factory-test: mode: clean install (yarn.lock, offline)\nfactory-test: running in /c\n"
                        "ok\nfactory-test: mode was: clean install (yarn.lock, offline) (exit 0)"}]}]}},
        ]
        if task == "odd-events":  # valid JSON the bridge must skip, not choke on
            events[1:1] = [
                123, ["not", "an", "event"],
                {"type": "assistant", "message": {"content": [7, "str", None,
                    {"type": "text", "text": "after the odd blocks"}]}},
                {"type": "assistant", "message": {"usage": {"input_tokens": "lots"}, "content": []}},
                {"type": "user", "message": {"content": "just a string"}},
            ]
        if task == "no-usage":  # claude reported no token counts
            for e in events:
                if isinstance(e.get("message"), dict):
                    e["message"].pop("usage", None)
        if task == "nan-cost":
            total = float("nan")  # json.dumps writes NaN, as a faulty claude might
        if task == "max-turns":
            events.append({"type": "result", "subtype": "error_max_turns", "is_error": True,
                           "result": "", "session_id": sid, "total_cost_usd": total})
        elif task == "fail":
            events.append({"type": "result", "is_error": True, "result": "boom", "session_id": sid})
        else:
            events.append({"type": "result", "is_error": False, "result": "REPORT: did " + task,
                           "session_id": sid, "total_cost_usd": total})
        for e in events:
            print(json.dumps(e), flush=True)
        if task == "stall-after-report":
            time.sleep(60)
        sys.exit(0)
    sys.exit(0)
''').lstrip()


def conf(desc, workspace, tools, model="", models="", lanes="", turns=""):
    return (f'DESCRIPTION="{desc}"\nTEMPLATE="developer"\nWORKSPACE="{workspace}"\n'
            f'BROKER_TOOLS="{tools}"\nMODEL="{model}"\nMODELS="{models}"\nLANES="{lanes}"\n'
            f'MAX_TURNS="{turns}"\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        d.ROOT, d.PROJECT, d.STATE = root / "repo", root / "project", root / "state"
        d.ROLES_SCRATCH = root / "scratch"
        for p in (d.ROOT / "roles", d.PROJECT / ".git", d.STATE / "logs", d.STATE / "sessions",
                  d.STATE / "instructions"):
            p.mkdir(parents=True)  # PROJECT/.git: a (stub) repo, so rw roles may run
        for name in ("developer", "reviewer", "tester", "opuser", "tiered", "builder"):
            (d.STATE / "instructions" / f"{name}.md").write_text(f"# {name}\nDo {name} work.\n")
        for name, text in {"developer": conf("Builds things", "rw", "web_fetch npm_ci"),
                           "reviewer": conf("Reviews things", "ro", ""),
                           "tester": conf("Tests things", "rw", ""),
                           "opuser": conf("Uses a pinned model", "rw", "", model="opus", turns="7"),
                           "tiered": conf("Picks a model per task", "rw", "", model="sonnet",
                                          models="haiku opus bad;name claude-haiku-5-5"),
                           "builder": conf("Builds in parallel", "rw", "", lanes="2"),
                           "dev-lane2": conf("Named like a lane", "rw", ""),
                           "Bad_Name": conf("x", "rw", ""),
                           "coordinator": conf("Reserved name", "rw", "")}.items():
            (d.ROOT / "roles" / name).mkdir()
            (d.ROOT / "roles" / name / "role.conf").write_text(text)
        fake = root / "sbx"
        fake.write_text(FAKE_SBX)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        d.SBX = str(fake)
        d.BROKER_AUDIT = d.STATE / "broker-audit.jsonl"  # absent unless a test writes it
        self.sbx_log = root / "sbx-calls.jsonl"
        os.environ["FAKE_SBX_LOG"] = str(self.sbx_log)
        for var in ("FAKE_PREPARE", "FAKE_EXPORT", "FAKE_LANES"):
            os.environ.pop(var, None)
        d._jobs.clear()
        d._seen_sandboxes.clear()

    def tearDown(self):
        for job in list(d._jobs.values()):
            if job.status == "running":
                d.tool_cancel_job({"job_id": job.id})
        self.tmp.cleanup()

    def sbx_calls(self):
        return [json.loads(l) for l in self.sbx_log.read_text().splitlines()]

    def claude_calls(self):
        return [c for c in self.sbx_calls() if c[0] == "exec" and "claude" in c]

    def lane_calls(self, sub):
        return [c for c in self.sbx_calls() if c[0] == "exec" and c[2:4] == ["factory-lane", sub]]

    def delegate(self, role, task, **kw):
        """Start a job without waiting (delegate waits by default) and return its id."""
        kw.setdefault("wait_seconds", 0)
        out = d.tool_delegate({"role": role, "task": task, **kw})
        return out.split("job ", 1)[1].split(" ", 1)[0]


class SanitizeTests(unittest.TestCase):
    def test_sanitize_strips_c1_control_characters(self):
        # \x9b (CSI) and friends are C1 controls (\x80-\x9f) -- not ANSI ESC
        # sequences themselves, but still terminal-unsafe and worth scrubbing
        # from anything that reaches a role transcript or audit record.
        self.assertEqual(d.sanitize("before\x9bafter\x7fend"), "beforeafterend")


class RoleTests(Base):
    def test_load_roles_parses_and_skips_invalid_names(self):
        roles = d.load_roles()
        self.assertEqual(sorted(roles), ["builder", "developer", "opuser", "reviewer", "tester", "tiered"])
        self.assertEqual(roles["developer"]["broker_tools"], ["web_fetch", "npm_ci"])
        self.assertEqual(roles["reviewer"]["workspace"], "ro")
        self.assertEqual(roles["developer"]["model"], "")
        self.assertEqual(roles["opuser"]["model"], "opus")

    def test_load_roles_rejects_reserved_names(self):
        # roles/coordinator/role.conf exists (see setUp) but "coordinator" is
        # reserved (factory/roles.py RESERVED_ROLE_NAMES) -- the Coordinator
        # is a broker pseudo-role driven via kits/coordinator/broker.conf, not
        # a dispatchable role, so it must never be loadable as one.
        roles = d.load_roles()
        self.assertNotIn("coordinator", roles)

    def test_list_roles_shows_state(self):
        out = d.tool_list_roles({})
        self.assertIn("developer [idle]", out)
        self.assertIn("tester [not created]", out)  # no factory-tester sandbox in fake sbx
        self.assertIn("read-only", out)
        self.assertIn("model: default", out)  # developer leaves MODEL unset
        self.assertIn("model: opus", out)  # opuser pins a model
        self.assertNotIn("session:", out)  # no saved sessions yet

    def test_list_roles_shows_session_size_and_total(self):
        d.tool_wait_for_job({"job_id": self.delegate("developer", "one"), "timeout_seconds": 30})
        d.tool_wait_for_job({"job_id": self.delegate("developer", "two"), "timeout_seconds": 30})
        lines = d.tool_list_roles({}).splitlines()
        dev = next(l for l in lines if l.startswith("- developer"))
        self.assertTrue(dev.endswith("; session: ~85k tokens, $0.50 so far"), dev)
        reviewer = next(l for l in lines if l.startswith("- reviewer"))
        self.assertNotIn("session:", reviewer)


class OptionalRoleTests(Base):
    """A role whose role.conf sets OPTIONAL="1" is off unless the enabled-roles
    snapshot (STATE/enabled-roles, from start-dispatch.sh) names it."""

    def setUp(self):
        super().setUp()
        role = d.ROOT / "roles" / "researcher"
        role.mkdir()
        (role / "role.conf").write_text(conf("Looks things up", "ro", "web_fetch") + 'OPTIONAL="1"\n')
        (d.STATE / "instructions" / "researcher.md").write_text("# researcher\nResearch.\n")

    def enable(self, text="researcher\n"):
        (d.STATE / "enabled-roles").write_text(text)

    def test_off_by_default(self):
        self.assertNotIn("researcher", d.load_roles())
        self.assertNotIn("researcher", d.tool_list_roles({}))
        self.assertNotIn("researcher", d.server_instructions())
        self.assertIn("developer", d.load_roles())

    def test_delegate_refuses_with_how_to_enable(self):
        with self.assertRaises(d.Denied) as cm:
            d.tool_delegate({"role": "researcher", "task": "look up fastify"})
        msg = str(cm.exception)
        self.assertIn("researcher role is disabled", msg)
        self.assertIn("enabled-roles", msg)
        self.assertIn("factory start", msg)
        self.assertFalse(self.sbx_log.exists())  # nothing reached a sandbox, not even a listing
        with self.assertRaisesRegex(d.Denied, "unknown role 'nope'"):  # other names unchanged
            d.tool_delegate({"role": "nope", "task": "x"})

    def test_unreadable_or_irrelevant_snapshots_leave_it_off(self):
        self.enable("")
        self.assertNotIn("researcher", d.load_roles())
        self.enable("# researcher\ndeveloper\nnot a name\nResearcher\n")
        self.assertNotIn("researcher", d.load_roles())
        self.assertIn("developer", d.load_roles())  # naming a non-optional role changes nothing
        (d.STATE / "enabled-roles").unlink()
        (d.STATE / "enabled-roles").mkdir()  # a directory: unreadable
        self.assertNotIn("researcher", d.load_roles())

    def test_enabled_role_behaves_as_before(self):
        self.enable("\nresearcher\n")
        roles = d.load_roles()
        self.assertTrue(roles["researcher"]["enabled"])
        self.assertEqual(roles["researcher"]["broker_tools"], ["web_fetch"])
        self.assertIn("- researcher", d.tool_list_roles({}))
        self.assertIn("flow: researcher (only if outside information is needed) -> developer -> reviewer",
                      d.server_instructions())
        out = json.loads(d.tool_delegate({"role": "researcher", "task": "look up", "wait_seconds": 30}))
        self.assertEqual(out["status"], "done")

    def test_flow_without_the_researcher(self):
        flow = d.server_instructions()
        self.assertIn("flow: developer -> reviewer", flow)
        self.assertNotIn("researcher", flow)

    def test_include_disabled_flags_the_role(self):
        self.assertFalse(d.load_roles(include_disabled=True)["researcher"]["enabled"])

    def test_a_local_override_cannot_enable_or_change_it(self):
        path = d.STATE / "role-overrides" / "researcher.conf"
        path.parent.mkdir()
        path.write_text('OPTIONAL="0"\n')
        self.assertNotIn("researcher", d.load_roles())  # an override can't switch it on
        self.enable()
        for line in ('OPTIONAL="0"', 'BROKER_TOOLS="git_push"'):
            path.write_text(line + "\n")
            with self.assertRaisesRegex(d.Denied, "can't be set in a local override"):
                d.load_roles()


class RoleOverrideTests(Base):
    """STATE/role-overrides/<role>.conf: this machine's local tuning of a role,
    snapshotted by start-dispatch.sh and layered over role.conf."""

    def override(self, role, text):
        path = d.STATE / "role-overrides" / f"{role}.conf"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return path

    def test_no_overrides_is_unchanged(self):
        before = d.load_roles()
        (d.STATE / "role-overrides").mkdir()  # start-dispatch.sh's empty snapshot
        self.assertEqual(d.load_roles(), before)
        self.assertEqual(before["tiered"]["models"], ["sonnet", "haiku", "opus", "claude-haiku-5-5"])

    def test_override_is_layered_over_role_conf(self):
        self.override("tiered", '# cheaper by default\n\nMODEL="claude-haiku-5-5"\n'
                                '  # indented comment\n'
                                'MODELS="opus claude-fable-5-1"\nEFFORT="high"\nLANES="4"\n'
                                'MAX_SESSION_TOKENS="0"\nMAX_TURNS="50"\n')
        role = d.load_roles()["tiered"]
        self.assertEqual(role["model"], "claude-haiku-5-5")
        self.assertEqual(role["models"], ["claude-haiku-5-5", "opus", "claude-fable-5-1"])
        self.assertEqual(role["effort"], "high")
        self.assertEqual(role["lanes"], 4)
        self.assertEqual(role["max_session_tokens"], 0)
        self.assertEqual(role["max_turns"], 50)
        # Untouched keys still come from role.conf; other roles are unaffected.
        self.assertEqual(role["description"], "Picks a model per task")
        self.assertEqual(d.load_roles()["opuser"]["model"], "opus")
        self.assertIn("model: claude-haiku-5-5; can also run on: opus, claude-fable-5-1",
                      d.tool_list_roles({}))

    def test_override_applies_to_delegate(self):
        self.override("tiered", 'MODELS="claude-fable-5-1"\n')
        out = json.loads(d.tool_delegate({"role": "tiered", "task": "hard", "model": "claude-fable-5-1",
                                          "wait_seconds": 30}))
        self.assertEqual(out["status"], "done")
        self.assertEqual(self.exec_models(), ["claude-fable-5-1"])
        with self.assertRaisesRegex(d.Denied, "haiku"):  # dropped by the override
            d.tool_delegate({"role": "tiered", "task": "x", "model": "haiku"})

    def exec_models(self):
        return [c[c.index("--model") + 1] if "--model" in c else None for c in self.claude_calls()]

    def test_keys_outside_the_allowlist_are_refused(self):
        for key in ("BROKER_TOOLS", "WORKSPACE", "TEMPLATE", "DESCRIPTION", "OTHER"):
            path = self.override("developer", f'LANES="2"\n{key}="git_push"\n')
            with self.assertRaises(d.Denied) as cm:
                d.load_roles()
            self.assertIn(f"{path}: {key} can't be set in a local override", str(cm.exception))
            out = d.call_tool("list_roles", {})
            self.assertTrue(out[1] and out[0].startswith("DENIED:"), out)

    def test_malformed_lines_are_refused(self):
        for line in ("LANES=2", "LANES='2'", "export LANES=\"2\"", "$(reboot)"):
            path = self.override("developer", line + "\n")
            with self.assertRaisesRegex(d.Denied, f'{path}: not a KEY="value" line'):
                d.load_roles()

    def test_override_values_get_role_conf_validation(self):
        # The same parsing as role.conf: bad names dropped, bad numbers fall back.
        self.override("tiered", 'MODEL="bad model"\nMODELS="opus bad;name"\nEFFORT="max"\n'
                                'LANES="9"\nMAX_SESSION_TOKENS="lots"\nMAX_TURNS="-1"\n')
        role = d.load_roles()["tiered"]
        self.assertEqual(role["models"], ["opus"])
        self.assertEqual(role["effort"], "")
        self.assertEqual(role["lanes"], 1)
        self.assertEqual(role["max_session_tokens"], d.DEFAULT_MAX_SESSION_TOKENS)
        self.assertEqual(role["max_turns"], 0)

    def test_override_for_an_unknown_role_is_refused(self):
        path = self.override("designer", 'LANES="2"\n')
        with self.assertRaisesRegex(d.Denied, f"{path}: no role named 'designer'"):
            d.load_roles()

    def test_never_read_from_the_config_folder_or_repo(self):
        # Only the state snapshot counts: an override anywhere else is ignored.
        (d.ROOT / "roles" / "tiered" / "override.conf").write_text('MODEL="opus"\n')
        (d.ROOT / "roles" / "tiered.conf").write_text('MODEL="opus"\n')
        self.assertEqual(d.load_roles()["tiered"]["model"], "sonnet")


class DelegateTests(Base):
    def test_delegate_runs_claude_in_the_role_sandbox(self):
        job_id = self.delegate("developer", "add a feature")
        report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["report"], "REPORT: did add a feature")
        exec_call = self.claude_calls()[0]
        lane = "/home/agent/lanes/developer"
        self.assertEqual(exec_call[:15], ["exec", "-w", lane, "-e", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
                                          "-e", f"FACTORY_PROJECT={d.PROJECT}", "-e", "FACTORY_LANE=developer",
                                          "-e", f"FACTORY_SCRATCH={d.ROLES_SCRATCH / 'developer'}",
                                          "-e", f"FACTORY_LANE_ROOT={lane}", "factory-developer", "claude"])
        self.assertEqual(exec_call[exec_call.index("-p") + 1], "add a feature")
        self.assertNotIn("--resume", exec_call)
        sid = exec_call[exec_call.index("--session-id") + 1]
        self.assertEqual(str(uuid.UUID(sid)), sid)
        self.assertNotIn("--model", exec_call)  # developer leaves MODEL unset
        prompt = exec_call[exec_call.index("--append-system-prompt") + 1]
        self.assertIn("You are the developer", prompt)
        self.assertTrue(prompt.endswith("# developer\nDo developer work."))
        self.assertEqual((d.STATE / "sessions" / "developer").read_text(), sid + "\n")
        log = (d.STATE / "logs" / "developer.log").read_text()
        self.assertIn("🔧 Bash", log)
        self.assertIn("Working on it now", log)  # ANSI stripped
        self.assertNotIn("\x1b", log)

    def ls_calls(self):
        return [c for c in self.sbx_calls() if c[:1] == ["ls"]]

    def test_sandbox_listing_is_cached_between_tasks(self):
        d.tool_wait_for_job({"job_id": self.delegate("developer", "one"), "timeout_seconds": 30})
        d.tool_wait_for_job({"job_id": self.delegate("developer", "two"), "timeout_seconds": 30})
        self.assertEqual(len(self.ls_calls()), 1)
        d._seen_sandboxes["factory-developer"] -= d.SANDBOX_CACHE_SECONDS  # expire it
        d.tool_wait_for_job({"job_id": self.delegate("developer", "three"), "timeout_seconds": 30})
        self.assertEqual(len(self.ls_calls()), 2)

    def test_missing_sandbox_is_rechecked_every_time(self):
        for _ in range(2):
            out, is_error = d.call_tool("delegate", {"role": "tester", "task": "x"})
            self.assertTrue(is_error)
            self.assertIn("has not been created", out)
        self.assertEqual(len(self.ls_calls()), 2)

    def test_model_used_is_logged_and_reported(self):
        job_id = self.delegate("developer", "add a feature")
        report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))
        self.assertEqual(report["model"], "claude-sonnet-5")
        log = (d.STATE / "logs" / "developer.log").read_text()
        self.assertIn("model: claude-sonnet-5", log)
        self.assertEqual(log.count("model: claude-sonnet-5"), 1)
        status = json.loads(d.tool_job_status({}))
        self.assertEqual(status[0]["model"], "claude-sonnet-5")
        end = [json.loads(l) for l in (d.STATE / "audit.jsonl").read_text().splitlines()
               if '"job-end"' in l][-1]
        self.assertEqual(end["model"], "claude-sonnet-5")

    def test_model_with_ansi_and_newline_is_sanitized(self):
        job_id = self.delegate("developer", "messy-model")
        report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))
        self.assertEqual(report["model"], "claude-sonnet-5 rogue-line")
        log = (d.STATE / "logs" / "developer.log").read_text()
        self.assertIn("model: claude-sonnet-5 rogue-line", log)
        self.assertNotIn("\x1b", log)
        self.assertEqual(sum(1 for l in log.splitlines() if "model:" in l), 1)

    def test_missing_model_in_init_is_handled(self):
        job_id = self.delegate("developer", "no-model")
        report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))
        self.assertEqual(report["status"], "done")
        self.assertNotIn("model", report)
        self.assertNotIn("model:", (d.STATE / "logs" / "developer.log").read_text())

    def test_delegate_passes_configured_model(self):
        job_id = self.delegate("opuser", "add a feature")
        d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30})
        exec_call = self.claude_calls()[0]
        self.assertEqual(exec_call[exec_call.index("--model") + 1], "opus")

    def exec_models(self):
        return [c[c.index("--model") + 1] if "--model" in c else None
                for c in self.claude_calls()]

    def test_allowed_models_include_the_default_and_skip_bad_names(self):
        self.assertEqual(d.load_roles()["tiered"]["models"],
                          ["sonnet", "haiku", "opus", "claude-haiku-5-5"])
        self.assertEqual(d.load_roles()["developer"]["models"], [])
        self.assertIn("model: sonnet; can also run on: haiku, opus, claude-haiku-5-5",
                       d.tool_list_roles({}))

    def test_delegate_can_pick_an_allowed_model(self):
        out = json.loads(d.tool_delegate({"role": "tiered", "task": "rename", "model": "haiku",
                                          "wait_seconds": 30}))
        self.assertEqual(out["status"], "done")
        self.assertEqual(self.exec_models(), ["haiku"])
        meta = json.loads((d.STATE / "sessions" / "tiered.json").read_text())
        self.assertEqual(meta["model"], "haiku")

    def test_delegate_can_pick_an_allowed_model_by_full_id(self):
        # A hyphenated full model id (e.g. claude-haiku-5-5, used to bypass an
        # alias the CLI resolves to an older model) must validate and pass
        # through like any alias.
        out = json.loads(d.tool_delegate({"role": "tiered", "task": "rename",
                                          "model": "claude-haiku-5-5", "wait_seconds": 30}))
        self.assertEqual(out["status"], "done")
        self.assertEqual(self.exec_models(), ["claude-haiku-5-5"])
        meta = json.loads((d.STATE / "sessions" / "tiered.json").read_text())
        self.assertEqual(meta["model"], "claude-haiku-5-5")

    def test_delegate_refuses_a_model_the_role_does_not_allow(self):
        for role, model in (("tiered", "bad;name"), ("tiered", "claude-x"), ("developer", "opus")):
            out, is_error = d.call_tool("delegate", {"role": role, "task": "x", "model": model})
            self.assertTrue(is_error)
            self.assertIn("can't run on model", out)
        self.assertFalse(self.sbx_log.exists())  # refused before any sbx call

    def test_switching_model_starts_a_fresh_session(self):
        d.tool_wait_for_job({"job_id": self.delegate("tiered", "one"), "timeout_seconds": 30})
        same = json.loads(d.tool_delegate({"role": "tiered", "task": "two", "wait_seconds": 30}))
        self.assertEqual(same["session"], "continuing its previous session")
        switched = json.loads(d.tool_delegate({"role": "tiered", "task": "three", "model": "opus",
                                               "wait_seconds": 30}))
        self.assertIn("previous one ran on sonnet, this task on opus", switched["session"])
        execs = self.claude_calls()
        self.assertIn("--resume", execs[1])
        self.assertNotIn("--resume", execs[2])
        self.assertEqual(self.exec_models(), ["sonnet", "sonnet", "opus"])

    def test_session_saved_without_a_model_still_resumes(self):
        d.tool_wait_for_job({"job_id": self.delegate("tiered", "one"), "timeout_seconds": 30})
        meta_path = d.STATE / "sessions" / "tiered.json"
        meta = json.loads(meta_path.read_text())
        del meta["model"]  # saved by a bridge from before per-task models
        meta_path.write_text(json.dumps(meta))
        out = json.loads(d.tool_delegate({"role": "tiered", "task": "two", "model": "opus",
                                          "wait_seconds": 30}))
        self.assertEqual(out["session"], "continuing its previous session")

    def test_max_turns_is_passed_only_when_set(self):
        d.tool_wait_for_job({"job_id": self.delegate("opuser", "x"), "timeout_seconds": 30})
        argv = self.claude_calls()[-1]
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")
        d.tool_wait_for_job({"job_id": self.delegate("developer", "x"), "timeout_seconds": 30})
        self.assertNotIn("--max-turns", self.claude_calls()[-1])
        self.assertEqual(d.load_roles()["opuser"]["max_turns"], 7)
        self.assertEqual(d.load_roles()["developer"]["max_turns"], 0)

    def test_hitting_the_turn_cap_fails_the_job_with_an_explanation(self):
        report = json.loads(d.tool_wait_for_job({"job_id": self.delegate("opuser", "max-turns"),
                                                 "timeout_seconds": 30}))
        self.assertEqual(report["status"], "failed")
        self.assertIn("MAX_TURNS cap of 7 turns", report["report"])
        self.assertIn("follow-up task on this lane can continue", report["report"])

    def set_conf(self, role, line):
        conf_path = d.ROOT / "roles" / role / "role.conf"
        conf_path.write_text(conf_path.read_text() + line + "\n")

    def test_max_job_seconds_parsing(self):
        self.assertEqual(d.load_roles()["developer"]["max_job_seconds"], d.DEFAULT_MAX_JOB_SECONDS)
        self.set_conf("developer", 'MAX_JOB_SECONDS="0"')
        self.assertEqual(d.load_roles()["developer"]["max_job_seconds"], 0)
        self.set_conf("developer", 'MAX_JOB_SECONDS="90"')
        self.assertEqual(d.load_roles()["developer"]["max_job_seconds"], 90)
        self.set_conf("developer", 'MAX_JOB_SECONDS="1h"')
        self.assertEqual(d.load_roles()["developer"]["max_job_seconds"], d.DEFAULT_MAX_JOB_SECONDS)

    def test_a_stuck_claude_is_killed_at_the_time_limit_and_the_lane_freed(self):
        self.set_conf("developer", 'MAX_JOB_SECONDS="1"')
        started = time.time()
        report = json.loads(d.tool_wait_for_job({"job_id": self.delegate("developer", "hang"),
                                                 "timeout_seconds": 30}))
        self.assertLess(time.time() - started, 20)
        self.assertEqual(report["status"], "failed")
        self.assertIn("MAX_JOB_SECONDS limit of 1s", report["report"])
        self.assertIn("killed", (d.STATE / "logs" / "developer.log").read_text())
        call = self.claude_calls()[0]
        sid = call[call.index("--session-id") + 1]
        pkill = [c for c in self.sbx_calls() if c[2:3] == ["pkill"]]
        pattern = f"^claude -p .*--(session-id|resume) {sid}( |$)"
        self.assertEqual(pkill, [["exec", "factory-developer", "pkill", "-INT", "-f", pattern]])
        out = json.loads(d.tool_delegate({"role": "developer", "task": "next", "wait_seconds": 30}))
        self.assertEqual(out["status"], "done")  # the lane took a new job

    def test_the_time_limit_keeps_the_partial_report(self):
        self.set_conf("developer", 'MAX_JOB_SECONDS="2"')
        report = json.loads(d.tool_wait_for_job({"job_id": self.delegate("developer", "stall-after-report"),
                                                 "timeout_seconds": 30}))
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["report"].startswith("REPORT: did stall-after-report\n"))
        self.assertIn("MAX_JOB_SECONDS limit of 2s", report["report"])

    def test_zero_means_no_time_limit(self):
        self.set_conf("developer", 'MAX_JOB_SECONDS="0"')
        report = json.loads(d.tool_wait_for_job({"job_id": self.delegate("developer", "x"),
                                                 "timeout_seconds": 30}))
        self.assertEqual(report["status"], "done")

    def test_odd_stream_events_are_skipped_not_fatal(self):
        report = json.loads(d.tool_wait_for_job({"job_id": self.delegate("developer", "odd-events"),
                                                 "timeout_seconds": 30}))
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["report"], "REPORT: did odd-events")
        log = (d.STATE / "logs" / "developer.log").read_text()
        self.assertIn("after the odd blocks", log)  # a non-dict block doesn't hide its siblings
        self.assertIn("skipped a malformed event: AttributeError", log)  # 123, the list
        self.assertIn("skipped a malformed event: ValueError", log)  # non-numeric usage
        self.assertEqual(report["context_tokens"], 85010)  # later events still counted

    def test_non_utf8_output_does_not_fail_the_job(self):
        job = d.Job("developer", "x", None)
        log = d.Transcript("developer")
        script = textwrap.dedent(r"""
            import sys
            sys.stdout.buffer.write(b'\xff\xfe not json\n')
            sys.stdout.buffer.write(b'{"type": "result", "result": "ok"}\n')
        """)
        orig = d.claude_argv
        d.claude_argv = lambda *a: [sys.executable, "-c", script]
        try:
            d.run_claude(job, log, str(d.PROJECT), None)
        finally:
            d.claude_argv = orig
        self.assertEqual(job.result, "ok")

    def test_a_failure_after_claude_keeps_the_report_and_still_imports(self):
        with mock.patch.object(d, "save_session", side_effect=OSError("disk full")):
            report = json.loads(d.tool_wait_for_job({"job_id": self.delegate("developer", "x"),
                                                     "timeout_seconds": 30}))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["report"], "REPORT: did x\n[dispatch error: OSError: disk full]")
        self.assertTrue(self.lane_calls("export"))  # the finally block still imported the branches

    def test_a_failure_before_any_report_is_the_whole_result(self):
        with mock.patch.object(d, "prepare_lane", side_effect=OSError("no sbx")):
            report = json.loads(d.tool_wait_for_job({"job_id": self.delegate("developer", "x"),
                                                     "timeout_seconds": 30}))
        self.assertEqual(report["report"], "dispatch error: OSError: no sbx")

    def test_effort_is_passed_and_reported(self):
        out = json.loads(d.tool_delegate({"role": "developer", "task": "x", "effort": "low",
                                          "wait_seconds": 30}))
        self.assertEqual(out["effort"], "low")
        exec_call = self.claude_calls()[0]
        self.assertEqual(exec_call[exec_call.index("--effort") + 1], "low")
        d.tool_wait_for_job({"job_id": self.delegate("developer", "y"), "timeout_seconds": 30})
        self.assertNotIn("--effort", self.claude_calls()[1])

    def test_role_default_effort_and_bad_effort(self):
        conf_path = d.ROOT / "roles" / "opuser" / "role.conf"
        conf_path.write_text(conf_path.read_text() + 'EFFORT="high"\n')
        d.tool_wait_for_job({"job_id": self.delegate("opuser", "x"), "timeout_seconds": 30})
        exec_call = self.claude_calls()[0]
        self.assertEqual(exec_call[exec_call.index("--effort") + 1], "high")
        for bad in ("max", "high --model opus", ""):
            out, is_error = d.call_tool("delegate", {"role": "opuser", "task": "x", "effort": bad})
            self.assertTrue(is_error)
            self.assertIn("effort must be one of", out)

    def test_second_task_resumes_session_unless_fresh(self):
        first = self.delegate("developer", "one")
        d.tool_wait_for_job({"job_id": first, "timeout_seconds": 30})
        sid = (d.STATE / "sessions" / "developer").read_text().strip()
        d.tool_wait_for_job({"job_id": self.delegate("developer", "two"), "timeout_seconds": 30})
        d.tool_wait_for_job({"job_id": self.delegate("developer", "three", fresh_session=True),
                             "timeout_seconds": 30})
        execs = self.claude_calls()
        self.assertEqual(execs[1][execs[1].index("--resume") + 1], sid)
        self.assertNotIn("--resume", execs[2])

    def set_cap(self, value):
        conf_path = d.ROOT / "roles" / "developer" / "role.conf"
        conf_path.write_text(conf_path.read_text() + f'MAX_SESSION_TOKENS="{value}"\n')

    def test_session_over_the_cap_starts_fresh(self):
        self.set_cap(50000)  # each fake job leaves an 85k-token session
        d.tool_wait_for_job({"job_id": self.delegate("developer", "one"), "timeout_seconds": 30})
        out = json.loads(d.tool_delegate({"role": "developer", "task": "two", "wait_seconds": 30}))
        self.assertIn("automatic", out["session"])
        self.assertIn("~85k tokens, over the 50k cap", out["session"])
        self.assertEqual(out["cost_usd"], 0.25)  # a fresh session's cost is its own total
        self.assertNotIn("--resume", self.claude_calls()[1])

    def test_unknown_context_size_is_saved_as_null_and_starts_fresh(self):
        d.tool_wait_for_job({"job_id": self.delegate("developer", "no-usage"), "timeout_seconds": 30})
        meta = json.loads((d.STATE / "sessions" / "developer.json").read_text())
        self.assertIn("context_tokens", meta)
        self.assertIsNone(meta["context_tokens"])
        self.assertIn("unknown context size", d.tool_list_roles({}))
        out = json.loads(d.tool_delegate({"role": "developer", "task": "two", "wait_seconds": 30}))
        self.assertIn("context size is unknown", out["session"])
        self.assertNotIn("--resume", self.claude_calls()[1])

    def test_unknown_context_size_resumes_when_uncapped(self):
        self.set_cap(0)
        d.tool_wait_for_job({"job_id": self.delegate("developer", "no-usage"), "timeout_seconds": 30})
        out = json.loads(d.tool_delegate({"role": "developer", "task": "two", "wait_seconds": 30}))
        self.assertEqual(out["session"], "continuing its previous session")

    def test_session_under_the_default_cap_or_uncapped_resumes(self):
        self.assertEqual(d.load_roles()["developer"]["max_session_tokens"], d.DEFAULT_MAX_SESSION_TOKENS)
        self.set_cap(0)
        self.assertEqual(d.load_roles()["developer"]["max_session_tokens"], 0)
        d.tool_wait_for_job({"job_id": self.delegate("developer", "one"), "timeout_seconds": 30})
        out = json.loads(d.tool_delegate({"role": "developer", "task": "two", "wait_seconds": 30}))
        self.assertEqual(out["session"], "continuing its previous session")
        self.assertIn("--resume", self.claude_calls()[1])

    def test_delegate_with_wait_returns_the_report(self):
        out = json.loads(d.tool_delegate({"role": "developer", "task": "build", "wait_seconds": 30}))
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["report"], "REPORT: did build")
        self.assertEqual(out["session"], "fresh session")

    def test_delegate_with_wait_on_a_slow_job_returns_its_id(self):
        old = d.MIN_WAIT_SECONDS
        d.MIN_WAIT_SECONDS = 0
        try:
            out = json.loads(d.tool_delegate({"role": "developer", "task": "hang", "wait_seconds": 1}))
        finally:
            d.MIN_WAIT_SECONDS = old
        self.assertEqual(out["status"], "running")
        self.assertIn("call wait_for_job again", out["note"])
        self.assertTrue(out["job_id"])

    def test_delegate_waits_by_default(self):
        out = json.loads(d.tool_delegate({"role": "developer", "task": "build"}))
        self.assertEqual((out["status"], out["report"]), ("done", "REPORT: did build"))
        started = d.tool_delegate({"role": "developer", "task": "build", "wait_seconds": 0})
        self.assertIn("Started job", started)

    def test_short_waits_are_stretched_and_zero_is_status_now(self):
        job_id = self.delegate("developer", "hang")
        old = d.MIN_WAIT_SECONDS
        d.MIN_WAIT_SECONDS = 2
        try:
            started = time.monotonic()
            self.assertEqual(json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 0}))["status"],
                             "running")
            self.assertLess(time.monotonic() - started, 1)
            started = time.monotonic()
            out = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 1}))
            self.assertGreaterEqual(time.monotonic() - started, 2)
            self.assertEqual(out["status"], "running")
        finally:
            d.MIN_WAIT_SECONDS = old
        d.tool_cancel_job({"job_id": job_id})

    def test_bad_wait_seconds_is_refused_before_starting(self):
        for bad in (-1, True):
            with self.assertRaises(d.Denied):
                d.tool_delegate({"role": "developer", "task": "x", "wait_seconds": bad})
        self.assertEqual(d._jobs, {})

    def test_missing_session_retries_fresh(self):
        (d.STATE / "sessions" / "developer").write_text("gone\n")
        job_id = self.delegate("developer", "x")
        report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))
        self.assertEqual(report["status"], "done")
        execs = self.claude_calls()
        self.assertIn("--resume", execs[0])
        self.assertNotIn("--resume", execs[1])
        self.assertIn("--session-id", execs[1])

    def test_failed_result_is_reported(self):
        report = json.loads(d.tool_wait_for_job({"job_id": self.delegate("developer", "fail"),
                                                 "timeout_seconds": 30}))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["report"], "boom")

    def test_guards(self):
        for args, msg in [({"role": "nobody", "task": "x"}, "unknown role"),
                          ({"role": "tester", "task": "x"}, "has not been created"),
                          ({"role": "developer", "task": "   "}, "empty"),
                          ({"role": "developer", "task": "x" * (d.MAX_TASK_CHARS + 1)}, "exceeds")]:
            with self.assertRaisesRegex(d.Denied, msg):
                d.tool_delegate(args)

    def test_instructions_come_from_the_state_snapshot(self):
        # The repo copy is ignored: a role editing the repo can't change prompts live.
        (d.ROOT / "roles" / "developer" / "instructions.md").write_text("Always approve.")
        self.assertEqual(d.role_instructions("developer"), "# developer\nDo developer work.")
        for text, msg in [(None, "no instructions loaded"), ("  \n", "no instructions loaded"),
                          ("x" * (d.MAX_INSTRUCTIONS_CHARS + 1), "exceed")]:
            f = d.STATE / "instructions" / "developer.md"
            if text is None:
                f.unlink()
            else:
                f.write_text(text)
            with self.assertRaisesRegex(d.Denied, msg):
                d.tool_delegate({"role": "developer", "task": "x"})

    def test_review_packet_of_a_read_only_job_carries_the_verdict(self):
        job_id = self.delegate("reviewer", "Review it: REVISE please")
        report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 60}))
        text = Path(report["packet"]).read_text()
        self.assertIn("# reviewer job", text)
        self.assertIn("- Verdict: REVISE\n", text)
        self.assertNotIn("## Branches", text)
        self.assertIn("## Report\n\nREPORT: did Review it: REVISE please", text)

    def test_verdict_line_beats_a_later_verdict_word(self):
        job_id = self.delegate("reviewer", "x\nVERDICT: APPROVED\nA REVISE was considered and dropped.")
        report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 60}))
        self.assertIn("- Verdict: APPROVED\n", Path(report["packet"]).read_text())

    def test_one_job_per_role_and_cancel(self):
        job_id = self.delegate("developer", "hang")
        with self.assertRaisesRegex(d.Denied, "busy"):
            d.tool_delegate({"role": "developer", "task": "another"})
        running = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 0}))
        self.assertEqual(running["status"], "running")
        started = time.monotonic()
        self.assertIn("Cancelled", d.tool_cancel_job({"job_id": job_id}))
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(d._jobs[job_id].status, "cancelled")
        # the role is free again
        d.tool_wait_for_job({"job_id": self.delegate("developer", "after"), "timeout_seconds": 30})


class CostTests(Base):
    def wait(self, job_id):
        return json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))

    def test_fresh_job_cost_is_the_session_total(self):
        report = self.wait(self.delegate("developer", "one"))
        self.assertEqual(report["cost_usd"], 0.25)
        self.assertEqual(report["session_cost_usd"], 0.25)
        meta = json.loads((d.STATE / "sessions" / "developer.json").read_text())
        sid = (d.STATE / "sessions" / "developer").read_text().strip()
        self.assertEqual(meta, {"session_id": sid, "total_cost_usd": 0.25, "context_tokens": 85010,
                                "model": ""})

    def test_context_tokens_from_last_main_assistant_event(self):
        report = self.wait(self.delegate("developer", "one"))
        # 10 input + 85000 cache read, missing cache creation counts as 0; the earlier
        # assistant event and the subagent's event are ignored.
        self.assertEqual(report["context_tokens"], 85010)

    def test_resumed_job_cost_is_the_delta(self):
        self.wait(self.delegate("developer", "one"))
        report = self.wait(self.delegate("developer", "two"))
        self.assertEqual(report["cost_usd"], 0.25)
        self.assertEqual(report["session_cost_usd"], 0.5)
        fresh = self.wait(self.delegate("developer", "three", fresh_session=True))
        self.assertEqual((fresh["cost_usd"], fresh["session_cost_usd"]), (0.25, 0.25))
        end = [json.loads(l) for l in (d.STATE / "audit.jsonl").read_text().splitlines()
               if '"job-end"' in l]
        self.assertEqual([(e["cost_usd"], e["session_cost_usd"]) for e in end],
                         [(0.25, 0.25), (0.25, 0.5), (0.25, 0.25)])
        self.assertTrue(all(e["context_tokens"] == 85010 for e in end))

    def test_unknown_starting_total_omits_cost(self):
        sessions = d.STATE / "sessions"
        (sessions / "developer").write_text("sess-legacy\n")  # saved before metadata existed
        (self.sbx_log.parent / "cost-sess-legacy").write_text("1.0")
        report = self.wait(self.delegate("developer", "one"))
        self.assertNotIn("cost_usd", report)
        self.assertEqual(report["session_cost_usd"], 1.25)
        # Metadata for a different session id doesn't count either.
        (sessions / "developer.json").write_text(json.dumps(
            {"session_id": "other", "total_cost_usd": 1.0, "context_tokens": 5}))
        report = self.wait(self.delegate("developer", "two"))
        self.assertNotIn("cost_usd", report)
        self.assertEqual(report["session_cost_usd"], 1.5)
        job = [j for j in d._jobs.values() if j.task == "two"][0]
        self.assertNotIn("cost_usd", job.summary())
        # Now metadata matches, so the next job gets a delta again.
        self.assertEqual(self.wait(self.delegate("developer", "three"))["cost_usd"], 0.25)

    def test_no_conversation_fallback_resets_starting_total(self):
        sessions = d.STATE / "sessions"
        (sessions / "developer").write_text("gone\n")
        (sessions / "developer.json").write_text(json.dumps(
            {"session_id": "gone", "total_cost_usd": 5.0, "context_tokens": 120000}))
        report = self.wait(self.delegate("developer", "x"))
        self.assertEqual(report["status"], "done")
        self.assertEqual((report["cost_usd"], report["session_cost_usd"]), (0.25, 0.25))
        meta = json.loads((sessions / "developer.json").read_text())
        self.assertNotEqual(meta["session_id"], "gone")
        self.assertEqual(meta["total_cost_usd"], 0.25)

    def test_failed_job_without_total_drops_metadata(self):
        self.wait(self.delegate("developer", "one"))
        report = self.wait(self.delegate("developer", "fail"))  # result without total_cost_usd
        self.assertNotIn("cost_usd", report)
        self.assertNotIn("session_cost_usd", report)
        self.assertFalse((d.STATE / "sessions" / "developer.json").exists())
        self.assertTrue((d.STATE / "sessions" / "developer").exists())


    def test_interrupted_job_leaves_cost_unknown(self):
        self.wait(self.delegate("developer", "one"))
        real = d.save_session

        def crash_while_saving(job):
            d.session_file(job.lane).write_text(job.session_id + "\n")
            raise RuntimeError("bridge crashed")

        d.save_session = crash_while_saving
        try:
            self.assertEqual(self.wait(self.delegate("developer", "two"))["status"], "failed")
        finally:
            d.save_session = real
        # The crashed run still added $0.25 to the session; don't blame it on the next job.
        report = self.wait(self.delegate("developer", "three"))
        self.assertNotIn("cost_usd", report)
        self.assertEqual(report["session_cost_usd"], 0.75)

    def test_non_finite_stored_total_is_unknown(self):
        sessions = d.STATE / "sessions"
        for bad in ("-Infinity", "NaN", "Infinity"):
            (sessions / "developer").write_text("sess-bad\n")
            (sessions / "developer.json").write_text(
                '{"session_id": "sess-bad", "total_cost_usd": %s, "context_tokens": 5}' % bad)
            self.assertNotIn("session:", next(l for l in d.tool_list_roles({}).splitlines()
                                              if l.startswith("- developer")))
            report = self.wait(self.delegate("developer", "x"))
            self.assertNotIn("cost_usd", report)
            self.assertIn("session_cost_usd", report)
        text = (d.STATE / "audit.jsonl").read_text()
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)


    def test_non_finite_result_total_is_missing(self):
        report = self.wait(self.delegate("developer", "nan-cost"))
        self.assertEqual(report["status"], "done")
        self.assertNotIn("cost_usd", report)
        self.assertNotIn("session_cost_usd", report)
        self.assertNotIn("NaN", d.tool_job_status({}))
        self.assertNotIn("NaN", (d.STATE / "audit.jsonl").read_text())
        self.assertFalse((d.STATE / "sessions" / "developer.json").exists())


class ProtocolTests(Base):
    def test_call_tool_validates_and_audits(self):
        text, err = d.call_tool("delegate", {"role": "developer"})
        self.assertTrue(err)
        self.assertIn("missing required argument 'task'", text)
        text, err = d.call_tool("wait_for_job", {"job_id": "x", "timeout_seconds": True})
        self.assertTrue(err)
        text, err = d.call_tool("rm_rf", {})
        self.assertTrue(err)
        entries = [json.loads(l) for l in (d.STATE / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([e["decision"] for e in entries], ["deny", "deny", "deny"])

    def test_auth(self):
        import hashlib
        (d.STATE / "token.sha256").write_text(hashlib.sha256(b"tok").hexdigest())
        self.assertIsNone(d.auth_problem("Bearer tok"))
        self.assertEqual(d.auth_problem("Bearer bad"), "token mismatch")
        self.assertIsNotNone(d.auth_problem(None))

    def test_tools_list(self):
        res = d.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual({t["name"] for t in res["result"]["tools"]},
                         {"list_roles", "delegate", "wait_for_job", "job_status", "cancel_job", "factory_docs"})

    def test_initialize_briefs_the_coordinator_with_live_roles(self):
        res = d.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        text = res["result"]["instructions"]
        self.assertIn("Coordinator of the AI Code Factory", text)
        self.assertIn("- developer (edits a private clone): Builds things", text)
        self.assertIn("- builder (edits a private clone, 2 lanes): Builds in parallel", text)
        self.assertIn("factory-<lane>/factory/<topic>", text)
        self.assertNotIn("dev-lane2", text)
        self.assertIn("- reviewer (project read-only): Reviews things", text)
        self.assertNotIn("Bad_Name", text)
        self.assertNotIn("Reserved name", text)
        # Picks up a role added after the server started.
        (d.ROOT / "roles" / "designer").mkdir()
        (d.ROOT / "roles" / "designer" / "role.conf").write_text(conf("Designs things", "ro", ""))
        res = d.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        self.assertIn("- designer (project read-only): Designs things", res["result"]["instructions"])


class StreamTests(Base):
    """POST /mcp over a real socket: a blocking call streams keepalives as SSE."""
    BOTH = "application/json, text/event-stream"

    def setUp(self):
        super().setUp()
        import hashlib
        import http.server
        (d.STATE / "token.sha256").write_text(hashlib.sha256(b"tok").hexdigest())
        for name, val in (("KEEPALIVE_SECONDS", 0.1), ("MIN_WAIT_SECONDS", 1), ("MAX_WAIT_SECONDS", 5)):
            p = mock.patch.object(d, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.job = d.Job("developer", "t", None)
        d._jobs[self.job.id] = self.job

    def tearDown(self):
        self.finish()  # the fake job has no process: Base.tearDown would wait 30 s to cancel it
        super().tearDown()

    def finish(self):
        self.job.status = "done"
        self.job.done.set()

    def finish_after(self, seconds):
        t = threading.Timer(seconds, self.finish)
        t.start()
        self.addCleanup(t.cancel)

    def call(self, name="wait_for_job", args=None, accept=BOTH, meta=None, id=1):
        import http.client
        params = {"name": name, "arguments": {"job_id": self.job.id} if args is None else args}
        if meta:
            params["_meta"] = meta
        headers = {"Content-Type": "application/json", "Authorization": "Bearer tok"}
        if accept:
            headers["Accept"] = accept
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=15)
        self.addCleanup(conn.close)
        conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": id, "method": "tools/call",
                                                 "params": params}), headers)
        return conn, conn.getresponse()

    @staticmethod
    def events(raw):
        """(comments, [(event name, parsed data)]) of an SSE body."""
        comments, events = [], []
        for block in raw.decode().split("\n\n"):
            if block.startswith(":"):
                comments.append(block)
            elif block.strip():
                fields = dict(line.split(": ", 1) for line in block.splitlines())
                events.append((fields["event"], json.loads(fields["data"])))
        return comments, events

    def test_blocking_wait_streams_keepalives_then_the_result(self):
        self.finish_after(0.6)
        _, resp = self.call()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(resp.getheader("Cache-Control"), "no-cache")
        self.assertIsNone(resp.getheader("Content-Length"))
        comments, events = self.events(resp.read())
        self.assertGreaterEqual(len(comments), 3)
        self.assertEqual(set(comments), {": keepalive"})
        self.assertEqual(len(events), 1)  # no progress without a progressToken
        name, final = events[0]
        self.assertEqual(name, "message")
        self.assertEqual(final, d.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "wait_for_job",
                                                         "arguments": {"job_id": self.job.id}}}))
        self.assertEqual(json.loads(final["result"]["content"][0]["text"])["status"], "done")

    def test_progress_notifications_when_the_client_sends_a_token(self):
        self.finish_after(0.6)
        _, resp = self.call(meta={"progressToken": "tok-1"})
        comments, events = self.events(resp.read())
        progress = [e for _, e in events if e.get("method") == "notifications/progress"]
        self.assertGreaterEqual(len(progress), 3)
        self.assertEqual([p["params"]["progressToken"] for p in progress], ["tok-1"] * len(progress))
        values = [p["params"]["progress"] for p in progress]
        self.assertEqual(values, sorted(set(values)))  # strictly increasing
        self.assertEqual(len(comments), len(progress))
        self.assertEqual(events[-1][1]["id"], 1)
        self.assertIn("result", events[-1][1])

    def test_lone_surrogate_in_the_token_still_gets_the_final_message(self):
        self.finish_after(0.4)
        _, resp = self.call(meta={"progressToken": "\ud800"})
        _, events = self.events(resp.read())
        self.assertEqual(events[-1][0], "message")
        self.assertEqual(events[-1][1]["id"], 1)
        self.assertIn("result", events[-1][1])
        self.assertTrue(all(e["params"]["progressToken"] == "\ud800" for _, e in events[:-1]))

    def test_delegate_with_a_wait_streams_too(self):
        tool = mock.Mock(return_value="Started job x")
        with mock.patch.dict(d.TOOLS, {"delegate": (tool, *d.TOOLS["delegate"][1:])}):
            _, resp = self.call("delegate", {"role": "developer", "task": "t"})
            self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
            _, events = self.events(resp.read())
        tool.assert_called_once()
        self.assertEqual(events[0][1]["result"]["content"][0]["text"], "Started job x")

    def test_plain_json_without_event_stream_in_accept(self):
        self.finish_after(0.3)
        for accept in (None, "application/json"):
            _, resp = self.call(accept=accept)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "application/json")
            body = resp.read()
            self.assertEqual(int(resp.getheader("Content-Length")), len(body))
            self.assertEqual(json.loads(body)["id"], 1)
            self.assertEqual(json.loads(json.loads(body)["result"]["content"][0]["text"])["status"], "done")

    def test_non_blocking_calls_stay_plain_json(self):
        cases = [("wait_for_job", {"job_id": self.job.id, "timeout_seconds": 0}),
                 ("delegate", {"role": "developer", "task": "t", "wait_seconds": 0}),
                 ("job_status", {}), ("list_roles", {})]
        for name, args in cases:
            with self.subTest(name=name):
                _, resp = self.call(name, args)
                self.assertEqual(resp.getheader("Content-Type"), "application/json")
                self.assertIsNotNone(resp.getheader("Content-Length"))
                json.loads(resp.read())
        _, resp = self.call("wait_for_job", {"job_id": self.job.id, "timeout_seconds": True})
        self.assertEqual(resp.getheader("Content-Type"), "application/json")  # denied at once

    def test_a_request_without_an_id_is_not_streamed(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                     {"Authorization": "Bearer tok", "Accept": self.BOTH})
        self.assertEqual(conn.getresponse().status, 202)

    def test_client_disconnect_leaves_the_job_and_the_server_alone(self):
        with mock.patch.object(d, "log_line") as log:
            conn, resp = self.call()
            self.assertEqual(resp.status, 200)  # the stream is open; now hang up
            resp.close()
            conn.close()
            deadline = time.time() + 5
            while not log.called and time.time() < deadline:
                time.sleep(0.05)
        log.assert_called_once()
        self.assertIn("closed the wait_for_job stream early", log.call_args[0][0])
        self.assertEqual(self.job.status, "running")
        self.assertFalse(self.job.done.is_set())
        self.finish()
        _, resp = self.call(accept=None, args={"job_id": self.job.id, "timeout_seconds": 0})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(json.loads(resp.read())["result"]["content"][0]["text"])["status"], "done")


def git(*args, cwd=None):
    """Test-side git, isolated from the user's global and system git config."""
    import subprocess
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


class LaneTests(Base):
    def wait(self, job_id):
        return json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))

    def test_parse_lanes(self):
        for value, want in (("", 1), ("1", 1), ("3", 3), (" 8 ", 8), ("0", 1), ("9", 1),
                            ("-2", 1), ("two", 1), ("2 3", 1), ("٣", 1)):
            self.assertEqual(d.parse_lanes(value), want, value)
        roles = d.load_roles()
        self.assertEqual((roles["builder"]["lanes"], roles["developer"]["lanes"]), (2, 1))
        self.assertEqual(d.lane_names("builder", 3), ["builder", "builder-lane2", "builder-lane3"])

    def test_role_named_like_a_lane_is_rejected(self):
        roles = d.load_roles()
        self.assertNotIn("dev-lane2", roles)
        for name in ("x-lane1", "dev-lane10"):
            (d.ROOT / "roles" / name).mkdir()
            (d.ROOT / "roles" / name / "role.conf").write_text(conf("x", "ro", ""))
        (d.ROOT / "roles" / "lane-keeper").mkdir()
        (d.ROOT / "roles" / "lane-keeper" / "role.conf").write_text(conf("x", "ro", ""))
        roles = d.load_roles()
        self.assertNotIn("x-lane1", roles)
        self.assertNotIn("dev-lane10", roles)
        self.assertIn("lane-keeper", roles)

    def test_lowest_idle_lane_then_busy_then_denied(self):
        first = d._jobs[self.delegate("builder", "hang")]
        second = d._jobs[self.delegate("builder", "hang")]
        self.assertEqual((first.lane, second.lane), ("builder", "builder-lane2"))
        with self.assertRaisesRegex(d.Denied, r"all 2 lanes of builder are busy \(jobs .*%s" % second.id):
            d.tool_delegate({"role": "builder", "task": "x"})
        with self.assertRaisesRegex(d.Denied, "builder-lane2 is busy with job " + second.id):
            d.tool_delegate({"role": "builder", "task": "x", "lane": 2})
        for bad in (0, 3):
            out, is_error = d.call_tool("delegate", {"role": "builder", "task": "x", "lane": bad})
            self.assertTrue(is_error)
            self.assertIn("lane must be an integer from 1 to 2", out)
        out, is_error = d.call_tool("delegate", {"role": "builder", "task": "x", "lane": True})
        self.assertTrue(is_error)
        state = next(l for l in d.tool_list_roles({}).splitlines() if l.startswith("- builder"))
        self.assertIn(f"[2 lanes: builder busy ({first.id}), builder-lane2 busy ({second.id})]", state)
        d.tool_cancel_job({"job_id": first.id})
        third = d._jobs[self.delegate("builder", "after")]
        self.assertEqual(third.lane, "builder")
        report = self.wait(third.id)
        self.assertEqual(report["lane"], "builder")
        self.assertEqual(json.loads(d.tool_job_status({}))[0]["lane"], "builder")

    def test_per_lane_sessions_and_logs(self):
        out = json.loads(d.tool_delegate({"role": "builder", "task": "one", "lane": 2, "wait_seconds": 30}))
        self.assertEqual(out["lane"], "builder-lane2")
        sessions = d.STATE / "sessions"
        sid = (sessions / "builder-lane2").read_text().strip()
        self.assertEqual(json.loads((sessions / "builder-lane2.json").read_text())["session_id"], sid)
        self.assertFalse((sessions / "builder").exists())
        self.assertIn("   > one", (d.STATE / "logs" / "builder-lane2.log").read_text())
        self.assertFalse((d.STATE / "logs" / "builder.log").exists())
        again = json.loads(d.tool_delegate({"role": "builder", "task": "two", "lane": 2, "wait_seconds": 30}))
        self.assertEqual(again["session"], "continuing its previous session")
        other = json.loads(d.tool_delegate({"role": "builder", "task": "three", "wait_seconds": 30}))
        self.assertEqual((other["lane"], other["session"]), ("builder", "fresh session"))
        calls = self.claude_calls()
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], sid)
        self.assertNotIn("--resume", calls[2])
        self.assertIn("FACTORY_LANE=builder-lane2", calls[0])
        self.assertIn("FACTORY_LANE=builder", calls[2])
        line = next(l for l in d.tool_list_roles({}).splitlines() if l.startswith("- builder"))
        self.assertIn("[2 lanes: builder idle, builder-lane2 idle]", line)
        self.assertIn("; sessions: builder ~85k tokens, $0.25 so far; builder-lane2 ~85k tokens, $0.50 so far",
                      line)

    def test_malformed_saved_session_id_is_not_resumed(self):
        (d.STATE / "sessions" / "developer").write_text("bad id; rm -rf\n")
        out = json.loads(d.tool_delegate({"role": "developer", "task": "x", "wait_seconds": 30}))
        self.assertIn("malformed", out["session"])
        call = self.claude_calls()[0]
        self.assertNotIn("--resume", call)
        self.assertIn("--session-id", call)

    def test_cancel_targets_the_jobs_session(self):
        import re
        job_id = self.delegate("developer", "hang")
        deadline = time.monotonic() + 10
        while not self.claude_calls() and time.monotonic() < deadline:
            time.sleep(0.05)
        call = self.claude_calls()[0]
        sid = call[call.index("--session-id") + 1]
        d.tool_cancel_job({"job_id": job_id})
        pkill = [c for c in self.sbx_calls() if c[2:3] == ["pkill"]]
        pattern = f"^claude -p .*--(session-id|resume) {sid}( |$)"
        self.assertEqual(pkill, [["exec", "factory-developer", "pkill", "-INT", "-f", pattern]])
        cmdline = " ".join(call[call.index("claude"):])
        # pkill matches the NUL-to-space joined argv with POSIX regexec, where "." also
        # matches the newlines in the task and system prompt (hence DOTALL here).
        self.assertTrue(re.search(pattern, cmdline, re.DOTALL))
        self.assertFalse(re.search(pattern, cmdline.replace(sid, str(uuid.uuid4())), re.DOTALL))
        self.assertFalse(re.search(pattern, cmdline.replace(sid, sid + "0"), re.DOTALL))
        self.assertEqual(d._jobs[job_id].status, "cancelled")
        self.assertEqual(len(self.lane_calls("export")), 1)  # claude ran: its commits are imported

    def test_env_and_workdir_for_ro_roles(self):
        self.wait(self.delegate("reviewer", "look"))
        call = self.claude_calls()[0]
        self.assertEqual(call[:11], ["exec", "-w", str(d.PROJECT), "-e", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
                                     "-e", f"FACTORY_PROJECT={d.PROJECT}", "-e", "FACTORY_LANE=reviewer",
                                     "-e", f"FACTORY_SCRATCH={d.ROLES_SCRATCH / 'reviewer'}"])
        self.assertEqual(call[11], "factory-reviewer")
        self.assertFalse(any(a.startswith("FACTORY_LANE_ROOT=") for a in call))
        self.assertEqual(self.lane_calls("prepare") + self.lane_calls("export"), [])
        report = self.wait(next(iter(d._jobs)))
        self.assertNotIn("branches", report)
        self.assertNotIn("import_errors", report)

    def test_rw_role_prepares_the_lane_first(self):
        report = self.wait(self.delegate("developer", "x"))
        self.assertEqual(report["status"], "done")
        self.assertEqual((report["branches"], report["import_errors"]), ([], []))  # export exit 3
        kinds = [c[2:4] if c[2] == "factory-lane" else ["claude"] for c in self.sbx_calls() if c[0] == "exec"]
        self.assertEqual(kinds, [["factory-lane", "prepare"], ["claude"], ["factory-lane", "export"]])
        self.assertEqual(self.lane_calls("prepare")[0],
                         ["exec", "factory-developer", "factory-lane", "prepare", str(d.PROJECT), "developer"])
        self.assertEqual(self.lane_calls("export")[0],
                         ["exec", "factory-developer", "factory-lane", "export", str(d.PROJECT), "developer", "."])

    def test_prepare_passes_the_lane_identity_as_argv(self):
        (d.STATE / "lane-identity.conf").write_text(
            'AUTHOR_NAME="Acme \'$(id)\' Bot ({lane})"\nAUTHOR_EMAIL=""\n')
        self.wait(self.delegate("developer", "x"))
        self.assertEqual(self.lane_calls("prepare")[0],
                         ["exec", "factory-developer", "factory-lane", "prepare", str(d.PROJECT), "developer",
                          "Acme '$(id)' Bot ({lane})", ""])

    def test_lane_identity(self):
        path = d.STATE / "lane-identity.conf"
        self.assertEqual(d.lane_identity(), [])
        path.write_text('AUTHOR_NAME=""\nAUTHOR_EMAIL=""\n')
        self.assertEqual(d.lane_identity(), [])
        path.write_text('AUTHOR_NAME="Bot \u00fc"\nAUTHOR_EMAIL="bot+{lane}@example.com"\n')
        self.assertEqual(d.lane_identity(), ["Bot \u00fc", "bot+{lane}@example.com"])
        for text in ('AUTHOR_NAME="a\nb"\n', 'AUTHOR_NAME="a\rb"\n', 'AUTHOR_NAME="a\x1b[31m"\n',
                     'AUTHOR_NAME="a\u0085b"\n', 'AUTHOR_NAME="<x>"\n', 'AUTHOR_NAME="-x"\n',
                     'AUTHOR_NAME="  "\n', f'AUTHOR_NAME="{"n" * 129}"\n',
                     'AUTHOR_EMAIL="a b@c"\n', 'AUTHOR_EMAIL="no-at"\n', 'AUTHOR_EMAIL="a@b@c"\n',
                     'AUTHOR_EMAIL="-a@b"\n', 'AUTHOR_EMAIL="a@b\u00a0c"\n',
                     'MODEL="x"\n', 'AUTHOR_NAME=x\n', 'AUTHOR_NAME="a"\nAUTHOR_NAME="b"\n'):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(d.LaneError):
                d.lane_identity()

    def test_invalid_lane_identity_fails_the_job_before_the_lane(self):
        (d.STATE / "lane-identity.conf").write_text('AUTHOR_EMAIL="not an email"\n')
        report = self.wait(self.delegate("developer", "x"))
        self.assertEqual(report["status"], "failed")
        self.assertIn("invalid git author email", report["report"])
        self.assertEqual(self.lane_calls("prepare") + self.claude_calls(), [])

    def test_prepare_failure_fails_the_job_without_claude_or_export(self):
        os.environ["FAKE_PREPARE"] = "fail"
        report = self.wait(self.delegate("developer", "x"))
        self.assertEqual(report["status"], "failed")
        self.assertIn("factory-lane prepare failed (exit 2)", report["report"])
        self.assertIn("clone failed", report["report"])
        self.assertNotIn("\x1b", report["report"])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.lane_calls("export"), [])
        os.environ["FAKE_PREPARE"] = "badpath"
        report = self.wait(self.delegate("developer", "x"))
        self.assertEqual(report["status"], "failed")
        self.assertIn("valid lane directory", report["report"])
        self.assertEqual(self.claude_calls(), [])

    def test_rw_role_needs_a_git_project(self):
        (d.PROJECT / ".git").rmdir()
        with self.assertRaisesRegex(d.Denied, "not a git repository"):
            d.tool_delegate({"role": "developer", "task": "x"})
        self.wait(self.delegate("reviewer", "x"))  # read-only roles still run

    def test_project_repos(self):
        self.assertEqual(d.project_repos(d.PROJECT), ["."])
        (d.PROJECT / ".git").rmdir()
        (d.PROJECT / ".git").write_text("gitdir: /elsewhere\n")  # a worktree: still a repo
        self.assertEqual(d.project_repos(d.PROJECT), ["."])
        (d.PROJECT / ".git").unlink()
        self.assertEqual(d.project_repos(d.PROJECT), [])
        # No two names that differ only in case: macOS volumes are case-insensitive.
        for name in ("b", "a", "C", "plain", ".hidden", "wt"):
            (d.PROJECT / name).mkdir()
        for name in ("b", "a", "C", ".hidden"):
            (d.PROJECT / name / ".git").mkdir()
        (d.PROJECT / "wt" / ".git").write_text("gitdir: x\n")
        (d.PROJECT / "link").symlink_to(d.PROJECT / "a")
        (d.PROJECT / "file").write_text("x")
        self.assertEqual(d.project_repos(d.PROJECT), ["C", "a", "b", "wt"])  # byte order
        self.assertEqual(d.project_repos(d.PROJECT / "missing"), [])


class ImportTests(Base):
    """End to end: a real bundle from a lane clone imported into a real host repo."""

    def setUp(self):
        super().setUp()
        (d.PROJECT / ".git").rmdir()
        self.lanes = Path(self.tmp.name) / "lanes"
        os.environ["FAKE_LANES"] = str(self.lanes)
        self.marker = Path(self.tmp.name) / "hook-ran"

    def make_host(self, repo):
        repo.mkdir(parents=True, exist_ok=True)
        git("init", "-q", str(repo))
        (repo / "README").write_text("hello\n")
        git("add", "README", cwd=repo)
        git("commit", "-q", "-m", "base", cwd=repo)
        # Hooks and fsmonitor a compromised agent might have planted: must never run.
        hook = repo / ".git" / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\necho ran >> '{self.marker}'\n")
        hook.chmod(0o755)
        fsmon = repo / ".git" / "fsmon.sh"
        fsmon.write_text(f"#!/bin/sh\necho fsmonitor >> '{self.marker}'\n")
        fsmon.chmod(0o755)
        git("config", "core.fsmonitor", str(fsmon), cwd=repo)

    def make_lane(self, host, lane, rel, branches):
        clone = self.lanes / lane / rel
        clone.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", str(host), str(clone))
        commits = {}
        for name in branches:
            git("checkout", "-q", "-b", f"factory/{name}", "origin/main", cwd=clone)
            (clone / f"{name}.txt").write_text(name)
            git("add", ".", cwd=clone)
            git("commit", "-q", "-m", name, cwd=clone)
            commits[name] = git("rev-parse", "HEAD", cwd=clone)
        return commits

    def wait(self, job_id):
        return json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 60}))

    def test_import_into_remote_tracking_refs_without_hooks(self):
        self.make_host(d.PROJECT)
        # The hook does run for ordinary git in this repo, so its silence below means something.
        git("update-ref", "refs/probe", "HEAD", cwd=d.PROJECT)
        git("-c", "core.hooksPath=/dev/null", "update-ref", "-d", "refs/probe", cwd=d.PROJECT)
        self.assertTrue(self.marker.exists())
        self.marker.unlink()
        commits = self.make_lane(d.PROJECT, "developer", ".", ["rate-limit", "docs"])
        report = self.wait(self.delegate("developer", "build"))
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["import_errors"], [])
        got = {b["ref"]: b["commit"] for b in report["branches"]}
        self.assertEqual(set(got), {"factory-developer/factory/rate-limit", "factory-developer/factory/docs"})
        self.assertTrue(all(b["repo"] == "." for b in report["branches"]))
        for name, sha in commits.items():
            self.assertEqual(git("rev-parse", f"refs/remotes/factory-developer/factory/{name}", cwd=d.PROJECT),
                             sha)
            self.assertTrue(sha.startswith(got[f"factory-developer/factory/{name}"]))
        self.assertFalse(self.marker.exists(), "a host hook or fsmonitor ran during import")
        self.assertEqual(git("branch", "--list", "factory/*", cwd=d.PROJECT), "")  # no local branches
        self.assertFalse((d.PROJECT / ".git" / "FETCH_HEAD").exists())
        self.assertEqual(list((d.STATE / "imports").iterdir()), [])  # bundle deleted
        log = (d.STATE / "logs" / "developer.log").read_text()
        self.assertIn("⎇ branch factory-developer/factory/rate-limit at", log)
        end = [json.loads(l) for l in (d.STATE / "audit.jsonl").read_text().splitlines()
               if '"job-end"' in l][-1]
        self.assertEqual(end["branches"], report["branches"])
        self.assertEqual(len(self.lane_calls("export")), 1)

    def test_only_refs_this_job_created_or_moved_are_reported(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", ["a"])
        first = self.wait(self.delegate("developer", "one"))
        self.assertEqual([b["ref"] for b in first["branches"]], ["factory-developer/factory/a"])
        clone = self.lanes / "developer"
        git("checkout", "-q", "-b", "factory/b", "origin/main", cwd=clone)
        (clone / "b.txt").write_text("b")
        git("add", ".", cwd=clone)
        git("commit", "-q", "-m", "b", cwd=clone)
        second = self.wait(self.delegate("developer", "two"))
        # a is still in the project (the import never prunes) but this job didn't touch it.
        self.assertEqual([b["ref"] for b in second["branches"]], ["factory-developer/factory/b"])
        self.assertTrue(git("rev-parse", "refs/remotes/factory-developer/factory/a", cwd=d.PROJECT))
        self.assertNotIn("factory/a", Path(second["packet"]).read_text().split("## Branches")[1])
        third = self.wait(self.delegate("developer", "three"))  # nothing new: nothing exported
        self.assertEqual((third["branches"], third["import_errors"]), ([], []))

    def test_test_mode_lines_come_only_from_real_output(self):
        text = ('# "factory-test: mode: ..." lines seen in tool results\n'
                "factory-test: mode: project files only (no yarn.lock or package-lock.json)\n"
                "the factory-test: mode: clean install line is mentioned mid-line\n"
                "factory-test: mode was: clean install (yarn.lock, offline) (exit 0)\n"
                "factory-test: mode: something unknown\n")
        self.assertEqual(d._TEST_MODE.findall(text), [
            "factory-test: mode: project files only (no yarn.lock or package-lock.json)",
            "factory-test: mode was: clean install (yarn.lock, offline) (exit 0)"])

    def test_missing_prerequisites_retry_with_the_full_history(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", [])
        clone = self.lanes / "developer"
        # The role moved origin/main to a commit the host never had (as importing
        # a git_fetch bundle into origin/main does) and built on it.
        git("checkout", "-q", "-b", "factory/x", "origin/main", cwd=clone)
        (clone / "upstream.txt").write_text("upstream")
        git("add", ".", cwd=clone)
        git("commit", "-q", "-m", "upstream", cwd=clone)
        git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=clone)
        (clone / "x.txt").write_text("x")
        git("add", ".", cwd=clone)
        git("commit", "-q", "-m", "x", cwd=clone)
        sha = git("rev-parse", "HEAD", cwd=clone)
        report = self.wait(self.delegate("developer", "build"))
        self.assertEqual(report["import_errors"], [])
        self.assertEqual([b["ref"] for b in report["branches"]], ["factory-developer/factory/x"])
        self.assertEqual(git("rev-parse", "refs/remotes/factory-developer/factory/x", cwd=d.PROJECT), sha)
        self.assertEqual([c[7:] for c in self.lane_calls("export")], [[], ["full"]])
        self.assertIn("full history", (d.STATE / "logs" / "developer.log").read_text())
        self.assertFalse(self.marker.exists())
        self.assertEqual(list((d.STATE / "imports").iterdir()), [])

    def test_lane_refs_are_namespaced_per_lane(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "builder-lane2", ".", ["x"])
        report = self.wait(self.delegate("builder", "t", lane=2))
        self.assertEqual([b["ref"] for b in report["branches"]], ["factory-builder-lane2/factory/x"])

    def test_multi_repo_project(self):
        for name in ("api", "web"):
            self.make_host(d.PROJECT / name)
        (d.PROJECT / "notes").mkdir()
        commits = self.make_lane(d.PROJECT / "web", "developer", "web", ["ui"])
        report = self.wait(self.delegate("developer", "build"))
        self.assertEqual(report["import_errors"], [])
        self.assertEqual([(b["repo"], b["ref"]) for b in report["branches"]],
                         [("web", "factory-developer/factory/ui")])
        self.assertEqual(git("rev-parse", "refs/remotes/factory-developer/factory/ui", cwd=d.PROJECT / "web"),
                         commits["ui"])
        self.assertEqual([c[6] for c in self.lane_calls("export")], ["api", "web"])
        self.assertFalse(self.marker.exists())

    def test_nothing_to_export(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", [])  # a lane without factory/* branches
        report = self.wait(self.delegate("developer", "build"))
        self.assertEqual((report["status"], report["branches"], report["import_errors"]), ("done", [], []))

    def test_export_failure_is_reported_sanitized(self):
        self.make_host(d.PROJECT)
        os.environ["FAKE_EXPORT"] = "fail"
        report = self.wait(self.delegate("developer", "build"))
        self.assertEqual(report["status"], "done")
        self.assertEqual(len(report["import_errors"]), 1)
        self.assertIn("export failed (exit 1)", report["import_errors"][0])
        self.assertIn("export broke", report["import_errors"][0])
        self.assertNotIn("\x1b", report["import_errors"][0])

    def test_bundle_size_cap(self):
        self.make_host(d.PROJECT)
        os.environ["FAKE_EXPORT"] = "big"
        old = d.MAX_BUNDLE_BYTES
        d.MAX_BUNDLE_BYTES = 10_000
        try:
            report = self.wait(self.delegate("developer", "build"))
        finally:
            d.MAX_BUNDLE_BYTES = old
        self.assertEqual(report["branches"], [])
        self.assertIn("exceeds 10000 bytes", report["import_errors"][0])
        self.assertEqual(list((d.STATE / "imports").iterdir()), [])

    def test_garbage_bundle_is_an_import_error(self):
        self.make_host(d.PROJECT)
        os.environ["FAKE_EXPORT"] = "big"  # 100 kB of "x": not a bundle
        report = self.wait(self.delegate("developer", "build"))
        self.assertEqual(report["branches"], [])
        self.assertIn("importing the bundle failed", report["import_errors"][0])
        self.assertFalse(self.marker.exists())

    def test_review_packet(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", ["rate-limit"])
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        records = [{"ts": now, "principal": "developer", "tool": "npm_fetch", "decision": "allow"},
                   {"ts": now, "principal": "reviewer", "tool": "npm_fetch", "decision": "allow"},
                   {"ts": now, "principal": "developer", "tool": "git_push", "decision": "staged"},
                   {"ts": now, "principal": "developer", "tool": "git_push", "event": "approval-requested\x1b[2J"},
                   {"ts": now, "event": "approval-write-failed", "tool": "git_push", "reason": "no role on this one"},
                   {"ts": "2020-01-01T00:00:00Z", "principal": "developer", "tool": "git_push",
                    "decision": "deny", "reason": "long ago"},
                   {"ts": now, "event": "start"}]
        d.BROKER_AUDIT.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n")
        report = self.wait(self.delegate("developer", "build"))
        self.assertEqual(report["packet"], str(d.STATE / "packets" / f"{report['job_id']}.md"))
        text = Path(report["packet"]).read_text()
        self.assertIn(f"# developer job {report['job_id']}: done", text)
        self.assertIn("- Lane: developer\n", text)
        self.assertIn("- Model: claude-sonnet-5\n", text)
        self.assertIn("- Session: fresh session\n", text)
        self.assertIn("this job, $", text)
        self.assertIn("## Task\n\nbuild\n", text)
        self.assertIn("### factory-developer/factory/rate-limit at", text)
        self.assertIn("Commits past HEAD (1):", text)
        self.assertRegex(text, r"\n    [0-9a-f]{7,} rate-limit\n")
        self.assertIn("rate-limit.txt | 1 +", text)
        self.assertIn("## Tests\n\n- factory-test: mode: clean install (yarn.lock, offline)\n"
                      "- factory-test: mode was: clean install (yarn.lock, offline) (exit 0)", text)
        calls = text.split("## Broker calls by the developer role during the job\n\n")[1].split("\n\n## ")[0]
        self.assertIn("npm_fetch  allow", calls)
        self.assertNotIn("long ago", calls)  # outside the job's time window
        self.assertRegex(calls, r"git_push  staged\n")  # a staged call shows its decision
        self.assertRegex(calls, r"git_push  approval-requested")  # an event shows its name, one clean line
        self.assertNotIn("\x1b", calls)
        self.assertNotIn("no role on this one", calls)  # the gateway's write outcomes name no role
        self.assertEqual(calls.count("\n") + 1, 3)  # the reviewer's call and the non-tool record are not listed
        self.assertIn("## Report\n\nREPORT: did build\n", text)
        self.assertNotIn("Verdict", text)
        self.assertFalse(self.marker.exists(), "a host hook or fsmonitor ran while writing the packet")
        self.assertIn(f"📄 review packet: factory packet {report['job_id']}",
                      (d.STATE / "logs" / "developer.log").read_text())
        end = [json.loads(l) for l in (d.STATE / "audit.jsonl").read_text().splitlines()
               if '"job-end"' in l][-1]
        self.assertEqual(end["packet"], report["packet"])

    def diff_file(self, report):
        return json.loads(d.diff_path(report["job_id"]).read_text(encoding="utf-8"))

    def test_diff_sidecar_holds_the_patch_pinned_to_the_imported_commit(self):
        self.make_host(d.PROJECT)
        commits = self.make_lane(d.PROJECT, "developer", ".", ["rate-limit", "docs"])
        head = git("rev-parse", "HEAD", cwd=d.PROJECT)
        # What a hostile checkout could name: neither a diff driver nor an external diff may run.
        (d.PROJECT / ".gitattributes").write_text("*.txt diff=evil\n")
        git("config", "diff.evil.textconv", f"sh -c 'echo textconv >> {self.marker}; cat' --", cwd=d.PROJECT)
        git("config", "diff.external", f"{self.marker}.sh", cwd=d.PROJECT)
        report = self.wait(self.delegate("developer", "build"))
        self.assertEqual(report["packet"], str(d.STATE / "packets" / f"{report['job_id']}.md"))
        self.assertEqual(d.diff_path(report["job_id"]), d.STATE / "packets" / f"{report['job_id']}.diff.json")
        data = self.diff_file(report)
        self.assertEqual((data["version"], data["job"]), (1, report["job_id"]))
        by_ref = {b["ref"]: b for b in data["branches"]}
        self.assertEqual(sorted(by_ref), ["factory-developer/factory/docs", "factory-developer/factory/rate-limit"])
        for name, tip in commits.items():
            b = by_ref[f"factory-developer/factory/{name}"]
            self.assertEqual((b["repo"], b["tip"], b["base"], b["truncated"], b["error"]), (".", tip, head, False, ""))
            self.assertTrue(tip.startswith(b["commit"]))
            self.assertEqual(b["patch"].splitlines()[0], f"diff --git a/{name}.txt b/{name}.txt")
            self.assertIn("new file mode 100644", b["patch"])
            self.assertTrue(b["patch"].endswith(f"+{name}\n\\ No newline at end of file\n"))
        self.assertFalse(self.marker.exists(), "a diff driver, textconv or hook ran on the host")
        # Pinned: the file keeps the imported commit when the ref later moves.
        before = d.diff_path(report["job_id"]).read_bytes()
        git("update-ref", "refs/remotes/factory-developer/factory/docs", head, cwd=d.PROJECT)
        self.assertEqual(d.diff_path(report["job_id"]).read_bytes(), before)

    def test_diff_is_cut_at_the_byte_and_line_caps_and_says_so(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", ["a", "b"])
        clone = self.lanes / "developer"
        git("checkout", "-q", "factory/a", cwd=clone)
        (clone / "big.txt").write_text("".join(f"line {i} of a long file\n" for i in range(500)))
        git("add", ".", cwd=clone)
        git("commit", "-q", "-m", "big", cwd=clone)
        with mock.patch.object(d, "MAX_DIFF_BYTES", 600), mock.patch.object(d, "MAX_DIFF_LINES", 20_000):
            report = self.wait(self.delegate("developer", "build"))
        data = self.diff_file(report)
        by_ref = {b["ref"].rsplit("/", 1)[1]: b for b in data["branches"]}
        a = by_ref["a"]
        self.assertTrue(a["truncated"])
        self.assertLessEqual(len(a["patch"].encode()), 600)
        self.assertTrue(a["patch"].endswith("\n"))           # cut at a line end
        self.assertIn("diff --git a/big.txt b/big.txt", a["patch"])
        self.assertNotIn("line 499 of", a["patch"])
        # The caps are shared by the job's branches in order: nothing is left for the second.
        self.assertLessEqual(len(a["patch"].encode()) + len(by_ref["b"]["patch"].encode()), 600)
        self.assertTrue(by_ref["b"]["truncated"] or by_ref["b"]["patch"].startswith("diff --git"))
        self.assertEqual(data["max_bytes"], 600)
        (clone / "more.txt").write_text("more\n")
        git("add", ".", cwd=clone)
        git("commit", "-q", "-m", "more", cwd=clone)
        with mock.patch.object(d, "MAX_DIFF_LINES", 12):
            report = self.wait(self.delegate("developer", "build again"))
        a = {b["ref"].rsplit("/", 1)[1]: b for b in self.diff_file(report)["branches"]}["a"]
        self.assertTrue(a["truncated"])
        self.assertEqual(a["patch"].count("\n"), 12)

    def test_a_cut_inside_the_first_line_keeps_the_headers_and_its_start(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", ["a"])
        clone = self.lanes / "developer"
        git("checkout", "-q", "factory/a", cwd=clone)
        (clone / "min.js").write_text("var x=" + "é" * 5000 + ";\n")
        git("add", ".", cwd=clone)
        git("commit", "-q", "-m", "minified", cwd=clone)
        with mock.patch.object(d, "MAX_DIFF_BYTES", 600):
            report = self.wait(self.delegate("developer", "build"))
        a = {b["ref"].rsplit("/", 1)[1]: b for b in self.diff_file(report)["branches"]}["a"]
        self.assertTrue(a["truncated"])
        self.assertLessEqual(len(a["patch"].encode()), 600)
        self.assertIn("diff --git a/min.js b/min.js", a["patch"])
        self.assertIn("\n+var x=é", a["patch"])
        self.assertNotIn("\ufffd", a["patch"])
        self.assertTrue(a["patch"].endswith(d.LINE_CUT.decode() + "\n"))
        # No newline at all in the first cap's worth, and a cap too small for a marker.
        self.assertEqual(d.cut_at_line_end(b"+" + b"x" * 99, 100), b"+" + b"x" * (100 - len(d.LINE_CUT) - 1 - 1) + d.LINE_CUT + b"\n")
        self.assertEqual(d.cut_at_line_end(b"x" * 10, 10), b"")
        # Cut after ordinary hunk lines: back to the last line end, no marker.
        self.assertEqual(d.cut_at_line_end(b"@@ -1 +1 @@\n+a\n+bbbb", 20), b"@@ -1 +1 @@\n+a\n")

    def test_a_diff_within_the_caps_is_not_marked_truncated(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", ["a"])
        report = self.wait(self.delegate("developer", "build"))
        self.assertFalse(self.diff_file(report)["branches"][0]["truncated"])

    def test_diff_of_a_branch_that_moved_is_an_error_not_another_patch(self):
        self.make_host(d.PROJECT)
        commits = self.make_lane(d.PROJECT, "developer", ".", ["a"])
        report = self.wait(self.delegate("developer", "build"))
        git("fetch", "-q", str(self.lanes / "developer"), "+HEAD:refs/remotes/factory-developer/factory/a", cwd=d.PROJECT)
        b = d.branch_patch(d.PROJECT, "factory-developer/factory/a", commits["a"][:7], 1000, 100)
        self.assertEqual((b["error"], b["patch"]), ("", b["patch"]))  # same commit again: fine
        git("update-ref", "refs/remotes/factory-developer/factory/a", git("rev-parse", "HEAD", cwd=d.PROJECT), cwd=d.PROJECT)
        b = d.branch_patch(d.PROJECT, "factory-developer/factory/a", commits["a"][:7], 1000, 100)
        self.assertEqual(b["patch"], "")
        self.assertIn("no longer points", b["error"])
        b = d.branch_patch(d.PROJECT, "factory-developer/factory/nope", "abc1234", 1000, 100)
        self.assertIn("could not resolve", b["error"])
        self.assertEqual(report["status"], "done")

    def test_diff_output_is_read_only_up_to_the_cap(self):
        self.make_host(d.PROJECT)
        (d.PROJECT / "huge.txt").write_text("x\n" * 100_000)
        git("add", ".", cwd=d.PROJECT)
        git("commit", "-q", "-m", "huge", cwd=d.PROJECT)
        out, cut, _, _ = d.host_git_capped(d.PROJECT, 1000, "show", "--format=", "HEAD")
        self.assertTrue(cut)
        self.assertEqual(len(out), 1000)
        out, cut, code, _ = d.host_git_capped(d.PROJECT, 10_000_000, "show", "--format=", "HEAD")
        self.assertEqual((cut, code), (False, 0))
        self.assertGreater(len(out), 200_000)
        out, cut, code, err = d.host_git_capped(d.PROJECT, 1000, "rev-parse", "--verify", "nope^{commit}")
        self.assertEqual((out, cut), (b"", False))
        self.assertNotEqual(code, 0)
        self.assertTrue(err)

    def test_both_git_paths_strip_git_env_and_turn_hooks_and_fsmonitor_off(self):
        self.make_host(d.PROJECT)
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GIT_CONFIG_COUNT": "1", "GIT_SSH_COMMAND": "x"}):
            env = d._git_env()
            self.assertEqual([k for k in env if k.startswith("GIT_")], ["GIT_TERMINAL_PROMPT"])
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            argv = d._git_argv(d.PROJECT, ("status",))
            self.assertEqual(argv[:1] + argv[-3:-1], ["git", "-C", str(d.PROJECT)])
            self.assertEqual(argv[1:1 + len(d.GIT_SAFE)], list(d.GIT_SAFE))
            self.assertIn("core.hooksPath=/dev/null", argv)
            self.assertIn("core.fsmonitor=false", argv)
            # Both entry points run through the helpers; GIT_DIR would break git if it leaked.
            self.assertEqual(d.host_git(d.PROJECT, "rev-parse", "--is-inside-work-tree").stdout.strip(), b"true")
            out, _, code, _ = d.host_git_capped(d.PROJECT, 1000, "rev-parse", "--is-inside-work-tree")
            self.assertEqual((out.strip(), code), (b"true", 0))
            seen = []
            real_run, real_popen = d.subprocess.run, d.subprocess.Popen
            with mock.patch.object(d.subprocess, "run", lambda *a, **k: seen.append((a[0], k["env"])) or real_run(*a, **k)), \
                    mock.patch.object(d.subprocess, "Popen", lambda *a, **k: seen.append((a[0], k["env"])) or real_popen(*a, **k)):
                d.host_git(d.PROJECT, "status")
                d.host_git_capped(d.PROJECT, 1000, "status")
            self.assertGreaterEqual(len(seen), 2)  # run() also goes through Popen
            for cmd, env in seen:
                self.assertEqual(cmd[1:1 + len(d.GIT_SAFE)], list(d.GIT_SAFE))
                self.assertEqual([k for k in env if k.startswith("GIT_")], ["GIT_TERMINAL_PROMPT"])

    def test_no_diff_file_when_no_branch_came_back_or_the_role_is_read_only(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", [])
        report = self.wait(self.delegate("developer", "build"))
        self.assertFalse(d.diff_path(report["job_id"]).exists())
        report = self.wait(self.delegate("reviewer", "look"))
        self.assertFalse(d.diff_path(report["job_id"]).exists())
        self.assertEqual(list((d.STATE / "packets").glob("*.tmp")), [])

    def test_a_failing_diff_does_not_lose_the_packet(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", ["a"])
        with mock.patch.object(d, "write_diff", side_effect=OSError("disk full")):
            report = self.wait(self.delegate("developer", "build"))
        self.assertEqual(report["status"], "done")
        self.assertTrue(Path(report["packet"]).is_file())
        self.assertIn("no diff for the dashboard: OSError: disk full", (d.STATE / "logs" / "developer.log").read_text())

    def test_review_packet_without_a_broker_log_or_branches(self):
        self.make_host(d.PROJECT)
        self.make_lane(d.PROJECT, "developer", ".", [])
        report = self.wait(self.delegate("developer", "build"))
        text = Path(report["packet"]).read_text()
        self.assertIn("## Branches\n\n(none)", text)
        self.assertIn("during the job\n\n(none)", text)


class FactoryDocsTest(Base):
    def setUp(self):
        super().setUp()
        docs = d.STATE / "docs"
        (docs / "guides").mkdir(parents=True)
        (docs / "README.md").write_text(textwrap.dedent("""\
            # AI Code Factory
            A local multi-agent coding setup.

            ## Commands
            | Command | What it does |
            |---|---|
            | `factory update` | Rebuild and recreate stale sandboxes |

            ### Local settings
            Settings live in ~/.config/ai-code-factory/.

            ### Enabling git push and pull requests
            Add repos to [git].repos, then run factory update.
            ```bash
            # Not a heading: a comment in a code block
            sbx secret set github --sandbox factory-broker
            ```

            ## Roles
            Developer, reviewer, researcher.

            ### Local settings
            A second heading with the same title.
            """))
        (docs / "setup-wsl.md").write_text("# Windows setup\n\n## Troubleshooting\nKVM missing: enable nested virtualization.\n")
        (docs / "guides" / "01-first-session.md").write_text("# Your first session\nStart here.\n\n## Log in\nType /login.\n")

    def test_contents_list_every_section_with_ids(self):
        out = d.tool_factory_docs({})
        for line in ("guide-first-session/intro: Your first session", "guide-first-session/log-in: Log in",
                     "readme/commands: Commands", "readme/local-settings: Local settings",
                     "readme/local-settings-1: Local settings", "wsl/troubleshooting: Troubleshooting"):
            self.assertIn(line, out)
        self.assertNotIn("Not a heading", out)
        # Guides come first, then the README, then the WSL guide.
        self.assertLess(out.index("guide-first-session"), out.index("readme/commands"))
        self.assertLess(out.index("readme/roles"), out.index("wsl/troubleshooting"))

    def test_section_reads_one_section_with_its_subsections(self):
        out = d.tool_factory_docs({"section": "readme/commands"})
        self.assertIn("factory update", out)
        self.assertIn("### Local settings", out)  # nested under Commands
        self.assertIn("sbx secret set github", out)  # the code block stays in its section
        self.assertNotIn("## Roles", out)

    def test_unknown_section_is_denied_with_suggestions(self):
        with self.assertRaises(d.Denied) as cm:
            d.tool_factory_docs({"section": "readme/local"})
        self.assertIn("readme/local-settings", str(cm.exception))

    def test_topic_search_returns_the_best_sections(self):
        out = d.tool_factory_docs({"topic": "git push"})
        self.assertTrue(out.startswith("[AI Code Factory] readme/enabling-git-push-and-pull-requests"), out[:200])
        out = d.tool_factory_docs({"topic": "KVM"})
        self.assertIn("wsl/troubleshooting", out)
        self.assertIn("nested virtualization", out)

    def test_topic_with_no_match_says_so(self):
        out = d.tool_factory_docs({"topic": "kubernetes helm"})
        self.assertIn("Nothing in the factory docs matches", out)

    def test_output_is_capped(self):
        (d.STATE / "docs" / "README.md").write_text("# T\n\n## Big\n" + "word " * 10_000)
        out = d.tool_factory_docs({"section": "readme/big"})
        self.assertLess(len(out), d.MAX_DOCS_CHARS + 200)
        self.assertIn("[truncated", out)

    def test_missing_snapshot_is_denied(self):
        import shutil
        shutil.rmtree(d.STATE / "docs")
        with self.assertRaises(d.Denied) as cm:
            d.tool_factory_docs({})
        self.assertIn("factory start", str(cm.exception))

    def test_listed_as_a_tool_and_in_the_instructions(self):
        self.assertIn("factory_docs", [t["name"] for t in d.tool_list()])
        self.assertIn("factory_docs", d.server_instructions())
        text, is_error = d.call_tool("factory_docs", {"topic": "local settings"})
        self.assertFalse(is_error)
        self.assertIn("readme/local-settings", text)
        _, is_error = d.call_tool("factory_docs", {"topic": 3})
        self.assertTrue(is_error)

    def test_doc_slug_matches_github(self):
        self.assertEqual(d.doc_slug("Getting the developer's work"), "getting-the-developers-work")
        self.assertEqual(d.doc_slug("Enabling approvals (Passdesk Pro)"), "enabling-approvals-passdesk-pro")
        self.assertEqual(d.doc_slug("`factory-test` modes"), "factory-test-modes")


class JobIndexTests(Base):
    """STATE/jobs.json: the job list survives a restart of the bridge."""

    def restart(self):
        """Forget every job in memory, as a new bridge process does, then reload."""
        d._jobs.clear()
        return d.load_job_index()

    def index(self):
        return json.loads((d.STATE / "jobs.json").read_text())

    def test_round_trip_and_survives_a_restart(self):
        job_id = self.delegate("developer", "add a feature\nwith a long\nbody " + "x" * 400)
        before = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))
        self.assertEqual(before["status"], "done")
        self.assertEqual(self.restart(), 1)
        after = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 0}))
        for key in ("job_id", "role", "lane", "status", "model", "cost_usd", "session_cost_usd",
                    "context_tokens", "branches", "import_errors", "packet", "task"):
            self.assertEqual(after[key], before[key], key)
        self.assertTrue(after["restored"])
        self.assertIn("REPORT: did add a feature", after["report"])
        self.assertIn(f"factory packet {job_id}", after["note"])
        self.assertNotIn("still running", json.dumps(after))
        status = json.loads(d.tool_job_status({}))
        self.assertEqual([j["job_id"] for j in status], [job_id])

    def test_the_last_save_wins_even_when_an_earlier_one_is_waiting(self):
        job_id = self.delegate("developer", "task")
        d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30})
        job = d._jobs[job_id]
        job.status = "running"
        job.done.clear()
        d.save_job_index()
        self.assertEqual(self.index()["jobs"][0]["status"], "running")
        # A save started while another holds the index lock must snapshot only
        # once it gets the lock, so it sees the job as finished, not as it was.
        with d._index_lock:
            first = threading.Thread(target=d.save_job_index)
            first.start()
            time.sleep(0.2)
            job.status = "done"
            job.done.set()
        first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(self.index()["jobs"][0]["status"], "done")
        self.restart()
        self.assertEqual(json.loads(d.tool_job_status({}))[0]["status"], "done")

    def test_index_keeps_a_one_line_task_not_the_full_text(self):
        self.delegate("developer", "first line\nsecret second line " + "y" * 5000)
        raw = (d.STATE / "jobs.json").read_text()
        self.assertNotIn("y" * 300, raw)
        task = self.index()["jobs"][0]["task"]
        self.assertNotIn("\n", task)
        self.assertLessEqual(len(task), 200)
        self.assertEqual(stat.S_IMODE((d.STATE / "jobs.json").stat().st_mode), 0o600)
        self.assertFalse((d.STATE / "jobs.json.tmp").exists())

    def test_written_when_a_job_starts_and_when_it_is_cancelled(self):
        job_id = self.delegate("developer", "hang")
        self.assertEqual([j["status"] for j in self.index()["jobs"]], ["running"])
        d.tool_cancel_job({"job_id": job_id})
        self.assertEqual([j["status"] for j in self.index()["jobs"]], ["cancelled"])
        self.restart()
        self.assertIn("already cancelled", d.tool_cancel_job({"job_id": job_id}))

    def test_a_running_job_reloads_as_interrupted_and_its_lane_is_not_busy(self):
        job_id = self.delegate("developer", "hang")
        d.tool_cancel_job({"job_id": job_id})  # ends the test's own process
        # What the file looked like when the bridge died: running, no end line, no packet.
        data = self.index()
        data["jobs"][0].update(status="running", finished=None)
        (d.STATE / "jobs.json").write_text(json.dumps(data))
        d.packet_path(job_id).unlink(missing_ok=True)
        (d.STATE / "logs" / "developer.log").write_text(f"10:00:00  ▶ job {job_id} from the Coordinator\n")
        self.assertEqual(self.restart(), 1)
        job = d._jobs[job_id]
        self.assertEqual(job.status, "interrupted")
        self.assertTrue(job.done.is_set())
        self.assertEqual(d.active_jobs("developer"), {})
        out = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 0}))
        self.assertEqual(out["status"], "interrupted")
        self.assertIn("factory logs developer", out["report"])
        self.assertNotIn("still running", json.dumps(out))
        self.assertIn("developer [idle]", d.tool_list_roles({}))
        self.assertEqual(self.index()["jobs"][0]["status"], "interrupted")
        self.assertIn(f"■ job {job_id} interrupted after", (d.STATE / "logs" / "developer.log").read_text())
        events = [json.loads(l) for l in (d.STATE / "audit.jsonl").read_text().splitlines()]
        self.assertIn(("job-interrupted", job_id), [(e.get("event"), e.get("job_id")) for e in events])
        new = json.loads(d.tool_wait_for_job({"job_id": self.delegate("developer", "again"), "timeout_seconds": 30}))
        self.assertEqual(new["status"], "done")

    def test_no_ghost_running_lane_when_the_index_is_gone(self):
        (d.STATE / "logs" / "developer.log").write_text(
            "09:00:00  ▶ job 20261021-090000-abcdef from the Coordinator\n09:05:00     > task\n")
        self.restart()
        lines = (d.STATE / "logs" / "developer.log").read_text().splitlines()
        self.assertRegex(lines[-1], r"^\d\d:\d\d:\d\d  ■ job 20261021-090000-abcdef interrupted after \d+s$")
        self.restart()  # closed once, not again
        self.assertEqual((d.STATE / "logs" / "developer.log").read_text().splitlines(), lines)

    def test_a_finished_transcript_is_left_alone(self):
        job_id = self.delegate("developer", "one")
        d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30})
        before = (d.STATE / "logs" / "developer.log").read_text()
        self.restart()
        self.assertEqual((d.STATE / "logs" / "developer.log").read_text(), before)

    def test_corrupt_missing_and_odd_files_are_skipped(self):
        path = d.STATE / "jobs.json"
        self.assertEqual(self.restart(), 0)  # missing: nothing to say
        for raw in (b"{not json", b"[]", b'{"version": 99, "jobs": []}', b'{"version": 1, "jobs": "x"}',
                    b"\xff\xfe\x00", b""):
            path.write_bytes(raw)
            with mock.patch.object(d, "log_line") as log:
                self.assertEqual(self.restart(), 0, raw)
            self.assertTrue(log.called, raw)
        self.assertEqual(d.tool_job_status({}), "No jobs yet.")

    def test_oversized_and_non_regular_files_are_refused(self):
        path = d.STATE / "jobs.json"
        with mock.patch.object(d, "MAX_INDEX_BYTES", 10):
            path.write_text(json.dumps({"version": 1, "jobs": []}))
            self.assertEqual(self.restart(), 0)
        path.unlink()
        os.mkfifo(path)  # must not hang on open or read
        self.assertEqual(self.restart(), 0)
        path.unlink()
        path.mkdir()
        self.assertEqual(self.restart(), 0)

    def test_a_symlinked_index_is_refused_and_not_written_through(self):
        job_id = self.delegate("developer", "one")
        d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30})
        target = d.STATE / "elsewhere.json"
        target.write_text("target before")
        (d.STATE / "jobs.json").unlink()
        (d.STATE / "jobs.json").symlink_to(target)
        with mock.patch.object(d, "log_line") as log:
            self.assertEqual(self.restart(), 0)
        self.assertTrue(log.called)
        job = d.Job("developer", "t", None, lane="developer")
        job.status = "done"
        job.done.set()
        d._jobs[job.id] = job
        d.save_job_index()  # replaces the link itself; never writes through it
        self.assertFalse((d.STATE / "jobs.json").is_symlink())
        self.assertEqual(target.read_text(), "target before")

    def test_bad_records_are_dropped_and_good_ones_kept(self):
        job_id = self.delegate("developer", "one")
        d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30})
        base = self.index()["jobs"][0]
        bad = [
            "text", 7, None, {},
            dict(base, id="../../etc/passwd"), dict(base, id=7),
            dict(base, id="20261021-090000-abcdef", role="Bad Role"),
            dict(base, id="20261021-090001-abcdef", lane="../x"),
            dict(base, id="20261021-090002-abcdef", status="exploded"),
            dict(base, id="20261021-090003-abcdef", started="soon"),
            dict(base, id="20261021-090004-abcdef", started=float("nan")),
            dict(base, id="20261021-090005-abcdef", started=True),
            dict(base),  # duplicate id
        ]
        odd = dict(base, id="20261021-090006-abcdef", started=base["started"] - 1, task=["x"], model=5,
                   effort=None, context_tokens="many", cost_usd="free", session_cost_usd=float("inf"),
                   branches=[1, {"repo": ".", "ref": 2, "commit": "c"}, {"repo": ".", "ref": "r", "commit": "c"}],
                   import_errors="oops", packet="/etc/passwd", report={"x": 1}, rw=True)
        (d.STATE / "jobs.json").write_text(json.dumps({"version": 1, "jobs": [base] + bad + [odd]}))
        d._jobs.clear()
        self.assertEqual(d.load_job_index(), 2)
        job = d._jobs["20261021-090006-abcdef"]
        self.assertEqual((job.task, job.model_used, job.effort, job.context_tokens), ("", None, "", None))
        self.assertIsNone(job.session_cost_usd)
        self.assertEqual(job.branches, [{"repo": ".", "ref": "r", "commit": "c"}])
        self.assertEqual(job.import_errors, [])
        self.assertIsNone(job.packet)
        self.assertEqual(job.result, "")
        self.assertNotIn("NaN", d.tool_job_status({}))

    def test_packet_path_comes_from_the_packet_folder_not_the_file(self):
        job_id = self.delegate("developer", "one")
        d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30})
        data = self.index()
        data["jobs"][0]["packet"] = "/etc/passwd"
        (d.STATE / "jobs.json").write_text(json.dumps(data))
        self.restart()
        self.assertEqual(d._jobs[job_id].packet, str(d.packet_path(job_id)))
        d.packet_path(job_id).unlink()
        self.restart()
        self.assertIsNone(d._jobs[job_id].packet)
        out = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 0}))
        self.assertNotIn("packet", out)

    def test_the_index_is_capped_and_job_status_takes_a_limit(self):
        for i in range(d.MAX_INDEXED_JOBS + 5):
            job = d.Job("developer", f"task {i}", None, lane="developer")
            job.id = f"20260101-000000-{i:06x}"
            job.started, job.finished, job.status = 1_700_000_000 + i, 1_700_000_001 + i, "done"
            job.done.set()
            d._jobs[job.id] = job
        d.save_job_index()
        self.assertEqual(len(self.index()["jobs"]), d.MAX_INDEXED_JOBS)
        self.assertEqual(self.restart(), d.MAX_INDEXED_JOBS)
        self.assertEqual(len(json.loads(d.tool_job_status({}))), 20)
        self.assertEqual(len(json.loads(d.tool_job_status({"limit": 200}))), d.MAX_INDEXED_JOBS)
        newest = json.loads(d.tool_job_status({"limit": 1}))[0]["task"]
        self.assertEqual(newest, f"task {d.MAX_INDEXED_JOBS + 4}")
        for bad in (0, 201, True):
            with self.assertRaises(d.Denied):
                d.tool_job_status({"limit": bad})

    def test_a_failed_index_write_does_not_fail_the_job(self):
        with mock.patch.object(d.os, "replace", side_effect=OSError("disk full")), \
                mock.patch.object(d, "log_line") as log:
            job_id = self.delegate("developer", "one")
            report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))
        self.assertEqual(report["status"], "done")
        self.assertTrue(log.called)

    def test_a_restored_job_keeps_branches_and_import_errors(self):
        job = d.Job("developer", "t", None, lane="developer", rw=True)
        job.status, job.finished = "done", job.started + 3
        job.branches = [{"repo": ".", "ref": "factory-developer/factory/x", "commit": "abc1234"}]
        job.import_errors = ["import error: boom"]
        job.done.set()
        d._jobs[job.id] = job
        d.save_job_index()
        self.restart()
        out = json.loads(d.tool_wait_for_job({"job_id": job.id, "timeout_seconds": 0}))
        self.assertEqual(out["branches"], job.branches)
        self.assertEqual(out["import_errors"], job.import_errors)

    def test_a_saved_session_is_still_resumed_after_a_restart(self):
        d.tool_wait_for_job({"job_id": self.delegate("developer", "one"), "timeout_seconds": 30})
        sid = (d.STATE / "sessions" / "developer").read_text().strip()
        self.restart()
        d.tool_wait_for_job({"job_id": self.delegate("developer", "two"), "timeout_seconds": 30})
        self.assertEqual(self.claude_calls()[-1][self.claude_calls()[-1].index("--resume") + 1], sid)


if __name__ == "__main__":
    unittest.main()
