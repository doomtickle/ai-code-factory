"""Settings from the environment, read once at import.

Other modules read these as config.NAME at call time (never `from config
import NAME`), so tests can point them at temporary directories.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SERVER_NAME = "passdesk"
SERVER_VERSION = "1.0.0"
USER_AGENT = f"{SERVER_NAME}/{SERVER_VERSION}"

WORKSPACE = Path(os.environ.get("BROKER_WORKSPACE", "/nonexistent"))
STATE = Path(os.environ.get("BROKER_STATE", "/nonexistent"))
# policy.toml sits beside the passdesk package (see Dockerfile).
POLICY_PATH = Path(os.environ.get("BROKER_POLICY", Path(__file__).resolve().parents[2] / "policy.toml"))
PORT = int(os.environ.get("BROKER_PORT", "8765"))
# Default "0.0.0.0" keeps today's deployments (e.g. sbx port publishing)
# working; set narrower (e.g. "127.0.0.1") when nothing outside the host
# needs to reach the broker directly.
BIND_HOST = os.environ.get("BROKER_BIND_HOST", "0.0.0.0")
MIRRORS = Path(os.environ.get("BROKER_MIRRORS", Path.home() / "mirrors"))

# "sbx" (default): the deployment this repo documents and tests (Passdesk as
# its own sbx sandbox, built by the AI Code Factory; see README "Deploying
# with Docker Sandboxes"). "standalone": any other deployment. Tools and the
# doctor use this to show (or suppress) hints that name the sbx CLI.
DEPLOYMENT = os.environ.get("BROKER_DEPLOYMENT", "sbx")

# Comma-separated Origin allowlist for DNS-rebinding protection (MCP spec).
# A request with no Origin header (the normal case for CLI-driven agent
# clients) is unaffected; a request whose Origin isn't in this list is
# refused with 403. Empty by default, so any Origin-bearing request is
# refused until a deployment opts specific origins in.
ALLOWED_ORIGINS = frozenset(
    o.strip() for o in os.environ.get("BROKER_ALLOWED_ORIGINS", "").split(",") if o.strip())

# Credentials the sbx egress proxy cannot inject (see the package docstring and
# CLAUDE.md's "Credentials" section). Written by scripts/start-broker.sh, read
# fresh on every call so rotating the host keychain entry and re-running that
# script rotates the credential without restarting the broker.
# SECRETS_DIR is where the three paths below default to, and also where
# credentials.secrets_dir() looks up any other credential name; each of the
# three is still independently overridable (BROKER_JIRA_SECRET, etc.) for a
# deployment that wants one of them somewhere else entirely.
SECRETS_DIR = os.environ.get("BROKER_SECRETS_DIR", "/etc/factory-broker/secrets")
JIRA_SECRET_PATH = Path(os.environ.get("BROKER_JIRA_SECRET", f"{SECRETS_DIR}/jira"))
GEMINI_SECRET_PATH = Path(os.environ.get("BROKER_GEMINI_SECRET", f"{SECRETS_DIR}/gemini"))
NPM_REGISTRY_SECRET_PATH = Path(os.environ.get("BROKER_NPM_REGISTRY_SECRET", f"{SECRETS_DIR}/npm-registry"))

# Plugin modules to import at start-up (comma-separated; see core/plugins.py).
PLUGINS = os.environ.get("PASSDESK_PLUGINS", "")

# Credential providers in lookup order (comma-separated; see core/credentials.py).
# With sbx the default reproduces the gateway's behaviour before providers
# existed (the egress proxy injects the GitHub token). A standalone
# deployment has no such proxy, so its default leaves `proxy` out and the
# gateway reads the `github` credential like any other. An explicit
# setting always wins.
CREDENTIAL_PROVIDERS = os.environ.get("PASSDESK_CREDENTIAL_PROVIDERS",
                                      "file,env-file" if DEPLOYMENT == "standalone" else "proxy,file,env-file")

MAX_REQUEST_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 20_000

# Name of the workspace directory git_fetch/npm_fetch/pip_download use to
# hand files to the agent's own sandbox (see core/workspace.py's factory_dir).
# Overridable for deployments that don't want the ".factory" name; the
# default is what every existing deployment and test already expects.
EXCHANGE_DIR = os.environ.get("BROKER_EXCHANGE_DIR", ".factory")

# Ref namespace git_push stages an incoming branch under, in the broker's own
# bare mirror, while it verifies and awaits approval (tools/git.py); never
# pushed itself. Must sit under "refs/" but not equal to or under a namespace
# git (or GitHub) already treats specially -- refs/heads, refs/tags,
# refs/remotes, refs/notes, refs/stash -- since e.g. "refs/remotes/origin"
# would land an unverified, pending-approval commit where git_fetch's bundle
# creation and anything else that walks refs/remotes sees it as origin's own
# ref.
REF_NAMESPACE = os.environ.get("BROKER_REF_NAMESPACE", "refs/factory")

# Fallback for [git].push_branch_prefix when a deployment's policy.toml
# doesn't set it at all (every policy.toml this repo ships does, so this
# only matters for a deployment's own from-scratch policy). Deliberately
# fail-closed (unlike upstream, which falls back to "" there): see
# docs/decisions.md.
DEFAULT_PUSH_BRANCH_PREFIX = os.environ.get("BROKER_PUSH_PREFIX", "factory/")

_REF_NAMESPACE_RE = re.compile(r"^refs/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_RESERVED_REF_PREFIXES = ("refs/heads", "refs/tags", "refs/remotes", "refs/notes", "refs/stash")
_PUSH_PREFIX_RE = re.compile(r"^[A-Za-z0-9._/-]*$")


def validate() -> None:
    """Fail fast on an invalid environment setting, before the server binds
    a port (see core/protocol.py's main()). Raises ValueError with a message
    naming the offending variable; module attributes are read at call time
    (not captured at def time) so a test can set config.NAME and call this
    directly, the same way other modules read config.NAME at call time."""
    if DEPLOYMENT not in ("sbx", "standalone"):
        raise ValueError(f'BROKER_DEPLOYMENT={DEPLOYMENT!r} must be "sbx" or "standalone"')
    ns = REF_NAMESPACE
    if not _REF_NAMESPACE_RE.fullmatch(ns) or ".." in ns or "//" in ns or ns.endswith(("/", ".")):
        raise ValueError(f'BROKER_REF_NAMESPACE={ns!r} must be a ref path under "refs/" '
                         '(e.g. "refs/factory"): no "..", no trailing slash, no empty component')
    if ns in _RESERVED_REF_PREFIXES or any(ns.startswith(p + "/") for p in _RESERVED_REF_PREFIXES):
        raise ValueError(f'BROKER_REF_NAMESPACE={ns!r} must not be, or sit under, a namespace git/GitHub '
                         f"already use ({', '.join(_RESERVED_REF_PREFIXES)})")
    for component in ns.split("/"):
        if component.startswith(".") or component.endswith(".lock"):
            raise ValueError(f'BROKER_REF_NAMESPACE={ns!r}: ref component {component!r} must not start '
                             'with "." or end with ".lock" (see git check-ref-format)')
    secrets_dir = SECRETS_DIR
    if not Path(secrets_dir).is_absolute() or ".." in Path(secrets_dir).parts:
        raise ValueError(f'BROKER_SECRETS_DIR={secrets_dir!r} must be an absolute path with no ".." component')
    if not _PUSH_PREFIX_RE.fullmatch(DEFAULT_PUSH_BRANCH_PREFIX):
        raise ValueError(f'BROKER_PUSH_PREFIX={DEFAULT_PUSH_BRANCH_PREFIX!r} must contain only ref-safe '
                         'characters (letters, digits, "." "_" "-" "/"), no whitespace or control characters')
