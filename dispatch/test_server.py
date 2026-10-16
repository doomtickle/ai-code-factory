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
        print("factory-developer\nfactory-reviewer\nfactory-opuser\nfactory-tiered")
        sys.exit(0)
    if args[0] == "exec" and "claude" in args:
        task = args[args.index("-p") + 1]
        resume = args[args.index("--resume") + 1] if "--resume" in args else None
        if resume == "gone":
            print("No conversation found with session ID: gone", file=sys.stderr)
            sys.exit(1)
        if task == "hang":
            time.sleep(60)
        sid = resume or "sess-" + str(os.getpid())
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


def conf(desc, workspace, tools, model="", models=""):
    return (f'DESCRIPTION="{desc}"\nTEMPLATE="developer"\nWORKSPACE="{workspace}"\n'
            f'BROKER_TOOLS="{tools}"\nMODEL="{model}"\nMODELS="{models}"\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        d.ROOT, d.PROJECT, d.STATE = root / "repo", root / "project", root / "state"
        for p in (d.ROOT / "roles", d.PROJECT, d.STATE / "logs", d.STATE / "sessions",
                  d.STATE / "instructions"):
            p.mkdir(parents=True)
        for name in ("developer", "reviewer", "tester", "opuser", "tiered"):
            (d.STATE / "instructions" / f"{name}.md").write_text(f"# {name}\nDo {name} work.\n")
        for name, text in {"developer": conf("Builds things", "rw", "web_fetch npm_ci"),
                           "reviewer": conf("Reviews things", "ro", ""),
                           "tester": conf("Tests things", "rw", ""),
                           "opuser": conf("Uses a pinned model", "rw", "", model="opus"),
                           "tiered": conf("Picks a model per task", "rw", "", model="sonnet",
                                          models="haiku opus bad;name"),
                           "Bad_Name": conf("x", "rw", ""),
                           "coordinator": conf("Reserved name", "rw", "")}.items():
            (d.ROOT / "roles" / name).mkdir()
            (d.ROOT / "roles" / name / "role.conf").write_text(text)
        fake = root / "sbx"
        fake.write_text(FAKE_SBX)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        d.SBX = str(fake)
        self.sbx_log = root / "sbx-calls.jsonl"
        os.environ["FAKE_SBX_LOG"] = str(self.sbx_log)
        d._jobs.clear()
        d._seen_sandboxes.clear()

    def tearDown(self):
        for job in list(d._jobs.values()):
            if job.status == "running":
                d.tool_cancel_job({"job_id": job.id})
        self.tmp.cleanup()

    def sbx_calls(self):
        return [json.loads(l) for l in self.sbx_log.read_text().splitlines()]

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
        self.assertEqual(sorted(roles), ["developer", "opuser", "reviewer", "tester", "tiered"])
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
        exec_call = [c for c in self.sbx_calls() if c[0] == "exec"][0]
        self.assertEqual(exec_call[:7], ["exec", "-w", str(d.PROJECT), "-e", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
                                         "factory-developer", "claude"])
        self.assertEqual(exec_call[exec_call.index("-p") + 1], "add a feature")
        self.assertNotIn("--resume", exec_call)
        self.assertNotIn("--model", exec_call)  # developer leaves MODEL unset
        prompt = exec_call[exec_call.index("--append-system-prompt") + 1]
        self.assertIn("You are the developer", prompt)
        self.assertTrue(prompt.endswith("# developer\nDo developer work."))
        self.assertTrue((d.STATE / "sessions" / "developer").read_text().startswith("sess-"))
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
        exec_call = [c for c in self.sbx_calls() if c[0] == "exec"][0]
        self.assertEqual(exec_call[exec_call.index("--model") + 1], "opus")

    def exec_models(self):
        return [c[c.index("--model") + 1] if "--model" in c else None
                for c in self.sbx_calls() if c[0] == "exec"]

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
        execs = [c for c in self.sbx_calls() if c[0] == "exec"]
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
        exec_call = [c for c in self.sbx_calls() if c[0] == "exec"][0]
        self.assertEqual(exec_call[exec_call.index("--effort") + 1], "low")
        d.tool_wait_for_job({"job_id": self.delegate("developer", "y"), "timeout_seconds": 30})
        self.assertNotIn("--effort", [c for c in self.sbx_calls() if c[0] == "exec"][1])

    def test_role_default_effort_and_bad_effort(self):
        conf_path = d.ROOT / "roles" / "opuser" / "role.conf"
        conf_path.write_text(conf_path.read_text() + 'EFFORT="high"\n')
        d.tool_wait_for_job({"job_id": self.delegate("opuser", "x"), "timeout_seconds": 30})
        exec_call = [c for c in self.sbx_calls() if c[0] == "exec"][0]
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
        execs = [c for c in self.sbx_calls() if c[0] == "exec"]
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
        self.assertNotIn("--resume", [c for c in self.sbx_calls() if c[0] == "exec"][1])

    def test_session_under_the_default_cap_or_uncapped_resumes(self):
        self.assertEqual(d.load_roles()["developer"]["max_session_tokens"], d.DEFAULT_MAX_SESSION_TOKENS)
        self.set_cap(0)
        self.assertEqual(d.load_roles()["developer"]["max_session_tokens"], 0)
        d.tool_wait_for_job({"job_id": self.delegate("developer", "one"), "timeout_seconds": 30})
        out = json.loads(d.tool_delegate({"role": "developer", "task": "two", "wait_seconds": 30}))
        self.assertEqual(out["session"], "continuing its previous session")
        self.assertIn("--resume", [c for c in self.sbx_calls() if c[0] == "exec"][1])

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
        execs = [c for c in self.sbx_calls() if c[0] == "exec"]
        self.assertIn("--resume", execs[0])
        self.assertNotIn("--resume", execs[1])

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
                         {"list_roles", "delegate", "wait_for_job", "job_status", "cancel_job"})

    def test_initialize_briefs_the_coordinator_with_live_roles(self):
        res = d.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        text = res["result"]["instructions"]
        self.assertIn("Coordinator of the AI Code Factory", text)
        self.assertIn("- developer (edits the project): Builds things", text)
        self.assertIn("- reviewer (project read-only): Reviews things", text)
        self.assertNotIn("Bad_Name", text)
        self.assertNotIn("Reserved name", text)
        # Picks up a role added after the server started.
        (d.ROOT / "roles" / "designer").mkdir()
        (d.ROOT / "roles" / "designer" / "role.conf").write_text(conf("Designs things", "ro", ""))
        res = d.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        self.assertIn("- designer (project read-only): Designs things", res["result"]["instructions"])


if __name__ == "__main__":
    unittest.main()
