#!/usr/bin/env bash
set -euo pipefail

# Starts the dispatch bridge on the host for a project (if it is not already
# running for that project) and registers it with the sbx MCP gateway, for use
# by the Coordinator sandbox. Called by 02-launch-factory.sh. The work is
# factory/dispatch_start.py's; this keeps the path and the arguments.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec "$(resolve_python)" "$ROOT/factory" _start-dispatch "$@"
