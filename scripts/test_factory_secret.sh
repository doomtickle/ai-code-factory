#!/usr/bin/env bash
set -euo pipefail

# Regression tests for "factory secret set/rm" (bin/factory) and the
# shared delivery helpers in scripts/lib/secrets.sh:
#
# 1. An invalid name (including path-like and multi-word input) is rejected
#    before sbx is touched at all.
# 2. The secret value reaches sbx only on stdin, never in argv, and lands
#    nowhere else under the test's temp dir.
# 3. A single trailing newline on stdin is stripped (so
#    "echo token | factory secret set jira" works); an empty value is
#    rejected.
# 4. "factory secret set" marks the name as manually managed (an empty
#    marker file, never the value) and "factory secret rm" removes both the
#    remote file and the marker.
# 5. scripts/start-broker.sh's per-name keychain sync
#    (secret_sync_from_keychain, scripts/lib/secrets.sh) skips both
#    delivery and revocation for a manually-managed name, and still revokes
#    an unmarked name whose keychain entry is empty - exercised directly,
#    since running the whole of start-broker.sh needs sbx/curl/jq/openssl
#    and a real broker sandbox.
# 6. A delivery failure (the remote "sbx exec -i" call fails) makes
#    "factory secret set" die without creating a marker.
# 7. secret_clear_all_manual (scripts/lib/secrets.sh), called by
#    scripts/start-broker.sh only when it creates a brand new broker
#    sandbox, removes every marker and reports each name it cleared.
# 8. "factory secret save/forget" with the Windows Credential Manager
#    backend (WSL2), driven through a fake powershell.exe: the value reaches
#    PowerShell only on stdin, a saved file credential is delivered to the
#    running broker and not marked manual, "github" becomes the
#    broker-scoped sbx secret (on stdin, never global), and a service name
#    that isn't ours never reaches PowerShell.
#
# Drives the real bin/factory as a subprocess (not sourced, since the
# command dispatch under test runs only in that mode - see the guard at the
# bottom of bin/factory), against a disposable FACTORY_STATE and a fake sbx
# on PATH that records every invocation's argv and the last call's stdin.
# No sbx, docker, or network access needed. There is no secret-tool/security
# binary on PATH either, so keychain_lookup (scripts/lib/secrets.sh) always
# reports "no value" here - exactly the state this test needs for part 5.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_empty() { [ -z "$2" ] && ok "$1" || bad "$1 (expected empty, got: $2)"; }
assert_contains() { # assert_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}
assert_not_contains() { # assert_not_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) bad "$1 (expected NOT to contain '$3', got: $2)" ;; *) ok "$1" ;; esac
}
# assert_value_not_leaked LABEL NEEDLE -> NEEDLE appears in no file under
# $tmp except the fake sbx's recorded stdin (FAKE_SBX_STDIN), where a
# delivered value legitimately lands (and, for part 8, the fake PowerShell's
# stdin and its fake credential store).
assert_value_not_leaked() {
  local hits
  hits="$(grep -rlF -- "$2" "$tmp" 2>/dev/null \
    | grep -vxF -e "$FAKE_SBX_STDIN" -e "$FAKE_SBX_SECRET_STDIN" -e "$tmp/ps-stdin.log" \
    | grep -v "^$tmp/wincred/" || true)"
  assert_empty "$1" "$hits"
}

# --- fake sbx ----------------------------------------------------------------
# Records every invocation's argv (one line each, in $FAKE_SBX_ARGS) and, for
# "exec -i" calls (the only ones that pipe anything in), the raw stdin bytes
# (overwriting $FAKE_SBX_STDIN each time - tests only ever make one such call
# at a time). Reports the broker sandbox as running and sbx as the pinned
# version, so require_sbx/sandbox_running pass without a real daemon.
mkdir -p "$tmp/bin"
# install_good_fake_sbx -> (re)install the well-behaved fake sbx above, so a
# test that swaps in a failing one can restore it afterwards.
install_good_fake_sbx() {
cat > "$tmp/bin/sbx" <<'FAKE_SBX'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_SBX_ARGS"
case "$1" in
  version) echo "sbx version: v0.45.1 testcommit" ;;
  ls) echo "factory-broker   template   running" ;;
  exec)
    shift
    if [ "$1" = "-i" ]; then
      shift; shift  # drop -i and the sandbox name
      cat > "$FAKE_SBX_STDIN"
    fi
    ;;
  secret) [ "${2:-}" != set ] || cat > "$FAKE_SBX_SECRET_STDIN" ;;
