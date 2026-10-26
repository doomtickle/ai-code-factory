#!/usr/bin/env bash
set -euo pipefail

# Tests for "factory restart" that need the real bash around the Python command
# (factory/commands/runstate.py; its own tests are factory/test_command_runstate.py),
# which the dashboard's RESTART key runs:
#
# 1. `factory restart` through the bin/factory shim runs 03-stop-factory.sh, then
#    the real bash `start` for the bound project (02-launch-factory.sh), in that
#    order, and with the web layout (the default) it doesn't run the dashboard in
#    the foreground: it prints how to open it and returns.
# 2. A stale saved layout ("watch", from an older version) falls back to the
#    dashboard layout with a warning and doesn't stop the restart.
# 3. The real 03-stop-factory.sh (which `factory stop`, `restart`, `reset` and
#    `update` run, and README.md names for stopping a copy by hand) kills only
#    the dispatch bridge's pid (a process running "$ROOT/dispatch/server.py") and
#    stops the factory-* sandboxes; a running dashboard survives it, so the control
#    unit that ran the restart is still there to show its log.
# 4. `factory help` lists restart.
#
# This stays shell until `start` is ported (docs/py-cli-migration.md): it drives a
# fixture checkout (bin/factory + factory/ + scripts/lib, stub 02-launch-factory.sh
# and 03-stop-factory.sh that record their calls, a stub broker/tools.txt) against a
# fake sbx and ps. Part 3 runs the real 03-stop-factory.sh against real (sleeping)
# processes and a fake sbx. No real sbx, docker or network access needed.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(cd "$(mktemp -d)" && pwd -P)"
pids=""
cleanup() {
  local p
  for p in $pids; do kill "$p" 2>/dev/null || true; done
  rm -rf "$tmp"
}
trap cleanup EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { # assert_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}
assert_not_contains() {
  case "$2" in *"$3"*) bad "$1 (did not expect '$3', got: $2)" ;; *) ok "$1" ;; esac
}

