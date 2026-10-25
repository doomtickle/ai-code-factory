"""Context.capture: when a program gets its own session (only with a timeout), and that an
interrupt or a timeout leaves no child running."""

import os
import signal
import sys
import time
import unittest
from unittest import mock

from factory import context


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class FakeProc:
    pid = 4242
    returncode = None
    stdout = stderr = None

    def __init__(self, communicate):
        self._communicate = communicate
        self.calls = []

    def communicate(self, timeout=None):
        self.calls.append(("communicate", timeout))
        return self._communicate(timeout)

    def terminate(self):
        self.calls.append(("terminate",))

    def kill(self):
        self.calls.append(("kill",))

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        return 0


class SessionTest(unittest.TestCase):
    def popen_kwargs(self, **kw):
        proc = FakeProc(lambda t: (b"", b""))
        proc.returncode = 0
        with mock.patch.object(context.subprocess, "Popen", return_value=proc) as popen:
            context._capture(["x"], {}, **kw)
        return popen.call_args.kwargs

    def test_no_timeout_stays_in_the_terminals_process_group(self):
        self.assertFalse(self.popen_kwargs()["start_new_session"])

    def test_a_timeout_starts_a_new_session(self):
        self.assertTrue(self.popen_kwargs(timeout=10)["start_new_session"])


class InterruptTest(unittest.TestCase):
    def interrupted(self, **kw):
        def boom(timeout):
            raise KeyboardInterrupt
        proc = FakeProc(boom)
        with mock.patch.object(context.subprocess, "Popen", return_value=proc), \
                mock.patch.object(context.os, "killpg") as killpg:
            with self.assertRaises(KeyboardInterrupt):
                context._capture(["x"], {}, **kw)
        return proc, killpg

    def test_without_a_timeout_the_child_is_terminated_and_waited_for(self):
        proc, killpg = self.interrupted()
        self.assertEqual(proc.calls, [("communicate", None), ("terminate",), ("wait", 5)])
        killpg.assert_not_called()

    def test_with_a_timeout_the_whole_group_is_killed_and_waited_for(self):
        proc, killpg = self.interrupted(timeout=10)
        killpg.assert_called_once_with(proc.pid, signal.SIGKILL)
        self.assertEqual(proc.calls, [("communicate", 10), ("wait", 5)])

    def test_an_interrupt_while_waiting_after_a_timeout_still_reaps_the_child(self):
        def communicate(timeout):
            if timeout == 10:
                raise context.subprocess.TimeoutExpired("x", 10)
            raise KeyboardInterrupt

        proc = FakeProc(communicate)
        with mock.patch.object(context.subprocess, "Popen", return_value=proc), \
                mock.patch.object(context.os, "killpg") as killpg:
            with self.assertRaises(KeyboardInterrupt):
                context._capture(["x"], {}, timeout=10)
        self.assertEqual(proc.calls, [("communicate", 10), ("communicate", 5), ("wait", 5)])
        self.assertEqual(killpg.call_count, 2)

    def test_a_child_that_ignores_terminate_is_killed(self):
        calls = []

        def wait(timeout=None):
            calls.append(("wait", timeout))
            if timeout:
                raise context.subprocess.TimeoutExpired("x", timeout)

        proc = FakeProc(lambda t: (_ for _ in ()).throw(KeyboardInterrupt()))
        proc.wait = wait
        proc.kill = lambda: calls.append(("kill",))
        proc.terminate = lambda: calls.append(("terminate",))
        with mock.patch.object(context.subprocess, "Popen", return_value=proc):
            with self.assertRaises(KeyboardInterrupt):
                context._capture(["x"], {})
        self.assertEqual(calls, [("terminate",), ("wait", 5), ("kill",), ("wait", None)])


class StopTest(unittest.TestCase):
    def test_a_group_that_is_already_gone_is_not_an_error(self):
        proc = FakeProc(None)
        with mock.patch.object(context.os, "killpg", side_effect=ProcessLookupError):
            context._stop(proc, True)
        self.assertEqual(proc.calls, [("wait", 5)])

    def test_a_child_that_is_already_gone_is_not_an_error(self):
        proc = FakeProc(None)
        proc.terminate = mock.Mock(side_effect=ProcessLookupError)
        context._stop(proc, False)
        proc.terminate.assert_called_once_with()
        self.assertEqual(proc.calls, [("wait", 5)])

    def test_a_kill_that_is_refused_is_not_an_error(self):
        proc = FakeProc(None)
        with mock.patch.object(context.os, "killpg", side_effect=PermissionError):
            context._stop(proc, True)
        self.assertEqual(proc.calls, [("wait", 5)])

    def test_the_pipes_are_closed(self):
        proc = FakeProc(None)
        proc.stdout, proc.stderr = mock.Mock(), mock.Mock()
        context._stop(proc, False)
        proc.stdout.close.assert_called_once_with()
        proc.stderr.close.assert_called_once_with()


class RealProcessTest(unittest.TestCase):
    def test_a_timeout_kills_the_group_and_returns_124(self):
        script = "import os,time; print(os.fork() or os.getpid(), flush=True); time.sleep(60)"
        start = time.time()
        got = context._capture([sys.executable, "-c", script], dict(os.environ), timeout=1)
        self.assertEqual(got.status, 124)
        self.assertLess(time.time() - start, 20)

    def test_an_interrupt_leaves_no_child_running(self):
        pids = []
        real_popen = context.subprocess.Popen

        def popen(*a, **kw):
            proc = real_popen(*a, **kw)
            pids.append(proc.pid)
            return proc

        def communicate(self, timeout=None):
            time.sleep(0.3)
            raise KeyboardInterrupt

        with mock.patch.object(context.subprocess, "Popen", popen), \
                mock.patch.object(real_popen, "communicate", communicate):
            with self.assertRaises(KeyboardInterrupt):
                context._capture([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ))
        self.assertFalse(alive(pids[0]))


if __name__ == "__main__":
    unittest.main()
