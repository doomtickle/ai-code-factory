"""Tests for scripts/watch.py (factory watch): the transcript parser, the tool
call condenser and the plain renderer. No curses, no terminal.

Run on the host with the system Python:
  python3 -m unittest discover -s scripts -p 'test_watch.py' -v
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

import watch as w

TRANSCRIPT = """\
00:09:07  ────────────────────────────────────────────────────────────
00:09:07  ▶ job 20261019-000907-67b08e from the Coordinator
00:09:07     > Docs consistency pass for the ai-code-factory repo.
00:09:07     >
00:09:07     > Goal: make sure the docs reflect the merged changes.
00:09:08     model: claude-sonnet-5
00:09:16  🔧 Bash: {"command": "git status && git log --oneline -10"}
00:09:16     ↳ On branch main Your branch is up to date with 'origin/main'. nothing to commit
00:09:46  🔧 Read: {"file_path": "/home/agent/lanes/developer/kits/coordinator/spec.yaml"}
00:09:50  🔧 mcp__mcp-gateway__git_push: {"repo": "doomtickle/x", "branch": "factory/docs-sync", "bundle": "docs-sync.bundle"}
00:09:51     ✗ DENIED by broker policy: repo doomtickle/x is not in the broker allowlist
00:10:00     Working through the guides now.
00:10:00     ```
00:13:00  ⎇ branch factory-developer/factory/docs-sync at dc10e58
00:13:01  📄 review packet: factory packet 20261019-000907-67b08e
00:13:01  ■ job 20261019-000907-67b08e done after 235s
"""


class ParseTests(unittest.TestCase):
    def test_kinds(self):
        kinds = [e.kind for e in map(w.parse_line, TRANSCRIPT.splitlines()) if e is not None]
        self.assertEqual(kinds, ["start", "task", "task", "task", "model", "tool", "result", "tool", "tool",
                                 "error", "text", "text", "branch", "packet", "end"])
        self.assertEqual(w.parse_line("00:00:00     >").text, "")
        self.assertIsNone(w.parse_line("garbage without a timestamp"))

    def test_condense_tool(self):
        self.assertEqual(w.condense_tool("Bash", json.dumps({"command": "git  status &&\n git log"})),
                         "$ git status && git log")
        self.assertEqual(w.condense_tool("Read", json.dumps({"file_path": "/home/agent/lanes/dev/a/b/c.py"})),
                         "read …/a/b/c.py")
        self.assertEqual(w.condense_tool("Edit", json.dumps({"file_path": "x/y.py"})), "edit x/y.py")
        self.assertEqual(w.condense_tool("mcp__mcp-gateway__npm_fetch", json.dumps({"path": "."})),
                         "broker npm_fetch(path=.)")
        self.assertEqual(w.condense_tool("ToolSearch", json.dumps({"query": "select:x"})), "tool search select:x")
        self.assertEqual(w.condense_tool("Agent", json.dumps({"description": "Push it", "prompt": "..."})),
                         "subagent: Push it")
        # The transcript cuts arguments at 140 characters, so the JSON may be broken.
        self.assertEqual(w.condense_tool("Bash", '{"command": "cd /x && python3 -m unittest disco…'),
                         "$ cd /x && python3 -m unittest disco…")
        self.assertEqual(w.condense_tool("Bash", '{"command": "echo \\"a\\" | head", "description": "Check th…'),
                         '$ echo "a" | head')
        self.assertEqual(w.condense_tool("Mystery", "{}"), "Mystery {}")
        self.assertEqual(w.condense_tool("mcp__mcp-gateway__github_create_pr", '{"repo": "o/r", "head": "factory/doc…'),
                         "broker github_create_pr(repo=o/r, head=factory/doc…)")


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "developer.log"
        self.path.write_text(TRANSCRIPT)
        self.lane = w.Lane("developer", "developer", self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_state_and_entries(self):
        self.lane.feed()
        job = self.lane.job
        self.assertEqual((job["id"], job["status"], job["seconds"], job["model"]),
                         ("20261019-000907-67b08e", "done", 235, "claude-sonnet-5"))
        self.assertEqual(job["task"], "Docs consistency pass for the ai-code-factory repo.")
        self.assertEqual(self.lane.last_packet, "20261019-000907-67b08e")
        kinds = [e.kind for e in self.lane.entries]
        self.assertNotIn("model", kinds)  # shown in the title instead
        self.assertEqual(kinds.count("text"), 1)  # the code fence line is dropped
        self.assertEqual(self.lane.elapsed(), 235)

    def test_incremental_reads_and_truncation(self):
        self.lane.feed()
        with open(self.path, "a") as f:
            f.write("00:20:00  ▶ job 20261019-002000-abcdef from the Coordinator\n00:20:01     > next\n")
        self.lane.feed()
        self.assertEqual(self.lane.job["status"], "running")
        self.assertEqual(self.lane.job["task"], "next")
        now = time.mktime(time.strptime("2026-10-19 00:21:30", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(self.lane.elapsed(now), 90)
        self.path.write_text("00:30:00  ▶ job 20261019-003000-ffffff from the Coordinator\n")
        self.lane.feed()  # the file shrank: start over
        self.assertEqual(self.lane.job["id"], "20261019-003000-ffffff")
        self.assertEqual(len(self.lane.entries), 1)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state, self.roles = root / "dispatch", root / "roles"
        (self.state / "logs").mkdir(parents=True)
        (self.state / "packets").mkdir()
        for role, lanes in (("developer", "2"), ("reviewer", "")):
            (self.roles / role).mkdir(parents=True)
            (self.roles / role / "role.conf").write_text(f'DESCRIPTION="x"\nLANES="{lanes}"\n')
        (self.state / "logs" / "developer.log").write_text(TRANSCRIPT)
        (self.state / "logs" / "developer-lane2.log").write_text("")
        (self.state / "logs" / "old-role.log").write_text("00:00:00  ▶ job 1 from the Coordinator\n")
        (self.state / "audit.jsonl").write_text(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": "job-end",
            "job_id": "20261019-000907-67b08e", "cost_usd": 1.3089, "status": "done"}) + "\n")
        (self.state / "packets" / "20261019-000907-67b08e.md").write_text("# developer job x: done\n\nbody\n")
        self.watch = w.Watch(self.state, self.roles, "myproj")
        self.watch.refresh()

    def tearDown(self):
        self.tmp.cleanup()

    def test_lanes_follow_roles_then_orphans(self):
        self.assertEqual([l.name for l in self.watch.lanes], ["developer", "developer-lane2", "reviewer", "old-role"])

    def test_layout(self):
        self.assertEqual(len(w.layout(6, 160, 40)), 6)
        self.assertEqual({x for _, x, _, _ in w.layout(6, 160, 40)}, {0, 80})  # 2 columns
        self.assertEqual({x for _, x, _, _ in w.layout(6, 80, 40)}, {0})  # 1 column
        self.assertEqual(w.layout(0, 80, 24), [])

    def at(self, clock):
        return time.mktime(time.strptime("2026-10-19 " + clock, "%Y-%m-%d %H:%M:%S"))

    def test_grid_frame_shows_active_lanes_and_a_status_strip(self):
        # 00:20: the developer's job ended 7 minutes ago (active); old-role is running.
        text = w.plain(w.render(self.watch, 130, 24, now=self.at("00:20:00")), 130)
        lines = text.splitlines()
        self.assertEqual(len(lines), 24)
        self.assertIn("factory watch  myproj", lines[0])
        self.assertIn("last 24h: 1 jobs $1.31", lines[0])
        self.assertIn("bridge down", lines[0])
        self.assertIn(" developer ─", text)
        self.assertIn("✓ done · 3:55 · sonnet-5 · $1.31", text)
        self.assertIn(" old-role ─", text)
        self.assertIn("● running · 20:00", text)
        self.assertIn(" developer-lane2 · idle", lines[-3])
        self.assertIn(" reviewer · idle", lines[-2])
        self.assertIn("a every lane", lines[-1])
        self.assertIn("1=developer", lines[-1])
        self.assertTrue(all(len(line) <= 130 for line in lines))

    def test_finished_lanes_leave_the_grid_after_ten_minutes(self):
        text = w.plain(w.render(self.watch, 100, 20, now=self.at("03:00:00")), 100)
        self.assertNotIn(" developer ─", text)
        self.assertIn(" developer ✓ done · 3:55 · sonnet-5 · $1.31 · at 00:13", text)
        self.assertIn(" old-role ─", text)  # still running: the only pane
        self.watch.all = True
        text = w.plain(w.render(self.watch, 130, 24, now=self.at("03:00:00")), 130)
        for name in ("developer", "developer-lane2", "reviewer", "old-role"):
            self.assertIn(f" {name} ─", text)
        self.assertIn("a active lanes only", text.splitlines()[-1])

    def test_nothing_active_shows_the_most_recent_lane(self):
        (self.state / "logs" / "old-role.log").write_text("")
        self.watch = w.Watch(self.state, self.roles, "myproj")
        self.watch.refresh()
        text = w.plain(w.render(self.watch, 100, 20, now=self.at("03:00:00")), 100)
        self.assertIn(" developer ─", text)
        self.assertIn(" reviewer · idle", text)

    def test_focus_frame_renders_the_transcript_for_reading(self):
        self.watch.focus = 0
        text = w.plain(w.render(self.watch, 100, 30), 100)
        self.assertIn("› Docs consistency pass", text)
        self.assertIn("$ git status && git log --oneline -10", text)
        self.assertIn("read …/kits/coordinator/spec.yaml", text)
        self.assertIn("broker git_push(repo=doomtickle/x", text)
        self.assertIn("✗ DENIED by broker policy", text)
        self.assertIn("⎇ factory-developer/factory/docs-sync at dc10e58", text)
        self.assertNotIn("```", text)
        self.assertIn("📄 factory packet 20261019-000907-67b08e", text)
        self.assertIn("■ done after 3:55", text)
        self.assertNotIn("model: claude", text)
        self.assertIn("focus   j/k scroll", text.splitlines()[-1])

    def test_packet_frame(self):
        self.watch.focus, self.watch.packet = 0, True
        text = w.plain(w.render(self.watch, 80, 12), 80)
        self.assertIn("review packet · developer", text)
        self.assertIn("# developer job x: done", text)
        self.assertIn("body", text)
        self.watch.focus = 1  # a lane with no job
        self.assertIn("(no job on this lane yet)", w.plain(w.render(self.watch, 80, 12), 80))

    def test_once_main(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = w.main(["--state", str(self.state), "--roles", str(self.roles), "--project", "p",
                         "--once", "--width", "100", "--height", "20"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.getvalue().rstrip("\n").split("\n")), 20)


if __name__ == "__main__":
    unittest.main()