esac
FAKE_SBX
chmod +x "$tmp/bin/sbx"
}
install_good_fake_sbx

export PATH="$tmp/bin:$PATH"
export XDG_STATE_HOME="$tmp/state"
export FAKE_SBX_ARGS="$tmp/sbx-args.log"
export FAKE_SBX_STDIN="$tmp/sbx-stdin.log"
export FAKE_SBX_SECRET_STDIN="$tmp/sbx-secret-stdin.log"
# No keychain unless a part asks for one: on a WSL2 machine the real Windows
# Credential Manager would otherwise be found and queried.
export FACTORY_KEYCHAIN=none
FACTORY_STATE_DIR="$tmp/state/ai-code-factory"
MARKER_DIR="$FACTORY_STATE_DIR/secrets-manual"

# run_factory ARGS... -> combined output in $F_OUT, exit in $F_RC, stdin closed.
# run_factory_with_stdin VALUE ARGS... -> same, with VALUE on stdin. Piping
# straight into a shell function would run it in a subshell (bash runs every
# non-last stage of a pipeline, and - without "shopt -s lastpipe" - the last
# stage too, in a subshell), silently losing the F_OUT/F_RC assignments; VALUE
# is a parameter instead, and the pipe is confined to the command
# substitution inside the function, which does not have that problem.
run_factory() {
  set +e
  F_OUT="$("$REPO_ROOT/bin/factory" "$@" 2>&1 </dev/null)"
  F_RC=$?
  set -e
}

run_factory_with_stdin() {
  local value="$1"
  shift
  set +e
  F_OUT="$(printf '%s' "$value" | "$REPO_ROOT/bin/factory" "$@" 2>&1)"
  F_RC=$?
  set -e
}

reset_fakes() { : > "$FAKE_SBX_ARGS"; rm -f "$FAKE_SBX_STDIN" "$FAKE_SBX_SECRET_STDIN"; }

# ===========================================================================
# Part 1: an invalid name is rejected before sbx is touched.
# ===========================================================================
reset_fakes
run_factory secret set ../etc-passwd </dev/null
[ "$F_RC" -ne 0 ] && ok "path-like name: factory secret set exits non-zero" \
  || bad "path-like name: factory secret set exits non-zero (got 0)"
assert_contains "path-like name: error names the problem" "$F_OUT" "unknown secret"
assert_empty "path-like name: sbx was never invoked" "$(cat "$FAKE_SBX_ARGS")"

reset_fakes
run_factory secret set not-a-real-secret </dev/null
assert_contains "unknown secret name is rejected" "$F_OUT" "unknown secret"
assert_empty "unknown secret name: sbx was never invoked" "$(cat "$FAKE_SBX_ARGS")"

reset_fakes
run_factory secret rm ../etc-passwd
assert_contains "secret rm: path-like name is rejected" "$F_OUT" "unknown secret"
assert_empty "secret rm: path-like name: sbx was never invoked" "$(cat "$FAKE_SBX_ARGS")"

# A space-joined pair of valid names must not pass just because the
# space-padded FACTORY_SECRET_NAMES string contains " jira " as a substring.
reset_fakes
run_factory secret set "jira gemini" </dev/null
assert_contains "multi-word name is rejected" "$F_OUT" "unknown secret"
assert_empty "multi-word name: sbx was never invoked" "$(cat "$FAKE_SBX_ARGS")"

# ===========================================================================
# Part 2 & 3: the value arrives on sbx's stdin only, a trailing newline is
# stripped, and an empty value is rejected.
# ===========================================================================
reset_fakes
run_factory_with_stdin $'topsecret123\n' secret set jira
assert_eq "secret set jira: exits 0" "$F_RC" "0"
assert_eq "secret set jira: value on sbx stdin has its trailing newline stripped" \
  "$(cat "$FAKE_SBX_STDIN")" "topsecret123"
