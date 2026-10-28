"""One outbound notification per approval card, to a webhook URL.

The URL is a credential (a Slack incoming-webhook URL grants posting), so it
is the `approvals-webhook` credential: a file the deployment delivers into
the gateway's own sandbox (`<secrets dir>/approvals-webhook`, a Docker
secret, or the file named by PASSDESK_SECRET_APPROVALS_WEBHOOK_FILE), read
fresh through core.credentials on every use and never logged. The body
carries the same plain-text summary under the keys Slack (`text`), Discord
(`content`) and ntfy (`message`) read. A failure is logged and never blocks
or leaks the card. `send_async` runs the POST on its own thread so a slow
webhook never delays the call (doomtickle/passdesk-ops#14).
`send_result_async` posts, the same way, the outcome of a staged write the
gateway made (or failed to make) after approval (doomtickle/passdesk-ops#91).
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import config, credentials
from .audit import audit
from .net import NO_REDIRECT_OPENER

CREDENTIAL = "approvals-webhook"

# Slack's hard limit is 3,000 characters; stay comfortably under it even if a
# card carries an unusually long branch, repo or diff-stat field.
MAX_SUMMARY_LEN = 2800


def usable_url(url: str | None) -> bool:
    """https, and no userinfo: none of the supported services puts a
    password in the URL, and urllib would echo one in an InvalidURL."""
    if not url or not url.startswith("https://"):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return bool(parts.hostname) and parts.username is None and parts.password is None


def webhook_url() -> str | None:
    """The delivered URL, or None if there is none or it isn't usable."""
    try:
        url = credentials.get(CREDENTIAL)
    except credentials.CredentialError:
        return None
    return url if usable_url(url) else None


def summary(card: dict, cid: str, expires_in_minutes: int) -> str:
    who = card.get("principal") or "an agent"
    if card["tool"] == "git_push":
        head = (card.get("head") or "")[:12]
        what = (f"{who} wants to push {card.get('branch')} to {card.get('repo')} "
                f"({card.get('commits')} commit(s), {card.get('file_count')} file(s), {card.get('shortstat') or 'no diff stat'}; "
                f"head {head}; secret scan {card.get('secret_scan')})")
    elif card["tool"] in ("github_create_pr", "gitlab_create_mr"):
        noun = "merge request" if card["tool"] == "gitlab_create_mr" else "pull request"
        kind = f"draft {noun}" if card.get("draft", True) else noun
        what = (f"{who} wants to open a {kind} on {card.get('repo')}: "
                f"{card.get('head')} -> {card.get('base')}, \"{(card.get('title') or '')[:120]}\"")
    elif card["tool"] == "github_update_pr":
        changes = [f"title to \"{(card.get('title') or '')[:120]}\"" if card.get("title") is not None else "",
                   "the body" if card.get("body") is not None else ""]
        what = (f"{who} wants to change {' and '.join(c for c in changes if c)} of pull request "
                f"#{card.get('number')} on {card.get('repo')} (head {card.get('head')})")
    else:
        what = f"{who} wants to run {card['tool']}"
    prefix = f"Passdesk approval {cid}: "
    tail = f". Decide within {expires_in_minutes} min: factory approvals approve {cid} (or decline)."
    budget = MAX_SUMMARY_LEN - len(prefix) - len(tail)
    if len(what) > budget:
        what = what[:max(0, budget - 1)] + "…"
    return prefix + what + tail


def result_summary(cid: str, what: str, ok: bool, result: str) -> str:
    """One line for the outcome of a staged write: what it was and the first
    line of its result (already filtered by the caller), capped like a
    card's summary."""
    first = " ".join((result.strip().splitlines() or [""])[0].split())
    text = (f"Passdesk approval {cid}: the gateway made the approved {what}: {first}" if ok else
            f"Passdesk approval {cid}: the approved {what} FAILED: {first} (factory approvals show {cid})")
    return text if len(text) <= MAX_SUMMARY_LEN else text[:MAX_SUMMARY_LEN - 1] + "…"


def send(card: dict, cid: str, expires_in_minutes: int, url: str | None = None) -> bool:
    """POST the summary; True if the webhook accepted it. Never raises."""
    return _post(summary(card, cid, expires_in_minutes), cid, url)


def _post(text: str, cid: str, url: str | None = None) -> bool:
    url = url or webhook_url()
    if not url:
        return False
    body = json.dumps({"text": text, "content": text, "message": text}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": config.USER_AGENT})
    try:
        with NO_REDIRECT_OPENER.open(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
    except urllib.error.URLError as e:
        # HTTPError's reason is the status phrase, URLError's the OS error:
        # neither carries the URL.
        audit({"event": "approval-notify-failed", "id": cid, "reason": str(getattr(e, "reason", e))[:300]})
        return False
    except (http.client.HTTPException, OSError, ValueError) as e:
        # Anything else (a bad status line, an InvalidURL) may quote the
        # URL in its message, so only the type is recorded.
        audit({"event": "approval-notify-failed", "id": cid, "reason": type(e).__name__})
        return False
    if not ok:
        audit({"event": "approval-notify-failed", "id": cid, "reason": f"HTTP {resp.status}"})
    return ok


def send_async(card: dict, cid: str, expires_in_minutes: int) -> threading.Thread:
    """`send` on a daemon thread, so the held call never waits on the webhook.
    Returns the thread (tests join it); the card is the redacted copy the
    hook was given, so nothing new leaves with it."""
    t = threading.Thread(target=send, args=(card, cid, expires_in_minutes), name=f"notify-{cid}", daemon=True)
    t.start()
    return t


def send_result_async(card: dict, cid: str, what: str, ok: bool, result: str) -> threading.Thread:
    """POST result_summary on a daemon thread (the queue's runner never
    waits on the webhook). `card` is the stored, redacted card; only `what`
    and the result's first line leave with the message."""
    t = threading.Thread(target=_post, args=(result_summary(cid, what, ok, result), cid),
                         name=f"notify-result-{cid}", daemon=True)
    t.start()
    return t
