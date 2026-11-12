#!/usr/bin/env python3
"""Local Kokoro speech and Whisper transcription for LOIS. Runs in its image, published on
loopback only.

POST /speak {text, mix: [[voice, weight], ...], speed, lang} streams one chunked frame per
sentence: uint32 sample count and uint32 lip byte count (both LE), the lip bytes (JSON, see
lips(); empty if unavailable), then float32 PCM at 24 kHz. POST /listen takes raw mono 16 kHz
signed 16-bit LE PCM (Content-Type audio/L16; rate=16000; channels=1, at most 60 s) and returns
{text, ms}. GET /health returns {ok, device, voices, listen, listen_model}. Nothing else is
served, cookies are ignored, and no text or audio is logged or kept.

torch, kokoro and faster_whisper are imported only in load_engine() and load_listener(), so the
HTTP side can be tested without them.
"""

import json
import math
import os
import re
import signal
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8873"))
BIND = os.environ.get("BIND", "127.0.0.1")  # The image sets 0.0.0.0; `docker run -p 127.0.0.1:...` keeps it local.
KOKORO_DIR = os.environ.get("KOKORO_DIR", "/opt/kokoro")
REPO = "hexgrad/Kokoro-82M"
WHISPER_DIR = os.environ.get("WHISPER_DIR", "/opt/whisper")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")

MAX_BODY = 8192
MAX_TEXT = 1200
MAX_VOICES = 4
SPLIT = r"(?<=[.!?])\s+"
LANGS = "ab"
PRELOAD = ("af_nicole", "af_kore", "bm_daniel", "bm_george")
VOICE_FILE = re.compile(r"([ab][fm]_\w+)\.pt")

LISTEN_RATE = 16000
MAX_AUDIO = 60 * LISTEN_RATE * 2  # 60 s of 16-bit mono: 1,920,000 bytes.

ORIGIN = re.compile(r"http://(127\.0\.0\.1|localhost):\d{1,5}", re.ASCII)
LENGTH = re.compile(r"[0-9]{1,7}")
SOCKET_TIMEOUT = 30  # A client that stops sending or reading is dropped, and so gives up the voice.
WAIT_FOR_VOICE = 30  # How long a request waits for the one generation slot before a 503.
WAIT_FOR_EARS = 30  # The same for the one transcription slot.


def say(text):
    """Status lines go to stderr. Never pass spoken text here."""
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def lips(result, vocab):
    """Kokoro's own timing for lip sync: p is the phonemes it spoke, d their durations in
    600-sample frames, with one extra entry at each end for the start and end tokens."""
    try:
        d = result.pred_dur.tolist()
        p = "".join(c for c in result.phonemes if c in vocab)
    except (AttributeError, TypeError):
        return b""
    if len(d) != len(p) + 2:
        return b""
    return json.dumps({"p": p, "d": d}, separators=(",", ":")).encode()


def frame(pcm, lip):
    """One wire frame from little-endian float32 PCM bytes and lip bytes."""
    return struct.pack("<II", len(pcm) // 4, len(lip)) + lip + pcm


def pcm_bytes(audio):
    array = audio.cpu().numpy() if hasattr(audio, "cpu") else audio
    return array.astype("<f4").tobytes()


class Engine:
    """Kokoro's pipelines (one per language, sharing one model) and the voice packs. A voice is
    named by its file in voice_dir, so nothing here can reach the Hub."""

    def __init__(self, pipes, voices, device, voice_dir):
        self.pipes = pipes
        self.voices = sorted(voices)
        self.device = device
        self.voice_dir = voice_dir
        self.packs = {}

    def pack(self, name):
        if name not in self.packs:
            self.packs[name] = self.pipes[name[0]].load_voice(os.path.join(self.voice_dir, name + ".pt"))
        return self.packs[name]

    def speak(self, text, mix, speed, lang):
        """Yield one frame per sentence. The mix is checked already; weights need not sum to 1."""
        total = sum(weight for _, weight in mix)
        voice = sum(self.pack(name) * (weight / total) for name, weight in mix)
        pipe = self.pipes[lang]
        for result in pipe(text, voice=voice, speed=speed, split_pattern=SPLIT):
            if result.audio is not None:
                yield frame(pcm_bytes(result.audio), lips(result, pipe.model.vocab))


class Listener:
    """faster-whisper behind one call: 16 kHz mono int16 LE PCM in, text out. The model is
    given a numpy array, so nothing is decoded from a file and nothing is written."""

    def __init__(self, model, name, numpy):
        self.model = model
        self.name = name
        self.np = numpy

    def transcribe(self, pcm):
        audio = self.np.frombuffer(pcm, dtype="<i2").astype(self.np.float32) / 32768.0
        segments, _ = self.model.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                            condition_on_previous_text=False, without_timestamps=True)
        return " ".join(s.text.strip() for s in segments if s.text.strip())


