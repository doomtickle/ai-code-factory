"""Settings from the environment, read once at import.

Other modules read these as config.NAME at call time (never `from config
import NAME`), so tests can point them at temporary directories.
"""

from __future__ import annotations

import os
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
JIRA_SECRET_PATH = Path(os.environ.get("BROKER_JIRA_SECRET", "/etc/factory-broker/secrets/jira"))
GEMINI_SECRET_PATH = Path(os.environ.get("BROKER_GEMINI_SECRET", "/etc/factory-broker/secrets/gemini"))
NPM_REGISTRY_SECRET_PATH = Path(os.environ.get("BROKER_NPM_REGISTRY_SECRET",
                                               "/etc/factory-broker/secrets/npm-registry"))

# Plugin modules to import at start-up (comma-separated; see core/plugins.py).
PLUGINS = os.environ.get("PASSDESK_PLUGINS", "")

# Credential providers in lookup order (comma-separated; see core/credentials.py).
# The default reproduces the gateway's behaviour before providers existed.
CREDENTIAL_PROVIDERS = os.environ.get("PASSDESK_CREDENTIAL_PROVIDERS", "proxy,file,env-file")

MAX_REQUEST_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 20_000

# Name of the workspace directory git_fetch/npm_fetch/pip_download use to
# hand files to the agent's own sandbox (see core/workspace.py's factory_dir).
# Overridable for deployments that don't want the ".factory" name; the
# default is what every existing deployment and test already expects.
EXCHANGE_DIR = os.environ.get("BROKER_EXCHANGE_DIR", ".factory")
