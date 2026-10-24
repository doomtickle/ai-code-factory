"""Outbound HTTP helpers shared by tools that carry a credential."""

from __future__ import annotations

import urllib.error
import urllib.request


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse 3xx redirects instead of following them.

    urllib's default HTTPRedirectHandler builds a follow-up Request for the
    redirect target and copies the original request's headers onto it -
    including an Authorization or API-key header. For an authenticated call
    that would send the credential to whatever host the 3xx names. Raising
    here means no follow-up request is ever built, so the header can't leak.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, "refusing to follow a redirect on an authenticated request", headers, fp)


# Shared by every request that carries a credential header (Jira, Gemini,
# the private npm registry), and by npm_fetch's public downloads.
NO_REDIRECT_OPENER = urllib.request.build_opener(RefuseRedirects)
