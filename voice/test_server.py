"""HTTP tests for voice/server.py with a fake pipeline and a fake Whisper model, so neither torch,
kokoro nor faster_whisper is needed.
Requests go over a raw socket, to send exactly the headers under test."""

import array
import contextlib
import io
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

VOICES = ["af_kore", "af_nicole", "bm_daniel", "bm_george", "bf_emma"]
PACKS = {"af_nicole": 1.0, "af_kore": 2.0, "bm_daniel": 4.0, "bm_george": 8.0, "bf_emma": 16.0}
GOOD = {"text": "One two. Three four!", "mix": [["af_nicole", 1]], "speed": 1, "lang": "a"}
L16 = "audio/L16; rate=16000; channels=1"


class FakeAudio:
    """Enough of a tensor for pcm_bytes."""

    def __init__(self, n):
        self.n = n

    def cpu(self):
        return self

    def numpy(self):
        return self

    def astype(self, kind):
        assert kind == "<f4"
        return self

    def tobytes(self):
        return array.array("f", [0.25] * self.n).tobytes()


class FakeDurations:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class FakeResult:
    def __init__(self, text):
        self.phonemes = "".join(c for c in text if c.isalpha())
        self.pred_dur = FakeDurations([1] + [2] * len(self.phonemes) + [1])
        self.audio = FakeAudio(sum(self.pred_dur.values) * 600)


class FakeModel:
    vocab = set("abcdefghijklmnopqrstuvwxyzOT")


class FakePipe:
    """Stands in for KPipeline: one result per sentence, split with the pattern it is given."""

    def __init__(self, record):
        self.model = FakeModel()
        self.record = record

    def load_voice(self, path):
        self.record["loaded"].append(path)
        return PACKS[os.path.basename(path)[:-3]]

    def __call__(self, text, voice=None, speed=None, split_pattern=None):
        self.record["calls"].append({"text": text, "voice": voice, "speed": speed})
        self.record["active"] += 1
        self.record["peak"] = max(self.record["peak"], self.record["active"])
        try:
            for sentence in re.split(split_pattern, text):
                if self.record.get("delay"):
                    time.sleep(self.record["delay"])
                if self.record.get("boom"):
                    raise RuntimeError("secret words " + text)
                yield FakeResult(sentence)
        finally:
            self.record["active"] -= 1