# --- fixture checkout --------------------------------------------------------
fx="$tmp/checkout"
mkdir -p "$fx/bin" "$fx/scripts/lib" "$fx/broker" "$fx/roles"
cp "$REPO_ROOT/bin/factory" "$fx/bin/factory"
cp -R "$REPO_ROOT/factory" "$fx/factory"
find "$fx/factory" -name __pycache__ -prune -exec rm -rf {} +
cp "$REPO_ROOT"/scripts/lib/*.sh "$fx/scripts/lib/"
echo "git_fetch" > "$fx/broker/tools.txt"
# Stubs that record each call in $CALLS.
cat > "$fx/scripts/03-stop-factory.sh" <<'STUB'
#!/bin/sh
echo "stop" >> "$CALLS"
echo "Factory stopped."
STUB
cat > "$fx/scripts/02-launch-factory.sh" <<'STUB'
#!/bin/sh
echo "launch $1 no_ui=${FACTORY_NO_UI:-}" >> "$CALLS"
STUB
cat > "$fx/scripts/dashboard.py" <<'STUB'
import os
with open(os.environ["CALLS"], "a") as f:
    f.write("dashboard\n")
STUB
chmod +x "$fx/scripts/03-stop-factory.sh" "$fx/scripts/02-launch-factory.sh"

# --- fake sbx and ps --------------------------------------------------
bin="$tmp/bin"
mkdir -p "$bin"
cat > "$bin/sbx" <<FAKE_SBX
#!/bin/sh
printf '%s\n' "\$*" >> "\$FAKE_SBX_ARGS"
case "\$1" in
  version) echo "sbx version: v$(sed -n 's/^readonly SBX_VERSION="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/lib/common.sh") testcommit" ;;
  settings) echo true ;;
  template) printf '%s\n' factory-developer:v3 factory-reviewer:v3 factory-broker:v3 ;;
  ls)
    if [ "\${2:-}" = --json ]; then
      echo '{"sandboxes":[{"name":"factory-broker","status":"running"},{"name":"factory-developer","status":"stopped"},{"name":"other","status":"running"}]}'
    fi ;;
  secret) echo "(global)  oauth  anthropic  configured" ;;
esac
FAKE_SBX
chmod +x "$bin/sbx"

export PATH="$bin:$PATH"
export FAKE_SBX_ARGS="$tmp/sbx-args.log"
export CALLS="$tmp/calls.log"
export XDG_STATE_HOME="$tmp/state"
export XDG_CONFIG_HOME="$tmp/config"
unset FACTORY_LAYOUT FACTORY_NO_UI

project="$tmp/project"
mkdir -p "$project" "$tmp/state/ai-code-factory/broker"
bound="$tmp/state/ai-code-factory/broker/project"

reset_run() { : > "$FAKE_SBX_ARGS"; : > "$CALLS"; }
run_fixture() {
  set +e
  F_OUT="$("$fx/bin/factory" "$@" 2>&1 </dev/null)"
  F_RC=$?
  set -e
}

# ===========================================================================
# Part 1: a bound project -> stop, then start it again; no foreground dashboard.
# ===========================================================================
reset_run
printf '%s\n' "$project" > "$bound"
run_fixture restart
assert_eq "bound project: factory restart exits 0" "$F_RC" "0"
assert_eq "bound project: stops, then launches the bound project" \
  "$(cat "$CALLS")" "stop
launch $project no_ui="
assert_contains "bound project: says how to open the dashboard instead of running it" \
  "$F_OUT" "factory dashboard"
assert_not_contains "bound project: the dashboard isn't run in the foreground" "$(cat "$CALLS")" "dashboard"

# ===========================================================================
# Part 2: a stale saved layout falls back to the dashboard, with a warning.
# ===========================================================================
reset_run
printf '%s\n' "$project" > "$bound"
mkdir -p "$tmp/config/ai-code-factory"
printf 'watch\n' > "$tmp/config/ai-code-factory/layout"
run_fixture restart
assert_eq "saved watch layout: exits 0" "$F_RC" "0"
assert_contains "saved watch layout: warns" "$F_OUT" "your saved 'watch' layout is gone"
assert_contains "saved watch layout: still stops, then launches" "$(cat "$CALLS")" "stop
launch $project no_ui="
rm -f "$tmp/config/ai-code-factory/layout"

# ===========================================================================
# Part 3: the real 03-stop-factory.sh kills only the dispatch bridge.
# ===========================================================================
real="$tmp/real"
mkdir -p "$real/scripts/lib" "$real/dispatch"
cp "$REPO_ROOT/scripts/03-stop-factory.sh" "$real/scripts/"
cp "$REPO_ROOT"/scripts/lib/*.sh "$real/scripts/lib/"
# Stand-ins, by command line: the dispatch bridge and a dashboard.
printf 'import time\ntime.sleep(60)\n' > "$real/dispatch/server.py"
cp "$real/dispatch/server.py" "$real/scripts/dashboard.py"
python3 "$real/dispatch/server.py" &
bridge=$!
python3 "$real/scripts/dashboard.py" &
dash=$!
pids="$bridge $dash"
mkdir -p "$tmp/state/ai-code-factory/dispatch"
echo "$bridge" > "$tmp/state/ai-code-factory/dispatch/pid"
reset_run
set +e
S_OUT="$("$real/scripts/03-stop-factory.sh" 2>&1)"
S_RC=$?
set -e
assert_eq "03-stop-factory.sh exits 0" "$S_RC" "0"
for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$bridge" 2>/dev/null || break; sleep 0.2; done
if kill -0 "$bridge" 2>/dev/null; then bad "03-stop-factory.sh kills the dispatch bridge"
else ok "03-stop-factory.sh kills the dispatch bridge"; fi
if kill -0 "$dash" 2>/dev/null; then ok "03-stop-factory.sh leaves the dashboard running"
else bad "03-stop-factory.sh leaves the dashboard running"; fi
assert_contains "03-stop-factory.sh stops the running factory-* sandboxes" "$(cat "$FAKE_SBX_ARGS")" "stop factory-broker"
assert_not_contains "03-stop-factory.sh leaves other sandboxes alone" "$(grep '^stop' "$FAKE_SBX_ARGS")" "other"
assert_contains "03-stop-factory.sh says so" "$S_OUT" "Factory stopped."

# A pid file naming another process (say, the dashboard after a pid reuse) kills nothing.
echo "$dash" > "$tmp/state/ai-code-factory/dispatch/pid"
"$real/scripts/03-stop-factory.sh" >/dev/null 2>&1 || true
sleep 0.2
if kill -0 "$dash" 2>/dev/null; then ok "a pid file naming the dashboard doesn't kill it"
else bad "a pid file naming the dashboard doesn't kill it"; fi

# ===========================================================================
# Part 4: usage.
# ===========================================================================
assert_contains "factory help lists restart" "$("$fx/bin/factory" help 2>&1)" "factory restart"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