assert_not_contains "secret set jira: the value never appears in any recorded argv" \
  "$(cat "$FAKE_SBX_ARGS")" "topsecret123"
assert_contains "secret set jira: delivery call targets the broker sandbox" \
  "$(cat "$FAKE_SBX_ARGS")" "exec -i factory-broker sh -c"
assert_value_not_leaked "secret set jira: value lands nowhere under the temp dir but the fake sbx's stdin" \
  "topsecret123"

reset_fakes
run_factory_with_stdin '' secret set gemini
[ "$F_RC" -ne 0 ] && ok "secret set gemini: empty value exits non-zero" \
  || bad "secret set gemini: empty value exits non-zero (got 0)"
assert_contains "secret set gemini: empty value error is clear" "$F_OUT" "empty value"
assert_eq "secret set gemini: empty value never reaches an exec -i call" \
  "$(grep -c '^exec -i' "$FAKE_SBX_ARGS" || true)" "0"

# A value with embedded (non-trailing) newlines keeps them; only the one
# trailing newline from "echo" is stripped.
reset_fakes
run_factory_with_stdin $'line1\nline2\n' secret set npm-registry
assert_eq "secret set npm-registry: embedded newline is preserved, only the trailing one is stripped" \
  "$(cat "$FAKE_SBX_STDIN")" "$(printf 'line1\nline2')"
assert_value_not_leaked "secret set npm-registry: value lands nowhere under the temp dir but the fake sbx's stdin" \
  "line1"
# This legitimately marks npm-registry as manual; Part 4c below clears every
# marker, so later parts find it unmarked again.

# ===========================================================================
# Part 4: "secret set" marks the name as manual (no value in the marker);
# "secret rm" removes the remote file and the marker.
# ===========================================================================
reset_fakes
run_factory_with_stdin $'gl-tok-xyz\n' secret set gitlab
assert_eq "secret set gitlab: exits 0" "$F_RC" "0"
[ -e "$MARKER_DIR/gitlab" ] \
  && ok "secret set gitlab: marker file created" \
  || bad "secret set gitlab: marker file created"
assert_not_contains "secret set gitlab: marker never contains the value" \
  "$(cat "$MARKER_DIR/gitlab" 2>/dev/null || true)" "gl-tok-xyz"
assert_value_not_leaked "secret set gitlab: value lands nowhere under the temp dir but the fake sbx's stdin" \
  "gl-tok-xyz"

reset_fakes
run_factory secret rm gitlab
assert_eq "secret rm gitlab: exits 0" "$F_RC" "0"
assert_contains "secret rm gitlab: removes the right remote path" \
  "$(cat "$FAKE_SBX_ARGS")" "exec factory-broker rm -f /etc/factory-broker/secrets/gitlab"
[ ! -e "$MARKER_DIR/gitlab" ] \
  && ok "secret rm gitlab: marker removed" \
  || bad "secret rm gitlab: marker removed"

# The retired licence name is refused: the gateway no longer has one.
reset_fakes
run_factory_with_stdin $'lic-key-xyz\n' secret set passdesk-license
[ "$F_RC" -ne 0 ] \
  && ok "secret set passdesk-license: refused (no such credential any more)" \
  || bad "secret set passdesk-license: refused (no such credential any more)"
assert_value_not_leaked "secret set passdesk-license: the refused value lands nowhere" "lic-key-xyz"

# rm without a prior "set" (no marker) still removes the remote file cleanly.
reset_fakes
run_factory secret rm approvals-webhook
assert_eq "secret rm approvals-webhook (no prior marker): exits 0" "$F_RC" "0"
assert_contains "secret rm approvals-webhook: removes the right remote path" \
  "$(cat "$FAKE_SBX_ARGS")" "exec factory-broker rm -f /etc/factory-broker/secrets/approvals-webhook"

