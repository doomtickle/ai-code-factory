"""Tests for factory/programs.py: run_tool and to_stderr, which keychain.py, secrets.py and the
other credential code run every program through. Each check runs a real tiny child
(sys.executable -c ...), never a mocked subprocess. A value goes only to a child's stdin, and a
lookup's output (the value) is kept and never printed: those two guarantees are asserted
explicitly (KeepTest, StdinTest).
"""

import io
import json
import os
import signal
import sys
import unittest
from unittest import mock

from factory import programs
from factory.fixtures import CommandTest
from factory.programs import DROP, KEEP, PASS, run_tool, to_stderr

SECRET = b"s3cr3t-value\xff\n"


def py(code, *args):
    return [sys.executable, "-c", code] + list(args)


WRITE_BOTH = "import sys; sys.stdout.write('OUT'); sys.stderr.write('ERR')"


class ProgramsTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.ctx_ = self.ctx()

    def run_(self, argv, **kw):
        return run_tool(self.ctx_, argv, **kw)

    def printed(self):
        return self.out.getvalue(), self.err.getvalue()


class StdinTest(ProgramsTest):
    ECHO = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"

    def test_no_data_means_the_child_reads_eof_at_once(self):
        # A child that would block on an inherited terminal or pipe returns instead.
        status, out, _ = self.run_(py("import sys; sys.stdout.write(repr(sys.stdin.buffer.read()))"), out=KEEP)
        self.assertEqual((status, out), (0, b"b''"))

    def test_no_data_gives_the_child_dev_null(self):
        code = "import os, sys; sys.stdout.write(str(os.path.samestat(os.fstat(0), os.stat(os.devnull))))"
        self.assertEqual(self.run_(py(code), out=KEEP)[1], b"True")

    def test_data_arrives_byte_for_byte(self):
        status, out, _ = self.run_(py(self.ECHO), data=SECRET, out=KEEP)
        self.assertEqual((status, out), (0, SECRET))

    def test_empty_data_is_an_empty_stdin(self):
        status, out, _ = self.run_(py(self.ECHO), data=b"", out=KEEP)
        self.assertEqual((status, out), (0, b""))

    def test_data_larger_than_a_pipe_buffer_is_delivered_whole(self):
        data = b"x" * 300000
        status, out, _ = self.run_(py("import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))"), data=data, out=KEEP)
        self.assertEqual((status, out), (0, b"300000"))

    def test_data_is_not_echoed_to_the_streams_or_the_argv(self):
        self.run_(py("pass"), data=SECRET)
        self.assertEqual(self.printed(), ("", ""))


class KeepTest(ProgramsTest):
    def test_kept_stdout_is_returned_and_never_printed(self):
        status, out, err = self.run_(py(WRITE_BOTH), out=KEEP, err=DROP)
        self.assertEqual((status, out, err), (0, b"OUT", b""))
        self.assertEqual(self.printed(), ("", ""))

    def test_kept_stderr_is_returned_and_never_printed(self):
        status, out, err = self.run_(py(WRITE_BOTH), out=DROP, err=KEEP)
        self.assertEqual((status, out, err), (0, b"", b"ERR"))
        self.assertEqual(self.printed(), ("", ""))

    def test_both_kept_are_returned_apart_and_nothing_is_printed(self):
        self.assertEqual(self.run_(py(WRITE_BOTH), out=KEEP, err=KEEP), (0, b"OUT", b"ERR"))
        self.assertEqual(self.printed(), ("", ""))

    def test_a_kept_value_is_returned_as_the_child_wrote_it(self):
        code = "import sys; sys.stdout.buffer.write(b'\\xff\\x00v\\n')"
        _, out, _ = self.run_(py(code), out=KEEP)
        self.assertEqual(out, b"\xff\x00v\n")
        self.assertEqual(self.printed(), ("", ""))

    def test_a_kept_value_is_not_printed_even_when_the_program_fails(self):
        status, out, _ = self.run_(py("import sys; sys.stdout.write('v'); sys.exit(3)"), out=KEEP)
        self.assertEqual((status, out), (3, b"v"))
        self.assertEqual(self.printed(), ("", ""))

    def test_a_missing_program_keeps_its_message_in_stderr_when_kept(self):
        self.environ["PATH"] = self.tmp
        status, out, err = self.run_(["no-such-tool"], err=KEEP)
        self.assertEqual((status, out, err), (127, b"", b"no-such-tool: command not found\n"))
        self.assertEqual(self.printed(), ("", ""))


