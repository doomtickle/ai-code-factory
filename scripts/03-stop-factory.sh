#!/usr/bin/env bash
set -euo pipefail

# Stops the factory: the dispatch bridge on the host and every factory-*
# sandbox. Sandboxes keep their state; 02-launch-factory.sh starts them again.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"
require_sbx
require_bin jq

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"

migrate_state "$ROOT"
pid="$(cat "$FACTORY_DISPATCH_STATE/pid" 2>/dev/null || true)"
if [ -n "$pid" ] && ps -p "$pid" -o command= 2>/dev/null | grep -qF "$ROOT/dispatch/server.py"; then
  echo "Stopping dispatch bridge ($pid)..."
  kill "$pid"
fi

running="$(sbx ls --json | jq -r '.sandboxes[] | select(.status == "running" and (.name | startswith("factory-"))) | .name')"
if [ -n "$running" ]; then
  echo "Stopping sandboxes: $(printf '%s ' $running)"
  # shellcheck disable=SC2086 # one argument per sandbox name
  sbx stop $running >/dev/null
fi
echo "Factory stopped."