# ===========================================================================
# Part 4b: a delivery failure (the remote "sbx exec -i" call fails) makes
# "factory secret set" die without creating a marker - otherwise the next
# "factory start" would skip a name secret_sync_from_keychain should be free
# to manage, believing it was successfully handed to "factory secret set".
# ===========================================================================
cat > "$tmp/bin/sbx" <<'FAKE_SBX_DELIVER_FAIL'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_SBX_ARGS"
case "$1" in
  version) echo "sbx version: v0.45.1 testcommit" ;;
  ls) echo "factory-broker   template   running" ;;
  exec)
    shift
    if [ "$1" = "-i" ]; then
      shift; shift  # drop -i and the sandbox name
      cat > "$FAKE_SBX_STDIN"
      exit 1
    fi
    ;;
esac
FAKE_SBX_DELIVER_FAIL
chmod +x "$tmp/bin/sbx"

reset_fakes
rm -f "$MARKER_DIR/gemini"
run_factory_with_stdin $'willnotarrive\n' secret set gemini
[ "$F_RC" -ne 0 ] && ok "secret set gemini: a delivery failure exits non-zero" \
  || bad "secret set gemini: a delivery failure exits non-zero (got 0)"
assert_contains "secret set gemini: a delivery failure's error names the problem" \
  "$F_OUT" "could not deliver"
[ ! -e "$MARKER_DIR/gemini" ] \
  && ok "secret set gemini: a delivery failure leaves no marker" \
  || bad "secret set gemini: a delivery failure leaves no marker"

install_good_fake_sbx

# ===========================================================================
# Part 4c: secret_clear_all_manual (scripts/lib/secrets.sh) removes every
# marker and reports each name it cleared. scripts/start-broker.sh calls it
# only when it creates a brand new broker sandbox (which starts with none of
# the credentials delivered), so a marker surviving from a previous sandbox
# doesn't make secret_sync_from_keychain skip a name forever.
# ===========================================================================
run_clear_all_manual() { # -> cleared names in $CA_OUT, exit in $CA_RC
  set +e
  CA_OUT="$( (
    # shellcheck disable=SC1091
    . "$REPO_ROOT/scripts/lib/common.sh"
    # shellcheck disable=SC1091
    . "$REPO_ROOT/scripts/lib/secrets.sh"
    secret_clear_all_manual
  ) 2>&1 )"
  CA_RC=$?
  set -e
}

reset_fakes
rm -rf "$MARKER_DIR"
mkdir -p "$MARKER_DIR"
: > "$MARKER_DIR/jira"
: > "$MARKER_DIR/npm-registry"
run_clear_all_manual
assert_eq "secret_clear_all_manual: exits 0" "$CA_RC" "0"
assert_eq "secret_clear_all_manual: reports both cleared names" \
  "$(printf '%s\n' "$CA_OUT" | sort | tr '\n' ' ')" "jira npm-registry "
[ ! -e "$MARKER_DIR/jira" ] && [ ! -e "$MARKER_DIR/npm-registry" ] \
  && ok "secret_clear_all_manual: both markers removed" \
  || bad "secret_clear_all_manual: both markers removed"
assert_empty "secret_clear_all_manual: never invokes sbx" "$(cat "$FAKE_SBX_ARGS")"

reset_fakes
rm -rf "$MARKER_DIR"
run_clear_all_manual
assert_eq "secret_clear_all_manual: exits 0 with no marker dir at all" "$CA_RC" "0"
assert_empty "secret_clear_all_manual: reports nothing when there is nothing to clear" "$CA_OUT"

# ===========================================================================
# Part 5: scripts/start-broker.sh's per-name keychain sync
# (secret_sync_from_keychain) skips both delivery and revocation for a
# manually-managed name, and still revokes an unmarked name whose keychain
# entry is empty (there is no secret-tool/security on PATH in this test, so
# keychain_lookup always reports "no value").
# ===========================================================================
run_sync() { # run_sync NAME -> output in $S_OUT, exit in $S_RC
  set +e
  S_OUT="$( (
    # shellcheck disable=SC1091
    . "$REPO_ROOT/scripts/lib/common.sh"
    # shellcheck disable=SC1091
    . "$REPO_ROOT/scripts/lib/secrets.sh"
    secret_sync_from_keychain "$1"
  ) 2>&1 )"
  S_RC=$?
  set -e
}