def audio_type(value):
    """True if a Content-Type is audio/L16 at 16 kHz, one channel. Type and parameter names
    are case-insensitive; channels may be left out (RFC 2586: one by default)."""
    kind, *params = [part.strip() for part in value.split(";")]
    if kind.lower() != "audio/l16":
        return False
    seen = {}
    for param in params:
        name, eq, val = param.partition("=")
        name = name.strip().lower()
        if not eq or name not in ("rate", "channels") or name in seen:
            return False
        seen[name] = val.strip().strip('"')
    return seen.get("rate") == str(LISTEN_RATE) and seen.get("channels", "1") == "1"


def parse_speak(body, engine):
    """The checked request as (text, mix, speed, lang), or ValueError. Longer text is cut at
    MAX_TEXT, as the prototype did, so a long utterance still speaks its start."""
    q = json.loads(body)
    if not isinstance(q, dict) or not isinstance(q.get("text"), str) or not isinstance(q.get("mix"), list):
        raise ValueError("bad fields")
    text = q["text"].strip()[:MAX_TEXT]
    speed = float(q.get("speed", 1))
    lang = q.get("lang", "a")
    mix = [(a, float(w)) for a, w in q["mix"]]
    if not text or not isinstance(lang, str) or lang not in engine.pipes or not 0.5 <= speed <= 2:
        raise ValueError("bad values")
    if not 1 <= len(mix) <= MAX_VOICES or any(a not in engine.voices for a, _ in mix):
        raise ValueError("bad voices")
    if any(not math.isfinite(w) or w < 0 for _, w in mix) or sum(w for _, w in mix) <= 0:
        raise ValueError("bad weights")  # json.loads accepts NaN and Infinity.
    return text, mix, speed, lang


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "factory-voice"
    sys_version = ""
    timeout = SOCKET_TIMEOUT
    allowed_origin = None

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Vary", "Origin")
        if self.allowed_origin:
            self.send_header("Access-Control-Allow-Origin", self.allowed_origin)
        super().end_headers()

    def fail(self, code, message):
        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def allowed(self):
        """Host must name this port on loopback (no DNS rebinding); a browser's Origin must be
        a loopback page. No Origin at all is curl, which is already on this machine."""
        self.allowed_origin = None
        port = self.server.server_address[1]
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0] not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            self.fail(403, "bad host")
            return False
        origins = self.headers.get_all("Origin") or []
        if origins:
            if len(origins) != 1 or not ORIGIN.fullmatch(origins[0]):
                self.fail(403, "bad origin")
                return False
            self.allowed_origin = origins[0]
        return True

    def do_OPTIONS(self):
        if not self.allowed():
            return
        if self.path not in ("/speak", "/listen"):
            return self.fail(404, "not found")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self.allowed():
            return
        if self.path.split("?")[0] != "/health":
            return self.fail(404, "not found")
        engine, listener = self.server.engine, self.server.listener
        self.reply({"ok": True, "device": engine.device, "voices": engine.voices,
                    "listen": listener is not None, "listen_model": listener.name if listener else None})

    def reply(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, limit=MAX_BODY):
        """The request body, or None once a refusal has been sent. Only a single, plain decimal
        Content-Length within the limit is read; anything else closes the connection."""
        if self.headers.get_all("Transfer-Encoding"):
            self.fail(400, "content-length only")
            return None
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not LENGTH.fullmatch(lengths[0].strip()):
            self.fail(400 if lengths else 411, "bad content-length")
            return None
        n = int(lengths[0])
        if not 0 < n <= limit:
            self.fail(413, "too large")
            return None
        try:
            body = self.rfile.read(n)
        except OSError:  # Includes the socket timeout.
            self.close_connection = True
            return None
        if len(body) != n:
            self.close_connection = True
            return None
        return body

    def do_POST(self):
        if not self.allowed():
            return
        if self.path == "/listen":
            return self.listen()
        if self.path != "/speak":
            return self.fail(404, "not found")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.fail(415, "json only")
        body = self.read_body()
        if body is None:
            return
        try:
            text, mix, speed, lang = parse_speak(body, self.server.engine)
        except (KeyError, TypeError, ValueError, RecursionError):
            return self.fail(400, "bad request")
        if not self.server.voice_lock.acquire(timeout=WAIT_FOR_VOICE):
            return self.fail(503, "busy")
        try:
            self.stream(text, mix, speed, lang)
        finally:
            self.server.voice_lock.release()

    def listen(self):
        """One transcription at a time, with its own lock: a speech stream lasts as long as its
        client takes to read it, and the owner talking must not wait behind LOIS talking. The
        audio and the text live only in this call's locals."""
        if not audio_type(self.headers.get("Content-Type", "")):
            return self.fail(415, "audio/L16; rate=16000; channels=1 only")
        listener = self.server.listener
        if listener is None:
            return self.fail(503, "no speech recognition in this image")
        pcm = self.read_body(MAX_AUDIO)
        if pcm is None:
            return
        if len(pcm) % 2:
            return self.fail(400, "not whole 16-bit samples")
        if not self.server.listen_lock.acquire(timeout=WAIT_FOR_EARS):
            return self.fail(503, "busy")
        try:
            started = time.monotonic()
            text = listener.transcribe(pcm)
            ms = round((time.monotonic() - started) * 1000)
        except Exception as e:  # The type only: a message could hold the text.
            say("listen failed: %s" % type(e).__name__)
            return self.fail(500, "transcription failed")
        finally:
            self.server.listen_lock.release()
        self.reply({"text": text, "ms": ms})

    def stream(self, text, mix, speed, lang):
        """One voice speaks at a time. A client that went away stops generation at the next
        write. Any failure ends the stream with no last chunk, so the client sees it broke."""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for data in self.server.engine.speak(text, mix, speed, lang):
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:  # Went away, or the socket timeout.
            self.close_connection = True
        except Exception as e:  # The type only: a message could hold the text.
            say("speak failed: %s" % type(e).__name__)
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, engine, listener=None):
        super().__init__(address, Handler)
        self.engine = engine
        self.listener = listener
        self.voice_lock = threading.Lock()
        self.listen_lock = threading.Lock()

    def handle_error(self, request, client_address):
        # The default prints a traceback, whose message could hold the text.
        say("request failed: %s" % sys.exc_info()[0].__name__)


