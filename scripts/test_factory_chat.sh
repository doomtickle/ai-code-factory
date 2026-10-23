#!/usr/bin/env bash
set -euo pipefail

# Regression tests for "factory chat" (cmd_chat in bin/factory), the
# short-cut for "sbx run --name factory-coordinator":
#
# 1. When the coordinator sandbox doesn't exist yet, "factory chat" dies
#    with a hint to run "factory start" first, and never calls "sbx run".
#    This reuses sandbox_exists (scripts/lib/common.sh), the same check
#    other commands already use, rather than a new mechanism.
# 2. When it exists, "factory chat" execs "sbx run --name
#    factory-coordinator", forwarding any extra arguments.
# 3. "factory coordinator" is accepted as an alias.
# 4. When the Coordinator is already open elsewhere (another process runs
#    `sbx run --name factory-coordinator`: the dashboard's
#    Terminal 0, another `factory chat`), it refuses rather than start a
#    second Coordinator in the same VM.
#
# Drives a fixture checkout (bin/factory + scripts/lib, with a stub
# broker/tools.txt so the roles.sh submodule check doesn't need the real
# broker/ submodule) against a fake sbx that records every invocation's
# argv. No real sbx, docker or network access needed.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_contains() { # assert_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}
assert_not_contains() {
  case "$2" in *"$3"*) bad "$1 (did not expect '$3', got: $2)" ;; *) ok "$1" ;; esac
}

# --- fixture checkout: bin/factory + scripts/lib, no real broker/ ----------
fx="$tmp/checkout"
mkdir -p "$fx/bin" "$fx/scripts/lib" "$fx/broker"
cp "$REPO_ROOT/bin/factory" "$fx/bin/factory"
cp "$REPO_ROOT"/scripts/lib/*.sh "$fx/scripts/lib/"
echo "git_fetch" > "$fx/broker/tools.txt"

# --- fake sbx: records every invocation's argv in $FAKE_SBX_ARGS, reports
# the pinned version, and lists whatever $FAKE_SBX_LIST currently holds ----
bin="$tmp/bin"
mkdir -p "$bin"
cat > "$bin/sbx" <<'FAKE_SBX'
#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_SBX_ARGS"
case "$1" in
  version) echo "sbx version: v0.45.1 testcommit" ;;
  ls) cat "$FAKE_SBX_LIST" 2>/dev/null || true ;;
  run) exit 0 ;;
esac
FAKE_SBX
chmod +x "$bin/sbx"
# fake ps: prints $FAKE_PS_OUT, so the host's real processes (say, the
# owner's own Coordinator terminal) never decide a test.
cat > "$bin/ps" <<'FAKE_PS'
#!/bin/sh
cat "$FAKE_PS_OUT" 2>/dev/null || true
FAKE_PS
chmod +x "$bin/ps"

export PATH="$bin:$PATH"
export FAKE_SBX_ARGS="$tmp/sbx-args.log"
export FAKE_SBX_LIST="$tmp/sbx-list"
export FAKE_PS_OUT="$tmp/ps-out"
export XDG_STATE_HOME="$tmp/state"

reset_run() { : > "$FAKE_SBX_ARGS"; : > "$FAKE_SBX_LIST"; : > "$FAKE_PS_OUT"; }
# run_fixture ARGS... -> the fixture's bin/factory, combined output in $F_OUT,
# exit status in $F_RC.
run_fixture() {
  set +e
  F_OUT="$("$fx/bin/factory" "$@" 2>&1 </dev/null)"
  F_RC=$?
  set -e
}

# ===========================================================================
# Part 1: no coordinator sandbox -> dies with a "factory start" hint, sbx
# "run" is never called.
# ===========================================================================
reset_run
run_fixture chat
[ "$F_RC" -ne 0 ] && ok "no sandbox: factory chat exits non-zero" \
  || bad "no sandbox: factory chat exits non-zero (got 0)"
assert_contains "no sandbox: error hints at 'factory start'" "$F_OUT" "factory start"
assert_not_contains "no sandbox: sbx run was never invoked" "$(cat "$FAKE_SBX_ARGS")" "run "

# ===========================================================================
# Part 2: the coordinator sandbox exists -> execs "sbx run --name
# factory-coordinator", forwarding extra arguments.
# ===========================================================================
reset_run
echo "factory-coordinator" > "$FAKE_SBX_LIST"
run_fixture chat
[ "$F_RC" -eq 0 ] && ok "sandbox exists: factory chat exits 0" \
  || bad "sandbox exists: factory chat exits 0 (got $F_RC: $F_OUT)"
assert_contains "sandbox exists: sbx run --name factory-coordinator was called" \
  "$(cat "$FAKE_SBX_ARGS")" "run --name factory-coordinator"

reset_run
echo "factory-coordinator" > "$FAKE_SBX_LIST"
run_fixture chat --resume
assert_contains "extra arguments are forwarded to sbx run" \
  "$(cat "$FAKE_SBX_ARGS")" "run --name factory-coordinator --resume"

# ===========================================================================
# Part 3: "factory coordinator" is accepted as an alias.
# ===========================================================================
reset_run
echo "factory-coordinator" > "$FAKE_SBX_LIST"
run_fixture coordinator
[ "$F_RC" -eq 0 ] && ok "'factory coordinator' alias exits 0" \
  || bad "'factory coordinator' alias exits 0 (got $F_RC: $F_OUT)"
assert_contains "'factory coordinator' alias calls sbx run --name factory-coordinator" \
  "$(cat "$FAKE_SBX_ARGS")" "run --name factory-coordinator"

# ===========================================================================
# Part 4: the Coordinator is already open elsewhere -> refuses, no sbx run.
# ===========================================================================
reset_run
echo "factory-coordinator" > "$FAKE_SBX_LIST"
printf '%s\n' "/usr/bin/sbx daemon start" "sbx run --name factory-coordinator" > "$FAKE_PS_OUT"
run_fixture chat
[ "$F_RC" -ne 0 ] && ok "open elsewhere: factory chat exits non-zero" \
  || bad "open elsewhere: factory chat exits non-zero (got 0)"
assert_contains "open elsewhere: says so" "$F_OUT" "already open elsewhere"
assert_not_contains "open elsewhere: sbx run was never invoked" "$(cat "$FAKE_SBX_ARGS")" "run "

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
