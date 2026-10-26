# The keychain lookups bin/factory's keychain_warn_missing still needs (for
# `start`): keychain_has and what it calls. Everything else about the
# credentials sbx cannot proxy-inject (the named exception in CLAUDE.md,
# "Credentials") is Python now: factory/secrets.py and factory/keychain.py,
# which `factory secret` and scripts/start-broker.sh (factory/broker_start.py)
# use. factory/keychain.py is the port of these functions; factory/test_secrets.py
# and factory/test_keychain.py run both on the same fakes. This file goes when
# `start` is ported (docs/py-cli-migration.md, step 7). Source after common.sh.
# shellcheck shell=bash

readonly FACTORY_SECRET_NAMES="jira gemini npm-registry gitlab approvals-webhook"

# The names `factory secret save/forget` accept: the files above, plus "github",
# which is not a file but the broker-scoped sbx secret the egress proxy
# injects. Keep in sync with factory/secrets.py and factory/keychain.py (a test
# compares them).
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

# wincred_run lookup SERVICE -> run the Credential Manager lookup through
# Windows PowerShell; the value travels only on stdout, never in argv. Only
# the lookup is left here (store and delete are in factory/keychain.py, whose
# scripts this one matches). SERVICE is embedded in the script, so it must be
# one of ours: "factory-" and a valid keychain name, nothing else.
wincred_run() {
  local op="$1" service="$2" ps body
  case "$service" in factory-*) ;; *) return 1 ;; esac
  keychain_name_valid "${service#factory-}" || return 1
  ps="$(wincred_powershell)"
  [ -n "$ps" ] || return 1
  case "$op" in
    lookup) body="try { \$c = \$v.Retrieve('$service', 'factory') } catch { exit 0 }
\$c.RetrievePassword(); [Console]::Out.Write(\$c.Password)" ;;
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
