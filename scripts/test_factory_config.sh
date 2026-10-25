#!/usr/bin/env bash
set -euo pipefail

# Tests for the Jira cloud_id check and the lane identity snapshot (`factory config init`
# itself is tested in factory/test_command_config.py):
#
# 1. The copied example is inert: no active key but the empty [git] repos list, no
#    active line in broker-kit/spec.yaml, no example host anywhere uncommented.
# 2. jira_cloud_id_set rejects an empty, missing, commented-out and all-zero
#    cloud_id, and accepts a real one.
# 3. write_lane_identity: no [git] author keys (the copied example included)
#    writes no snapshot; set ones ("..." or '...', "{lane}" kept) are written
#    as AUTHOR_NAME/AUTHOR_EMAIL lines, one alone leaving the other empty;
#    escapes, non-strings and invalid names or emails are refused (exit 1)
#    and leave no snapshot.
#
# Sources functions out of bin/factory and scripts/lib/common.sh. No sbx, keychain or network.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac; }

# --- 1. the copied example is inert ------------------------------------------------
active() { grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$' || true; }
assert_eq "inert: policy.toml's only active lines are the empty [git] repos list" \
  "$(active "$REPO_ROOT/config.example/policy.toml")" $'[git]\nrepos = []'
assert_eq "inert: broker-kit/spec.yaml allows no host" \
  "$(active "$REPO_ROOT/config.example/broker-kit/spec.yaml" | grep -c 'example\|permissions\|allow' || true)" 0
assert_eq "inert: no uncommented example host in the example" \
  "$(grep -rh -v '^[[:space:]]*#' "$REPO_ROOT/config.example" | grep -c 'example\.' || true)" 0
assert_contains "help: lists config init" "$("$REPO_ROOT/bin/factory" help)" "factory config init"
assert_contains "help: lists docs --print" "$("$REPO_ROOT/bin/factory" help)" "factory docs [--print]"

# --- 2. jira_cloud_id_set ----------------------------------------------------------
fns="$tmp/fns.sh"
for f in policy_value policy_string jira_cloud_id_set; do
  sed -n "/^$f() {/,/^}/p" "$REPO_ROOT/bin/factory" "$REPO_ROOT/scripts/lib/common.sh" >> "$fns"
  grep -q "^$f() {" "$fns" || { echo "bin/factory and lib/common.sh have no $f()"; exit 1; }
done
# shellcheck source=/dev/null
. "$fns"
ROOT="$tmp/checkout"
mkdir -p "$ROOT/broker"
printf '[jira]\ncloud_id = ""\n' > "$ROOT/broker/policy.toml"
FACTORY_CONFIG="$tmp/jira-config"
mkdir -p "$FACTORY_CONFIG"
cloud() { printf '%s\n' "$1" > "$FACTORY_CONFIG/policy.toml"; if jira_cloud_id_set; then printf set; else printf unset; fi; }
assert_eq "cloud_id: a real UUID is set" "$(cloud $'[jira]\ncloud_id = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"')" set
assert_eq "cloud_id: empty is unset" "$(cloud $'[jira]\ncloud_id = ""')" unset
assert_eq "cloud_id: all-zero UUID is unset" "$(cloud $'[jira]\ncloud_id = "00000000-0000-0000-0000-000000000000"')" unset
assert_eq "cloud_id: a UUID with one non-zero digit is set" "$(cloud $'[jira]\ncloud_id = "00000000-0000-0000-0000-000000000001"')" set
assert_eq "cloud_id: the commented-out example is unset" \
  "$(cloud "$(cat "$REPO_ROOT/config.example/policy.toml")")" unset
assert_eq "cloud_id: no overlay falls back to the empty repo policy" "$(rm "$FACTORY_CONFIG/policy.toml"; jira_cloud_id_set && echo set || echo unset)" unset

# --- 3. write_lane_identity -------------------------------------------------------
for f in die policy_toml_string lane_identity_problem write_lane_identity; do
  sed -n "/^$f() {/,/^}/p" "$REPO_ROOT/scripts/lib/common.sh" >> "$fns"
  grep -q "^$f() {" "$fns" || { echo "lib/common.sh has no $f()"; exit 1; }
done
# shellcheck source=/dev/null
. "$fns"
printf '[git]\nrepos = []\n' > "$ROOT/broker/policy.toml"
FACTORY_CONFIG="$tmp/identity-config"
mkdir -p "$FACTORY_CONFIG"
snap="$tmp/lane-identity.conf"
# ident POLICY -> exit status, then the snapshot (or "none").
ident() {
  local rc=0
  printf '%s\n' "$1" > "$FACTORY_CONFIG/policy.toml"
  echo stale > "$snap"
  (write_lane_identity "$snap") >/dev/null 2>&1 || rc=$?
  printf '%s:%s' "$rc" "$(cat "$snap" 2>/dev/null || echo none)"
}
assert_eq "identity: unset writes no snapshot (a stale one is removed)" "$(ident $'[git]\nrepos = ["a/b"]')" "0:none"
assert_eq "identity: the copied example writes no snapshot" \
  "$(ident "$(cat "$REPO_ROOT/config.example/policy.toml")")" "0:none"
assert_eq "identity: name and email are written" \
  "$(ident $'[git]\nauthor_name = "Acme Bot ({lane})"  # a comment\nauthor_email = \'bot+{lane}@example.com\'')" \
  $'0:AUTHOR_NAME="Acme Bot ({lane})"\nAUTHOR_EMAIL="bot+{lane}@example.com"'
assert_eq "identity: an email alone leaves the name to the default" \
  "$(ident $'[git]\nauthor_email = "team@example.com"')" $'0:AUTHOR_NAME=""\nAUTHOR_EMAIL="team@example.com"'
assert_eq "identity: a key in another table is ignored" "$(ident $'[jira]\nauthor_name = "x"')" "0:none"
for bad in 'author_name = "a\nb"' 'author_name = "a\"b"' 'author_name = 42' 'author_name = "open' \
           'author_name = "<root>"' 'author_name = "-x"' 'author_name = "   "' \
           "author_name = \"$(printf 'a\033[31m')\"" "author_name = '$(printf 'a\tb')'" \
           'author_email = "a b@example.com"' 'author_email = "no-at"' 'author_email = "a@b@c"' \
           'author_email = "-a@b"' 'author_email = "<a@b>"'; do
  assert_eq "identity: refused: $bad" "$(ident "$(printf '[git]\n%s\n' "$bad")")" "1:none"
done

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
