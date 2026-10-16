"""Who is calling, and which tools they may use.

A principal is whatever a token was minted for. The factory mints one per role
(and one for the Coordinator), so today principal names are role names.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from . import config
from .registry import TOOLS


def authenticate(header: str | None) -> tuple[str | None, str | None]:
    """Map a bearer token to the principal it was minted for.

    Returns (principal, None) on success or (None, reason). Each principal's
    token hash is STATE/tokens/<principal>.sha256, written by
    scripts/start-broker.sh. The reason never includes the token itself.
    """
    if not header:
        return None, "missing Authorization header"
    if not header.startswith("Bearer "):
        return None, "Authorization header is not a Bearer token"
    got = hashlib.sha256(header[len("Bearer "):].strip().encode()).hexdigest()
    for path in sorted((config.STATE / "tokens").glob("*.sha256")):
        try:
            expected = path.read_text().strip()
        except OSError:
            continue
        if expected and hmac.compare_digest(got, expected):
            return path.stem, None
    return None, f"token mismatch (received {len(header) - 7} characters)"


def principal_tools(principal: str) -> set[str]:
    """Tools a principal may use, from STATE/roles.json (written from roles/*/role.conf)."""
    try:
        grants = json.loads((config.STATE / "roles.json").read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {t for t in grants.get(principal, []) if t in TOOLS}
