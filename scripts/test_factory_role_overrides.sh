#!/usr/bin/env bash
set -euo pipefail

# Tests for local role overrides (FACTORY_CONFIG/roles/<role>.conf), the
# helpers in scripts/lib/roles.sh:
#
# 1. No override folder: role_get, role_lanes and validate_role read role.conf
#    alone, as before.
# 2. An override layers its tuning keys over role.conf (MODEL, MODELS, LANES,
#    ...); keys it doesn't set still come from role.conf, and an explicitly
#    empty value wins.
# 3. Keys outside the allowlist (BROKER_TOOLS, WORKSPACE, TEMPLATE,
#    DESCRIPTION, anything else) are refused by validate_role, naming the file
#    and key, and role_get never reads them from the override.
# 4. Override values get role.conf's validation (model names, EFFORT, LANES
#    1-8, ...), and the error names the override file.
# 5. A line that isn't KEY="value" is refused; the override is never executed.
# 6. validate_role_overrides refuses an override for a role not in roles/.
#
# Uses a fixture repo (roles/, broker/tools.txt) rather than this checkout, so
# it needs neither the broker/ submodule nor this repo's role.conf values.

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
export XDG_STATE_HOME="$tmp/state"

# shellcheck source=lib/common.sh
. "$REPO_ROOT/scripts/lib/common.sh"

ROOT="$tmp/repo"
mkdir -p "$ROOT/broker" "$ROOT/roles/developer/kit"
printf '# tools\nnpm_fetch\ngit_push\n' > "$ROOT/broker/tools.txt"
echo "name: developer" > "$ROOT/roles/developer/kit/spec.yaml"
echo "Develop." > "$ROOT/roles/developer/instructions.md"
cat > "$ROOT/roles/developer/role.conf" <<'EOF'
DESCRIPTION="Writes code"
TEMPLATE="developer"
WORKSPACE="rw"
LANES="3"
BROKER_TOOLS="npm_fetch"
MODEL="sonnet"
MODELS="haiku opus"
EFFORT=""
MAX_TURNS="200"
EOF

# shellcheck source=lib/roles.sh
. "$REPO_ROOT/scripts/lib/roles.sh"

overrides="$FACTORY_CONFIG/roles"
override="$overrides/developer.conf"

# run CMD... -> output (stdout+stderr) in $R_OUT, exit status in $R_RC; die()
# exits, so every check runs in a subshell.
run() {
  set +e
  R_OUT="$( ("$@") 2>&1)"
  R_RC=$?
  set -e
}

# 1. No override folder.
assert_eq "no override: MODEL from role.conf" "$(role_get developer MODEL)" "sonnet"
assert_eq "no override: lanes from role.conf" "$(role_lanes developer)" "3"
run validate_role developer
assert_eq "no override: validate_role passes" "$R_RC" "0"
run validate_role_overrides
assert_eq "no override: validate_role_overrides passes" "$R_RC" "0"
sed -i.bak 's/^EFFORT=""/EFFORT="max"/' "$ROOT/roles/developer/role.conf"
run validate_role developer
assert_eq "no override: role.conf error unchanged" "$R_OUT" \
  "error: role developer: EFFORT must be low, medium, high or xhigh (or empty)"
mv "$ROOT/roles/developer/role.conf.bak" "$ROOT/roles/developer/role.conf"

# 2. Merge.
mkdir -p "$overrides"
cat > "$override" <<'EOF'
# Cheaper default, more lanes.
MODELS="claude-haiku-5-5 opus claude-fable-5-1"

  # indented comment
EFFORT="high"
LANES="5"
MAX_TURNS=""
EOF
assert_eq "override: MODELS replaced" "$(role_get developer MODELS)" "claude-haiku-5-5 opus claude-fable-5-1"
assert_eq "override: EFFORT replaced" "$(role_get developer EFFORT)" "high"
assert_eq "override: lanes replaced" "$(role_lanes developer)" "5"
assert_eq "override: empty value wins" "$(role_get developer MAX_TURNS)" ""
assert_eq "override: unset key from role.conf" "$(role_get developer MODEL)" "sonnet"
assert_eq "override: role_conf_file names the override" "$(role_conf_file developer LANES)" "$override"
run validate_role developer
assert_eq "override: validate_role passes" "$R_RC" "0"

# 3. Allowlist.
for key in BROKER_TOOLS WORKSPACE TEMPLATE DESCRIPTION OTHER_KEY; do
  printf 'LANES="2"\n%s="npm_fetch git_push"\n' "$key" > "$override"
  run validate_role developer
  assert_eq "refuses $key" "$R_RC" "1"
  assert_contains "refusal names the file and $key" "$R_OUT" "$override: $key can't be set in a local override"
done
printf 'BROKER_TOOLS="npm_fetch git_push"\nWORKSPACE="ro"\n' > "$override"
assert_eq "role_get ignores BROKER_TOOLS in an override" "$(role_get developer BROKER_TOOLS)" "npm_fetch"
assert_eq "role_get ignores WORKSPACE in an override" "$(role_get developer WORKSPACE)" "rw"

# 4. Values are validated like role.conf's.
check_value() { # check_value LINE EXPECTED-MESSAGE
  printf '%s\n' "$1" > "$override"
  run validate_role developer
  assert_eq "refuses $1" "$R_RC" "1"
  assert_contains "error for $1" "$R_OUT" "$2"
  assert_contains "error for $1 names the override" "$R_OUT" "(set in $override)"
}
check_value 'MODEL="bad model"' "MODEL must contain only"
check_value 'MODELS="opus bad;model"' "MODELS entries must contain only"
check_value 'EFFORT="max"' "EFFORT must be low, medium, high or xhigh"
check_value 'LANES="9"' "LANES must be a whole number from 1 to 8"
check_value 'LANES="0"' "LANES must be a whole number from 1 to 8"
check_value 'MAX_SESSION_TOKENS="lots"' "MAX_SESSION_TOKENS must be a whole number"
check_value 'MAX_TURNS="-1"' "MAX_TURNS must be a whole number"

# 5. Strict lines; never executed.
for line in 'LANES=2' "LANES='2'" 'lanes="2"' 'export LANES="2"' '$(touch '"$tmp"'/pwned)' 'source /etc/profile'; do
  printf '%s\n' "$line" > "$override"
  run validate_role developer
  assert_eq "refuses line: $line" "$R_RC" "1"
  assert_contains "error names the file for: $line" "$R_OUT" "$override: not a KEY=\"value\" line"
done
printf 'MODEL="$(touch %s/pwned)"\n' "$tmp" > "$override"
run validate_role developer
assert_eq "command substitution in a value is refused as a model name" "$R_RC" "1"
[ ! -e "$tmp/pwned" ] && ok "override never executed" || bad "override was executed"

# 6. Unknown role.
rm -f "$override"
echo 'LANES="2"' > "$overrides/designer.conf"
run validate_role_overrides
assert_eq "unknown role refused" "$R_RC" "1"
assert_contains "unknown role error names the file" "$R_OUT" "$overrides/designer.conf: no role named 'designer'"
rm -f "$overrides/designer.conf"
run validate_role_overrides
assert_eq "empty override folder passes" "$R_RC" "0"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
