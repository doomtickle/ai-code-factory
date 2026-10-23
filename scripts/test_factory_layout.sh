#!/usr/bin/env bash
set -euo pipefail

# Regression tests for resolve_layout (scripts/lib/common.sh), the helper
# behind `factory start --layout web` / FACTORY_LAYOUT. The dashboard ("web")
# is the only layout:
#
# 1. With nothing saved and neither a flag nor an environment value it returns
#    "web" quietly and writes nothing.
# 2. "web" from the flag or the environment is accepted and nothing is saved.
# 3. The removed "watch" and "tabs" layouts, from the flag, the environment or
#    a saved layout file, fall back to "web" with a one-line warning and exit
#    0; a stale saved file is removed so the warning shows once.
# 4. Any other flag or environment value dies with the command's usage; any
#    other saved value is ignored.
# 5. bin/factory ends a start in the dashboard, which gets the Coordinator's name.
# 6. coordinator_attached, which keeps a second `sbx run` on
#    factory-coordinator from starting (`factory chat`, the dashboard), finds
#    every form that names the sandbox (--name NAME, --name=NAME, a bare
#    positional NAME, global flags before `run`) and nothing else.
# 7. `factory update` never ends in the foreground dashboard: it passes
#    --no-dashboard to cmd_start, which then prints how to open it.
# 8. local_settings_present ignores a lone layout file.
#
# Sources scripts/lib/common.sh directly (not bin/factory): bin/factory
# itself sources scripts/lib/roles.sh for every command but "docs"/"help",
# which needs the broker/ submodule initialized - irrelevant to resolve_layout
# and not guaranteed in every checkout this test runs in.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { # assert_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}

export XDG_CONFIG_HOME="$tmp/config"
export XDG_STATE_HOME="$tmp/state"  # common.sh reads this too; keep it out of the way

# shellcheck source=lib/common.sh
. "$REPO_ROOT/scripts/lib/common.sh"

layout_file="$FACTORY_CONFIG/layout"

# run_resolve FLAG ENV -> stdout in $R_OUT, stderr in $R_ERR, exit in $R_RC
# (never exits the test itself: resolve_layout calls die() on bad input).
run_resolve() {
  set +e
  R_OUT="$(resolve_layout "$1" "$2" 2>"$tmp/err")"
  R_RC=$?
  set -e
  R_ERR="$(cat "$tmp/err")"
}

# ===========================================================================
# Part 1: nothing saved, no flag, no env -> "web", quietly, nothing written.
# ===========================================================================
rm -f "$layout_file"
run_resolve "" ""
assert_eq "no flag, no env, nothing saved: exits 0" "$R_RC" "0"
assert_eq "no flag, no env, nothing saved: web (the dashboard)" "$R_OUT" "web"
assert_eq "no flag, no env, nothing saved: no warning" "$R_ERR" ""
[ ! -e "$layout_file" ] && ok "no flag, no env, nothing saved: nothing written" \
  || bad "no flag, no env, nothing saved: nothing written (found $layout_file)"

# ===========================================================================
# Part 2: "web" from the flag or the environment; nothing is saved.
# ===========================================================================
run_resolve "web" ""
assert_eq "flag 'web': returns web" "$R_OUT" "web"
assert_eq "flag 'web': no warning" "$R_ERR" ""
run_resolve "" "web"
assert_eq "env 'web': returns web" "$R_OUT" "web"
[ ! -e "$layout_file" ] && ok "'web': nothing saved" || bad "'web': nothing saved (found $layout_file)"

# ===========================================================================
# Part 3: the removed layouts fall back to web with a one-line warning.
# ===========================================================================
for old in watch tabs; do
  run_resolve "$old" ""
  assert_eq "flag '$old': exits 0" "$R_RC" "0"
  assert_eq "flag '$old': falls back to web" "$R_OUT" "web"
  assert_contains "flag '$old': warns" "$R_ERR" "warning: the '$old' layout is gone"
  assert_eq "flag '$old': the warning is one line" "$(printf '%s\n' "$R_ERR" | wc -l | tr -d ' ')" "1"
  run_resolve "" "$old"
  assert_eq "env '$old': falls back to web" "$R_OUT" "web"
  assert_contains "env '$old': warns" "$R_ERR" "warning: the '$old' layout is gone"
  [ ! -e "$layout_file" ] && ok "flag/env '$old': nothing saved" || bad "flag/env '$old': nothing saved"
  mkdir -p "$FACTORY_CONFIG"
  printf '%s\n' "$old" > "$layout_file"
  run_resolve "" ""
  assert_eq "saved '$old': exits 0" "$R_RC" "0"
  assert_eq "saved '$old': falls back to web" "$R_OUT" "web"
  assert_contains "saved '$old': warns" "$R_ERR" "your saved '$old' layout is gone"
  assert_eq "saved '$old': the warning is one line" "$(printf '%s\n' "$R_ERR" | wc -l | tr -d ' ')" "1"
  [ ! -e "$layout_file" ] && ok "saved '$old': the stale file is removed" || bad "saved '$old': the stale file is removed"
  run_resolve "" ""
  assert_eq "saved '$old': the second run is quiet" "$R_ERR" ""
