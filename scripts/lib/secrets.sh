# Shared helpers for the credentials sbx cannot proxy-inject (Jira, Gemini,
# the private npm registry token, the GitLab token and the approvals webhook
# URL) - the named
# exception in CLAUDE.md ("Credentials"). Delivered into
# /etc/factory-broker/secrets/<name> inside the broker sandbox over sbx exec
# stdin, never through argv, an env flag, a log, or git. Source after
# common.sh (needs FACTORY_STATE and BROKER_SANDBOX).
#
# Two ways a value reaches here:
#   - scripts/start-broker.sh, from the host OS keychain (secret_sync_from_keychain),
#     every time the broker starts;
#   - `factory secret set/rm` (factory/commands/secret.py), for machines with
#     no OS keychain (e.g. WSL2), which deliver/revoke directly and never touch
#     the keychain.
# The Python port of these helpers (factory/secrets.py) runs the same remote
# code byte for byte; factory/test_secrets.py runs both on the same fakes.
# Keep them in step until start-broker.sh is ported too.
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

readonly FACTORY_SECRET_NAMES="jira gemini npm-registry gitlab approvals-webhook"

# secret_name_valid NAME -> success if NAME is exactly one of
# FACTORY_SECRET_NAMES (not a substring match: "jira gemini" or "x jira y"
# must not pass just because the padded string contains " jira ").
secret_name_valid() {
  local n
  for n in $FACTORY_SECRET_NAMES; do [ "$1" = "$n" ] && return 0; done
  return 1
}

# Names `factory secret save/forget` accept: the files above, plus "github",
# which is not a file but the broker-scoped sbx secret the egress proxy
# injects (github_sync_from_keychain below).
readonly FACTORY_KEYCHAIN_NAMES="$FACTORY_SECRET_NAMES github"

keychain_name_valid() {
  local n
  for n in $FACTORY_KEYCHAIN_NAMES; do [ "$1" = "$n" ] && return 0; done
  return 1
}

# wincred_powershell -> path to Windows PowerShell when running under WSL with
# interop, else nothing. FACTORY_POWERSHELL overrides it (tests use a fake).
wincred_powershell() {
  local ps="${FACTORY_POWERSHELL:-}"
  if [ -z "$ps" ]; then
    grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null || return 0
    ps="$(command -v powershell.exe 2>/dev/null || true)"
    if [ -z "$ps" ] && [ -x /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe ]; then
      ps=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
    fi
  fi
  [ -z "$ps" ] || printf '%s' "$ps"
}

# keychain_backend -> macos | secret-tool | wincred | none: where this
# machine keeps the factory's credentials (services "factory-<name>").
# wincred is the Windows Credential Manager, used on WSL2 when there is no
# secret-tool: encrypted under the Windows login, no unlock prompt, and it
# survives WSL restarts. FACTORY_KEYCHAIN overrides the choice (tests).
# Picking secret-tool doesn't mean it works (no Secret Service running, or a
# locked keyring): keychain_lookup reports that as "unavailable" (status 2)
# rather than falling back to another backend or to "none", which would
# read as "nothing stored" and get credentials revoked.
keychain_backend() {
  if [ -n "${FACTORY_KEYCHAIN:-}" ]; then printf '%s' "$FACTORY_KEYCHAIN"; return 0; fi
  if [ "$(uname -s)" = Darwin ]; then printf macos; return 0; fi
  if command -v secret-tool >/dev/null 2>&1; then printf secret-tool; return 0; fi
  if [ -n "$(wincred_powershell)" ]; then printf wincred; return 0; fi
  printf none
}

# wincred_run OP SERVICE -> run one Credential Manager operation (lookup,
# store, delete) through Windows PowerShell. The value travels only on
# stdin/stdout, never in argv. SERVICE is embedded in the script, so it must
# be one of ours: "factory-" and a valid keychain name, nothing else.
wincred_run() {
  local op="$1" service="$2" ps body
  case "$service" in factory-*) ;; *) return 1 ;; esac
  keychain_name_valid "${service#factory-}" || return 1
  ps="$(wincred_powershell)"
  [ -n "$ps" ] || return 1
  case "$op" in
    lookup) body="try { \$c = \$v.Retrieve('$service', 'factory') } catch { exit 0 }
\$c.RetrievePassword(); [Console]::Out.Write(\$c.Password)" ;;
    store) body="[Console]::InputEncoding = New-Object Text.UTF8Encoding \$false
