#!/usr/bin/env bash
set -euo pipefail

# Starts the dispatch bridge on the host for a project (if it is not already
# running for that project) and registers it with the sbx MCP gateway, for use
# by the Coordinator sandbox. Called by 02-launch-factory.sh.
#
# The bridge listens on 127.0.0.1 only and authenticates the gateway with a
# token minted once: the value is kept in the sbx secret store, the bridge sees
# only its SHA-256 (token.sha256 in the dispatch state dir, outside the repo).

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

[ $# -eq 1 ] || die "usage: $0 <project-dir>"
[ -d "$1" ] || die "project directory not found: $1"

require_sbx
for bin in curl openssl; do require_bin "$bin"; done
require_sha256

PYTHON="$(resolve_python)"

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# shellcheck source=lib/roles.sh
. "$ROOT/scripts/lib/roles.sh"
PROJECT="$(cd "$1" && pwd -P)"
STATE="$FACTORY_DISPATCH_STATE"
URL="http://127.0.0.1:$DISPATCH_PORT/mcp"
SECRET="mcp:$DISPATCH_MCP:token"

migrate_state "$ROOT"
mkdir -p "$STATE"
chmod 700 "$STATE"

if [ ! -s "$STATE/token.sha256" ] || ! sbx secret ls 2>/dev/null | grep -q " $SECRET "; then
  echo "Minting dispatch token..."
  token="$(openssl rand -hex 32)"
  printf '%s' "$token" | sha256_hash > "$STATE/token.sha256"
  printf '%s' "$token" | sbx secret set -f "$SECRET" >/dev/null 2>&1  # -f: without it an existing secret silently stays
  unset token
fi

# Snapshot each role's instructions (dispatch passes them to every task). The
# bridge reads this copy, not roles/, so edits take effect on the next start.
# Notes for this project from the factory config folder (e.g. its test
# command), if any, are appended for every role: they live outside the project,
# so the project needs no factory files and no role can change what the others
# are told.
notes="$FACTORY_CONFIG/projects/$(basename "$PROJECT").md"
rm -rf "$STATE/instructions.tmp"
mkdir -p "$STATE/instructions.tmp"
for role in $(list_roles); do
  validate_role "$role"
  cp "$ROOT/roles/$role/instructions.md" "$STATE/instructions.tmp/$role.md"
  if [ -s "$notes" ]; then
    printf '\n# This project (%s)\n' "$(basename "$PROJECT")" >> "$STATE/instructions.tmp/$role.md"
    cat "$notes" >> "$STATE/instructions.tmp/$role.md"
  fi
done
[ ! -s "$notes" ] || echo "Added project notes from $notes to the role instructions"
rm -rf "$STATE/instructions"
mv "$STATE/instructions.tmp" "$STATE/instructions"

# The factory's own docs, for the Coordinator's factory_docs tool. A snapshot,
# like the instructions above: the bridge reads this copy, so a role working on
# the factory's own repo can't change what the Coordinator is told.
rm -rf "$STATE/docs.tmp"
mkdir -p "$STATE/docs.tmp/guides"
cp "$ROOT/README.md" "$STATE/docs.tmp/README.md"
[ ! -f "$ROOT/docs/setup-wsl.md" ] || cp "$ROOT/docs/setup-wsl.md" "$STATE/docs.tmp/setup-wsl.md"
for guide in "$ROOT"/docs/guides/*.md; do
  if [ -f "$guide" ]; then cp "$guide" "$STATE/docs.tmp/guides/"; fi
done
rm -rf "$STATE/docs"
mv "$STATE/docs.tmp" "$STATE/docs"

running_pid() {
  local pid
  pid="$(cat "$STATE/pid" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= | grep -qF "$ROOT/dispatch/server.py" \
    && printf '%s' "$pid"
}

pid="$(running_pid || true)"
if [ -n "$pid" ] && [ "$(cat "$STATE/project" 2>/dev/null)" != "$PROJECT" ]; then
  echo "Dispatch is running for another project; restarting it..."
  kill "$pid"
  sleep 1
  pid=""
fi

# port_holder -> description of whatever listens on DISPATCH_PORT (best effort:
# lsof is standard on macOS but not on every Linux).
port_holder() {
  local holder
  command -v lsof >/dev/null 2>&1 || return 0
  holder="$(lsof -nP -t -iTCP:"$DISPATCH_PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  [ -n "$holder" ] && printf 'pid %s: %s' "$holder" "$(ps -p "$holder" -o command= 2>/dev/null || true)"
}

# Anything answering on the port now is not this checkout's bridge (e.g. one
# started from another copy of this repo). Our bridge would fail to bind and the
# readiness check below would be answered by the stranger.
if [ -z "$pid" ] && curl -s -o /dev/null -m 3 "$URL"; then
  holder="$(port_holder || true)"
  die "port $DISPATCH_PORT is already in use by another process${holder:+ ($holder)}.
It is not this checkout's dispatch bridge; it may be one started from another copy of the factory repo.
Stop it (e.g. run that copy's scripts/03-stop-factory.sh, or kill the process), then re-run."
fi

if [ -z "$pid" ]; then
  echo "Starting dispatch bridge for $PROJECT..."
  printf '%s\n' "$PROJECT" > "$STATE/project"
  # env(1), not VAR=value prefixes: DISPATCH_PORT is readonly in this shell.
  nohup env DISPATCH_ROOT="$ROOT" DISPATCH_PROJECT="$PROJECT" DISPATCH_STATE="$STATE" \
    DISPATCH_PORT="$DISPATCH_PORT" DISPATCH_SBX="$(command -v sbx)" \
    "$PYTHON" "$ROOT/dispatch/server.py" >>"$STATE/dispatch.log" 2>&1 </dev/null &
  pid=$!
  echo "$pid" > "$STATE/pid"
fi

# Ready = our bridge process is alive and the port answers 401 (auth required).
# The liveness check catches a bridge that exited (e.g. could not bind the port)
# while something else answers in its place.
code=000
for _ in $(seq 1 15); do
  kill -0 "$pid" 2>/dev/null || die "dispatch bridge exited; see $STATE/dispatch.log"
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 3 -X POST -d '{}' "$URL" || true)"
  [ "$code" = "401" ] && kill -0 "$pid" 2>/dev/null && break
  sleep 1
done
[ "$code" = "401" ] || die "dispatch did not become ready (last HTTP status: $code); see $STATE/dispatch.log"

if ! sbx mcp ls -q 2>/dev/null | grep -qx "$DISPATCH_MCP"; then
  # shellcheck disable=SC2016 # ${token} is an sbx secret placeholder, not a shell variable
  sbx mcp add "$DISPATCH_MCP" --url "$URL" --header 'Authorization: Bearer ${token}' --skip-auth >/dev/null
fi

echo "Dispatch ready: $URL (audit log: $STATE/audit.jsonl, transcripts: $STATE/logs/)"