class DropTest(ProgramsTest):
    def test_dropped_output_is_neither_returned_nor_printed(self):
        self.assertEqual(self.run_(py(WRITE_BOTH), out=DROP, err=DROP), (0, b"", b""))
        self.assertEqual(self.printed(), ("", ""))

    def test_dropped_stdout_does_not_drop_stderr(self):
        self.assertEqual(self.run_(py(WRITE_BOTH), out=DROP), (0, b"", b""))
        self.assertEqual(self.printed(), ("", "ERR"))

    def test_dropped_stderr_does_not_drop_stdout(self):
        self.assertEqual(self.run_(py(WRITE_BOTH), err=DROP), (0, b"", b""))
        self.assertEqual(self.printed(), ("OUT", ""))

    def test_a_dropped_missing_program_prints_nothing(self):
        self.environ["PATH"] = self.tmp
        self.assertEqual(self.run_(["no-such-tool"], err=DROP), (127, b"", b""))
        self.assertEqual(self.printed(), ("", ""))


class PassTest(ProgramsTest):
    def test_the_default_passes_both_streams_on_and_returns_nothing(self):
        self.assertEqual(self.run_(py(WRITE_BOTH)), (0, b"", b""))
        self.assertEqual(self.printed(), ("OUT", "ERR"))

    def test_the_explicit_pass_is_the_default(self):
        self.assertEqual(self.run_(py(WRITE_BOTH), out=PASS, err=PASS), (0, b"", b""))
        self.assertEqual(self.printed(), ("OUT", "ERR"))

    def test_stdout_passes_to_ctx_stdout_only(self):
        self.assertEqual(self.run_(py(WRITE_BOTH), err=DROP), (0, b"", b""))
        self.assertEqual(self.printed(), ("OUT", ""))

    def test_stderr_passes_to_ctx_stderr_only(self):
        self.assertEqual(self.run_(py(WRITE_BOTH), out=DROP), (0, b"", b""))
        self.assertEqual(self.printed(), ("", "ERR"))

    def test_one_stream_kept_and_the_other_passed(self):
        self.assertEqual(self.run_(py(WRITE_BOTH), out=KEEP), (0, b"OUT", b""))
        self.assertEqual(self.printed(), ("", "ERR"))
        self.assertEqual(self.run_(py(WRITE_BOTH), err=KEEP), (0, b"", b"ERR"))
        self.assertEqual(self.printed(), ("OUT", "ERR"))

    def test_passed_output_is_decoded_as_utf_8_with_replacement(self):
        code = "import sys; sys.stdout.buffer.write('é'.encode() + b'\\xff'); sys.stderr.buffer.write(b'\\xfe')"
        self.run_(py(code))
        self.assertEqual(self.printed(), ("é�", "�"))

    def test_a_missing_program_passes_its_message_to_stderr(self):
        self.environ["PATH"] = self.tmp
        self.assertEqual(self.run_(["no-such-tool"]), (127, b"", b""))
        self.assertEqual(self.printed(), ("", "no-such-tool: command not found\n"))

    def test_stderr_is_written_after_stdout_is_flushed(self):
        log = []

        class Rec(io.StringIO):
            def __init__(self, name):
                super().__init__()
                self.name = name

            def write(self, text):
                log.append((self.name, "write", text))
                return super().write(text)

            def flush(self):
                log.append((self.name, "flush"))

        self.ctx_.stdout, self.ctx_.stderr = Rec("out"), Rec("err")
        self.run_(py(WRITE_BOTH))
        self.assertEqual(log, [("out", "write", "OUT"), ("out", "flush"), ("err", "flush"), ("err", "write", "ERR")])


