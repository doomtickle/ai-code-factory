#!/usr/bin/env bash
set -euo pipefail

# Regression tests for "factory update"'s handling of broker/ (the
# passdesk-broker submodule):
#
# 1. scripts/lib/roles.sh auto-initializes an empty submodule checkout
#    instead of dying (a plain "git clone", without --recurse-submodules,
#    leaves broker/ empty).
# 2. bin/factory's pull_factory fast-forwards the superproject and moves
#    broker/ to its new pin, but refuses - rather than silently pulling
#    forward, detaching a commit, or leaving something for
#    scripts/00-build-templates.sh to bake into the broker image unreviewed -
#    when broker/ has uncommitted changes (tracked or untracked) or a local
#    commit that is not part of the pinned history. Being behind the pin is
#    fine: that is exactly what "git submodule update --init" fixes.
# 3. scripts/lib/common.sh's require_broker_at_pin, used by
#    scripts/00-build-templates.sh before it builds the broker image, refuses
#    the same dirty states and anything off the pin (no "behind is fine"
#    leniency here: the image must be built from exactly the pinned commit).
#
# broker/ is writable by an agent working on this repo's own code like any
# other file, so all of the above matters for real, not just for a
# mis-synced checkout.
#
# This test sources the real bin/factory (for pull_factory) and
# scripts/lib/common.sh (for require_broker_at_pin, broker_dirty, broker_pin,
# broker_head) and calls them directly, against disposable local fixture
# repos. There is no other shell test harness in this repo (bin/factory is
# otherwise driven by sbx/docker, which this test must not require), so this
# exercises the git plumbing directly: no network access, no sbx, no docker.

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
assert_contains() { # assert_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}

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
# pull_factory's last step, on a pull that actually moves HEAD, re-execs
# "$ROOT/bin/factory update --pulled". A tracked stub here lets that happen
# for real in this fixture, without needing the real factory's sbx/docker
# machinery.
mkdir -p "$tmp/seed/bin"
cat > "$tmp/seed/bin/factory" <<'STUB'
#!/bin/sh
echo "stub factory: $*"
STUB
chmod +x "$tmp/seed/bin/factory"
git -C "$tmp/seed" add bin/factory
git -C "$tmp/seed" commit -q -m "add broker submodule at v1, with a stub bin/factory"
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

# --- helpers for exercising the real code -----------------------------------

# fresh_checkout NAME -> path to a fresh --recurse-submodules clone of
# origin.git, tracking main, under $tmp/NAME.
fresh_checkout() {
  git clone -q --recurse-submodules "$tmp/origin.git" "$tmp/$1" >/dev/null 2>&1
  git -C "$tmp/$1" branch -q --set-upstream-to=origin/main main 2>/dev/null || true
  echo "$tmp/$1"
}

# run_pull_factory DIR -> pull_factory's (bin/factory) combined stdout+stderr
# in $PF_OUT, its exit status in $PF_RC. Sourcing bin/factory with ROOT
# pre-set points its git commands at the fixture DIR instead of this repo
# (see the ROOT guard bin/factory's top adds for exactly this); running it in
# a subshell keeps ROOT and every function/variable bin/factory defines from
# leaking into the rest of this script.
run_pull_factory() {
  set +e
  PF_OUT="$( (
    ROOT="$1"
    # shellcheck disable=SC1091
    . "$REPO_ROOT/bin/factory"
    pull_factory
  ) 2>&1 )"
  PF_RC=$?
  set -e
}

# run_require_broker_at_pin DIR -> require_broker_at_pin's (scripts/lib/common.sh)
# combined output in $RBAP_OUT, its exit status in $RBAP_RC.
run_require_broker_at_pin() {
  set +e
  RBAP_OUT="$( (
    # shellcheck disable=SC1091
    . "$REPO_ROOT/scripts/lib/common.sh"
    require_broker_at_pin "$1"
  ) 2>&1 )"
  RBAP_RC=$?
  set -e
}

# ===========================================================================
# Part 2: pull_factory's git sequence (bin/factory) actually moves broker/ to
# a new pin, and the post-pull dirty check does not then block the next
# update.
# ===========================================================================
checkout="$(fresh_checkout checkout)"

assert_empty "clean checkout: dirty-check is empty before any pull" \
  "$(git -C "$checkout" status --porcelain --ignore-submodules=all)"

run_pull_factory "$checkout"
assert_eq "pull_factory on an already-current checkout exits 0" "$PF_RC" "0"
assert_contains "pull_factory on an already-current checkout reports up to date" \
  "$PF_OUT" "already up to date"

# Bump the broker pin upstream, the way a real pin bump commit does (reusing
# "seed", whose broker/ checkout is already initialized).
git -C "$tmp/sub" commit -q --allow-empty -m "sub v2"
SUB_V2="$(git -C "$tmp/sub" rev-parse HEAD)"
git -C "$tmp/seed/broker" fetch -q origin
git -C "$tmp/seed/broker" checkout -q "$SUB_V2"
git -C "$tmp/seed" add broker
git -C "$tmp/seed" commit -q -m "bump broker pin to v2"
git -C "$tmp/seed" push -q origin HEAD:main

run_pull_factory "$checkout"
assert_eq "pull_factory exits 0 pulling a real pin bump" "$PF_RC" "0"
assert_eq "pull_factory's real run moves broker/ to the new pin" \
  "$(git -C "$checkout/broker" rev-parse HEAD)" "$SUB_V2"
assert_empty "the pin bump does not leave the checkout looking dirty" \
  "$(git -C "$checkout" status --porcelain --ignore-submodules=all)"
assert_contains "pull_factory re-execs 'factory update --pulled' after a real pull" \
  "$PF_OUT" "stub factory: update --pulled"