class FakeNumpy:
    """Enough of numpy for Listener.transcribe: samples as a list of floats."""

    float32 = "float32"

    class Samples(list):
        def astype(self, kind):
            return self

        def __truediv__(self, by):
            return FakeNumpy.Samples(x / by for x in self)

    @classmethod
    def frombuffer(cls, data, dtype):
        assert dtype == "<i2"
        return cls.Samples(x for (x,) in struct.iter_unpack("<h", data))


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeWhisper:
    """Stands in for WhisperModel: records what it was given, answers with fixed segments."""

    def __init__(self, record):
        self.record = record

    def transcribe(self, audio, **options):
        self.record["heard"].append({"audio": audio, "options": options})
        self.record["ears"] += 1
        self.record["ears_peak"] = max(self.record["ears_peak"], self.record["ears"])
        try:
            if self.record.get("delay"):
                time.sleep(self.record["delay"])
            if self.record.get("boom"):
                raise RuntimeError("secret words heard")
            return iter([FakeSegment(" Hello there."), FakeSegment("  "), FakeSegment(" Ship it. ")]), None
        finally:
            self.record["ears"] -= 1


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.record = {"loaded": [], "calls": [], "active": 0, "peak": 0, "heard": [], "ears": 0, "ears_peak": 0}
        pipes = {lang: FakePipe(self.record) for lang in "ab"}
        engine = server.Engine(pipes, VOICES, "cpu", "/opt/kokoro/voices")
        listener = server.Listener(FakeWhisper(self.record), "base.en", FakeNumpy)
        self.server = server.Server(("127.0.0.1", 0), engine, listener)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host = "127.0.0.1:%d" % self.port

    def raw(self, data):
        """Send bytes, read to the close, return (status, headers, raw body)."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(data)
            got = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                got += chunk
        head, _, body = got.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.lower()] = value.strip()
        return int(lines[0].split()[1]), headers, body

    def request(self, method="GET", path="/health", headers=None, body=None, host=None, no_host=False):
        lines = ["%s %s HTTP/1.1" % (method, path)]
        if not no_host:
            lines.append("Host: %s" % (host or self.host))
        lines.append("Connection: close")
        for name, value in (headers or []):
            lines.append("%s: %s" % (name, value))
        data = ("\r\n".join(lines) + "\r\n\r\n").encode() + (body or b"")
        return self.raw(data)

    def speak(self, payload=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(GOOD if payload is None else payload).encode()
        hs = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))] + (headers or [])
        return self.request("POST", "/speak", hs, body)

    def listen(self, pcm=b"\x00\x00" * 1600, kind=L16, headers=None):
        hs = [("Content-Type", kind), ("Content-Length", str(len(pcm)))] + (headers or [])
        return self.request("POST", "/listen", hs, pcm)

    @staticmethod
    def chunks(body):
        out = []
        while True:
            size, _, rest = body.partition(b"\r\n")
            n = int(size, 16)
            if n == 0:
                return out
            out.append(rest[:n])
            body = rest[n + 2:]

    @staticmethod
    def parse_frame(chunk):
        samples, lip_len = struct.unpack("<II", chunk[:8])
        lip = chunk[8:8 + lip_len]
        pcm = chunk[8 + lip_len:]
        return samples, lip, pcm

    # Host

    def test_good_hosts_pass(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            status, _, body = self.request(host=host)
            self.assertEqual(status, 200, host)
            self.assertEqual(json.loads(body), {"ok": True, "device": "cpu", "voices": sorted(VOICES),
                                                "listen": True, "listen_model": "base.en"})

    def test_bad_hosts_are_refused(self):
        for host in ("evil.example:%d" % self.port, "127.0.0.1", "localhost", "127.0.0.1:1", "0.0.0.0:%d" % self.port,
                     "LOCALHOST:%d" % self.port, "[::1]:%d" % self.port, "127.0.0.1:%d.evil.example" % self.port):
            self.assertEqual(self.request(host=host)[0], 403, host)
            self.assertEqual(self.request("POST", "/speak", host=host)[0], 403, host)
            self.assertEqual(self.request("OPTIONS", "/speak", host=host)[0], 403, host)
            self.assertEqual(self.request("POST", "/listen", [("Content-Type", L16)], host=host)[0], 403, host)
            self.assertEqual(self.request("OPTIONS", "/listen", host=host)[0], 403, host)

    def test_no_host_and_two_hosts_are_refused(self):
        self.assertEqual(self.request(no_host=True)[0], 403)
        self.assertEqual(self.request(headers=[("Host", "evil.example")])[0], 403)

    # Origin and CORS

    def test_a_loopback_origin_gets_the_cors_header_and_never_credentials(self):
        for origin in ("http://127.0.0.1:8874", "http://localhost:1", "http://127.0.0.1:65535"):
            for status, headers, _ in (self.request(headers=[("Origin", origin)]),
                                       self.speak(headers=[("Origin", origin)]),
                                       self.listen(headers=[("Origin", origin)])):
                self.assertEqual(status, 200)
                self.assertEqual(headers["access-control-allow-origin"], origin)
                self.assertNotIn("access-control-allow-credentials", headers)

    def test_no_origin_is_allowed_with_no_cors_header(self):
        status, headers, _ = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn("access-control-allow-origin", headers)

    def test_any_other_origin_is_refused(self):
        for origin in ("http://evil.example", "https://127.0.0.1:8874", "http://127.0.0.1", "null", "",
                       "http://localhost:123456", "http://127.0.0.1:80/", "http://127.0.0.1:80.evil.example",
                       "http://user@127.0.0.1:80", "http://127.0.0.2:80", "HTTP://localhost:80", "*"):
            for status, headers, _ in (self.request(headers=[("Origin", origin)]),
                                       self.speak(headers=[("Origin", origin)]),
                                       self.request("OPTIONS", "/speak", [("Origin", origin)]),
                                       self.listen(headers=[("Origin", origin)]),
                                       self.request("OPTIONS", "/listen", [("Origin", origin)])):
                self.assertEqual(status, 403, origin)
                self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(self.record["calls"], [])
        self.assertEqual(self.record["heard"], [])

    def test_two_origins_are_refused(self):
        self.assertEqual(self.request(headers=[("Origin", "http://localhost:1"), ("Origin", "http://evil.example")])[0], 403)

    def test_preflight(self):
        for path in ("/speak", "/listen"):
            status, headers, body = self.request("OPTIONS", path, [
                ("Origin", "http://127.0.0.1:8874"), ("Access-Control-Request-Method", "POST"),
                ("Access-Control-Request-Headers", "content-type")])
            self.assertEqual(status, 204, path)
            self.assertEqual(body, b"")
            self.assertEqual(headers["access-control-allow-origin"], "http://127.0.0.1:8874")
            self.assertEqual(headers["access-control-allow-methods"], "POST")
            self.assertEqual(headers["access-control-allow-headers"], "Content-Type")
            self.assertNotIn("access-control-allow-credentials", headers)
        self.assertEqual(self.request("OPTIONS", "/health", [("Origin", "http://127.0.0.1:8874")])[0], 404)

    def test_the_cors_header_does_not_outlive_its_request_on_a_kept_connection(self):
        request = ("GET /health HTTP/1.1\r\nHost: %s\r\n%s\r\n")
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall((request % (self.host, "Origin: http://localhost:1\r\n")).encode())
            first = s.recv(65536)
            s.sendall((request % (self.host, "Connection: close\r\n")).encode())
            second = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                second += chunk
        self.assertIn(b"Access-Control-Allow-Origin", first)
        self.assertNotIn(b"Access-Control-Allow-Origin", second)

    # Nothing else is served

    def test_no_static_files_and_no_other_paths(self):
        for path in ("/", "/index.html", "/server.py", "/voices", "/../etc/passwd", "/speak", "/listen"):
            self.assertEqual(self.request(path=path)[0], 404, path)
        self.assertEqual(self.request("POST", "/health", [("Content-Type", "application/json")])[0], 404)

    # Limits

    def test_the_body_limit(self):
        self.assertEqual(self.request("POST", "/speak", [("Content-Type", "application/json"), ("Content-Length", "8193")])[0], 413)
        pad = dict(GOOD, pad="x" * (server.MAX_BODY - len(json.dumps(GOOD)) - 11))
        body = json.dumps(pad).encode()
        self.assertEqual(len(body), server.MAX_BODY)
        self.assertEqual(self.speak(raw=body)[0], 200)

    def test_bad_content_lengths_are_refused_without_reading(self):
        for value in ("abc", "-1", "+5", "1_0", "0x10", "1e3", "9" * 30, ""):
            status = self.request("POST", "/speak", [("Content-Type", "application/json"), ("Content-Length", value)])[0]
            self.assertIn(status, (400, 411), repr(value))
        self.assertEqual(self.request("POST", "/speak", [("Content-Type", "application/json"), ("Content-Length", "0")])[0], 413)
        self.assertEqual(self.request("POST", "/speak", [("Content-Type", "application/json")])[0], 411)
        self.assertEqual(self.request("POST", "/speak", [("Content-Type", "application/json"), ("Content-Length", "5"), ("Content-Length", "6")], b"12345")[0], 400)
        self.assertEqual(self.request("POST", "/speak", [("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")], b"0\r\n\r\n")[0], 400)
        self.assertEqual(self.record["calls"], [])

    def test_a_body_shorter_than_promised_starts_nothing(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(("POST /speak HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{" % self.host).encode())
            s.shutdown(socket.SHUT_WR)
            self.assertEqual(s.recv(100), b"")
        self.assertEqual(self.record["calls"], [])

    def test_only_json(self):
        body = json.dumps(GOOD).encode()
        for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            status = self.request("POST", "/speak", [("Content-Type", kind), ("Content-Length", str(len(body)))], body)[0]
            self.assertEqual(status, 415, kind)
        self.assertEqual(self.request("POST", "/speak", [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))], body)[0], 200)

    def test_bad_requests(self):
        nan = b'{"text":"x","mix":[["af_nicole",NaN]]}'
        inf = b'{"text":"x","mix":[["af_nicole",Infinity]]}'
        bad_speed = b'{"text":"x","mix":[["af_nicole",1]],"speed":NaN}'
        for raw in (b"not json", b"[]", b"\xff", b"[" * 8000, b"{}", nan, inf, bad_speed):
            self.assertEqual(self.speak(raw=raw)[0], 400, raw[:30])
        for change in ({"text": ""}, {"text": "   "}, {"text": 5}, {"text": None}, {"speed": 0.49}, {"speed": 2.01},
                       {"speed": "fast"}, {"lang": "z"}, {"lang": ["a"]}, {"mix": []}, {"mix": "af_nicole"},
                       {"mix": [["af_nicole"]]}, {"mix": [["nobody", 1]]}, {"mix": [["../../etc/passwd", 1]]},
                       {"mix": [["af_nicole", -1]]}, {"mix": [["af_nicole", 0]]}, {"mix": [["af_nicole", "x"]]},
                       {"mix": [[["af_nicole"], 1]]},
                       {"mix": [["af_nicole", 1]] * 5}):
            self.assertEqual(self.speak(dict(GOOD, **change))[0], 400, change)
        self.assertEqual(self.record["calls"], [])

    def test_good_edges_pass(self):
        for change in ({"speed": 0.5}, {"speed": 2}, {"lang": "b"}, {"mix": [["af_nicole", 1]] * 4}):
            self.assertEqual(self.speak(dict(GOOD, **change))[0], 200, change)

    def test_text_is_cut_at_the_limit(self):
        self.assertEqual(self.speak(dict(GOOD, text="a" * (server.MAX_TEXT + 500)))[0], 200)
        self.assertEqual(len(self.record["calls"][-1]["text"]), server.MAX_TEXT)

    # Frames

    def test_one_chunk_per_sentence_in_the_frame_format(self):
        status, headers, body = self.speak()
        self.assertEqual(status, 200)
        self.assertEqual(headers["transfer-encoding"], "chunked")
        self.assertEqual(headers["content-type"], "application/octet-stream")
        chunks = self.chunks(body)
        self.assertEqual(len(chunks), 2)
        for chunk, phonemes in zip(chunks, ("Onetwo", "Threefour")):
            samples, lip, pcm = self.parse_frame(chunk)
            parsed = json.loads(lip)
            self.assertEqual(parsed["p"], "".join(c for c in phonemes if c in FakeModel.vocab))
            self.assertEqual(samples, sum(parsed["d"]) * 600)
            self.assertEqual(len(pcm), samples * 4)
            self.assertEqual(pcm[:4], struct.pack("<f", 0.25))

    def test_the_split_is_after_sentence_ends_only(self):
        self.speak(dict(GOOD, text="a.b c? d! e"))
        chunks = self.chunks(self.speak(dict(GOOD, text="a.b c? d! e"))[2])
        self.assertEqual(len(chunks), 3)

    def test_the_blend_is_normalised(self):
        self.speak(dict(GOOD, mix=[["af_nicole", 2], ["af_kore", 2], ["bm_daniel", 4]]))
        self.assertAlmostEqual(self.record["calls"][-1]["voice"], 1.0 * 0.25 + 2.0 * 0.25 + 4.0 * 0.5)

    def test_voices_load_from_their_file_once_each_through_their_own_pipeline(self):
        self.speak(dict(GOOD, mix=[["bm_daniel", 1]]))
        self.speak(dict(GOOD, mix=[["bm_daniel", 1]]))
        self.assertEqual(self.record["loaded"], ["/opt/kokoro/voices/bm_daniel.pt"])

    def test_speed_reaches_the_pipeline(self):
        self.speak(dict(GOOD, speed=1.3))
        self.assertEqual(self.record["calls"][-1]["speed"], 1.3)

    def test_unusable_lip_data_gives_an_empty_lip(self):
        result = FakeResult("abc")
        result.pred_dur = FakeDurations([1, 2])
        self.assertEqual(server.lips(result, FakeModel.vocab), b"")
        result.pred_dur = None
        self.assertEqual(server.lips(result, FakeModel.vocab), b"")
        self.assertEqual(server.frame(b"\0\0\0\0", b""), struct.pack("<II", 1, 0) + b"\0\0\0\0")

    # One at a time, failures, privacy

    def test_one_generation_at_a_time(self):
        self.record["delay"] = 0.02
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.speak()[0])) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(results, [200] * 4)
        self.assertEqual(self.record["peak"], 1)
        self.assertEqual(len(self.record["calls"]), 4)

    def test_busy_past_the_wait_is_a_503(self):
        self.server.voice_lock.acquire()
        self.addCleanup(self.server.voice_lock.release)
        original = server.WAIT_FOR_VOICE
        server.WAIT_FOR_VOICE = 0.05
        self.addCleanup(setattr, server, "WAIT_FOR_VOICE", original)
        self.assertEqual(self.speak()[0], 503)

    def test_a_failure_ends_the_stream_without_the_last_chunk_and_logs_no_text(self):
        self.record["boom"] = True
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status, _, body = self.speak()
        self.assertEqual(status, 200)
        self.assertNotIn(b"0\r\n\r\n", body)
        self.assertEqual(stderr.getvalue(), "speak failed: RuntimeError\n")
        self.record["boom"] = False
        self.assertEqual(self.speak()[0], 200)  # The voice was given back.

    def test_requests_log_nothing(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.speak(dict(GOOD, text="canary words."))
            self.request(host="evil.example:1")
            self.request("DELETE", "/speak")
        self.assertEqual(stderr.getvalue() + stdout.getvalue(), "")

    def test_cookies_are_ignored(self):
        status, headers, _ = self.speak(headers=[("Cookie", "token=abc")])
        self.assertEqual(status, 200)
        self.assertNotIn("set-cookie", headers)

    def test_importing_the_server_needs_no_torch(self):
        self.assertNotIn("torch", sys.modules)
        self.assertNotIn("kokoro", sys.modules)
        self.assertNotIn("faster_whisper", sys.modules)
        self.assertNotIn("ctranslate2", sys.modules)

    # Listen

    def test_listen_returns_the_text_and_the_time(self):
        status, headers, body = self.listen(struct.pack("<3h", 0, 16384, -32768))
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["cache-control"], "no-store")
        reply = json.loads(body)
        self.assertEqual(set(reply), {"text", "ms"})
        self.assertEqual(reply["text"], "Hello there. Ship it.")
        self.assertIsInstance(reply["ms"], int)
        self.assertGreaterEqual(reply["ms"], 0)
        heard = self.record["heard"][-1]
        self.assertEqual(heard["audio"], [0.0, 0.5, -1.0])  # int16 LE scaled to [-1, 1).
        self.assertEqual(heard["options"]["language"], "en")

    def test_listen_takes_only_16_khz_mono_l16(self):
        for kind in ("audio/L16; rate=16000; channels=1", "audio/l16;rate=16000;channels=1",
                     "AUDIO/L16; RATE=16000; CHANNELS=1", "audio/L16; rate=16000", 'audio/L16; rate="16000"; channels=1'):
            self.assertEqual(self.listen(kind=kind)[0], 200, kind)
        for kind in ("", "application/json", "application/octet-stream", "audio/wav", "audio/webm",
                     "audio/L16", "audio/L16; rate=48000; channels=1", "audio/L16; rate=16000; channels=2",
                     "audio/L16; rate=16000; channels=1; rate=16000", "audio/L16; rate=16000; channels=1; x=y",
                     "audio/L16; rate", "audio/L24; rate=16000; channels=1", "multipart/form-data"):
            self.assertEqual(self.listen(kind=kind)[0], 415, kind)
        self.assertEqual(self.request("POST", "/listen", [("Content-Length", "2")], b"\0\0")[0], 415)
        self.assertEqual(len(self.record["heard"]), 5)

    def test_listen_limits(self):
        self.assertEqual(server.MAX_AUDIO, 1920000)
        self.assertEqual(self.listen(b"\x01\x00" * (server.MAX_AUDIO // 2))[0], 200)
        self.assertEqual(len(self.record["heard"][-1]["audio"]), 60 * 16000)
        for n in (str(server.MAX_AUDIO + 1), str(server.MAX_AUDIO + 2), "9999999"):
            self.assertEqual(self.request("POST", "/listen", [("Content-Type", L16), ("Content-Length", n)])[0], 413, n)
        for value in ("abc", "-1", "9" * 30, "0x10"):
            self.assertIn(self.request("POST", "/listen", [("Content-Type", L16), ("Content-Length", value)])[0], (400, 411))
        self.assertEqual(self.request("POST", "/listen", [("Content-Type", L16), ("Content-Length", "0")])[0], 413)
        self.assertEqual(self.request("POST", "/listen", [("Content-Type", L16)])[0], 411)
        self.assertEqual(self.request("POST", "/listen", [("Content-Type", L16), ("Transfer-Encoding", "chunked")], b"0\r\n\r\n")[0], 400)
        self.assertEqual(self.listen(b"\x00\x00\x00")[0], 400)  # Half a sample.
        self.assertEqual(len(self.record["heard"]), 1)

    def test_the_speak_limit_does_not_apply_to_listen_nor_the_listen_limit_to_speak(self):
        self.assertEqual(self.listen(b"\x00" * (server.MAX_BODY + 2))[0], 200)
        self.assertEqual(self.request("POST", "/speak", [("Content-Type", "application/json"), ("Content-Length", str(server.MAX_BODY + 2))])[0], 413)

    def test_a_short_audio_body_starts_nothing(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(("POST /listen HTTP/1.1\r\nHost: %s\r\nContent-Type: %s\r\nContent-Length: 100\r\n\r\n\0\0" % (self.host, L16)).encode())
            s.shutdown(socket.SHUT_WR)
            self.assertEqual(s.recv(100), b"")
        self.assertEqual(self.record["heard"], [])

    def test_one_transcription_at_a_time(self):
        self.record["delay"] = 0.05
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.listen()[0])) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(results, [200] * 3)
        self.assertEqual(self.record["ears_peak"], 1)
        self.assertEqual(len(self.record["heard"]), 3)

    def test_listening_busy_past_the_wait_is_a_503(self):
        self.server.listen_lock.acquire()
        self.addCleanup(self.server.listen_lock.release)
        original = server.WAIT_FOR_EARS
        server.WAIT_FOR_EARS = 0.05
        self.addCleanup(setattr, server, "WAIT_FOR_EARS", original)
        self.assertEqual(self.listen()[0], 503)
        self.assertEqual(self.record["heard"], [])

    def test_only_a_few_listeners_wait_for_the_slot_and_the_rest_get_an_instant_503(self):
        self.server.listen_lock.acquire()
        released = []
        def release():
            if not released:
                released.append(1)
                self.server.listen_lock.release()
        self.addCleanup(release)
        results = []
        waiters = [threading.Thread(target=lambda: results.append(self.listen()[0]))
                   for _ in range(server.LISTEN_QUEUE + 1)]
        for t in waiters:
            t.start()
        deadline = time.monotonic() + 10
        while self.server.listen_places._value > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        self.assertEqual(self.listen()[0], 503)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.record["heard"], [])
        release()
        for t in waiters:
            t.join(20)
        self.assertEqual(results, [200] * (server.LISTEN_QUEUE + 1))
        self.assertEqual(self.listen()[0], 200)  # The places were given back.

    def test_listening_and_speaking_do_not_wait_for_each_other(self):
        with self.server.voice_lock:
            self.assertEqual(self.listen()[0], 200)
        with self.server.listen_lock:
            self.assertEqual(self.speak()[0], 200)

    def test_a_failed_transcription_is_a_500_that_logs_no_text_and_frees_the_slot(self):
        self.record["boom"] = True
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status, _, body = self.listen()
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)
        self.assertEqual(stderr.getvalue(), "listen failed: RuntimeError\n")
        self.record["boom"] = False
        self.assertEqual(self.listen()[0], 200)

    def test_listening_logs_nothing_and_keeps_nothing(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.listen()
            self.listen(kind="audio/wav")
            self.request("POST", "/listen", [("Content-Type", L16)], host="evil.example:1")
        self.assertEqual(stderr.getvalue() + stdout.getvalue(), "")
        listener = self.server.listener
        self.assertEqual(sorted(vars(listener)), ["model", "name", "np"])

    def test_without_a_model_listen_is_off(self):
        self.server.listener = None
        status, _, body = self.request()
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertIs(health["listen"], False)
        self.assertIsNone(health["listen_model"])
        self.assertEqual(self.listen()[0], 503)
        self.assertEqual(self.listen(kind="text/plain")[0], 415)
        self.assertEqual(self.speak()[0], 200)  # Speech does not need it.


class ParseTest(unittest.TestCase):
    def test_a_missing_espeak_stops_the_start(self):
        class G2P:
            fallback = None

        class Pipe:
            g2p = G2P()

        with self.assertRaises(RuntimeError):
            server.require_espeak({"a": Pipe()})
        G2P.fallback = object()
        server.require_espeak({"a": Pipe()})

    def test_audio_types(self):
        self.assertTrue(server.audio_type("audio/L16; rate=16000; channels=1"))
        self.assertFalse(server.audio_type("audio/L16; channels=1"))
        self.assertFalse(server.audio_type("audio/L16; rate=16000; channels=01"))

    def test_defaults(self):
        engine = server.Engine({"a": None, "b": None}, VOICES, "cpu", "/v")
        text, mix, speed, lang = server.parse_speak(b'{"text":" hi ","mix":[["af_kore",1]]}', engine)
        self.assertEqual((text, mix, speed, lang), ("hi", [("af_kore", 1.0)], 1.0, "a"))


if __name__ == "__main__":
    unittest.main()
