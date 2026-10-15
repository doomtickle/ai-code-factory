"""Settings from the environment, read once at import.

Other modules read these as config.NAME at call time (never `from config
import NAME`), so tests can point them at temporary directories.
"""

from __future__ import annotations

import os
from pathlib import Path

SERVER_NAME = "factory-broker"
SERVER_VERSION = "1.0.0"
USER_AGENT = f"{SERVER_NAME}/{SERVER_VERSION}"

WORKSPACE = Path(os.environ.get("BROKER_WORKSPACE", "/nonexistent"))
STATE = Path(os.environ.get("BROKER_STATE", "/nonexistent"))
# policy.toml sits beside the agentbroker package (templates/broker/Dockerfile).
POLICY_PATH = Path(os.environ.get("BROKER_POLICY", Path(__file__).resolve().parents[2] / "policy.toml"))
PORT = int(os.environ.get("BROKER_PORT", "8765"))
MIRRORS = Path(os.environ.get("BROKER_MIRRORS", Path.home() / "mirrors"))

# Credentials the sbx egress proxy cannot inject (see the package docstring and
# CLAUDE.md's "Credentials" section). Written by scripts/start-broker.sh, read
# fresh on every call so rotating the host keychain entry and re-running that
# script rotates the credential without restarting the broker.
JIRA_SECRET_PATH = Path(os.environ.get("BROKER_JIRA_SECRET", "/etc/factory-broker/secrets/jira"))
GEMINI_SECRET_PATH = Path(os.environ.get("BROKER_GEMINI_SECRET", "/etc/factory-broker/secrets/gemini"))
NPM_REGISTRY_SECRET_PATH = Path(os.environ.get("BROKER_NPM_REGISTRY_SECRET",
                                               "/etc/factory-broker/secrets/npm-registry"))

MAX_REQUEST_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 20_000