def require_espeak(pipes):
    """Kokoro only warns when espeak-ng fails to load, and every word not in its lexicon is then
    silently skipped. A voice that drops words is worse than a service that will not start."""
    if any(pipe.g2p.fallback is None for pipe in pipes.values()):
        raise RuntimeError("espeak-ng did not load (is /tmp mounted noexec?)")


def load_engine(kokoro_dir=KOKORO_DIR):
    """Load the baked model, both pipelines and the common voices. Everything comes from
    kokoro_dir; the image also sets HF_HUB_OFFLINE, so a miss is an error, not a download."""
    import torch
    from kokoro import KModel, KPipeline
    from loguru import logger

    logger.remove()  # kokoro and misaki put phonemes and words in their warnings.
    # torch makes only the last level of its kernel cache folder, and warns if the rest is missing.
    os.makedirs(os.path.join(os.environ.get("XDG_CACHE_HOME", "/tmp/.cache"), "torch"), exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = KModel(repo_id=REPO, config=os.path.join(kokoro_dir, "config.json"),
                   model=os.path.join(kokoro_dir, "kokoro-v1_0.pth")).to(device).eval()
    pipes = {lang: KPipeline(lang_code=lang, repo_id=REPO, model=model) for lang in LANGS}
    require_espeak(pipes)
    voice_dir = os.path.join(kokoro_dir, "voices")
    voices = [m.group(1) for m in map(VOICE_FILE.fullmatch, os.listdir(voice_dir)) if m]
    engine = Engine(pipes, voices, device, voice_dir)
    for name in PRELOAD:
        engine.pack(name)
    return engine


def load_listener(whisper_dir=WHISPER_DIR, name=WHISPER_MODEL):
    """The baked Whisper model from whisper_dir/name, on the GPU when CTranslate2 sees one.
    local_files_only and HF_HUB_OFFLINE make a missing model an error, never a download."""
    import ctranslate2
    import numpy
    from faster_whisper import WhisperModel

    gpu = ctranslate2.get_cuda_device_count() > 0
    model = WhisperModel(os.path.join(whisper_dir, name), device="cuda" if gpu else "cpu",
                         compute_type="float16" if gpu else "int8", local_files_only=True)
    return Listener(model, name, numpy)


def main():
    # As PID 1 in a container, SIGTERM is ignored unless handled, and `docker stop` would wait.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    engine = load_engine()
    started = time.time()
    list(engine.speak("Ready.", [(PRELOAD[0], 1.0)], 1.0, "a"))
    say("ready on %s, warm-up %.2fs, %d voices" % (engine.device, time.time() - started, len(engine.voices)))
    # Listening is optional: an image without the model still speaks, and /health says so.
    try:
        listener = load_listener()
        listener.transcribe(bytes(LISTEN_RATE * 2))  # One second of silence, to warm it up.
        say("listening with whisper %s" % listener.name)
    except Exception as e:
        listener = None
        say("not listening: %s" % type(e).__name__)
    # misaki prints words it cannot read. Nothing of ours writes to stdout.
    sys.stdout = open(os.devnull, "w")
    Server((BIND, PORT), engine, listener).serve_forever()


if __name__ == "__main__":
    main()