class ToStderrTest(ProgramsTest):
    def test_it_writes_the_text_on_stderr_only(self):
        to_stderr(self.ctx_, "warning\n")
        self.assertEqual(self.printed(), ("", "warning\n"))

    def test_it_flushes_the_context_before_writing(self):
        calls = []
        self.ctx_._flush = lambda: calls.append(("flush", self.err.getvalue()))
        to_stderr(self.ctx_, "x")
        self.assertEqual(calls, [("flush", "")])
        self.assertEqual(self.err.getvalue(), "x")

    def test_it_keeps_stderr_after_what_stdout_already_holds(self):
        order = []
        self.ctx_.stdout.flush = lambda: order.append("stdout flushed")
        self.ctx_.stderr.write = lambda text: order.append("stderr: " + text)
        to_stderr(self.ctx_, "late")
        self.assertEqual(order, ["stdout flushed", "stderr: late"])


class StatusTest(ProgramsTest):
    def test_zero_is_passed_through(self):
        self.assertEqual(self.run_(py("pass"))[0], 0)

    def test_a_non_zero_status_is_passed_through(self):
        for n in (1, 2, 42, 255):
            self.assertEqual(self.run_(py("import sys; sys.exit(%d)" % n))[0], n)

    def test_a_missing_program_is_127_with_its_name(self):
        self.environ["PATH"] = self.tmp
        self.assertEqual(self.run_(["no-such-tool", "arg"], err=KEEP), (127, b"", b"no-such-tool: command not found\n"))

    def test_a_missing_path_is_127_with_the_path_as_given(self):
        missing = os.path.join(self.tmp, "nope", "tool")
        self.assertEqual(self.run_([missing], err=KEEP), (127, b"", ("%s: command not found\n" % missing).encode()))

    def test_a_non_executable_file_is_126(self):
        path = self.write("plain-file", "#!/bin/sh\nexit 0\n", 0o644)
        status, out, err = self.run_([path], out=KEEP, err=KEEP)
        self.assertEqual((status, out), (126, b""))
        self.assertEqual(err, ("%s: Permission denied\n" % path).encode())

    def test_a_directory_is_126(self):
        status, _, err = self.run_([self.tmp], err=KEEP)
        self.assertEqual(status, 126)
        self.assertTrue(err.startswith(self.tmp.encode() + b": "), err)

    def test_a_126_message_is_passed_to_stderr_by_default(self):
        path = self.write("plain-file", "", 0o644)
        self.assertEqual(self.run_([path])[0], 126)
        self.assertEqual(self.printed(), ("", "%s: Permission denied\n" % path))

    def test_a_child_killed_by_signal_n_is_128_plus_n(self):
        for sig in (signal.SIGTERM, signal.SIGKILL, signal.SIGHUP):
            code = "import os, signal; os.kill(os.getpid(), signal.%s)" % sig.name
            self.assertEqual(self.run_(py(code))[0], 128 + sig, sig.name)

    def test_sigterm_is_143(self):
        self.assertEqual(self.run_(py("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"))[0], 143)

    def test_output_written_before_a_signal_is_still_returned(self):
        code = "import os, signal, sys; sys.stdout.write('v'); sys.stdout.flush(); os.kill(os.getpid(), signal.SIGTERM)"
        self.assertEqual(self.run_(py(code), out=KEEP), (143, b"v", b""))


