"""Mint and manage the principals a deployment's tokens/ and grants.json
name - the same on-disk state core.auth reads and ai-code-factory's
scripts/start-broker.sh writes, so either can manage what the other wrote.

A principal's token hash lives at STATE/tokens/<name>.sha256 (its SHA-256
hex digest, newline-terminated - see core.auth.authenticate); its grant
lives under its name in STATE/grants.json, in the object form
{"tools": [...], "source": "..."} core.auth.grant_tools() reads. The token
itself is never written anywhere: add() returns it once, to be shown and
then discarded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import tools as _tools  # noqa: F401 - registers every tool
from .core import auth, config
from .core.registry import TOOLS

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
SOURCE = "passdesk principal"


class PrincipalError(ValueError):
    """A bad principal name, an unknown tool, or a principal that already
    exists (add) or does not (revoke)."""


@dataclass(frozen=True)
class Principal:
    name: str
    tools: list[str]
    has_token: bool


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise PrincipalError(
            f"invalid principal name {name!r}: must start with a lowercase letter and contain only "
            "lowercase letters, digits, '_' or '-' (max 64 characters)")
    return name


def validate_tools(tool_names: list[str]) -> list[str]:
    if not tool_names:
        raise PrincipalError("at least one --tools name is required")
    unknown = sorted({t for t in tool_names if t not in TOOLS})
    if unknown:
        raise PrincipalError(f"unknown tool(s): {', '.join(unknown)} (known tools: {', '.join(sorted(TOOLS))})")
    return sorted(set(tool_names))


def _tokens_dir() -> Path:
    return config.STATE / "tokens"


def _grants_path() -> Path:
    return config.STATE / "grants.json"


def _ensure_state_dirs() -> None:
    config.STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(config.STATE, 0o700)
    _tokens_dir().mkdir(parents=True, exist_ok=True)
    os.chmod(_tokens_dir(), 0o700)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write `data` to `path`, replacing it atomically, mode `mode` and
    nothing looser at any point (the temp file is created with that mode
    from the start, not chmod'd after - no window where it is readable by
    anyone but its owner)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _load_grants() -> dict:
    """The grants currently in effect, read the same way the server resolves
    them (auth.grants_path(): grants.json if present, else the older
    roles.json), so a deployment that has only ever run start-broker.sh
    (grants.json never written) seeds from its roles.json instead of
    looking empty - add() writing a "full" grants.json from an empty
    baseline would otherwise make every other principal's grant invisible
    the moment grants.json starts existing."""
    path = auth.grants_path()
    try:
        grants = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise PrincipalError(f"{path}: cannot read grants: {e}") from e
    return grants if isinstance(grants, dict) else {}


def _save_grants(grants: dict) -> None:
    _atomic_write(_grants_path(), (json.dumps(grants, indent=2, sort_keys=True) + "\n").encode(), 0o600)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def add(name: str, tool_names: list[str]) -> str:
    """Create `name` with the given tools, minting a fresh token. Returns the
    token - the only time it exists outside the caller's memory. Raises
    PrincipalError if the name or any tool is invalid, or the principal
    already exists (a token file or a grants.json entry)."""
    validate_name(name)
    wanted = validate_tools(tool_names)
    _ensure_state_dirs()

    token_path = _tokens_dir() / f"{name}.sha256"
    grants = _load_grants()
    if token_path.exists() or name in grants:
        raise PrincipalError(f"principal {name!r} already exists; revoke it first to replace it")

    token = secrets.token_hex(32)  # 256 bits, matches the factory's `openssl rand -hex 32`
    _atomic_write(token_path, (token_digest(token) + "\n").encode(), 0o600)
    grants[name] = {"tools": wanted, "source": SOURCE}
    try:
        _save_grants(grants)
    except BaseException:
        # Don't leave a token file with no matching grant: a retried add()
        # must see the principal as not existing at all.
        token_path.unlink(missing_ok=True)
        raise
    return token


def list_principals() -> list[Principal]:
    """Every principal with a token and/or a grant, sorted by name. Never
    includes a token hash or a token."""
    grants = _load_grants()
    names = {p.stem for p in _tokens_dir().glob("*.sha256")} if _tokens_dir().is_dir() else set()
    names |= set(grants)
    result = []
    for name in sorted(names):
        tool_list = sorted(auth.grant_tools(grants.get(name, [])))
        result.append(Principal(name=name, tools=tool_list, has_token=(_tokens_dir() / f"{name}.sha256").exists()))
    return result


def revoke(name: str) -> None:
    """Remove `name`'s token hash and grant. Raises PrincipalError if neither
    exists."""
    validate_name(name)
    token_path = _tokens_dir() / f"{name}.sha256"
    grants = _load_grants()
    had_token = token_path.exists()
    had_grant = name in grants
    if not had_token and not had_grant:
        raise PrincipalError(f"no such principal: {name!r}")
    if had_token:
        token_path.unlink()
    if had_grant:
        del grants[name]
        _save_grants(grants)
