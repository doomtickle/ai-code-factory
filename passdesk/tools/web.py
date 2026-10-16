"""web_fetch: GET a page from an allowlisted documentation host."""

from __future__ import annotations

import html.parser
import re
import urllib.error
import urllib.parse
import urllib.request

from ..core import config
from ..core.errors import Denied
from ..core.filters import (
    block_outbound_secrets,
    neutralize_markers,
    sanitize,
    untrusted,
)
from ..core.policy import host_allowed
from ..core.registry import register


def tool_web_fetch(args: dict, policy: dict) -> str:
    url = args["url"]
    web = policy["web"]
    allow = web.get("allow_hosts", [])
    block_outbound_secrets("URL", url)

    def check(u: str) -> None:
        parsed = urllib.parse.urlsplit(u)
        if parsed.scheme != "https":
            raise Denied("only https:// URLs are allowed")
        if parsed.username or parsed.password:
            raise Denied("URLs with credentials are not allowed")
        if not host_allowed(parsed.hostname or "", allow):
            raise Denied(f"host {parsed.hostname} is not in the web allowlist (policy.toml [web].allow_hosts)")

    class Redirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    check(url)
    opener = urllib.request.build_opener(Redirects)
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": config.USER_AGENT})
    max_bytes = int(web.get("max_bytes", 500_000))
    try:
        with opener.open(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if not re.match(r"(text/|application/(json|xml|xhtml\+xml))", ctype):
                raise Denied(f"content type {ctype!r} is not allowed (text, HTML, JSON, XML only)")
            raw = resp.read(max_bytes + 1)
            final_url = resp.geturl()
            status = resp.status
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} from {url}"
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    if "html" in ctype:
        text = html_to_text(text)
    body = sanitize(text, limit=max_bytes)
    if truncated:
        body += f"\n[truncated at {max_bytes} bytes]"
    return f"HTTP {status} {neutralize_markers(final_url)}\n" + untrusted(final_url, body)


class _TextExtractor(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(src: str) -> str:
    p = _TextExtractor()
    p.feed(src)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


register({
    "web_fetch": (tool_web_fetch, "Fetch an https page from an allowlisted documentation host (GET only). "
                  "Returns text wrapped in UNTRUSTED CONTENT markers.",
                  {"url": {"type": "string", "description": "https:// URL"}}, ["url"]),
})
