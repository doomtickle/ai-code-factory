"""Tests for factory/commands/voice.py and factory/voice.py. No docker needed: a fake context
records every argv and answers as a scripted docker would; one test runs a stand-in docker for
real, through the real capture."""

import io
import os
import re
import unittest
from unittest import mock

from factory import voice
from factory.cli import COMMANDS, main
from factory.commands import voice as command
from factory.context import Captured, Context, FactoryError
from factory.fixtures import REPO, CommandTest

RUN_FLAGS = ["run", "-d", "--name", "factory-voice", "--restart", "no", "-p", "127.0.0.1:8873:8873",
             "--read-only", "--tmpfs", "/tmp:exec", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
NO_CONTAINER = Captured(1, "", "Error response from daemon: No such container: factory-voice\n")
NO_IMAGE = Captured(1, "[]\n", "Error response from daemon: No such image: factory-voice:1\n")


class FakeContext(Context):
    """capture and run record the argv and answer from the test's scripted docker."""

    def __init__(self, test, *args, **kw):
        super().__init__(*args, **kw)
        self.test = test

    def capture(self, argv, **kw):
        self.test.calls.append(list(argv))
        return self.test.answer(list(argv))


class VoiceCommandTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.environ["PATH"] = os.path.join(self.tmp, "bin")
        self.docker = self.tool("docker", "exit 99\n")
        self.calls = []
        self.state = None
        self.image = True
        self.nvidia_status = 0
        self.overrides = {}
        self.build_status = 0

    def answer(self, argv):
        if os.path.basename(argv[0]) == "nvidia-smi":
            return Captured(self.nvidia_status, "GPU 0\n", "")
        verb = argv[1]
        if verb in self.overrides:
            return self.overrides[verb]
        if verb == "container":
            return Captured(0, self.state + "\n", "") if self.state else NO_CONTAINER
        if verb == "image":
            return Captured(0, "sha256:abc\n", "") if self.image else NO_IMAGE
        return Captured(0, "id\n", "")

    def voice_ctx(self):
        self.out, self.err = io.StringIO(), io.StringIO()

        def runner(argv, env=None, **kw):
            self.calls.append(list(argv))
            return self.build_status

        return FakeContext(self, self.paths, self.environ, self.out, self.err, runner, self.execer)

    def cmd(self, *args):
        return command.run(self.voice_ctx(), list(args))

    def verbs(self):
        return [c[1:] for c in self.calls if c[0] == self.docker]

    def acting(self):
        return [v for v in self.verbs() if v[0] not in ("container", "image")]

    def test_start_runs_the_exact_argv_on_cpu_without_nvidia_smi(self):
        self.assertEqual(self.cmd("start"), 0)
        self.assertEqual(self.acting(), [RUN_FLAGS + ["factory-voice:1"]])
        self.assertIn("CPU", self.out.getvalue())

    def test_start_adds_gpus_only_when_nvidia_smi_works(self):
        self.tool("nvidia-smi", "exit 0\n")
        self.assertEqual(self.cmd("start"), 0)
        self.assertEqual(self.acting(), [RUN_FLAGS + ["--gpus", "all", "factory-voice:1"]])
        self.assertIn("GPU", self.out.getvalue())

    def test_a_gpu_docker_refuses_falls_back_to_cpu(self):
        self.tool("nvidia-smi", "exit 0\n")
        real = self.answer

        def refuses(argv):
            if "--gpus" in argv:
                return Captured(125, "", "could not select device driver")
            return real(argv)

        self.answer = refuses
        self.assertEqual(self.cmd("start"), 0)
        self.assertEqual(self.acting(), [RUN_FLAGS + ["--gpus", "all", "factory-voice:1"], ["rm", "factory-voice"], RUN_FLAGS + ["factory-voice:1"]])
        self.assertIn("CPU", self.out.getvalue())

    def test_a_failing_nvidia_smi_means_cpu(self):
        self.tool("nvidia-smi", "exit 9\n")
        self.nvidia_status = 9
        self.assertEqual(self.cmd("start"), 0)
        self.assertEqual(self.acting(), [RUN_FLAGS + ["factory-voice:1"]])

    def test_start_without_the_image_says_how_and_builds_nothing(self):
        self.image = False
        with self.assertRaises(FactoryError) as caught:
            self.cmd("start")
        self.assertIn("factory voice build", str(caught.exception))
        self.assertIn("several GB", str(caught.exception))
        self.assertEqual(self.acting(), [])

    def test_start_build_builds_then_runs(self):
        self.image = False
        self.assertEqual(self.cmd("start", "--build"), 0)
        folder = os.path.join(self.root, "voice")
        self.assertEqual(self.acting(), [["build", "-t", "factory-voice:1", "-f", os.path.join(folder, "Dockerfile"), folder],
                                         RUN_FLAGS + ["factory-voice:1"]])

    def test_a_failed_build_stops_there(self):
        self.image = False
        self.build_status = 1
        with self.assertRaises(FactoryError):
            self.cmd("start", "--build")
        self.assertEqual(len(self.acting()), 1)

    def test_start_when_running_does_nothing(self):
        self.state = "running"
        self.assertEqual(self.cmd("start"), 0)
        self.assertEqual(self.acting(), [])
        self.assertIn("already running", self.out.getvalue())

    def test_start_removes_a_stopped_leftover_by_exact_name(self):
        self.state = "exited"
        self.assertEqual(self.cmd("start"), 0)
        self.assertEqual(self.acting(), [["rm", "factory-voice"], RUN_FLAGS + ["factory-voice:1"]])

    def test_a_failed_docker_run_is_an_error_with_dockers_words(self):
        self.overrides["run"] = Captured(125, "", "port is already allocated\n")
        with self.assertRaises(FactoryError) as caught:
            self.cmd("start")
        self.assertIn("port is already allocated", str(caught.exception))

    def test_stop_stops_and_removes_that_one_container(self):
        self.state = "running"
        self.assertEqual(self.cmd("stop"), 0)
        self.assertEqual(self.acting(), [["stop", "factory-voice"], ["rm", "factory-voice"]])

    def test_stop_with_nothing_there_is_fine(self):
        self.assertEqual(self.cmd("stop"), 0)
        self.assertEqual(self.acting(), [])
        self.assertIn("not running", self.out.getvalue())

    def test_status_reports_the_device_from_health(self):
        self.state = "running"
        with mock.patch.object(voice, "health", return_value={"ok": True, "device": "cuda", "voices": ["a", "b"]}):
            self.assertEqual(self.cmd("status"), 0)
        self.assertIn("device cuda, 2 voices", self.out.getvalue())
        self.assertEqual(self.acting(), [])

    def test_status_while_the_model_loads(self):
        self.state = "running"
        with mock.patch.object(voice, "health", return_value=None):
            self.assertEqual(self.cmd("status"), 0)
        self.assertIn("not answering yet", self.out.getvalue())

    def test_status_when_off_mentions_a_missing_image(self):
        self.image = False
        self.assertEqual(self.cmd("status"), 0)
        self.assertIn("not running", self.out.getvalue())
        self.assertIn("factory voice build", self.out.getvalue())

    def test_build_passes_only_the_fixed_argv(self):
        self.assertEqual(self.cmd("build"), 0)
        folder = os.path.join(self.root, "voice")
        self.assertEqual(self.acting(), [["build", "-t", "factory-voice:1", "-f", os.path.join(folder, "Dockerfile"), folder]])

    def test_a_docker_that_does_not_answer_is_an_error(self):
        self.overrides["container"] = Captured(1, "", "Cannot connect to the Docker daemon\n")
        with self.assertRaises(FactoryError) as caught:
            self.cmd("status")
        self.assertIn("Cannot connect", str(caught.exception))

    def test_unknown_use_is_refused_and_runs_nothing(self):
        for args in ([], ["nope"], ["start", "--evil"], ["start", "factory-voice; rm -rf /"], ["stop", "other"],
                     ["status", "x"], ["build", "--no-cache"]):
            with self.assertRaises(FactoryError):
                self.cmd(*args)
        self.assertEqual(self.calls, [])

    def test_no_docker_is_a_clear_error_and_status_one(self):
        os.remove(self.docker)
        for sub in ("start", "stop", "status", "build"):
            self.voice_ctx()
            status = main(["voice", sub], root=self.root, environ=self.environ, stdout=self.out, stderr=self.err)
            self.assertEqual(status, 1, sub)
            self.assertIn("docker not found", self.err.getvalue())
        self.assertEqual(self.calls, [])

    def test_the_real_capture_runs_a_stand_in_docker_with_no_shell(self):
        # Quoted arguments arrive whole, so no shell split them. {{.State.Status}} would be
        # unchanged by a shell anyway; the spaces in the log line are the check.
        log = os.path.join(self.tmp, "argv.log")
        self.tool("docker", 'for a in "$@"; do echo "[$a]" >> "%s"; done\n'
                            'case "$1" in container) echo "No such container" >&2; exit 1 ;; esac\n' % log)
        self.assertEqual(main(["voice", "status"], root=self.root, environ=self.environ,
                              stdout=io.StringIO(), stderr=io.StringIO()), 0)
        with open(log) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[:5], ["[container]", "[inspect]", "[--format]", "[{{.State.Status}}]", "[factory-voice]"])