class EnvironmentTest(ProgramsTest):
    DUMP = "import json, os, sys; json.dump(dict(os.environ), sys.stdout)"

    def child_env(self):
        status, out, _ = self.run_(py(self.DUMP), out=KEEP)
        self.assertEqual(status, 0)
        return json.loads(out.decode())

    def test_a_var_set_only_in_ctx_environ_is_visible(self):
        self.environ["ONLY_IN_CTX"] = "yes"
        self.assertNotIn("ONLY_IN_CTX", os.environ)
        self.assertEqual(self.child_env()["ONLY_IN_CTX"], "yes")

    def test_a_var_in_os_environ_but_not_in_ctx_environ_is_absent(self):
        with mock.patch.dict(os.environ, {"ONLY_IN_OS": "leak"}):
            self.assertNotIn("ONLY_IN_OS", self.environ)
            self.assertNotIn("ONLY_IN_OS", self.child_env())

    def test_the_child_environment_is_exactly_ctx_environ(self):
        self.environ["A"] = "1"
        got = self.child_env()
        # The interpreter may add LC_CTYPE when it coerces a C locale (PEP 538).
        if "LC_CTYPE" not in self.environ:
            got.pop("LC_CTYPE", None)
        self.assertEqual(got, dict(self.environ))

    def test_the_path_searched_for_a_bare_name_is_ctx_environ_path(self):
        self.tool("fake-lookup", "printf found\n")
        status, out, _ = self.run_(["fake-lookup"], out=KEEP)
        self.assertEqual((status, out), (0, b"found"))

    def test_a_tool_on_the_host_path_only_is_not_found(self):
        self.tool("fake-lookup", "printf found\n")
        bin_dir = os.path.join(self.tmp, "bin")
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")}):
            self.environ["PATH"] = os.path.join(self.tmp, "empty")
            self.assertEqual(self.run_(["fake-lookup"], err=DROP)[0], 127)

    def test_changing_ctx_environ_after_the_call_does_not_reach_the_child(self):
        self.environ["V"] = "before"
        self.assertEqual(self.child_env()["V"], "before")
        self.environ["V"] = "after"
        self.assertEqual(self.child_env()["V"], "after")


class ArgvTest(ProgramsTest):
    SHOW = "import json, sys; json.dump(sys.argv[1:], sys.stdout)"

    def args(self, *args):
        status, out, _ = self.run_(py(self.SHOW, *args), out=KEEP)
        self.assertEqual(status, 0)
        return json.loads(out.decode())

    def test_shell_syntax_in_an_argument_arrives_literally(self):
        marker = os.path.join(self.tmp, "ran")
        hostile = ["$HOME; echo x", "`touch %s`" % marker, "$(touch %s)" % marker, "a b", "*", "~", "> %s" % marker, ""]
        self.assertEqual(self.args(*hostile), hostile)
        self.assertFalse(os.path.exists(marker))

    def test_a_variable_reference_is_not_expanded(self):
        self.assertEqual(self.args("$HOME", "${PATH}"), ["$HOME", "${PATH}"])

    def test_argv_may_be_any_sequence_not_just_a_list(self):
        status, out, _ = self.run_(tuple(py("import sys; sys.stdout.write(sys.argv[1])", "t")), out=KEEP)
        self.assertEqual((status, out), (0, b"t"))

    def test_the_callers_argv_is_not_modified(self):
        argv = py("pass", "a")
        before = list(argv)
        self.run_(argv, data=b"x")
        self.assertEqual(argv, before)

    def test_a_shell_metacharacter_program_name_is_a_missing_program_not_a_command_line(self):
        self.environ["PATH"] = self.tmp
        status, _, err = self.run_(["echo hi; echo there"], err=KEEP)
        self.assertEqual((status, err), (127, b"echo hi; echo there: command not found\n"))
        self.assertEqual(self.printed(), ("", ""))

    def test_the_module_never_asks_subprocess_for_a_shell(self):
        with mock.patch.object(programs.subprocess, "run", wraps=programs.subprocess.run) as run:
            self.run_(py("pass"))
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertFalse(run.call_args.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
