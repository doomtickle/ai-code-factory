"""Where LOIS's local voice service lives (voice/, run by `factory voice`). The dashboard
imports PORT from here. No conflict with the owner's prototype on 8880."""

import json
import urllib.request

PORT = 8873
IMAGE = "factory-voice:1"
CONTAINER = "factory-voice"


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
