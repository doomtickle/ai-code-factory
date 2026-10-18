"""Who is calling, and which tools they may use.

A principal is whatever a token was minted for: a role, a user, a session -
the deployment decides. Passdesk itself just maps a token to a principal name
and a principal name to a grant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from . import config
from .registry import TOOLS


def authenticate(header: str | None) -> tuple[str | None, str | None]:
    """Map a bearer token to the principal it was minted for.

    Returns (principal, None) on success or (None, reason). Each principal's
    token hash is STATE/tokens/<principal>.sha256, written by the deployment.
    The reason never includes the token itself.
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


def grants_path() -> Path:
    """Where the grants file lives: STATE/grants.json if present, else the
    older STATE/roles.json (same format), for deployments that still write
    that name.
    """
    path = config.STATE / "grants.json"
    return path if path.is_file() else config.STATE / "roles.json"


def _load_grants() -> dict:
    try:
        grants = json.loads(grants_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return grants if isinstance(grants, dict) else {}


def _grant(principal: str) -> dict | list:
    return _load_grants().get(principal, [])


def grant_tools(grant: object) -> list[str]:
    """The tool list a grant carries, or [] if it is malformed.

    A grant is either a bare list of tool names, or an object
    {"tools": [...], "source": "..."} - see grant_source(). Anything that
    isn't a list of strings (missing, null, a non-list, non-string items)
    is treated as no grant at all, i.e. deny.
    """
    tools = grant.get("tools", []) if isinstance(grant, dict) else grant
    return tools if isinstance(tools, list) and all(isinstance(t, str) for t in tools) else []


def is_valid_grant(grant: object) -> bool:
    """Whether `grant` has the shape a grant must have: a bare list of tool
    names, or an object carrying such a list under "tools" (missing "tools"
    counts as an empty grant). Used by doctor to flag grants.json entries
    that parse as JSON but aren't a usable grant.
    """
    if isinstance(grant, dict):
        grant = grant.get("tools", [])
    return isinstance(grant, list) and all(isinstance(t, str) for t in grant)


def principal_tools(principal: str) -> set[str]:
    """Tools a principal may use, from the grants file (see grants_path())."""
    return {t for t in grant_tools(_grant(principal)) if t in TOOLS}


def grant_source(principal: str) -> str | None:
    """The optional `source` a grant can carry, for deny messages to quote -
    e.g. where a deployment keeps that principal's grant configured. None
    when the grant is the bare list form, carries no source, or the source
    isn't a string.
    """
    grant = _grant(principal)
    source = grant.get("source") if isinstance(grant, dict) else None
    return source if isinstance(source, str) else None
