#!/bin/sh
# Keeps the broker server running inside its sandbox; crashes are logged and
# the server restarts. Launched by scripts/start-broker.sh via `sbx exec`.
set -eu
: "${BROKER_STATE:?}" "${BROKER_WORKSPACE:?}"
while :; do
  PYTHONDONTWRITEBYTECODE=1 python3 /opt/factory-broker/server.py >>"$BROKER_STATE/broker.log" 2>&1 || true
  echo "$(date -u +%FT%TZ) broker exited; restarting in 2s" >>"$BROKER_STATE/broker.log"
  sleep 2
done
