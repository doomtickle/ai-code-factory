"""Policy loading and the checks shared by more than one tool."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from . import config
from .errors import Denied


def merge_policy(base: dict, local: dict) -> dict:
    """Overlay local on base: tables merge key by key, any other value replaces."""
    out = dict(base)
    for key, val in local.items():
        out[key] = merge_policy(out[key], val) if isinstance(val, dict) and isinstance(out.get(key), dict) else val
    return out


def load_policy(path: Path | None = None) -> dict:
    """policy.toml plus, if present, policy.local.toml beside it: this
    deployment's own settings (private registry, Jira site), baked into the
    image next to policy.toml as a build-time overlay (see Dockerfile)."""
    path = path or config.POLICY_PATH
    with open(path, "rb") as f:
        policy = tomllib.load(f)
    local = path.with_name("policy.local.toml")
    if local.is_file():
        with open(local, "rb") as f:
            policy = merge_policy(policy, tomllib.load(f))
    return policy


def host_allowed(host: str, patterns: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for p in patterns:
        p = p.lower().rstrip(".")
        if p.startswith("*."):
            if host.endswith(p[1:]) and host != p[2:]:
                return True
        elif host == p:
            return True
    return False


def check_package(ecosystem: str, name: str, policy: dict) -> None:
    pk = policy.get("packages", {})
    norm = name.lower() if ecosystem == "npm" else re.sub(r"[-_.]+", "-", name.lower())
    deny = [d.lower() for d in pk.get(f"{ecosystem}_deny", [])]
    allow = [a.lower() for a in pk.get(f"{ecosystem}_allow", [])]
    if norm in deny:
        raise Denied(f"{ecosystem} package {name} is denied by policy")
    if allow and norm not in allow:
        raise Denied(f"{ecosystem} package {name} is not in the allowlist")
