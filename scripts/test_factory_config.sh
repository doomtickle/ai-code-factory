#!/usr/bin/env bash
set -euo pipefail

# Tests for `factory config init` and the Jira cloud_id check:
#
# 1. config init writes every file of config.example/ into the config folder
#    (XDG_CONFIG_HOME/ai-code-factory) and points at 'factory doctor'.
# 2. A copied example is inert: no active key but the empty [git] repos list, no
#    active line in broker-kit/spec.yaml, no example host anywhere uncommented.
# 3. It refuses to overwrite (exit 1, nothing written, files untouched) and
#    --force overwrites; a dangling symlink counts as existing; unknown arguments and subcommands are refused.
# 4. jira_cloud_id_set rejects an empty, missing, commented-out and all-zero
#    cloud_id, and accepts a real one.
# 5. write_lane_identity: no [git] author keys (the copied example included)
#    writes no snapshot; set ones ("..." or '...', "{lane}" kept) are written
#    as AUTHOR_NAME/AUTHOR_EMAIL lines, one alone leaving the other empty;
#    escapes, non-strings and invalid names or emails are refused (exit 1)
#    and leave no snapshot.
#
# Runs bin/factory against a throwaway config folder. No sbx, keychain or network.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac; }

export XDG_CONFIG_HOME="$tmp/xdg"
cfg="$XDG_CONFIG_HOME/ai-code-factory"
factory() { "$REPO_ROOT/bin/factory" "$@"; }

# --- 1. config init writes the example ---------------------------------------------
rc=0; out="$(factory config init 2>&1)" || rc=$?
assert_eq "init: exit 0 on a fresh folder" "$rc" 0
expected="$(cd "$REPO_ROOT/config.example" && find . -type f | sort)"
actual="$(cd "$cfg" && find . -type f | sort)"
assert_eq "init: writes every example file" "$actual" "$expected"
assert_eq "init: policy.toml is a copy" "$(cmp "$cfg/policy.toml" "$REPO_ROOT/config.example/policy.toml" && echo same)" same
assert_contains "init: prints what it wrote" "$out" "wrote $cfg/policy.toml"
assert_contains "init: prints the next step" "$out" "factory doctor"

# --- 2. the copy is inert ----------------------------------------------------------
active() { grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$' || true; }
assert_eq "inert: policy.toml's only active lines are the empty [git] repos list" \
  "$(active "$cfg/policy.toml")" $'[git]\nrepos = []'
assert_eq "inert: broker-kit/spec.yaml allows no host" \
  "$(active "$cfg/broker-kit/spec.yaml" | grep -c 'example\|permissions\|allow' || true)" 0
assert_eq "inert: no uncommented example host in the example" \
  "$(grep -rh -v '^[[:space:]]*#' "$REPO_ROOT/config.example" | grep -c 'example\.' || true)" 0

# --- 3. overwrite protection -------------------------------------------------------
echo "# mine" > "$cfg/policy.toml"
rc=0; out="$(factory config init 2>&1)" || rc=$?
assert_eq "refuse: exit 1 when files exist" "$rc" 1
assert_eq "refuse: the existing file is untouched" "$(cat "$cfg/policy.toml")" "# mine"
assert_contains "refuse: names the file and --force" "$out" "--force"
rm "$cfg/roles/developer.conf"
rc=0; factory config init >/dev/null 2>&1 || rc=$?
assert_eq "refuse: all-or-nothing, a missing file is not written alone" \
  "$([ -e "$cfg/roles/developer.conf" ] && echo written || echo absent)" absent
rc=0; out="$(factory config init --force 2>&1)" || rc=$?
assert_eq "force: exit 0" "$rc" 0
assert_eq "force: overwrites the existing file" \
  "$(cmp "$cfg/policy.toml" "$REPO_ROOT/config.example/policy.toml" && echo same)" same
assert_eq "force: writes the missing file" "$([ -e "$cfg/roles/developer.conf" ] && echo written)" written

# A dangling symlink counts as existing: nothing is written, and neither the link
# nor its target is touched. --force replaces the link instead of writing through it.
lnk="$tmp/xdg-link"
lcfg="$lnk/ai-code-factory"
mkdir -p "$lcfg"
ln -s "$tmp/nowhere/target.toml" "$lcfg/policy.toml"
rc=0; out="$(XDG_CONFIG_HOME="$lnk" factory config init 2>&1)" || rc=$?
assert_eq "symlink: a dangling policy.toml link is refused" "$rc" 1
assert_contains "symlink: names the link" "$out" "exists: $lcfg/policy.toml"
assert_eq "symlink: nothing else is written" "$(find "$lcfg" -type f | wc -l | tr -d ' ')" 0
assert_eq "symlink: the link is untouched" "$(readlink "$lcfg/policy.toml")" "$tmp/nowhere/target.toml"
assert_eq "symlink: the target is not created" "$([ -e "$tmp/nowhere" ] && echo created || echo absent)" absent
rc=0; XDG_CONFIG_HOME="$lnk" factory config init --force >/dev/null 2>&1 || rc=$?
assert_eq "symlink: --force exit 0" "$rc" 0
assert_eq "symlink: --force replaces the link with a regular file" \
  "$([ -L "$lcfg/policy.toml" ] && echo link || echo file)" file
assert_eq "symlink: --force never writes through the link" "$([ -e "$tmp/nowhere" ] && echo created || echo absent)" absent

rc=0; factory config init --bogus >/dev/null 2>&1 || rc=$?
assert_eq "args: unknown flag is refused" "$rc" 1
rc=0; factory config >/dev/null 2>&1 || rc=$?
assert_eq "args: missing subcommand is refused" "$rc" 1
assert_contains "help: lists config init" "$(factory help)" "factory config init"

# --- 4. jira_cloud_id_set ----------------------------------------------------------
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

# --- 5. write_lane_identity -------------------------------------------------------
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
