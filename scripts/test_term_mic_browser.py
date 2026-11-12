"""Talking to LOIS in Terminal 0's field frame (term-mic.js) in a real browser: headless Chrome with its fake microphone playing
a WAV this test writes (short tone bursts that sound like phrases, with pauses), driven by test_term_mic_browser.js. The
dashboard runs here with a fake Coordinator (test_dashboard_term's), and a stub stands in for the local voice service (its
/health says listen: true; POST /listen answers the words the node side queued for the next utterance, else nothing) on a free
port the frame is pointed at through dashboard_term.VOICE_PORT. Checks: a hold of the mic key sends one 16 kHz 16-bit body
and types its words into the Coordinator's terminal; always on sends a "LOIS ..." line after the 2 s window, never a line
without the wake word, nothing after Escape or a click, and nothing while LOIS speaks; the pane neither grows nor takes focus
for any of it; a refused microphone sends nothing. Skipped when node or chrome-headless-shell is missing.

  python3 -m unittest discover -s scripts -p 'test_term_mic_browser.py' -v
"""

import json
import math
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import dashboard_term as dt
from test_dashboard import TOKEN
from test_dashboard_term import Server

SCRIPTS = Path(__file__).resolve().parent
TYPE = "audio/L16; rate=16000; channels=1"


class Voice:
    """The local voice's listening half, as the factory/voice-listen branch defines it, plus /__next for the test's driver."""

    def __init__(self):
        self.posts, self.next = [], []
        voice = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def cors(self):
                origin = self.headers.get("Origin")
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")

            def reply(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.cors()
                self.send_header("Access-Control-Allow-Methods", "POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_GET(self):
                if self.path == "/health":
                    return self.reply(200, {"ok": True, "listen": True, "listen_model": "base.en"})
                if self.path == "/__posts":   # the driver's view (node, no Origin): how many bodies have come
                    return self.reply(200, {"n": len([p for p in voice.posts if p["path"] == "/listen"])})
                self.reply(404, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/__next":   # the driver queues the words for the next utterance
                    voice.next.append(json.loads(body)["text"])
                    return self.reply(200, {})
                voice.posts.append({"path": self.path, "type": self.headers.get("Content-Type"), "len": len(body),
                                    "cookie": self.headers.get("Cookie"), "origin": self.headers.get("Origin")})
                if self.path != "/listen":
                    return self.reply(404, {})
                if self.headers.get("Content-Type") != TYPE:
                    return self.reply(415, {})
                if not body or len(body) > 60 * 32000:
                    return self.reply(413, {})
                if len(body) % 2:
                    return self.reply(400, {})
                self.reply(200, {"text": " " + (voice.next.pop(0) if voice.next else "") + "\n", "ms": 5})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def phrases(path):
    """A loop Chrome plays as the microphone: 1 s of a 180 Hz tone with a 4 Hz syllable swell, then 1.5 s of near silence."""
    rate, out = 48000, bytearray()
    for i in range(int(2.5 * rate)):
        t = i / rate
        a = 0.3 * (0.55 + 0.45 * math.sin(2 * math.pi * 4 * t)) * math.sin(2 * math.pi * 180 * t) if t < 1.0 else 0.0
        out += struct.pack("<h", int(32767 * a))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(out))


@unittest.skipUnless(shutil.which("node") and shutil.which("chrome-headless-shell"), "needs node and chrome-headless-shell")
class TermMicBrowserTests(unittest.TestCase):
    def test_push_to_talk_always_on_with_the_wake_word_the_window_and_the_hush(self):
        voice = Voice()
        self.addCleanup(voice.close)
        patch = mock.patch.object(dt, "VOICE_PORT", voice.port)
        patch.start()
        self.addCleanup(patch.stop)
        srv = Server()
        self.addCleanup(srv.close)
        srv.term.power_on()
        self.assertTrue(dt.wait_for(lambda: b"READY" in bytes(srv.term.buffer)))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wav = Path(tmp.name) / "phrases.wav"
        phrases(wav)
        url = f"http://127.0.0.1:{srv.port}/?token={TOKEN}"
        run = subprocess.run(["node", str(SCRIPTS / "test_term_mic_browser.js"), url, str(voice.port), str(wav)],
                             capture_output=True, text=True, timeout=420, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        held = json.loads(run.stdout.strip().splitlines()[-1])["held"]
        listens = [p for p in voice.posts if p["path"] == "/listen"]
        self.assertEqual([p for p in voice.posts if p["path"] != "/listen"], [], "nothing but /listen is posted")
        # Every body: 16-bit samples at 16 kHz, no cookie, from the terminal's own origin, at most 30 s for an utterance.
        for post in listens:
            self.assertEqual(post["type"], TYPE)
            self.assertIsNone(post["cookie"])
            self.assertEqual(post["origin"], f"http://127.0.0.1:{srv.tport}")
            self.assertEqual(post["len"] % 2, 0)
            self.assertLessEqual(post["len"], 2 * 16000 * 30)
        # The first is the hold: no shorter than MIN, no longer than the hold at 16 kHz (not the device's 44.1 or 48).
        self.assertGreaterEqual(listens[0]["len"], 2 * 16000 * 0.3)
        self.assertLessEqual(listens[0]["len"], 2 * 16000 * (held + 0.5), held)
        # Always-on utterances are a phrase with its pre-roll and tail: about 1.5 s, never the pause alone.
        for post in listens[1:]:
            self.assertGreater(post["len"], 2 * 16000 * 0.6, post)
            self.assertLess(post["len"], 2 * 16000 * 2.2, post)
        out = bytes(srv.term.buffer).decode("utf-8", "replace")
        self.assertIn("ECHO:start the review of the auth branch", out, out[-600:])   # held, sent at once
        self.assertIn("ECHO:run the tests.", out, out[-600:])                        # "Lois, run the tests." after the window
        for never in ("nice weather", "drop this one", "click me away", "while she speaks", "Lois", "Lowis"):
            self.assertNotIn(never, out, never)


if __name__ == "__main__":
    unittest.main()
