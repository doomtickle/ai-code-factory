"""Tests for factory/commands/packet.py (factory packet)."""

import io
import os
import unittest

from factory.cli import COMMANDS, main
from factory.context import FactoryError
from factory.fixtures import CommandTest

NO_PACKETS = "no review packets yet (the dispatch bridge writes one per job to %s)"


class PacketTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.state, "dispatch", "packets")

    def packet(self, job_id, title, lane, body="body\n"):
        self.write("state/ai-code-factory/dispatch/packets/%s.md" % job_id,
                   "# %s\n\n- Lane: %s\n- Role: x\n\n%s" % (title, lane, body))

    def seed(self):
        self.packet("20261024-090000-aaaa", "Add widget", "developer")
        self.packet("20261025-100000-bbbb", "Fix gadget", "developer-lane2")
        self.packet("20261025-110000-cccc", "Review gadget", "reviewer")

    def run_packet(self, *args):
        rc = COMMANDS["packet"](self.ctx(), list(args))
        return rc, self.out.getvalue()

    def fails(self, *args):
        with self.assertRaises(FactoryError) as cm:
            self.run_packet(*args)
        return str(cm.exception)

    def read(self, job_id):
        with open(os.path.join(self.dir, job_id + ".md")) as f:
            return f.read()

    def test_the_aliases(self):
        self.assertIs(COMMANDS["packets"], COMMANDS["packet"])

    def test_no_packets_yet(self):
        self.assertEqual(self.fails(), NO_PACKETS % self.dir)
        os.makedirs(self.dir)
        self.assertEqual(self.fails("latest"), NO_PACKETS % self.dir)
        self.assertEqual(self.fails("--list"), NO_PACKETS % self.dir)

    def test_too_many_arguments(self):
        self.seed()
        self.assertEqual(self.fails("a", "b"), "usage: factory packet [<job-id> | latest | <lane> | --list]")

    def test_latest_is_the_default_and_the_newest(self):
        self.seed()
        want = self.read("20261025-110000-cccc")
        self.assertEqual(self.run_packet(), (0, want))
        self.assertEqual(self.run_packet("latest"), (0, want))
        self.assertEqual(self.run_packet(""), (0, want))

    def test_a_job_id(self):
        self.seed()
        self.assertEqual(self.run_packet("20261024-090000-aaaa"), (0, self.read("20261024-090000-aaaa")))

    def test_a_lane_is_its_newest_packet(self):
        self.seed()
        self.packet("20261025-120000-dddd", "Fix more", "developer")
        self.assertEqual(self.run_packet("developer"), (0, self.read("20261025-120000-dddd")))
        self.assertEqual(self.run_packet("developer-lane2"), (0, self.read("20261025-100000-bbbb")))

    def test_a_lane_needs_the_exact_line(self):
        self.packet("20261025-100000-bbbb", "Fix gadget", "developer-lane2")
        self.write("state/ai-code-factory/dispatch/packets/20261025-130000-eeee.md",
                   "# t\n\nsee - Lane: reviewer\n- Lane: reviewer-lane2\n  - Lane: reviewer\n")
        self.assertIn("no packet for job or lane 'reviewer'", self.fails("reviewer"))
        self.assertEqual(self.run_packet("reviewer-lane2")[0], 0)

    def test_unknown_job_or_lane(self):
        self.seed()
        self.assertEqual(self.fails("nobody"), "no packet for job or lane 'nobody' (see: factory packet --list)")

    def test_names_that_are_not_ids(self):
        self.seed()
        for bad in ("a b", "../x", "x" * 81, "a_b"):
            self.assertEqual(self.fails(bad), "not a job id or lane: %s" % bad)

    def test_list_shows_the_newest_20_with_titles(self):
        self.seed()
        want = ("20261025-110000-cccc  Review gadget\n20261025-100000-bbbb  Fix gadget\n"
                "20261024-090000-aaaa  Add widget\n")
        for flag in ("--list", "-l", "list"):
            self.assertEqual(self.run_packet(flag), (0, want))
        for i in range(25):
            self.packet("20260101-%06d-zz" % i, "T%d" % i, "x")
        lines = self.run_packet("--list")[1].splitlines()
        self.assertEqual(len(lines), 20)
        self.assertEqual(lines[0], "20261025-110000-cccc  Review gadget")
        self.assertEqual(lines[3], "20260101-000024-zz  T24")

    def test_a_packet_without_a_title_line_lists_an_empty_title(self):
        self.write("state/ai-code-factory/dispatch/packets/20261026-000000-x.md", "no heading\n#nospace\n")
        self.write("state/ai-code-factory/dispatch/packets/20261025-000000-y.md", "")
        self.assertEqual(self.run_packet("-l"), (0, "20261026-000000-x  \n20261025-000000-y  \n"))

    def test_only_markdown_files_are_packets(self):
        self.seed()
        self.write("state/ai-code-factory/dispatch/packets/zzz.txt", "stray\n")
        self.write("state/ai-code-factory/dispatch/packets/.hidden.md", "# h\n")
        self.assertEqual(self.run_packet("latest"), (0, self.read("20261025-110000-cccc")))
        self.assertEqual(self.run_packet("--list")[1].count("\n"), 3)

    def test_a_packet_is_printed_byte_for_byte(self):
        class Out(io.StringIO):
            buffer = io.BytesIO()

        raw = b"# t\n- Lane: x\ncaf\xc3\xa9 \xff no newline at end"
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "20261025-000000-a.md"), "wb") as f:
            f.write(raw)
        ctx = self.ctx()
        ctx.stdout = Out()
        COMMANDS["packet"](ctx, [])
        self.assertEqual(Out.buffer.getvalue(), raw)

    def test_through_main_an_error_exits_1(self):
        err = io.StringIO()
        rc = main(["packet"], root=self.root, environ=self.environ, stdout=io.StringIO(), stderr=err)
        self.assertEqual(rc, 1)
        self.assertEqual(err.getvalue(), "error: " + NO_PACKETS % self.dir + "\n")


if __name__ == "__main__":
    unittest.main()
