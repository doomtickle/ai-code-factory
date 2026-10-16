#!/usr/bin/env bash
set -euo pipefail

# Regression test for the "factory update" / submodule bug: a pin bump to
# broker/ (the passdesk-broker submodule) never reached the checkout because
# pull_factory() (bin/factory) ran a plain "git pull --ff-only" with no
# submodule handling, and the resulting pin/checkout mismatch then made
# "git status --porcelain" report " M broker", permanently blocking every
# future pull. It also covers the matching fix in scripts/lib/roles.sh and
# scripts/00-build-templates.sh: a plain "git clone" (without
# --recurse-submodules) left broker/ empty and those scripts just died.
#
# There is no existing shell test harness in this repo (bin/factory is driven
# by sbx/docker, which this test must not require), so this exercises the git
# plumbing directly: the same commands pull_factory and roles.sh now run,
# against disposable local repos. No network access, no sbx, no docker.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Local-path submodule remotes are disabled by git by default (CVE-2022-39253
# hardening); this test's fixtures are local-only, so allow the "file"
# transport for this process alone (no global git config is touched).
export GIT_ALLOW_PROTOCOL=file

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_empty() { [ -z "$2" ] && ok "$1" || bad "$1 (expected empty, got: $2)"; }
assert_nonempty() { [ -n "$2" ] && ok "$1" || bad "$1 (expected non-empty, got empty)"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

# --- fixtures: a "sub" repo standing in for passdesk-broker, and an "origin"
# superproject with it added as submodule "broker" -------------------------
git init -q "$tmp/sub"
# tools.txt is how roles.sh notices an uninitialized submodule; it must be
# committed in the submodule itself, not added through the superproject.
echo "git_fetch" > "$tmp/sub/tools.txt"
git -C "$tmp/sub" add tools.txt
git -C "$tmp/sub" commit -q -m "sub v1"
SUB_V1="$(git -C "$tmp/sub" rev-parse HEAD)"

git init -q --bare -b main "$tmp/origin.git"
git clone -q "$tmp/origin.git" "$tmp/seed"
git -C "$tmp/seed" checkout -q -b main 2>/dev/null || true
git -C "$tmp/seed" -c protocol.file.allow=always submodule add -q "$tmp/sub" broker
git -C "$tmp/seed" commit -q -m "add broker submodule at v1"
git -C "$tmp/seed" push -q origin HEAD:main

# ===========================================================================
# Part 1: scripts/lib/roles.sh auto-initializes an empty submodule checkout
# (a plain "git clone", without --recurse-submodules, instead of dying).
# ===========================================================================
git clone -q "$tmp/origin.git" "$tmp/plain-clone"
if [ ! -e "$tmp/plain-clone/broker/tools.txt" ]; then
  ok "fixture: a plain clone leaves broker/ uninitialized"
else
  bad "fixture: a plain clone leaves broker/ uninitialized"
fi

died=""
(
  ROOT="$tmp/plain-clone"
  die() { echo "DIED: $*" >&2; exit 1; }
  export -f die
  export ROOT
  # shellcheck disable=SC1091
  . "$REPO_ROOT/scripts/lib/roles.sh"
  [ -f "$ROOT/broker/tools.txt" ] || { echo "tools.txt still missing after sourcing roles.sh" >&2; exit 1; }
  [ -n "$KNOWN_BROKER_TOOLS" ] || { echo "KNOWN_BROKER_TOOLS is empty" >&2; exit 1; }
) && ok "sourcing roles.sh against an uninitialized clone auto-runs submodule update --init" \
  || bad "sourcing roles.sh against an uninitialized clone auto-runs submodule update --init"

[ -f "$tmp/plain-clone/broker/tools.txt" ] \
  && ok "roles.sh left broker/tools.txt populated on disk" \
  || bad "roles.sh left broker/tools.txt populated on disk"

# ===========================================================================
# Part 2: pull_factory's git sequence (bin/factory) actually moves broker/ to
# the new pin, and the post-pull dirty check does not then block the next
# update.
# ===========================================================================
git clone -q --recurse-submodules "$tmp/origin.git" "$tmp/checkout"
git -C "$tmp/checkout" branch -q --set-upstream-to=origin/main main 2>/dev/null || true

# Confirm the dirty-check command pull_factory now uses (--ignore-submodules=all)
# is empty on a clean, up-to-date checkout.
assert_empty "clean checkout: dirty-check is empty before any pull" \
  "$(git -C "$tmp/checkout" status --porcelain --ignore-submodules=all)"

# Bump the broker pin upstream, the way a real pin bump commit does (reusing
# "seed", whose broker/ checkout is already initialized).
git -C "$tmp/sub" commit -q --allow-empty -m "sub v2"
SUB_V2="$(git -C "$tmp/sub" rev-parse HEAD)"
git -C "$tmp/seed/broker" fetch -q origin
git -C "$tmp/seed/broker" checkout -q "$SUB_V2"
git -C "$tmp/seed" add broker
git -C "$tmp/seed" commit -q -m "bump broker pin to v2"
git -C "$tmp/seed" push -q origin HEAD:main

# This is exactly the command sequence pull_factory (bin/factory) runs.
git -C "$tmp/checkout" pull -q --ff-only --recurse-submodules
git -C "$tmp/checkout" submodule update -q --init

assert_eq "pull_factory's sequence moves broker/ to the new pin" \
  "$(git -C "$tmp/checkout/broker" rev-parse HEAD)" "$SUB_V2"
assert_empty "the pin bump does not leave the checkout looking dirty" \
  "$(git -C "$tmp/checkout" status --porcelain --ignore-submodules=all)"

# A real uncommitted change in the superproject must still block the next pull.
echo "stray" > "$tmp/checkout/stray.txt"
assert_nonempty "a genuine uncommitted change in the superproject still blocks" \
  "$(git -C "$tmp/checkout" status --porcelain --ignore-submodules=all)"
rm -f "$tmp/checkout/stray.txt"

# Document why --ignore-submodules=dirty (the task's suggested first option)
# does not work here: unlike "=all", it still reports a submodule whose
# checked-out commit doesn't match the superproject's recorded pin -- exactly
# the (harmless) state right after a plain "git pull --ff-only" with no
# submodule update.
git -C "$tmp/checkout/broker" checkout -q "$SUB_V1"
assert_nonempty "--ignore-submodules=dirty still flags a pin-only mismatch (why =all is used instead)" \
  "$(git -C "$tmp/checkout" status --porcelain --ignore-submodules=dirty)"
assert_empty "--ignore-submodules=all correctly ignores that same pin-only mismatch" \
  "$(git -C "$tmp/checkout" status --porcelain --ignore-submodules=all)"
git -C "$tmp/checkout/broker" checkout -q "$SUB_V2"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
