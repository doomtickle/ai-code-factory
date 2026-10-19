#!/usr/bin/env bash
set -euo pipefail

# Regression tests for resolve_layout (scripts/lib/common.sh), the helper
# behind `factory start --layout watch|tabs` / FACTORY_LAYOUT:
#
# 1. With nothing saved yet and neither a flag nor an environment value, it
#    defaults to "watch".
# 2. A valid flag is accepted, returned, and saved to FACTORY_LAYOUT_FILE so a
#    later call with neither flag nor env reads it back.
# 3. The flag wins over the environment value when both are given, and the
#    winner is what gets saved.
# 4. The environment value is used (and saved) when no flag is given.
# 5. An invalid flag or environment value dies with the command's usage,
#    without touching FACTORY_LAYOUT_FILE.
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

# run_resolve FLAG ENV -> stdout in $R_OUT, exit in $R_RC (never exits the
# test itself: resolve_layout calls die(), which would otherwise kill it).
run_resolve() {
  set +e
  R_OUT="$(resolve_layout "$1" "$2" 2>&1)"
  R_RC=$?
  set -e
}

# ===========================================================================
# Part 1: nothing saved, no flag, no env -> "watch".
# ===========================================================================
[ ! -e "$layout_file" ] || rm -f "$layout_file"
run_resolve "" ""
assert_eq "no flag, no env, nothing saved: exits 0" "$R_RC" "0"
assert_eq "no flag, no env, nothing saved: defaults to watch" "$R_OUT" "watch"
[ ! -e "$layout_file" ] && ok "no flag, no env, nothing saved: nothing written" \
  || bad "no flag, no env, nothing saved: nothing written (found $layout_file)"

# ===========================================================================
# Part 2: a valid flag is saved, and read back later with neither flag nor env.
# ===========================================================================
rm -f "$layout_file"
run_resolve "tabs" ""
assert_eq "flag 'tabs': exits 0" "$R_RC" "0"
assert_eq "flag 'tabs': returns tabs" "$R_OUT" "tabs"
assert_eq "flag 'tabs': saved to the layout file" "$(cat "$layout_file" 2>/dev/null || true)" "tabs"

run_resolve "" ""
assert_eq "later call, no flag, no env: reads the saved 'tabs' back" "$R_OUT" "tabs"

# ===========================================================================
# Part 3: the flag wins over the environment, and the winner is saved.
# ===========================================================================
rm -f "$layout_file"
run_resolve "watch" "tabs"
assert_eq "flag 'watch' with env 'tabs': the flag wins" "$R_OUT" "watch"
assert_eq "flag 'watch' with env 'tabs': the flag's value is what gets saved" \
  "$(cat "$layout_file" 2>/dev/null || true)" "watch"

# ===========================================================================
# Part 4: no flag, but an environment value -> used and saved.
# ===========================================================================
rm -f "$layout_file"
run_resolve "" "tabs"
assert_eq "no flag, env 'tabs': uses the environment value" "$R_OUT" "tabs"
assert_eq "no flag, env 'tabs': saved" "$(cat "$layout_file" 2>/dev/null || true)" "tabs"

# ===========================================================================
# Part 5: an invalid flag or environment value dies with the usage, and
# never touches the layout file.
# ===========================================================================
rm -f "$layout_file"
run_resolve "bogus" ""
[ "$R_RC" -ne 0 ] && ok "invalid flag: exits non-zero" || bad "invalid flag: exits non-zero (got 0)"
assert_contains "invalid flag: error names the usage" "$R_OUT" "usage: factory start"
assert_contains "invalid flag: usage lists --layout watch|tabs" "$R_OUT" "--layout watch|tabs"
[ ! -e "$layout_file" ] && ok "invalid flag: nothing written" \
  || bad "invalid flag: nothing written (found $layout_file)"

printf 'watch\n' > "$layout_file"  # a prior valid save, to prove it's untouched below
run_resolve "" "bogus"
[ "$R_RC" -ne 0 ] && ok "invalid env value: exits non-zero" || bad "invalid env value: exits non-zero (got 0)"
assert_contains "invalid env value: error names the usage" "$R_OUT" "usage: factory start"
assert_eq "invalid env value: the previously saved layout is untouched" \
  "$(cat "$layout_file")" "watch"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
