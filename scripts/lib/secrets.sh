# Shared helpers for the credentials sbx cannot proxy-inject (Jira, Gemini,
# the private npm registry token, and, when the Passdesk Pro plugin is
# installed, its license key and the approvals webhook URL) - the named
# exception in CLAUDE.md ("Credentials"). Delivered into
# /etc/factory-broker/secrets/<name> inside the broker sandbox over sbx exec
# stdin, never through argv, an env flag, a log, or git. Source after
# common.sh (needs FACTORY_STATE and BROKER_SANDBOX).
#
# Two ways a value reaches here:
#   - scripts/start-broker.sh, from the host OS keychain (secret_sync_from_keychain),
#     every time the broker starts;
#   - `factory secret set/rm` (bin/factory), for machines with no OS keychain
#     (e.g. WSL2), which deliver/revoke directly and never touch the keychain.
# Both go through secret_deliver_file / secret_revoke_file, so the remote
# code is byte-for-byte identical either way.
#
# A name `factory secret set` has delivered is marked at
# $FACTORY_STATE/secrets-manual/<name> (an empty file - never the value) so
# secret_sync_from_keychain leaves it alone: an empty or absent keychain
# entry for the same name must not revoke a value `factory secret set` just
# delivered. `factory secret rm` clears the marker. The marker dir is under
# FACTORY_STATE directly, not FACTORY_BROKER_STATE (a subdirectory that is
# bind-mounted read-write into the broker sandbox): a marker decides whether
# a later `factory start` is allowed to revoke a credential, so it must sit
# somewhere a compromised broker can't reach and plant one to block that
# revocation. FACTORY_STATE itself is never mounted into any sandbox - only
# its broker/ and roles/<role>/ subdirectories are (scripts/start-broker.sh,
# scripts/02-launch-factory.sh).
# shellcheck shell=bash

readonly FACTORY_SECRET_NAMES="jira gemini npm-registry passdesk-license approvals-webhook"

# secret_name_valid NAME -> success if NAME is exactly one of
# FACTORY_SECRET_NAMES (not a substring match: "jira gemini" or "x jira y"
# must not pass just because the padded string contains " jira ").
secret_name_valid() {
  local n
  for n in $FACTORY_SECRET_NAMES; do [ "$1" = "$n" ] && return 0; done
  return 1
}

# keychain_lookup SERVICE -> value on stdout, or nothing (no OS keychain
# support, or no entry stored).
keychain_lookup() {
  case "$(uname -s)" in
    Darwin) security find-generic-password -s "$1" -w 2>/dev/null || true ;;
    *) command -v secret-tool >/dev/null 2>&1 && secret-tool lookup service "$1" 2>/dev/null || true ;;
  esac
}

# secret_deliver_file SANDBOX NAME -> write stdin (the secret value) into
# /etc/factory-broker/secrets/NAME inside SANDBOX, atomically: umask 077,
# mkdir -p the secrets dir, write to a .tmp file and rename it into place.
# We don't know which user `sbx exec` runs commands as for this sandbox: if
# it's root, the chowns make this work even on an old broker image that
# predates /etc/factory-broker/secrets (otherwise the file would end up
# root-owned 0600, unreadable by "agent", who server.py runs as); if it's
# already "agent" (who owns the directory per broker/Dockerfile on a rebuilt
# image), id -u is non-zero and both chowns are skipped as unnecessary (and
# would fail). Non-zero exit if delivery fails.
secret_deliver_file() {
  local sandbox="$1" name="$2"
  sbx exec -i "$sandbox" sh -c \
    'umask 077 && dir="/etc/factory-broker/secrets" && mkdir -p "$dir" \
     && { [ "$(id -u)" != 0 ] || chown agent:agent "$dir"; } \
     && dest="$dir/$1" && cat > "$dest.tmp" && mv "$dest.tmp" "$dest" \
     && { [ "$(id -u)" != 0 ] || chown agent:agent "$dest"; }' \
    _ "$name"
}

# secret_revoke_file SANDBOX NAME -> remove /etc/factory-broker/secrets/NAME
# inside SANDBOX. Non-zero exit if the removal fails.
secret_revoke_file() {
  sbx exec "$1" rm -f "/etc/factory-broker/secrets/$2"
}

# secret_manual_dir -> directory holding one marker file per manually-managed name
secret_manual_dir() { printf '%s/secrets-manual' "$FACTORY_STATE"; }

# secret_is_manual NAME -> success if NAME is marked as managed by `factory secret`
secret_is_manual() {
  [ -e "$(secret_manual_dir)/$1" ]
}

# secret_mark_manual NAME -> create NAME's marker (idempotent, empty file, never the value)
secret_mark_manual() {
  local dir
  dir="$(secret_manual_dir)"
  mkdir -p "$dir"
  chmod 700 "$dir"
  : > "$dir/$1"
}

# secret_unmark_manual NAME -> remove NAME's marker, if any
secret_unmark_manual() {
  rm -f "$(secret_manual_dir)/$1"
}

# secret_clear_all_manual -> remove every marker, printing the name of each
# one cleared (one per line, on stdout). Called only when a brand new broker
# sandbox is created (scripts/start-broker.sh): it starts with none of the
# credentials delivered, so any marker left over from a previous sandbox is
# stale and would make secret_sync_from_keychain skip a name that the new
# sandbox has no file for at all.
secret_clear_all_manual() {
  local dir f
  dir="$(secret_manual_dir)"
  [ -d "$dir" ] || return 0
  for f in "$dir"/*; do
    [ -e "$f" ] || continue
    basename "$f"
    rm -f "$f"
  done
}

# secret_sync_from_keychain NAME -> called once per name by
# scripts/start-broker.sh on every broker start. Delivers the current
# keychain value (service "factory-<name>") to the broker sandbox, or - if
# the keychain has none, whether never configured or deliberately cleared -
# revokes any stale file so the tool reports DENIED instead of silently
# continuing to work off an old credential. A no-op (one note, no keychain
# lookup, no delivery or revocation) when NAME is marked manual
# (secret_is_manual): the whole point of `factory secret set/rm` is that
# re-running this must not overwrite or wipe what it delivered.
# Exit status: 2 if a revocation failed (the file may still be active, so
# the caller must not report success); 0 otherwise, including a delivery
# failure, which is only a soft warning (as before this was factored out).
secret_sync_from_keychain() {
  local name="$1" service="factory-$1" value
  if secret_is_manual "$name"; then
    echo "note: $name is managed by 'factory secret set/rm'; leaving it as is"
    return 0
  fi
  value="$(keychain_lookup "$service")"
  if [ -z "$value" ]; then
    if ! secret_revoke_file "$BROKER_SANDBOX" "$name"; then
      echo "error: could not remove the $name credential file from the broker sandbox;" \
           "it may still be active even though its keychain entry is gone or unreadable" >&2
      return 2
    fi
    return 0
  fi
  if ! printf '%s' "$value" | secret_deliver_file "$BROKER_SANDBOX" "$name"; then
    echo "warning: could not deliver the $name credential to the broker sandbox" \
         "(an old broker image? see README, Upgrading, for the rebuild steps)" >&2
  fi
  return 0
}
