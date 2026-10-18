#!/bin/sh
# Keeps the broker server running; crashes are logged and the server
# restarts. Resolves server.py relative to this script's own directory, so
# it works wherever the image installs it (INSTALL_DIR in the Dockerfile).
# With sbx + the AI Code Factory, this is launched by scripts/start-broker.sh
# via `sbx exec`.
set -eu
: "${BROKER_STATE:?}" "${BROKER_WORKSPACE:?}"
dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
while :; do
  PYTHONDONTWRITEBYTECODE=1 python3 "$dir/server.py" >>"$BROKER_STATE/broker.log" 2>&1 || true
  echo "$(date -u +%FT%TZ) broker exited; restarting in 2s" >>"$BROKER_STATE/broker.log"
  sleep 2
done
