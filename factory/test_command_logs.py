"""Tests for factory/commands/logs.py (factory logs). The audit formatting is compared with
the jq filter the bash command used, when jq is installed; following is tested with a stand-in
tail on PATH, so nothing blocks."""

import io
import json
import os
import shutil
import subprocess
import unittest

from factory.cli import COMMANDS, main
from factory.commands.logs import format_audit_line
from factory.fixtures import CommandTest

JQ_FILTER = ('[.ts[11:19], (.role // ""), (.tool // .event // ""), (.decision // .status // '
             '(if .tool then .event else null end) // ""), (.reason // "")] | map(select(. != "")) | join("  ")')

RECORDS = [
    {"ts": "2026-10-25T09:08:07.123Z", "role": "developer", "tool": "git_push", "decision": "allow", "event": "call"},
    {"ts": "2026-10-25T09:08:08Z", "role": "reviewer", "tool": "web_fetch", "event": "denied", "reason": "host not allowed"},
    {"ts": "2026-10-25T09:08:09Z", "event": "job-end", "status": "done"},
    {"ts": "2026-10-25T09:08:10Z", "event": "job-end", "role": "x", "lane": "x-lane2"},
    {"ts": "2026-10-25T09:08:11Z", "tool": "t", "event": "e", "status": "s", "reason": "r"},
    {"ts": "2026-10-25T09:08:12Z", "tool": "t", "decision": False, "status": None, "event": "e"},
    {"ts": "2026-10-25T09:08:13Z", "role": "", "tool": "", "reason": ""},
    {"ts": "short", "event": "e"},
    {"event": "no-time", "role": "r"},
    {"ts": "2026-10-25T09:08:14Z", "event": "e", "reason": 42},
    {},
]


class FormatTest(unittest.TestCase):
    def test_fields_are_dropped_when_empty_and_joined_by_two_spaces(self):
        self.assertEqual(format_audit_line(json.dumps(RECORDS[0])), "09:08:07  developer  git_push  allow")
        self.assertEqual(format_audit_line(json.dumps(RECORDS[1])),
                         "09:08:08  reviewer  web_fetch  denied  host not allowed")
        self.assertEqual(format_audit_line(json.dumps(RECORDS[2])), "09:08:09  job-end  done")
        self.assertEqual(format_audit_line(json.dumps(RECORDS[3])), "09:08:10  x  job-end")
        self.assertEqual(format_audit_line(json.dumps(RECORDS[4])), "09:08:11  t  s  r")

    def test_a_line_that_is_not_a_record_is_shown_as_it_is(self):
        self.assertEqual(format_audit_line("not json"), "not json")
        self.assertEqual(format_audit_line("[1]"), "[1]")

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_the_output_is_the_jq_filters(self):
        for rec in RECORDS:
            line = json.dumps(rec)
            expected = subprocess.run(["jq", "-r", JQ_FILTER], input=line.encode(), stdout=subprocess.PIPE,
                                      check=True).stdout.decode().rstrip("\n")
            self.assertEqual(format_audit_line(line), expected, line)