# Document why --ignore-submodules=dirty (the task's suggested first option)
# does not work here: unlike "=all", it still reports a submodule whose
# checked-out commit doesn't match the superproject's recorded pin -- exactly
# the (harmless) state right after a plain "git pull --ff-only" with no
# submodule update.
rationale="$(fresh_checkout rationale)"
git -C "$rationale/broker" checkout -q "$SUB_V1"
assert_nonempty "--ignore-submodules=dirty still flags a pin-only mismatch (why =all is used instead)" \
  "$(git -C "$rationale" status --porcelain --ignore-submodules=dirty)"
assert_empty "--ignore-submodules=all correctly ignores that same pin-only mismatch" \
  "$(git -C "$rationale" status --porcelain --ignore-submodules=all)"

# ===========================================================================
# Part 2b: pull_factory refuses when broker/ has a real edit or an
# unreviewed local commit, instead of silently pulling forward over it.
# ===========================================================================

# --- a dirty tracked file in broker/ ----------------------------------------
dirty_tracked="$(fresh_checkout dirty-tracked)"
echo "more" >> "$dirty_tracked/broker/tools.txt"
run_pull_factory "$dirty_tracked"
assert_eq "dirty tracked file in broker/: pull_factory exits 0 (refuses, does not die)" "$PF_RC" "0"
assert_contains "dirty tracked file in broker/ is refused with a clear message" \
  "$PF_OUT" "broker has uncommitted changes"
assert_eq "dirty tracked file in broker/: broker/ HEAD is left untouched" \
  "$(git -C "$dirty_tracked/broker" rev-parse HEAD)" "$SUB_V2"

# --- an untracked file in broker/ -------------------------------------------
untracked="$(fresh_checkout untracked)"
echo "x" > "$untracked/broker/stray.txt"
run_pull_factory "$untracked"
assert_eq "untracked file in broker/: pull_factory exits 0 (refuses, does not die)" "$PF_RC" "0"
assert_contains "untracked file in broker/ is refused with a clear message" \
  "$PF_OUT" "broker has uncommitted changes"
assert_eq "untracked file in broker/: broker/ HEAD is left untouched" \
  "$(git -C "$untracked/broker" rev-parse HEAD)" "$SUB_V2"

# --- a local commit in broker/ not on the pin -------------------------------
local_commit="$(fresh_checkout local-commit)"
echo "local change" > "$local_commit/broker/local.txt"
git -C "$local_commit/broker" add local.txt
git -C "$local_commit/broker" commit -q -m "local, unreviewed commit"
LOCAL_COMMIT="$(git -C "$local_commit/broker" rev-parse HEAD)"
run_pull_factory "$local_commit"
assert_eq "local commit in broker/ not on the pin: pull_factory exits 0 (refuses, does not die)" "$PF_RC" "0"
assert_contains "a local commit in broker/ not on the pin is refused with a clear message" \
  "$PF_OUT" "isn't part of the pinned history"
assert_eq "local commit in broker/ not on the pin: broker/ HEAD is left untouched" \
  "$(git -C "$local_commit/broker" rev-parse HEAD)" "$LOCAL_COMMIT"

# --- broker/ behind the pin: allowed, and updated to the pin ----------------
behind="$(fresh_checkout behind-pin)"
git -C "$behind/broker" checkout -q "$SUB_V1"
run_pull_factory "$behind"
assert_eq "broker/ behind the pin: pull_factory exits 0" "$PF_RC" "0"
assert_eq "broker/ behind the pin: pull_factory updates it to the pin" \
  "$(git -C "$behind/broker" rev-parse HEAD)" "$SUB_V2"

# ===========================================================================
# Part 3: require_broker_at_pin (scripts/lib/common.sh), used by
# scripts/00-build-templates.sh before it bakes broker/ into the broker
# image, refuses the same dirty states, refuses anything off the pin (even a
# commit that pull_factory would tolerate as "behind"), and accepts a clean
# checkout exactly at the pin.
# ===========================================================================
run_require_broker_at_pin "$dirty_tracked"
assert_eq "require_broker_at_pin refuses a dirty tracked file in broker/" "$RBAP_RC" "1"
assert_contains "require_broker_at_pin's dirty refusal names the problem" \
  "$RBAP_OUT" "uncommitted changes"

run_require_broker_at_pin "$untracked"
assert_eq "require_broker_at_pin refuses an untracked file in broker/" "$RBAP_RC" "1"
assert_contains "require_broker_at_pin's untracked-file refusal names the problem" \
  "$RBAP_OUT" "uncommitted changes"

run_require_broker_at_pin "$local_commit"
assert_eq "require_broker_at_pin refuses a local commit in broker/ off the pin" "$RBAP_RC" "1"
assert_contains "require_broker_at_pin's off-pin refusal names the problem" \
  "$RBAP_OUT" "not the pinned commit"

behind_for_build="$(fresh_checkout behind-pin-for-build)"
git -C "$behind_for_build/broker" checkout -q "$SUB_V1"
run_require_broker_at_pin "$behind_for_build"
assert_eq "require_broker_at_pin refuses broker/ behind the pin too (exact match required for a build)" \
  "$RBAP_RC" "1"
assert_contains "require_broker_at_pin's behind-pin refusal names the problem" \
  "$RBAP_OUT" "not the pinned commit"

clean_at_pin="$(fresh_checkout clean-at-pin)"
run_require_broker_at_pin "$clean_at_pin"
assert_eq "require_broker_at_pin accepts a clean checkout exactly at the pin" "$RBAP_RC" "0"
assert_empty "require_broker_at_pin prints nothing when it accepts" "$RBAP_OUT"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
