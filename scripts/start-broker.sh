#!/usr/bin/env bash
set -euo pipefail

# Starts (or restarts) the broker sandbox for a project and registers it with
# the sbx MCP gateway once per role that has BROKER_TOOLS. Called by
# 02-launch-factory.sh; safe to run on its own.
#
# Each role authenticates with its own bearer token, minted once: the value goes
# only into the sbx secret store (the gateway sends it as that role's header),
# the broker sees only its SHA-256 in tokens/<role>.sha256 and maps it back to
# the role. grants.json tells the broker which tools each role may use. Both live
# in the broker state dir (FACTORY_BROKER_STATE, outside the repo), which is
# shared into the broker sandbox only, never into an agent sandbox.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

[ $# -eq 1 ] || die "usage: $0 <project-dir>"
[ -d "$1" ] || die "project directory not found: $1"

require_sbx
for bin in curl jq openssl; do require_bin "$bin"; done
require_sha256

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# shellcheck source=lib/roles.sh
. "$ROOT/scripts/lib/roles.sh"
# shellcheck source=lib/secrets.sh
. "$ROOT/scripts/lib/secrets.sh"
PROJECT="$(cd "$1" && pwd -P)"
STATE="$FACTORY_BROKER_STATE"
URL="http://127.0.0.1:$BROKER_PORT/mcp"

require_state_outside "$PROJECT"
migrate_state "$ROOT"
mkdir -p "$STATE/tokens"
chmod 700 "$STATE"

# A broker's workspace is fixed when its sandbox is created.
if sandbox_exists "$BROKER_SANDBOX" && [ -f "$STATE/project" ] \
    && [ "$(cat "$STATE/project")" != "$PROJECT" ]; then
  die "the broker is bound to $(cat "$STATE/project"). To switch projects, see README (Switching projects)."
fi
printf '%s\n' "$PROJECT" > "$STATE/project"

# Per-role tool grants, from roles/*/role.conf (the broker re-reads this per
# request). Written in object form ({"tools": [...], "source": "..."}) so a
# denied principal's error message can point at the file that grants it.
grants='{}'
# Transition shim: a broker still on a pin before grants.json landed reads
# only the old bare-list roles.json ({"principal": ["tool", ...]}); without
# it every call is denied. Drop this (and roles_bare below) once that pin has
# settled.
roles_bare='{}'
broker_roles=""
for role in $(list_roles); do
  validate_role "$role"
  tools="$(role_get "$role" BROKER_TOOLS)"
  [ -n "$tools" ] || continue
  broker_roles="$broker_roles $role"
  # shellcheck disable=SC2086 # split the tool list into jq arguments
  grants="$(printf '%s' "$grants" | jq --arg r "$role" --arg src "roles/$role/role.conf" \
    '.[$r] = {tools: $ARGS.positional, source: $src}' --args $tools)"
  # shellcheck disable=SC2086 # split the tool list into jq arguments
  roles_bare="$(printf '%s' "$roles_bare" | jq --arg r "$role" '.[$r] = $ARGS.positional' --args $tools)"
done

# The Coordinator is a broker pseudo-role (kits/coordinator/broker.conf): it
# calls broker tools directly, with no role in between. Same mechanics as a
# role's grant, gated the same way against KNOWN_BROKER_TOOLS.
coordinator_tools="$(coordinator_broker_tools)"
if [ -n "$coordinator_tools" ]; then
  for t in $coordinator_tools; do
    in_list "$t" "$KNOWN_BROKER_TOOLS" \
      || die "kits/coordinator/broker.conf: unknown broker tool '$t' (known: $KNOWN_BROKER_TOOLS)"
  done
  broker_roles="$broker_roles coordinator"
  # shellcheck disable=SC2086 # split the tool list into jq arguments
  grants="$(printf '%s' "$grants" | jq --arg r "coordinator" --arg src "kits/coordinator/broker.conf" \
    '.[$r] = {tools: $ARGS.positional, source: $src}' --args $coordinator_tools)"
  # shellcheck disable=SC2086 # split the tool list into jq arguments
  roles_bare="$(printf '%s' "$roles_bare" | jq --arg r "coordinator" '.[$r] = $ARGS.positional' --args $coordinator_tools)"
fi
printf '%s\n' "$grants" > "$STATE/grants.json.tmp" && mv "$STATE/grants.json.tmp" "$STATE/grants.json"
# roles.json is the older name; a broker still on a pin before grants.json
# landed reads only this file (see the transition shim comment above).
printf '%s\n' "$roles_bare" > "$STATE/roles.json.tmp" && mv "$STATE/roles.json.tmp" "$STATE/roles.json"

# One token per broker role. Minted once and kept: the sbx daemon caches header
# secrets, so a new value only reaches the gateway after `sbx daemon restart`
# (see README, "Rotating broker tokens").
secrets="$(sbx secret ls 2>/dev/null || true)"
for role in $broker_roles; do
  secret="mcp:$(role_broker_mcp "$role"):token"
  if [ ! -s "$STATE/tokens/$role.sha256" ] || ! printf '%s' "$secrets" | grep -q " $secret "; then
    echo "Minting broker token for $role..."
    token="$(openssl rand -hex 32)"
    printf '%s' "$token" | sha256_hash > "$STATE/tokens/$role.sha256"
    printf '%s' "$token" | sbx secret set -f "$secret" >/dev/null 2>&1  # -f: without it an existing secret silently stays
    unset token
  fi
done

if sandbox_exists "$BROKER_SANDBOX"; then
  echo "Starting existing broker sandbox..."
  run_or_die "could not start the broker sandbox" sbx run -d --name "$BROKER_SANDBOX"
else
  echo "Creating broker sandbox for $PROJECT..."
  # This machine's extra broker egress hosts (e.g. a private npm registry), if any.
  set -- --kit "$ROOT/kits/broker"
  if [ -f "$FACTORY_CONFIG/broker-kit/spec.yaml" ]; then
    echo "Including local broker kit from $FACTORY_CONFIG/broker-kit"
    set -- "$@" --kit "$FACTORY_CONFIG/broker-kit"
  fi
  # Plugins baked into the image (00-build-templates.sh) load only when named
  # here; the directory names under the local plugins folder are the modules.
  if [ -n "$(broker_plugins)" ]; then
    echo "Enabling broker plugins: $(broker_plugins)"
    set -- "$@" -e "PASSDESK_PLUGINS=$(broker_plugins)"
  fi
  run_or_die "could not create the broker sandbox" \
    sbx run -d --name "$BROKER_SANDBOX" --pull never -t "factory-broker:$TEMPLATE_TAG" \
    -p "127.0.0.1:$BROKER_PORT:8765" \
    -e "BROKER_WORKSPACE=$PROJECT" -e "BROKER_STATE=$STATE" \
    shell "$@" "$PROJECT" "$STATE"
  record_sandbox_fingerprint "$BROKER_SANDBOX"
  # A brand new sandbox has none of the credentials delivered, so any marker
  # left over from a previous one (e.g. after 'factory reset' or 'factory
  # update' removed the old broker sandbox) is stale: left in place, it would
  # make secret_sync_from_keychain below skip that name forever, reporting
  # "leaving it as is" for a credential that no longer exists anywhere.
  cleared="$(secret_clear_all_manual)"
  if [ -n "$cleared" ]; then
    echo "note: this is a new broker sandbox, so the 'factory secret set' marker(s) for:$(printf ' %s' $cleared)" \
         "were cleared; run 'factory secret set <name>' again for each to redeliver it"
  fi
fi

# Start the server if it is not running (kit startup commands do not re-run on restart).
# The pgrep pattern is anchored so it cannot match this sh -c command line itself.
sbx exec "$BROKER_SANDBOX" sh -c \
  'pgrep -f "^/bin/sh /opt/factory-broker/run.sh" >/dev/null || setsid /opt/factory-broker/run.sh </dev/null >/dev/null 2>&1 &'

# Port publishing does not survive every restart; re-publish if it is gone.
if ! sbx ports "$BROKER_SANDBOX" --json 2>/dev/null \
    | jq -e --argjson p "$BROKER_PORT" 'any(.[]; .host_port == $p)' >/dev/null; then
  sbx ports "$BROKER_SANDBOX" --publish "127.0.0.1:$BROKER_PORT:8765" >/dev/null
fi

# Ready = the server answers and demands a token (401 without one).
echo "Waiting for the broker to answer..."
code=000
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 3 -X POST -d '{}' "$URL" || true)"
  [ "$code" = "401" ] && break
  sleep 1