\$val = [Console]::In.ReadToEnd(); if (\$val.Length -eq 0) { exit 3 }
try { \$v.Remove(\$v.Retrieve('$service', 'factory')) } catch {}
\$v.Add((New-Object Windows.Security.Credentials.PasswordCredential('$service', 'factory', \$val)))" ;;
    delete) body="try { \$v.Remove(\$v.Retrieve('$service', 'factory')) } catch {}" ;;
    *) return 1 ;;
  esac
  "$ps" -NoProfile -NonInteractive -Command "# factory-keychain op=$op
\$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding \$false
[void][Windows.Security.Credentials.PasswordVault, Windows.Security.Credentials, ContentType = WindowsRuntime]
\$v = New-Object Windows.Security.Credentials.PasswordVault
$body"
}

# keychain_search_empty SERVICE -> success only if 'secret-tool search'
# exits 0, prints nothing on stderr and lists no item (no "[" line) for
# SERVICE; a search that fails in any way is not taken as "empty". The
# listing can hold "secret = <value>", so stdout goes straight to grep -c
# (which reads it all, so secret-tool never gets SIGPIPE) and only the count
# is kept; stderr goes to a temp file that is removed before returning.
keychain_search_empty() {
  local errf res count status
  errf="$(mktemp)" || return 1
  res="$(secret-tool search service "$1" 2>"$errf" | { grep -c '^\[' || :; }
         echo " ${PIPESTATUS[0]}")"
  if [ -s "$errf" ]; then rm -f "$errf"; return 1; fi
  rm -f "$errf"
  count="${res%%[!0-9]*}"; status="${res##*[!0-9]}"
  [ "$status" = 0 ] && [ "$count" = 0 ]
}

# keychain_lookup SERVICE -> value on stdout. Exit status tells the cases
# apart, so a locked keychain is never mistaken for a cleared entry:
#   0  found (a non-empty value is on stdout);
#   1  definitely not stored: secret-tool exiting 1 with nothing on stderr
#      and then a clean, empty 'secret-tool search' (keychain_search_empty),
#      macOS `security` exiting 44 (errSecItemNotFound), the Credential
#      Manager reporting no such entry - or no OS keychain here at all;
#   2  undetermined: the keychain is locked, unavailable (no Secret Service,
#      no D-Bus session), the user denied the prompt, or anything else.
# Nothing the backend prints is passed on; secret-tool's stderr is read only
# to tell "not found" (silent) from an error.
keychain_lookup() {
  local backend value rc=0 errf err
  backend="$(keychain_backend)"
  case "$backend" in
    macos)
      value="$(security find-generic-password -s "$1" -w 2>/dev/null)" || rc=$?
      if [ "$rc" = 44 ]; then return 1; fi ;;
    secret-tool)
      errf="$(mktemp)" || return 2
      value="$(secret-tool lookup service "$1" 2>"$errf")" || rc=$?
      err="$(cat "$errf")"; rm -f "$errf"
      # A locked item whose unlock prompt was dismissed also comes back as a
      # silent exit 1: list it without unlocking (or prompting) to be sure.
      if [ "$rc" = 1 ] && [ -z "$err" ] && [ -z "$value" ]; then
        keychain_search_empty "$1" && return 1
        return 2
      fi ;;
    wincred)
      # The lookup script exits 0 with no output when there is no entry.
      value="$(wincred_run lookup "$1" 2>/dev/null </dev/null)" || rc=$?
      if [ "$rc" = 0 ] && [ -z "$value" ]; then return 1; fi ;;
    *) return 1 ;;
  esac
  if [ "$rc" = 0 ] && [ -n "$value" ]; then printf '%s' "$value"; return 0; fi
  return 2
}

# keychain_unavailable_warning SERVICE WHAT -> one warning on stderr: the
# keychain couldn't be read for SERVICE, so WHAT is being left as it is, and
# how to fix that on this machine's backend. Never prints a value.
keychain_unavailable_warning() {
  local hint
  case "$(keychain_backend)" in
    macos) hint="unlock the Keychain (security unlock-keychain) and allow access when asked" ;;
    secret-tool) hint="unlock your login keyring (log in to the desktop session, or open Passwords and Keys) and check that a Secret Service such as gnome-keyring-daemon is running (try: secret-tool lookup service $1)" ;;
    wincred) hint="check that Windows PowerShell runs from WSL (powershell.exe) and can reach the Credential Manager" ;;
    *) hint="check this machine's keychain" ;;
  esac
  echo "warning: could not read $1 from the keychain (locked or unavailable); leaving $2 as it is." \
       "To fix: $hint, then run 'factory start' again" >&2
}

