#!/usr/bin/env bash
set -euo pipefail

# Tests for `factory config init` and the Jira cloud_id check:
#
# 1. config init writes every file of config.example/ into the config folder
#    (XDG_CONFIG_HOME/ai-code-factory) and points at 'factory doctor'.
# 2. A copied example is inert: no active key but the empty [git] repos list, no
#    active line in broker-kit/spec.yaml, no example host anywhere uncommented.
# 3. It refuses to overwrite (exit 1, nothing written, files untouched) and
#    --force overwrites; unknown arguments and subcommands are refused.
# 4. jira_cloud_id_set rejects an empty, missing, commented-out and all-zero
#    cloud_id, and accepts a real one.
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
rc=0; factory config init --bogus >/dev/null 2>&1 || rc=$?
assert_eq "args: unknown flag is refused" "$rc" 1
rc=0; factory config >/dev/null 2>&1 || rc=$?
assert_eq "args: missing subcommand is refused" "$rc" 1
assert_contains "help: lists config init" "$(factory help)" "factory config init"

# --- 4. jira_cloud_id_set ----------------------------------------------------------
fns="$tmp/fns.sh"
for f in policy_value policy_string jira_cloud_id_set; do
  sed -n "/^$f() {/,/^}/p" "$REPO_ROOT/bin/factory" >> "$fns"
  grep -q "^$f() {" "$fns" || { echo "bin/factory has no $f()"; exit 1; }
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

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
