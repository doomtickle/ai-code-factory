#!/usr/bin/env bash
set -euo pipefail

# Tests for the bash helpers of bin/factory and scripts/lib that `factory doctor` and
# `factory status` (now Python: factory/test_command_doctor.py, test_command_status.py) do not
# cover, because only `start`, `setup` and the other commands still in bash call them:
#
# 1. Any command still in bash stops with a message on a checkout whose
#    broker/ is incomplete (no broker/tools.txt), naming the command that
#    restores it. ('factory doctor' and 'factory setup' report it instead.)
# 2. require_project_commits (factory start): a project repo with no commits is
#    refused by name, before anything is created; repos with commits, and a
#    project that isn't a repo, pass.
# 3. sbx_not_signed_in: true only when sbx says "Not authenticated".
# 4. run_or_die: silent on success, the last 20 lines of output on failure.
#
# Fixtures are local git repos and stub commands on a private PATH: no network,
# no sbx, no docker, no keychain.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

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

# --- PATH: system tools plus stubs; no sbx, docker or secret-tool --------
sys="$tmp/sys"
mkdir -p "$sys"
for t in bash sh env cat dirname basename readlink grep mkdir sed tr tail head ls rm mv cp chmod sleep id uname \
  git curl jq openssl column python3 awk cut sort mktemp ps wc timeout printf test find seq sw_vers; do
  p="$(command -v "$t" 2>/dev/null)" || continue
  ln -sf "$p" "$sys/$t"
done
stubs="$tmp/stubs"
mkdir -p "$stubs"
for t in sbx docker; do
  printf '#!/bin/sh\necho "%s $*" >> "%s/forbidden"\nexit 1\n' "$t" "$tmp" > "$stubs/$t"
  chmod +x "$stubs/$t"
done