class ConstantsTest(unittest.TestCase):
    def test_names_are_plain_docker_names(self):
        self.assertTrue(re.fullmatch(r"[a-z][a-z0-9-]*", voice.CONTAINER))
        self.assertTrue(re.fullmatch(r"[a-z][a-z0-9-]*:[0-9]+", voice.IMAGE))
        self.assertTrue(1024 < voice.PORT < 65536)
        self.assertNotEqual(voice.PORT, 8880)  # The owner's prototype.

    def test_the_command_is_registered(self):
        self.assertIs(COMMANDS["voice"], command.run)

    def test_the_image_and_the_factory_agree_on_the_port(self):
        with open(os.path.join(REPO, "voice", "Dockerfile")) as f:
            dockerfile = f.read()
        self.assertIn("PORT=%d" % voice.PORT, dockerfile)
        self.assertIn("EXPOSE %d" % voice.PORT, dockerfile)
        with open(os.path.join(REPO, "voice", "server.py")) as f:
            self.assertIn('environ.get("PORT", "%d")' % voice.PORT, f.read())

    def test_no_shell_anywhere_in_the_command(self):
        for path in ("factory/commands/voice.py", "factory/voice.py"):
            with open(os.path.join(REPO, path)) as f:
                source = f.read()
            for banned in ("shell=True", "os.system", "os.popen", "subprocess"):
                self.assertNotIn(banned, source, path)

    def test_health_is_none_when_nothing_listens(self):
        self.assertIsNone(voice.health(port=1, timeout=0.2))