reset_fakes
mkdir -p "$MARKER_DIR"
: > "$MARKER_DIR/jira"
run_sync jira
assert_eq "sync: marked name (jira) exits 0" "$S_RC" "0"
assert_contains "sync: marked name prints a one-line note" "$S_OUT" "managed by 'factory secret set/rm'"
assert_empty "sync: marked name never calls sbx (no delivery, no revocation)" "$(cat "$FAKE_SBX_ARGS")"

reset_fakes
rm -f "$MARKER_DIR/gemini"
run_sync gemini
assert_eq "sync: unmarked name with an empty keychain entry exits 0" "$S_RC" "0"
assert_contains "sync: unmarked empty name is revoked at the right remote path" \
  "$(cat "$FAKE_SBX_ARGS")" "exec factory-broker rm -f /etc/factory-broker/secrets/gemini"

# A revocation failure (fake sbx exits non-zero for this one "exec" call) is
# reported with exit status 2, so start-broker.sh's caller
# ("secret_sync_from_keychain NAME || revocation_failed=1") treats it as
# fatal rather than a soft warning.
cat > "$tmp/bin/sbx" <<'FAKE_SBX_FAIL'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_SBX_ARGS"
case "$1" in
  exec) exit 1 ;;
esac
FAKE_SBX_FAIL
chmod +x "$tmp/bin/sbx"
reset_fakes
rm -f "$MARKER_DIR/npm-registry"  # Part 4c already cleared it; harmless if it hadn't
run_sync npm-registry
assert_eq "sync: a failed revocation returns exit status 2" "$S_RC" "2"
assert_contains "sync: a failed revocation is reported as an error" "$S_OUT" "could not remove"

# ===========================================================================
# Part 8: the Windows Credential Manager backend (WSL2) through a fake
# powershell.exe, which records its argv and stdin and keeps a fake store
# under $tmp/wincred/<service>. It reads the operation and service from the
# script it is given (scripts/lib/secrets.sh wincred_run).
# ===========================================================================
install_good_fake_sbx
mkdir -p "$tmp/wincred"
cat > "$tmp/bin/powershell.exe" <<'FAKE_PS'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_PS_ARGS"
script="${*: -1}"
op="$(printf '%s' "$script" | sed -n 's/.*op=\([a-z]*\).*/\1/p' | head -n1)"
svc="$(printf '%s' "$script" | grep -o "Retrieve('factory-[a-z-]*'" | head -n1 | sed "s/Retrieve('//; s/'//")"
case "$op" in
  lookup) [ ! -f "$FAKE_PS_STORE/$svc" ] || cat "$FAKE_PS_STORE/$svc" ;;
  store) cat > "$FAKE_PS_STDIN"; cp "$FAKE_PS_STDIN" "$FAKE_PS_STORE/$svc" ;;
  delete) rm -f "$FAKE_PS_STORE/$svc" ;;
esac
FAKE_PS
chmod +x "$tmp/bin/powershell.exe"
export FAKE_PS_ARGS="$tmp/ps-args.log" FAKE_PS_STDIN="$tmp/ps-stdin.log" FAKE_PS_STORE="$tmp/wincred"
reset_ps() { : > "$FAKE_PS_ARGS"; rm -f "$FAKE_PS_STDIN"; }

FACTORY_KEYCHAIN=wincred
FACTORY_POWERSHELL="$tmp/bin/powershell.exe"
export FACTORY_KEYCHAIN FACTORY_POWERSHELL

reset_fakes; reset_ps
rm -rf "$MARKER_DIR"; mkdir -p "$MARKER_DIR"; : > "$MARKER_DIR/jira"
run_factory_with_stdin $'wc-secret-1\n' secret save jira
assert_eq "save jira (wincred): exits 0" "$F_RC" "0"
assert_eq "save jira: PowerShell got exactly the value on stdin" "$(cat "$FAKE_PS_STDIN")" "wc-secret-1"
assert_not_contains "save jira: the value never appears in PowerShell's argv" "$(cat "$FAKE_PS_ARGS")" "wc-secret-1"
assert_contains "save jira: stored under factory-jira" "$(cat "$FAKE_PS_ARGS")" "Retrieve('factory-jira'"
assert_eq "save jira: delivered to the running broker on stdin" "$(cat "$FAKE_SBX_STDIN" 2>/dev/null)" "wc-secret-1"
assert_not_contains "save jira: the value never appears in sbx's argv" "$(cat "$FAKE_SBX_ARGS")" "wc-secret-1"
[ ! -e "$MARKER_DIR/jira" ] && ok "save jira: the 'factory secret set' marker is cleared (the keychain manages it now)" \
  || bad "save jira: the 'factory secret set' marker is cleared"
