"""Tests for the dispatch bridge, using a fake `sbx` executable.

Run on the host with the system Python:
  /usr/bin/python3 -m unittest discover -s dispatch -v
"""

import json
import os
import stat
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path

import server as d

FAKE_SBX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys, time
    args = sys.argv[1:]
    log = os.environ["FAKE_SBX_LOG"]
    with open(log, "a") as f:
        f.write(json.dumps(args) + "\n")
    if args[:2] == ["ls", "-q"]:
        print("factory-developer\nfactory-reviewer\nfactory-opuser\nfactory-tiered\nfactory-builder")
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
        if task == "nan-cost":
            total = float("nan")  # json.dumps writes NaN, as a faulty claude might
        if task == "fail":
            events.append({"type": "result", "is_error": True, "result": "boom", "session_id": sid})
        else:
            events.append({"type": "result", "is_error": False, "result": "REPORT: did " + task,
                           "session_id": sid, "total_cost_usd": total})
        for e in events:
            print(json.dumps(e), flush=True)
        sys.exit(0)
    sys.exit(0)
''').lstrip()


def conf(desc, workspace, tools, model="", models="", lanes=""):
    return (f'DESCRIPTION="{desc}"\nTEMPLATE="developer"\nWORKSPACE="{workspace}"\n'
            f'BROKER_TOOLS="{tools}"\nMODEL="{model}"\nMODELS="{models}"\nLANES="{lanes}"\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        d.ROOT, d.PROJECT, d.STATE = root / "repo", root / "project", root / "state"
        for p in (d.ROOT / "roles", d.PROJECT / ".git", d.STATE / "logs", d.STATE / "sessions",
                  d.STATE / "instructions"):
            p.mkdir(parents=True)  # PROJECT/.git: a (stub) repo, so rw roles may run
        for name in ("developer", "reviewer", "tester", "opuser", "tiered", "builder"):
            (d.STATE / "instructions" / f"{name}.md").write_text(f"# {name}\nDo {name} work.\n")
        for name, text in {"developer": conf("Builds things", "rw", "web_fetch npm_ci"),
                           "reviewer": conf("Reviews things", "ro", ""),
                           "tester": conf("Tests things", "rw", ""),
                           "opuser": conf("Uses a pinned model", "rw", "", model="opus"),
                           "tiered": conf("Picks a model per task", "rw", "", model="sonnet",
                                          models="haiku opus bad;name"),
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
        # reserved (scripts/lib/roles.sh RESERVED_ROLE_NAMES) -- the Coordinator
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


class DelegateTests(Base):
    def test_delegate_runs_claude_in_the_role_sandbox(self):
        job_id = self.delegate("developer", "add a feature")
        report = json.loads(d.tool_wait_for_job({"job_id": job_id, "timeout_seconds": 30}))
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["report"], "REPORT: did add a feature")
        exec_call = self.claude_calls()[0]
        lane = "/home/agent/lanes/developer"
        self.assertEqual(exec_call[:13], ["exec", "-w", lane, "-e", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
                                          "-e", f"FACTORY_PROJECT={d.PROJECT}", "-e", "FACTORY_LANE=developer",
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
        self.assertEqual(d.load_roles()["tiered"]["models"], ["sonnet", "haiku", "opus"])
        self.assertEqual(d.load_roles()["developer"]["models"], [])
        self.assertIn("model: sonnet; can also run on: haiku, opus", d.tool_list_roles({}))

    def test_delegate_can_pick_an_allowed_model(self):
        out = json.loads(d.tool_delegate({"role": "tiered", "task": "rename", "model": "haiku",
                                          "wait_seconds": 30}))
        self.assertEqual(out["status"], "done")
        self.assertEqual(self.exec_models(), ["haiku"])
        meta = json.loads((d.STATE / "sessions" / "tiered.json").read_text())
        self.assertEqual(meta["model"], "haiku")

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
        out = json.loads(d.tool_delegate({"role": "developer", "task": "hang", "wait_seconds": 1}))
        self.assertEqual(out["status"], "running")
        self.assertIn("call wait_for_job again", out["note"])
        self.assertTrue(out["job_id"])

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
        real = d.render_event

        def crash_on_result(event, log, job):
            if event.get("type") == "result":
                raise RuntimeError("bridge crashed")
            real(event, log, job)

        d.render_event = crash_on_result
        try:
            self.assertEqual(self.wait(self.delegate("developer", "two"))["status"], "failed")
        finally:
            d.render_event = real
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
        self.assertEqual(call[:9], ["exec", "-w", str(d.PROJECT), "-e", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
                                    "-e", f"FACTORY_PROJECT={d.PROJECT}", "-e", "FACTORY_LANE=reviewer"])
        self.assertEqual(call[9], "factory-reviewer")
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
        report = self.wait(d.tool_delegate({"role": "builder", "task": "t", "lane": 2})
                           .split("job ", 1)[1].split(" ", 1)[0])
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
        self.assertNotIn("git_push", calls)  # outside the job's time window
        self.assertEqual(calls.count("\n") + 1, 1)  # the reviewer's call and the non-tool record are not listed
        self.assertIn("## Report\n\nREPORT: did build\n", text)
        self.assertNotIn("Verdict", text)
        self.assertFalse(self.marker.exists(), "a host hook or fsmonitor ran while writing the packet")
        self.assertIn(f"📄 review packet: factory packet {report['job_id']}",
                      (d.STATE / "logs" / "developer.log").read_text())
        end = [json.loads(l) for l in (d.STATE / "audit.jsonl").read_text().splitlines()
               if '"job-end"' in l][-1]
        self.assertEqual(end["packet"], report["packet"])

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


if __name__ == "__main__":
    unittest.main()
