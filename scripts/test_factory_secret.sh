#!/usr/bin/env bash
set -euo pipefail

# Regression tests for "factory secret set/rm" (bin/factory) and the
# shared delivery helpers in scripts/lib/secrets.sh:
#
# 1. An invalid name (including path-like input) is rejected before sbx is
#    touched at all.
# 2. The secret value reaches sbx only on stdin, never in argv.
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

# --- fake sbx ----------------------------------------------------------------
# Records every invocation's argv (one line each, in $FAKE_SBX_ARGS) and, for
# "exec -i" calls (the only ones that pipe anything in), the raw stdin bytes
# (overwriting $FAKE_SBX_STDIN each time - tests only ever make one such call
# at a time). Reports the broker sandbox as running and sbx as the pinned
# version, so require_sbx/sandbox_running pass without a real daemon.
mkdir -p "$tmp/bin"
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
esac
FAKE_SBX
chmod +x "$tmp/bin/sbx"

export PATH="$tmp/bin:$PATH"
export XDG_STATE_HOME="$tmp/state"
export FAKE_SBX_ARGS="$tmp/sbx-args.log"
export FAKE_SBX_STDIN="$tmp/sbx-stdin.log"
BROKER_STATE="$tmp/state/ai-code-factory/broker"
MARKER_DIR="$BROKER_STATE/secrets-manual"

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

reset_fakes() { : > "$FAKE_SBX_ARGS"; rm -f "$FAKE_SBX_STDIN"; }

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
# This legitimately marks npm-registry as manual; Part 5 below clears it
# again before using that name to test the unmarked path.

# ===========================================================================
# Part 4: "secret set" marks the name as manual (no value in the marker);
# "secret rm" removes the remote file and the marker.
# ===========================================================================
reset_fakes
run_factory_with_stdin $'lic-key-xyz\n' secret set passdesk-license
assert_eq "secret set passdesk-license: exits 0" "$F_RC" "0"
[ -e "$MARKER_DIR/passdesk-license" ] \
  && ok "secret set passdesk-license: marker file created" \
  || bad "secret set passdesk-license: marker file created"
assert_not_contains "secret set passdesk-license: marker never contains the value" \
  "$(cat "$MARKER_DIR/passdesk-license" 2>/dev/null || true)" "lic-key-xyz"

reset_fakes
run_factory secret rm passdesk-license
assert_eq "secret rm passdesk-license: exits 0" "$F_RC" "0"
assert_contains "secret rm passdesk-license: removes the right remote path" \
  "$(cat "$FAKE_SBX_ARGS")" "exec factory-broker rm -f /etc/factory-broker/secrets/passdesk-license"
[ ! -e "$MARKER_DIR/passdesk-license" ] \
  && ok "secret rm passdesk-license: marker removed" \
  || bad "secret rm passdesk-license: marker removed"

# rm without a prior "set" (no marker) still removes the remote file cleanly.
reset_fakes
run_factory secret rm approvals-webhook
assert_eq "secret rm approvals-webhook (no prior marker): exits 0" "$F_RC" "0"
assert_contains "secret rm approvals-webhook: removes the right remote path" \
  "$(cat "$FAKE_SBX_ARGS")" "exec factory-broker rm -f /etc/factory-broker/secrets/approvals-webhook"

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
rm -f "$MARKER_DIR/npm-registry"  # the embedded-newline test above legitimately marked it
run_sync npm-registry
assert_eq "sync: a failed revocation returns exit status 2" "$S_RC" "2"
assert_contains "sync: a failed revocation is reported as an error" "$S_OUT" "could not remove"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
