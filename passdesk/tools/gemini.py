"""gemini_flash_query: a cheap model call for summarizing or triage."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..core import config
from ..core.errors import Denied
from ..core.filters import block_outbound_secrets, sanitize, untrusted
from ..core.net import NO_REDIRECT_OPENER, read_secret_file
from ..core.registry import register


def tool_gemini_flash_query(args: dict, policy: dict) -> str:
    prompt = args["prompt"]
    gemini = policy.get("gemini", {})
    max_chars = int(gemini.get("max_prompt_chars", 50_000))
    if not prompt.strip():
        raise Denied("prompt is empty")
    if len(prompt) > max_chars:
        raise Denied(f"prompt exceeds {max_chars} characters")
    block_outbound_secrets("prompt", prompt)
    api_key = read_secret_file(config.GEMINI_SECRET_PATH)
    if not api_key:
        raise Denied("no Gemini API key found for the broker sandbox; store one in the host "
                     "keychain (service \"factory-gemini\") and run scripts/start-broker.sh "
                     "again - see README, Enabling Gemini Flash queries")
    model = gemini.get("model", "gemini-2.5-flash")
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=payload, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key,
                 "User-Agent": config.USER_AGENT})
    try:
        with NO_REDIRECT_OPENER.open(req, timeout=30) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = sanitize(e.read().decode("utf-8", "replace"), 2000)
        hint = ("\nHint: the broker sandbox's Gemini API key may be missing or invalid; check "
                "the \"factory-gemini\" host keychain entry and run scripts/start-broker.sh "
                "again") if e.code in (401, 403) else ""
        return f"Gemini API returned HTTP {e.code}: {detail}{hint}"
    parts = []
    for cand in result.get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            if "text" in part:
                parts.append(part["text"])
    text = sanitize("\n".join(parts))
    return untrusted(f"Gemini ({model})", text)


register({
    "gemini_flash_query": (tool_gemini_flash_query, "Send a prompt to Gemini Flash for cheap/fast summarization "
                           "or triage (e.g. condensing a large fetched page before reporting back). The API key "
                           "is read from a file inside the broker sandbox and is never seen by agents. "
                           "Returns text wrapped in UNTRUSTED CONTENT markers.",
                           {"prompt": {"type": "string"}}, ["prompt"]),
})