done
[ "$code" = "401" ] || die "broker did not become ready (last HTTP status: $code); see $STATE/broker.log"

# Register each broker role with the MCP gateway once; the header secret is
# resolved at connect time.
registered="$(sbx mcp ls -q 2>/dev/null || true)"
for role in $broker_roles; do
  name="$(role_broker_mcp "$role")"
  if ! printf '%s\n' "$registered" | grep -qx "$name"; then
    # shellcheck disable=SC2016 # ${token} is an sbx secret placeholder, not a shell variable
    sbx mcp add "$name" --url "$URL" --header 'Authorization: Bearer ${token}' --skip-auth >/dev/null
  fi
done

# Drop registrations, tokens and secrets of roles that no longer have broker access.
for name in $(printf '%s\n' "$registered" | grep '^factory-broker-' || true); do
  role="${name#factory-broker-}"
  in_list "$role" "$broker_roles" && continue
  echo "Removing broker access for former role $role..."
  sbx mcp rm --force "$name" >/dev/null 2>&1 || true
  sbx secret rm -f "mcp:$name:token" >/dev/null 2>&1 || true
  sbx secret rm -f "mcp:$name:token:endpoint" >/dev/null 2>&1 || true
  rm -f "$STATE/tokens/$role.sha256"
done

# Credentials the sbx egress proxy cannot inject (Jira, Gemini, the private
# npm registry, GitLab, and the approvals webhook URL): the narrowly scoped
# exception in CLAUDE.md
# ("Credentials"). Looked up in the host OS keychain and piped over stdin
# into the running broker sandbox (secret_sync_from_keychain, scripts/lib/secrets.sh),
# so the value never appears in argv, an env flag, a log, git, or on host
# disk. Re-running this script re-delivers the value, so rotating the
# keychain entry and re-running it rotates the credential the broker uses -
# and *deleting* the keychain entry and re-running it revokes the
# credential: the stale file is removed rather than left behind for the tool
# to keep using. A name delivered instead by `factory secret set` (for
# machines with no OS keychain, e.g. WSL2) is left alone here - see its
# marker check in secret_sync_from_keychain. Run last (after MCP
# registration above) and never fatal on a delivery failure: a keychain
# prompt the user denies or a locked keychain, or an old broker image
# predating /etc/factory-broker/secrets (see README, Upgrading), should warn
# and leave the rest of the factory usable rather than aborting the whole
# launch. A revocation failure is different (see secret_sync_from_keychain)
# and is fatal, checked below.
revocation_failed=0
secret_sync_from_keychain jira || revocation_failed=1
secret_sync_from_keychain gemini || revocation_failed=1
secret_sync_from_keychain npm-registry || revocation_failed=1
# GitLab's token is a file too: sbx has no gitlab service to proxy-inject it.
secret_sync_from_keychain gitlab || revocation_failed=1
# The approvals webhook URL is a credential too (it grants posting); the
# gateway reads it as its approvals-webhook credential when holds are on.
secret_sync_from_keychain approvals-webhook || revocation_failed=1
# The GitHub token is not a file: it is the broker-scoped sbx secret the
# egress proxy injects. 'sbx rm' deletes it with the sandbox, so restore it
# from the keychain (factory-github, 'factory secret save github') on every
# start; without a keychain entry, a token set by hand is left alone.
github_sync_from_keychain

[ "$revocation_failed" = 0 ] \
  || die "a credential revocation failed above; it may still be active in the broker sandbox - see the error(s) above"

echo "Broker ready: $URL, roles:${broker_roles:- none} (audit log: $STATE/audit.jsonl)"
