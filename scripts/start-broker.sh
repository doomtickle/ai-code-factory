#!/usr/bin/env bash
set -euo pipefail

# Starts (or restarts) the broker sandbox for a project, registers it with the
# sbx MCP gateway once per role that has BROKER_TOOLS, and delivers the
# credentials sbx can't proxy-inject. Called by 02-launch-factory.sh; safe to
# run on its own. The work is in Python: factory/broker_start.py. The usage
# check stays here, as it names this script the way it was called.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

[ $# -eq 1 ] || die "usage: $0 <project-dir>"

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec "$(resolve_python)" "$ROOT/factory" _start-broker "$@"