# keychain_has SERVICE -> 0 if the keychain holds an entry, 1 if it
# definitely doesn't, 2 if that can't be told (locked or unavailable), as
# keychain_lookup. On macOS this checks existence only, without reading the
# value (no Keychain prompt).
keychain_has() {
  local rc=0
  case "$(keychain_backend)" in
    macos)
      security find-generic-password -s "$1" >/dev/null 2>&1 || rc=$?
      case "$rc" in 0) return 0 ;; 44) return 1 ;; *) return 2 ;; esac ;;
    none) return 1 ;;
    *) keychain_lookup "$1" >/dev/null ;;
  esac
}

# keychain_store SERVICE -> save stdin (the value) as SERVICE. Non-zero exit
# if there is no keychain here or saving failed. Not for macOS: `security`
# has no stdin form, so 'factory secret save' lets it prompt for the value
# itself rather than pass it in argv.
keychain_store() {
  local service="$1"
  case "$(keychain_backend)" in
    secret-tool) secret-tool store --label "AI Code Factory: $service" service "$service" ;;
    wincred) wincred_run store "$service" >/dev/null ;;
    *) return 1 ;;
  esac
}

# keychain_delete SERVICE -> remove SERVICE from the keychain (no error if absent).
keychain_delete() {
  case "$(keychain_backend)" in
    macos) security delete-generic-password -s "$1" >/dev/null 2>&1 || true ;;
    secret-tool) secret-tool clear service "$1" 2>/dev/null || true ;;
    wincred) wincred_run delete "$1" >/dev/null 2>&1 </dev/null || true ;;
  esac
}

# github_sync_from_keychain -> called by scripts/start-broker.sh on every
# broker start. If the keychain holds factory-github, (re)register it as the
# broker-scoped sbx secret the egress proxy injects for GitHub, so a broker
# recreated by 'factory update' or 'factory reset' gets its token back
# without anyone retyping it. Scoped to the broker sandbox, never global
# (CLAUDE.md). With no keychain entry, whatever sbx already has is left
# alone (a token set by hand with 'sbx secret set github --sandbox ...'), and
# so is it when the keychain is locked or unavailable (with a warning).
# Never fatal: a failure warns and the factory still starts.
github_sync_from_keychain() {
  local value rc=0
  value="$(keychain_lookup factory-github)" || rc=$?
  case "$rc" in
    0) ;;
    1) return 0 ;;
    *) keychain_unavailable_warning factory-github "the broker's GitHub token"; return 0 ;;
  esac
  if printf '%s' "$value" | sbx secret set -f github --sandbox "$BROKER_SANDBOX" >/dev/null 2>&1; then
    echo "GitHub token for $BROKER_SANDBOX restored from the keychain (factory-github)"
  else
    echo "warning: could not register the GitHub token from the keychain for $BROKER_SANDBOX;" \
         "run 'factory secret save github' again" >&2
  fi
  return 0
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
# the keychain definitely has none (keychain_lookup status 1: never
# configured, or deliberately cleared) - revokes any stale file so the tool
# reports DENIED instead of silently continuing to work off an old
# credential. If the keychain can't be read (status 2: locked, no Secret
# Service, prompt denied), nothing is known about the entry, so the file is
# left untouched and one warning names the secret and how to fix it. A
# no-op (one note, no keychain lookup, no delivery or revocation) when NAME
# is marked manual (secret_is_manual): the whole point of `factory secret
# set/rm` is that re-running this must not overwrite or wipe what it
# delivered.
# Exit status: 2 if a revocation failed (the file may still be active, so
# the caller must not report success); 0 otherwise, including a delivery
# failure or an unreadable keychain, which are only soft warnings.
secret_sync_from_keychain() {
  local name="$1" service="factory-$1" value rc=0
  if secret_is_manual "$name"; then
    echo "note: $name is managed by 'factory secret set/rm'; leaving it as is"
    return 0
  fi
  value="$(keychain_lookup "$service")" || rc=$?
  case "$rc" in
    0) ;;
    1)
      if ! secret_revoke_file "$BROKER_SANDBOX" "$name"; then
        echo "error: could not remove the $name credential file from the broker sandbox;" \
             "it may still be active even though its keychain entry is gone" >&2
        return 2
      fi
      return 0 ;;
    *)
      keychain_unavailable_warning "$service" "the broker's $name credential"
      return 0 ;;
  esac
  if ! printf '%s' "$value" | secret_deliver_file "$BROKER_SANDBOX" "$name"; then
    echo "warning: could not deliver the $name credential to the broker sandbox" \
         "(an old broker image? see README, Upgrading, for the rebuild steps)" >&2
  fi
  return 0
}