done

# ===========================================================================
# Part 4: anything else in the flag or env dies with the usage; a junk saved
# value is ignored.
# ===========================================================================
run_resolve "bogus" ""
[ "$R_RC" -ne 0 ] && ok "invalid flag: exits non-zero" || bad "invalid flag: exits non-zero (got 0)"
assert_contains "invalid flag: error names the usage" "$R_ERR" "usage: factory start"
assert_contains "invalid flag: usage lists --layout web" "$R_ERR" "--layout web"
run_resolve "" "bogus"
[ "$R_RC" -ne 0 ] && ok "invalid env value: exits non-zero" || bad "invalid env value: exits non-zero (got 0)"
assert_contains "invalid env value: error names the usage" "$R_ERR" "usage: factory start"
printf 'bogus\n' > "$layout_file"
run_resolve "" ""
assert_eq "a junk saved value: web" "$R_OUT" "web"
assert_eq "a junk saved value: quiet" "$R_ERR" ""
rm -f "$layout_file"

# ===========================================================================
# Part 5: bin/factory ends a start in the dashboard.
# ===========================================================================
grep -q -- '--coordinator "$COORDINATOR_SANDBOX"' "$REPO_ROOT/bin/factory" \
  && ok "bin/factory: the dashboard gets the Coordinator's name" \
  || bad "bin/factory: the dashboard gets the Coordinator's name"

# ===========================================================================
# Part 6: coordinator_attached matches `sbx run --name factory-coordinator` only.
# ===========================================================================
check_attached() { # check_attached LABEL EXPECTED(0|1) PS-LINES...
  local label="$1" want="$2" got
  shift 2
  FAKE_PS="$(printf '%s\n' "$@")"
  ps() { printf '%s\n' "$FAKE_PS"; }
  if coordinator_attached; then got=0; else got=1; fi
  unset -f ps
  assert_eq "coordinator_attached: $label" "$got" "$want"
}
check_attached "a plain sbx run" 0 "/sbin/init" "sbx run --name factory-coordinator"
check_attached "a full path, extra args" 0 "/usr/local/bin/sbx run --name factory-coordinator --resume"
check_attached "--name=" 0 "sbx run --name=factory-coordinator"
check_attached "positional sandbox name" 0 "sbx run factory-coordinator"
check_attached "agent then --name" 0 "sbx run claude --name factory-coordinator"
check_attached "global flag before run" 0 "sbx -D run --name factory-coordinator"
check_attached "flags before a positional name" 0 "/opt/bin/sbx run -e X=1 factory-coordinator"
check_attached "after -- (an agent argument)" 1 "sbx run claude -- factory-coordinator"
check_attached "sbx ls with the name" 1 "sbx ls factory-coordinator"
check_attached "positional, another sandbox" 1 "sbx --debug run factory-coordinator-old"
check_attached "another sandbox" 1 "sbx run --name factory-coordinator-old"
check_attached "grep for it" 1 "grep sbx run --name factory-coordinator"
check_attached "sbx exec" 1 "sbx exec factory-coordinator true"
check_attached "nothing" 1 "/usr/bin/sbx daemon start"

# ===========================================================================
# Part 7: `factory update` doesn't end in the foreground dashboard.
# ===========================================================================
grep -q 'cmd_start "$project" --no-dashboard' "$REPO_ROOT/bin/factory" \
  && ok "bin/factory: cmd_update starts the factory with --no-dashboard" \
  || bad "bin/factory: cmd_update starts the factory with --no-dashboard"
awk '/^cmd_start\(\)/,/^}/' "$REPO_ROOT/bin/factory" > "$tmp/cmd_start"
grep -q -- '--no-dashboard) no_dashboard=1' "$tmp/cmd_start" \
  && grep -q 'Run ${B}factory dashboard${N} to open it.' "$tmp/cmd_start" \
  && ok "bin/factory: cmd_start --no-dashboard prints how to open the dashboard instead" \
  || bad "bin/factory: cmd_start --no-dashboard prints how to open the dashboard instead"

# ===========================================================================
# Part 8: local_settings_present (what `factory status` reports as "Local settings").
# ===========================================================================
rm -rf "$FACTORY_CONFIG"
mkdir -p "$FACTORY_CONFIG"
printf 'web\n' > "$layout_file"
local_settings_present && bad "local settings: a lone layout file doesn't count" \
  || ok "local settings: a lone layout file doesn't count"
for item in policy.toml broker-kit roles projects plugins; do
  rm -rf "${FACTORY_CONFIG:?}/$item"
  case "$item" in
    *.toml) : > "$FACTORY_CONFIG/$item" ;;
    *) mkdir "$FACTORY_CONFIG/$item" ;;
  esac
  local_settings_present && ok "local settings: $item counts" || bad "local settings: $item counts"
  rm -rf "${FACTORY_CONFIG:?}/$item"
done

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
