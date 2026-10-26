#!/usr/bin/env bash
set -euo pipefail

# Host-wide sbx security baseline. Safe to re-run. The work is in Python:
# factory/security_init.py.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec "$(resolve_python)" "$ROOT/factory" _init-security