class PinsTest(unittest.TestCase):
    """The repo rule: everything in the image is pinned."""

    def read(self, name):
        with open(os.path.join(REPO, "voice", name)) as f:
            return f.read()

    def test_the_base_image_is_pinned_by_digest(self):
        froms = re.findall(r"^FROM (\S+)", self.read("Dockerfile"), re.M)
        self.assertEqual(len(froms), 1)
        self.assertRegex(froms[0], r"@sha256:[0-9a-f]{64}$")

    def test_every_download_has_a_checksum_and_a_fixed_revision(self):
        adds = [l for l in self.read("Dockerfile").splitlines() if l.startswith("ADD ")]
        self.assertEqual(len(adds), 30)  # config, model, 28 voices.
        for line in adds:
            self.assertRegex(line, r"^ADD --checksum=sha256:[0-9a-f]{64} --chmod=644 "
                                   r"https://huggingface\.co/hexgrad/Kokoro-82M/resolve/[0-9a-f]{40}/\S+ /opt/kokoro/\S+$")

    def test_runs_as_non_root_and_offline(self):
        dockerfile = self.read("Dockerfile")
        self.assertIn("USER 10001", dockerfile)
        self.assertIn("HF_HUB_OFFLINE=1", dockerfile)
        self.assertIn("--require-hashes", dockerfile)

    def test_every_requirement_is_exact_and_hashed(self):
        entries = re.sub(r"\s*\\\n\s*", " ", self.read("requirements.lock")).splitlines()
        entries = [e for e in entries if e and not e.startswith("#")]
        self.assertGreater(len(entries), 100)
        for entry in entries:
            self.assertRegex(entry, r"^\S+( @ https://\S+|==\S+)( --hash=sha256:[0-9a-f]{64})+$")


if __name__ == "__main__":
    unittest.main()
