"""Where LOIS's local voice service lives (voice/, run by `factory voice`). The dashboard
imports PORT from here. No conflict with the owner's prototype on 8880."""

import json
import struct
import time
import urllib.request

PORT = 8873
IMAGE = "factory-voice:1"
CONTAINER = "factory-voice"
SAMPLE_RATE = 24000
TEST_TEXT = ("Three jobs are running, and one write is waiting for you. The reviewer has finished its work. "
             "Spend today is two dollars and forty cents.")


def health(port=PORT, timeout=2):
    """What the running service says about itself ({ok, device, voices}), None if it does
    not answer: it is off, or still loading the model. Never through a proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/health" % port, timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_exactly(response, size):
    data = response.read(size)
    if len(data) != size:
        raise ValueError("the service stopped in the middle of a frame")
    return data


def speak_test(voice, port=PORT, timeout=300):
    """Speaks TEST_TEXT through /speak and throws the audio away. Returns (seconds of speech, seconds it
    took, seconds until the first audio, loudest sample). Raises OSError or ValueError if that fails."""
    body = json.dumps({"text": TEST_TEXT, "mix": [[voice, 1]], "speed": 1, "lang": "b" if voice.startswith("b") else "a"})
    request = urllib.request.Request("http://127.0.0.1:%d/speak" % port, data=body.encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    samples, loudest, first = 0, 0.0, None
    started = time.monotonic()
    with opener.open(request, timeout=timeout) as response:
        while True:
            header = response.read(8)
            if not header:
                break
            if len(header) != 8:
                raise ValueError("the service stopped in the middle of a frame")
            count, lips = struct.unpack("<II", header)
            _read_exactly(response, lips)
            pcm = _read_exactly(response, count * 4)
            if first is None:
                first = time.monotonic() - started
            samples += count
            loudest = max([loudest] + [abs(v[0]) for v in struct.iter_unpack("<f", pcm)])
    return samples / SAMPLE_RATE, time.monotonic() - started, first or 0.0, loudest