# --- fixtures ---------------------------------------------------------------------
# make_checkout DIR [nobroker] -> a git checkout of the factory's bin/ and
# scripts/lib/, committed with a broker/tools.txt unless "nobroker" is given.
make_checkout() {
  local d="$1"
  mkdir -p "$d/bin" "$d/scripts/lib"
  cp "$REPO_ROOT/bin/factory" "$d/bin/factory"
  cp "$REPO_ROOT"/scripts/lib/*.sh "$d/scripts/lib/"
  if [ "${2:-}" != nobroker ]; then
    mkdir -p "$d/broker"
    echo "git_fetch" > "$d/broker/tools.txt"
  fi
  git init -q "$d"
  git -C "$d" add -A
  git -C "$d" commit -q -m "fixture"
}

# run_in DIR ARGS... -> run DIR's bin/factory, output to $tmp/out, status in $RC
RC=0
run_in() {
  local d="$1"
  shift
  rm -f "$tmp/forbidden"
  RC=0
  env PATH="$stubs:$sys" HOME="$tmp/home" XDG_STATE_HOME="$tmp/state" XDG_CONFIG_HOME="$tmp/config" \
    "$d/bin/factory" "$@" > "$tmp/out" 2>&1 || RC=$?
}

# ===========================================================================
# Part 1: a checkout whose broker/ is incomplete
# ===========================================================================
make_checkout "$tmp/fresh3" nobroker
run_in "$tmp/fresh3" stop
out="$(cat "$tmp/out")"
[ "$RC" -ne 0 ] && ok "other commands still stop without broker/" || bad "other commands still stop without broker/"
assert_contains "roles.sh says what is missing" "$out" "broker/tools.txt is missing"
assert_contains "roles.sh shows the restore command" "$out" "checkout HEAD -- broker"

# ===========================================================================
# Parts 2-4: functions, from a checkout that has a broker/ (so sourcing works)
# ===========================================================================
rdy="$tmp/ready"
make_checkout "$rdy"
fn() { # fn CODE -> run CODE in a subshell with bin/factory sourced, output in $tmp/out
  RC=0
  (
    export PATH="$stubs:$sys" HOME="$tmp/home" XDG_STATE_HOME="$tmp/state" XDG_CONFIG_HOME="$tmp/config"
    # shellcheck disable=SC1091
    . "$rdy/bin/factory"
    eval "$1"
  ) > "$tmp/out" 2>&1 || RC=$?
}

# --- Part 2: require_project_commits ---------------------------------------------
proj="$tmp/proj"
mkdir -p "$proj/empty-repo" "$proj/good-repo" "$proj/not-a-repo"
git init -q "$proj/empty-repo"
git init -q "$proj/good-repo"
git -C "$proj/good-repo" commit -q --allow-empty -m first

fn "require_project_commits '$proj'"
[ "$RC" -ne 0 ] && ok "a folder holding a repo with no commits is refused" || bad "a folder holding a repo with no commits is refused"
assert_contains "the refusal names the empty repo" "$(cat "$tmp/out")" "$proj/empty-repo is a git repository with no commits"
assert_contains "the refusal says how to fix it" "$(cat "$tmp/out")" "commit --allow-empty"
assert_not_contains "the repo with commits is not blamed" "$(cat "$tmp/out")" "good-repo is a git"

fn "require_project_commits '$proj/empty-repo'"
[ "$RC" -ne 0 ] && ok "an empty repo as the project is refused" || bad "an empty repo as the project is refused"
assert_contains "it names the repo" "$(cat "$tmp/out")" "$proj/empty-repo/. is a git repository with no commits"

fn "require_project_commits '$proj/good-repo'"
[ "$RC" -eq 0 ] && ok "a repo with a commit passes" || bad "a repo with a commit passes ($(cat "$tmp/out"))"

fn "require_project_commits '$proj/not-a-repo'"
[ "$RC" -eq 0 ] && ok "a project that isn't a repo passes (nothing to check)" || bad "a project that isn't a repo passes ($(cat "$tmp/out"))"

# --- Part 3: sbx_not_signed_in ------------------------------------------------
sb="$tmp/sb"
mkdir -p "$sb"
printf '#!/bin/sh\necho "Error: Not authenticated. Run sbx login" >&2\nexit 1\n' > "$sb/sbx"
chmod +x "$sb/sbx"
fn "PATH='$sb':\$PATH; sbx_not_signed_in"
[ "$RC" -eq 0 ] && ok "sbx saying 'Not authenticated' is not signed in" || bad "sbx saying 'Not authenticated' is not signed in"
printf '#!/bin/sh\necho "factory-developer"\n' > "$sb/sbx"
fn "PATH='$sb':\$PATH; sbx_not_signed_in"
[ "$RC" -ne 0 ] && ok "sbx answering normally is not reported as signed out" || bad "sbx answering normally is not reported as signed out"
printf '#!/bin/sh\nexit 1\n' > "$sb/sbx"
fn "PATH='$sb':\$PATH; sbx_not_signed_in"
[ "$RC" -ne 0 ] && ok "an sbx that fails for another reason is left to the build" || bad "an sbx that fails for another reason is left to the build"

# --- Part 4: run_or_die ---------------------------------------------------------
loud="$tmp/loud"
mkdir -p "$loud"
printf '#!/bin/sh\nfor i in $(seq 1 25); do echo "line $i" >&2; done\nexit 3\n' > "$loud/fail"
printf '#!/bin/sh\necho noisy\nexit 0\n' > "$loud/succeed"
chmod +x "$loud/fail" "$loud/succeed"
fn "run_or_die 'could not do it' '$loud/fail' a b"
[ "$RC" -ne 0 ] && ok "run_or_die fails when the command does" || bad "run_or_die fails when the command does"
out="$(cat "$tmp/out")"
assert_contains "run_or_die says what failed" "$out" "error: could not do it (exit status 3)"
assert_contains "run_or_die shows the last line" "$out" "line 25"
assert_contains "run_or_die shows the 20th line from the end" "$out" "line 6"
assert_not_contains "run_or_die cuts the earlier lines" "$out" "line 5"
fn "run_or_die 'could not do it' '$loud/succeed'"
[ "$RC" -eq 0 ] && [ ! -s "$tmp/out" ] && ok "run_or_die is silent on success" || bad "run_or_die is silent on success"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
