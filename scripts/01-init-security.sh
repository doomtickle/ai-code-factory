#!/usr/bin/env bash
set -euo pipefail

# Host-wide sbx security baseline. Safe to re-run.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

require_sbx

echo "Initializing Docker Sandbox baseline security..."

# The broker listens on the host loopback, which only the local MCP gateway can
# reach (a hosted gateway cannot). Changing this requires a daemon restart.
if [ "$(sbx settings get mcp.forceLocalGateway)" != "true" ]; then
  [ -z "$(sbx ls -q)" ] || die "stop and remove running sandboxes first; enabling the local MCP gateway restarts the sbx daemon"
  sbx settings set mcp.forceLocalGateway true
  sbx daemon restart
fi

# Enforce default-deny egress on the host proxy. One-time: later runs verify it instead.
if ! out="$(sbx policy init deny-all 2>&1)"; then
  case "$out" in
    *"already initialized"*) echo "Global network policy already initialized; verifying it is deny-all." ;;
    *) die "$out" ;;
  esac
fi
# `sbx policy check` exits non-zero for a denied host, so match on its output.
check="$(sbx policy check network example.com 2>&1 || true)"
case "$check" in
  *"default deny"*) ;;
  *) die "global network policy is not deny-all; run 'sbx policy reset' and re-run this script" ;;
esac

# Allow baseline provider access globally.
check="$(sbx policy check network api.anthropic.com 2>&1 || true)"
case "$check" in
  Allowed*) ;;
  *) sbx policy allow network api.anthropic.com ;;
esac

echo "Security baseline established. Verification:"
sbx policy ls