assert_value_not_leaked "save jira: value lands nowhere else under the temp dir" "wc-secret-1"

reset_fakes; reset_ps
run_factory_with_stdin $'gh-token-abc\n' secret save github
assert_eq "save github: exits 0" "$F_RC" "0"
assert_contains "save github: registers the broker-scoped sbx secret" \
  "$(cat "$FAKE_SBX_ARGS")" "secret set -f github --sandbox factory-broker"
assert_eq "save github: the token reaches sbx on stdin" "$(cat "$FAKE_SBX_SECRET_STDIN" 2>/dev/null)" "gh-token-abc"
assert_not_contains "save github: the token never appears in sbx's argv" "$(cat "$FAKE_SBX_ARGS")" "gh-token-abc"
assert_eq "save github: never a global secret (every 'secret set' names the broker sandbox)" \
  "$(grep '^secret set' "$FAKE_SBX_ARGS" | grep -vc -- '--sandbox factory-broker' || true)" "0"

run_github_sync() { # -> output in $G_OUT
  G_OUT="$( (
    # shellcheck disable=SC1091
    . "$REPO_ROOT/scripts/lib/common.sh"
    # shellcheck disable=SC1091
    . "$REPO_ROOT/scripts/lib/secrets.sh"
    github_sync_from_keychain
  ) 2>&1 )"
}
reset_fakes; reset_ps
run_github_sync
assert_contains "github sync with a keychain entry: re-registers it (what start-broker.sh does on every start)" \
  "$(cat "$FAKE_SBX_ARGS")" "secret set -f github --sandbox factory-broker"
assert_contains "github sync with a keychain entry: says where the token came from" "$G_OUT" "restored from the keychain"
rm -f "$tmp/wincred/factory-github"
reset_fakes; reset_ps
run_github_sync
assert_empty "github sync without a keychain entry: leaves sbx alone" "$(cat "$FAKE_SBX_ARGS")"

reset_fakes; reset_ps
run_factory secret save not-a-secret
assert_contains "save: unknown name is rejected" "$F_OUT" "unknown secret"
assert_empty "save: unknown name never reaches PowerShell" "$(cat "$FAKE_PS_ARGS")"

reset_fakes; reset_ps
run_factory_with_stdin '' secret save gemini
[ "$F_RC" -ne 0 ] && ok "save: empty value exits non-zero" || bad "save: empty value exits non-zero (got 0)"
assert_empty "save: empty value is never stored" "$(cat "$FAKE_PS_ARGS")"

reset_fakes; reset_ps
run_factory secret forget jira
assert_eq "forget jira: exits 0" "$F_RC" "0"
[ ! -e "$tmp/wincred/factory-jira" ] && ok "forget jira: deleted from the keychain" || bad "forget jira: deleted from the keychain"
assert_contains "forget jira: removes the broker's file" \
  "$(cat "$FAKE_SBX_ARGS")" "exec factory-broker rm -f /etc/factory-broker/secrets/jira"

reset_ps
set +e
( . "$REPO_ROOT/scripts/lib/common.sh"; . "$REPO_ROOT/scripts/lib/secrets.sh"
  keychain_lookup "factory-jira'); Remove-Item C:\\ ; ('" ) >/dev/null 2>&1
set -e
assert_empty "wincred: a service name that isn't ours never reaches PowerShell" "$(cat "$FAKE_PS_ARGS")"

FACTORY_KEYCHAIN=none
reset_fakes; reset_ps
run_factory_with_stdin 'x' secret save jira
assert_contains "save with no keychain: says what to do instead" "$F_OUT" "no keychain"
assert_empty "save with no keychain: PowerShell is never called" "$(cat "$FAKE_PS_ARGS")"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