class LogsTest(CommandTest):
    def setUp(self):
        super().setUp()
        for name, lanes, extra in (("developer", "3", ""), ("reviewer", "", ""), ("researcher", "", 'OPTIONAL="1"\n')):
            self.write("repo/roles/%s/role.conf" % name, 'DESCRIPTION="d"\n%s%s' % (
                'LANES="%s"\n' % lanes if lanes else "", extra))
        self.tail_log = os.path.join(self.tmp, "tail-calls")
        self.tail_output = os.path.join(self.tmp, "tail-output")
        self.write("tail-output", "")
        self.tool("tail", 'echo "$*" >> "%s"\ncat "%s"\n' % (self.tail_log, self.tail_output))

    def logs(self, *args):
        rc = COMMANDS["logs"](self.ctx(), list(args))
        return rc, self.out.getvalue(), self.err.getvalue()

    def tail_calls(self):
        with open(self.tail_log) as f:
            return f.read().splitlines()

    def test_no_name_lists_the_enabled_roles_and_the_two_audit_logs(self):
        rc, out, err = self.logs()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "Usage: factory logs <name>   where <name> is one of:\n  developer reviewer broker dispatch\n")
        self.write("config/ai-code-factory/enabled-roles", "researcher\n")
        self.assertEqual(self.logs("")[1],
                         "Usage: factory logs <name>   where <name> is one of:\n  developer researcher reviewer broker dispatch\n")

    def test_the_aliases(self):
        self.assertIs(COMMANDS["log"], COMMANDS["logs"])

    def test_an_audit_log_is_tailed_30_lines_and_formatted(self):
        for name in ("broker", "dispatch"):
            path = self.write("state/ai-code-factory/%s/audit.jsonl" % name, "")
            self.write("tail-output", "\n".join(json.dumps(r) for r in RECORDS[:3]) + "\nnot json\n")
            rc, out, err = self.logs(name)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(out, "09:08:07  developer  git_push  allow\n09:08:08  reviewer  web_fetch  denied  host not allowed\n"
                                  "09:08:09  job-end  done\nnot json\n")
            self.assertEqual(self.tail_calls()[-1], "-n 30 -F " + path)

    def test_a_missing_audit_log_is_an_error(self):
        with self.assertRaises(Exception) as cm:
            self.logs("broker")
        self.assertEqual(str(cm.exception), "no broker log yet")

    def test_a_role_follows_all_its_lanes_and_creates_missing_transcripts(self):
        logs = os.path.join(self.state, "dispatch", "logs")
        self.write("state/ai-code-factory/dispatch/logs/developer-lane2.log", "kept\n")
        self.assertEqual(self.logs("developer")[0], 0)
        (argv, env), = self.execs
        self.assertEqual(argv[1:], ["-n", "100", "-F"] + [os.path.join(logs, n + ".log") for n in
                                                          ("developer", "developer-lane2", "developer-lane3")])
        self.assertEqual(os.path.basename(argv[0]), "tail")
        for n in ("developer", "developer-lane2", "developer-lane3"):
            self.assertTrue(os.path.isfile(os.path.join(logs, n + ".log")))
        with open(os.path.join(logs, "developer-lane2.log")) as f:
            self.assertEqual(f.read(), "kept\n")

    def test_one_lane_of_a_role(self):
        self.logs("developer-lane3")
        (argv, _), = self.execs
        self.assertEqual(argv[1:], ["-n", "100", "-F", os.path.join(self.state, "dispatch", "logs", "developer-lane3.log")])

    def test_a_single_lane_role_follows_one_file(self):
        self.logs("reviewer")
        self.assertEqual(len(self.execs[0][0]), 5)

    def test_names_that_are_not_a_role_or_one_of_its_lanes(self):
        for name in ("Developer", "d", "x" * 42, "../etc", "dev eloper", "a_b"):
            with self.assertRaises(Exception) as cm:
                self.logs(name)
            self.assertEqual(str(cm.exception), "unknown role '%s' (see: factory roles)" % name)
        for name in ("nobody", "developer-lane4", "developer-lane1", "developer-lane", "reviewer-lane2", "nobody-lane2",
                     "developer-lane2x"):
            with self.assertRaises(Exception) as cm:
                self.logs(name)
            self.assertEqual(str(cm.exception), "unknown role '%s' (see: factory roles; one lane: <role>-lane<N>, "
                                                "N up to its LANES)" % name)
        self.assertEqual(self.execs, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "dispatch", "logs")))

    def test_through_main_an_error_exits_1(self):
        err = io.StringIO()
        rc = main(["logs", "nobody"], root=self.root, environ=self.environ, stdout=io.StringIO(), stderr=err)
        self.assertEqual(rc, 1)
        self.assertTrue(err.getvalue().startswith("error: unknown role 'nobody'"))

    def test_a_missing_tail_is_reported(self):
        self.environ["PATH"] = os.path.join(self.tmp, "nowhere")
        with self.assertRaises(Exception) as cm:
            self.logs("reviewer")
        self.assertEqual(str(cm.exception), "tail not found")


if __name__ == "__main__":
    unittest.main()
