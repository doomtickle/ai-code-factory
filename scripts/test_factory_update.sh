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
#    leniency here).
# 4. Nothing trusts broker/.git: a redirected gitfile, or one pointing at a
#    fake repository whose config sets core.fsmonitor, is refused by
#    pull_factory, require_broker_at_pin and broker_export, and the fsmonitor
#    payload never runs. An uninitialized submodule is refused by the build.
# 5. The broker image's build context is a "git archive" export of the
#    pinned commit (broker_export), byte-for-byte, never the broker/ working
#    tree: a git-ignored __pycache__/*.pyc planted in broker/ doesn't reach
#    it. scripts/00-build-templates.sh is run for real against a fixture,
#    with stub docker and sbx commands, to show it builds from that export.
# 6. An exported ROOT doesn't redirect a direct run of bin/factory.
# 7. `factory update --plan` (what the dashboard's UPDATE key shows before
#    asking to confirm) changes nothing but the remote-tracking refs: it
#    lists incoming commits, the sandboxes update would recreate (and what
#    each loses), the ones the incoming files may make stale, and notes for
#    a dirty checkout or a missing upstream; --plan with --yes is refused.
#
# broker/ is writable by an agent working on this repo's own code like any
# other file, so all of the above matters for real, not just for a
# mis-synced checkout.
#
# This test sources the real bin/factory (for pull_factory) and
# scripts/lib/common.sh (for the broker_* helpers) and calls them directly,
# against disposable local fixture repos. There is no other shell test
# harness in this repo (bin/factory is otherwise driven by sbx/docker, which
# this test must not require), so this exercises the git plumbing directly:
# no network access, no sbx, no docker.

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
# Dockerfile, passdesk/app.py and the *.pyc ignore rule mirror the real
# broker's layout for the build-context tests.
echo "git_fetch" > "$tmp/sub/tools.txt"
echo "FROM scratch" > "$tmp/sub/Dockerfile"
printf '__pycache__/\n*.pyc\n' > "$tmp/sub/.gitignore"
mkdir -p "$tmp/sub/passdesk"
echo "print('reviewed')" > "$tmp/sub/passdesk/app.py"
git -C "$tmp/sub" add tools.txt Dockerfile .gitignore passdesk/app.py
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

(
  # shellcheck disable=SC1091
  . "$REPO_ROOT/scripts/lib/common.sh"
  ROOT="$tmp/plain-clone"
  # shellcheck disable=SC1091
  . "$REPO_ROOT/scripts/lib/roles.sh"
  [ -f "$ROOT/broker/tools.txt" ] || { echo "tools.txt still missing after sourcing roles.sh" >&2; exit 1; }
  [ -n "$KNOWN_BROKER_TOOLS" ] || { echo "KNOWN_BROKER_TOOLS is empty" >&2; exit 1; }
) && ok "sourcing roles.sh against an uninitialized clone auto-runs submodule update --init" \
  || bad "sourcing roles.sh against an uninitialized clone auto-runs submodule update --init"

[ -f "$tmp/plain-clone/broker/tools.txt" ] \
  && ok "roles.sh left broker/tools.txt populated on disk" \
  || bad "roles.sh left broker/tools.txt populated on disk"

# FACTORY_SRC: a copy of this repo's bin/ and scripts/ with a stub
# broker/tools.txt, for the tests that source bin/factory. Sourcing it always
# loads scripts/lib/roles.sh against its own root (it resets ROOT first), which
# dies unless broker/ is initialised; a lane clone of this repo has no
# submodules until it is initialised, and factory-test's copy has no .git. The
# copy keeps those tests independent of the real broker/. They set ROOT to a
# fixture afterwards, as before.
FACTORY_SRC="$tmp/factory-src"
mkdir -p "$FACTORY_SRC/broker"
cp -R "$REPO_ROOT/bin" "$REPO_ROOT/scripts" "$FACTORY_SRC/"
echo "git_fetch" > "$FACTORY_SRC/broker/tools.txt"

# --- helpers for exercising the real code -----------------------------------

# fresh_checkout NAME -> path to a fresh --recurse-submodules clone of
# origin.git, tracking main, under $tmp/NAME.
fresh_checkout() {
  git clone -q --recurse-submodules "$tmp/origin.git" "$tmp/$1" >/dev/null 2>&1
  git -C "$tmp/$1" branch -q --set-upstream-to=origin/main main 2>/dev/null || true
  echo "$tmp/$1"
}

# run_pull_factory DIR -> pull_factory's (bin/factory) combined stdout+stderr
# in $PF_OUT, its exit status in $PF_RC. Setting ROOT after sourcing
# bin/factory (which always sets it to this repo) points its git commands at
# the fixture DIR; running it in a subshell keeps ROOT and every
# function/variable bin/factory defines from leaking into the rest of this
# script.
run_pull_factory() {
  set +e
  PF_OUT="$( (
    # shellcheck disable=SC1091
    . "$FACTORY_SRC/bin/factory"
    ROOT="$1"
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

# ===========================================================================
# Part 4: nothing trusts broker/.git.
# ===========================================================================

# run_common FN ARGS... -> run a scripts/lib/common.sh function in a
# subshell; combined output in $C_OUT, exit status in $C_RC.
run_common() {
  set +e
  C_OUT="$( (
    # shellcheck disable=SC1091
    . "$REPO_ROOT/scripts/lib/common.sh"
    "$@"
  ) 2>&1 )"
  C_RC=$?
  set -e
}

# fake_broker_repo CHECKOUT NAME [FSMONITOR-PAYLOAD] -> point CHECKOUT's
# broker/.git at a fake repository $tmp/NAME (a clone of the broker at the
# pin) in which an edit to passdesk/app.py is hidden with skip-worktree, so
# plain "git -C broker" sees a clean checkout exactly at the pin. With a
# payload script, the fake repository's config also runs it as its
# core.fsmonitor hook.
fake_broker_repo() {
  local co="$1" fake="$tmp/$2"
  git clone -q --no-checkout "$tmp/sub" "$fake"
  echo "print('planted')" > "$co/broker/passdesk/app.py"
  git --git-dir="$fake/.git" --work-tree="$co/broker" read-tree HEAD
  git --git-dir="$fake/.git" --work-tree="$co/broker" update-index -q --refresh >/dev/null 2>&1 || true
  git --git-dir="$fake/.git" --work-tree="$co/broker" update-index --skip-worktree passdesk/app.py
  [ -z "${3:-}" ] || git --git-dir="$fake/.git" config core.fsmonitor "$3"
  printf 'gitdir: %s\n' "$fake/.git" > "$co/broker/.git"
}

# --- a redirected broker/.git ------------------------------------------------
redirected="$(fresh_checkout redirected)"
fake_broker_repo "$redirected" fake-redirect
# Control: plain git in broker/ is fooled, which is what 496e013's checks did.
assert_empty "control: plain 'git -C broker status' is fooled by the fake repository" \
  "$(git -C "$redirected/broker" status --porcelain --untracked-files=all)"
assert_eq "control: plain 'git -C broker rev-parse HEAD' reports the pin" \
  "$(git -C "$redirected/broker" rev-parse HEAD)" "$SUB_V2"

run_require_broker_at_pin "$redirected"
assert_eq "require_broker_at_pin refuses a redirected broker/.git" "$RBAP_RC" "1"
assert_contains "require_broker_at_pin's redirect refusal names the problem" \
  "$RBAP_OUT" "does not point at"
mkdir "$tmp/ctx-redirected"
run_common broker_export "$redirected" "$tmp/ctx-redirected"
assert_eq "broker_export refuses a redirected broker/.git" "$C_RC" "1"
assert_empty "broker_export wrote nothing for a redirected broker/.git" \
  "$(ls -A "$tmp/ctx-redirected")"
run_pull_factory "$redirected"
assert_eq "redirected broker/.git: pull_factory exits 0 (refuses, does not die)" "$PF_RC" "0"
assert_contains "pull_factory refuses a redirected broker/.git" "$PF_OUT" "does not point at"
assert_eq "redirected broker/.git: pull_factory left it untouched" \
  "$(cat "$redirected/broker/.git")" "gitdir: $tmp/fake-redirect/.git"

# --- broker/.git replaced by a directory or a symlink -------------------------
gitfile_dir="$(fresh_checkout gitfile-dir)"
rm "$gitfile_dir/broker/.git"
mkdir "$gitfile_dir/broker/.git"
run_require_broker_at_pin "$gitfile_dir"
assert_eq "require_broker_at_pin refuses a broker/.git directory" "$RBAP_RC" "1"
assert_contains "the broker/.git directory refusal names the problem" "$RBAP_OUT" "not a regular gitfile"

gitfile_link="$(fresh_checkout gitfile-link)"
cp "$gitfile_link/broker/.git" "$tmp/gitfile-copy"
rm "$gitfile_link/broker/.git"
ln -s "$tmp/gitfile-copy" "$gitfile_link/broker/.git"
run_require_broker_at_pin "$gitfile_link"
assert_eq "require_broker_at_pin refuses a symlinked broker/.git, even to the right content" "$RBAP_RC" "1"

# --- an fsmonitor payload in a fake gitdir -----------------------------------
marker="$tmp/fsmonitor-ran"
cat > "$tmp/payload.sh" <<PAYLOAD
#!/bin/sh
touch "$marker"
PAYLOAD
chmod +x "$tmp/payload.sh"
fsmon="$(fresh_checkout fsmonitor)"
fake_broker_repo "$fsmon" fake-fsmonitor "$tmp/payload.sh"
# Control: the payload is live -- plain "git -C broker status" runs it.
git -C "$fsmon/broker" status --porcelain >/dev/null 2>&1 || true
[ -e "$marker" ] && ok "control: plain 'git -C broker status' runs the fsmonitor payload" \
  || bad "control: plain 'git -C broker status' runs the fsmonitor payload"
rm -f "$marker"

run_require_broker_at_pin "$fsmon"
assert_eq "require_broker_at_pin refuses a broker/.git pointing at an fsmonitor payload" "$RBAP_RC" "1"
mkdir "$tmp/ctx-fsmon"
run_common broker_export "$fsmon" "$tmp/ctx-fsmon"
assert_eq "broker_export refuses a broker/.git pointing at an fsmonitor payload" "$C_RC" "1"
run_pull_factory "$fsmon"
assert_contains "pull_factory refuses a broker/.git pointing at an fsmonitor payload" \
  "$PF_OUT" "does not point at"
[ ! -e "$marker" ] && ok "the fsmonitor payload was never run by the checks or pull_factory" \
  || bad "the fsmonitor payload was never run by the checks or pull_factory"

# An fsmonitor (or hook) in broker/'s real module config -- which lives under
# the main repo's .git and is trusted -- is still forced off by broker_git.
fsmon_real="$(fresh_checkout fsmonitor-real)"
git --git-dir="$fsmon_real/.git/modules/broker" config core.fsmonitor "$tmp/payload.sh"
run_require_broker_at_pin "$fsmon_real"
assert_eq "require_broker_at_pin accepts a clean broker/ whose module config sets core.fsmonitor" "$RBAP_RC" "0"
run_pull_factory "$fsmon_real"
assert_eq "pull_factory runs normally with core.fsmonitor in the module config" "$PF_RC" "0"
[ ! -e "$marker" ] && ok "broker_git forces core.fsmonitor off even from the real module config" \
  || bad "broker_git forces core.fsmonitor off even from the real module config"

# --- an uninitialized submodule ----------------------------------------------
git clone -q "$tmp/origin.git" "$tmp/uninit"
run_require_broker_at_pin "$tmp/uninit"
assert_eq "require_broker_at_pin refuses an uninitialized submodule" "$RBAP_RC" "1"
assert_contains "the uninitialized refusal names the problem" "$RBAP_OUT" "not initialized"
mkdir "$tmp/ctx-uninit"
run_common broker_export "$tmp/uninit" "$tmp/ctx-uninit"
assert_eq "broker_export refuses an uninitialized submodule" "$C_RC" "1"
assert_contains "broker_export's uninitialized refusal names the problem" "$C_OUT" "not initialized"

# ...but a module directory without the pinned commit is refused too.
missing_pin="$(fresh_checkout missing-pin)"
git -C "$tmp/sub" commit -q --allow-empty -m "sub v3, never fetched by missing-pin"
SUB_V3="$(git -C "$tmp/sub" rev-parse HEAD)"
git -C "$missing_pin" update-index --cacheinfo "160000,$SUB_V3,broker"
git -C "$missing_pin" commit -q -m "pin broker to a commit missing-pin's module dir lacks"
mkdir "$tmp/ctx-missing-pin"
run_common broker_export "$missing_pin" "$tmp/ctx-missing-pin"
assert_eq "broker_export refuses a pin missing from broker/'s repository" "$C_RC" "1"
assert_contains "broker_export's missing-pin refusal names the commit" "$C_OUT" "$SUB_V3"

# ===========================================================================
# Part 5: the broker build context is an export of the pin, and
# 00-build-templates.sh builds from it.
# ===========================================================================
pinned_tree="$tmp/pinned-tree"
mkdir "$pinned_tree"
git -C "$tmp/sub" archive "$SUB_V2" | tar -x -f - -C "$pinned_tree"

ignored="$(fresh_checkout ignored-pyc)"
mkdir -p "$ignored/broker/passdesk/__pycache__"
echo "planted bytecode" > "$ignored/broker/passdesk/__pycache__/app.cpython-312.pyc"
run_common broker_dirty "$ignored"
assert_contains "broker_dirty reports a git-ignored .pyc (--ignored)" "$C_OUT" "!! passdesk/__pycache__/"
run_require_broker_at_pin "$ignored"
assert_eq "require_broker_at_pin does not block on git-ignored files" "$RBAP_RC" "0"
mkdir "$tmp/ctx-ignored"
run_common broker_export "$ignored" "$tmp/ctx-ignored"
assert_eq "broker_export succeeds with a git-ignored .pyc in broker/" "$C_RC" "0"
[ ! -e "$tmp/ctx-ignored/passdesk/__pycache__" ] \
  && ok "a git-ignored .pyc in broker/ is not in the exported build context" \
  || bad "a git-ignored .pyc in broker/ is not in the exported build context"
diff -r "$pinned_tree" "$tmp/ctx-ignored" >/dev/null \
  && ok "the exported build context matches 'git archive' of the pin exactly" \
  || bad "the exported build context matches 'git archive' of the pin exactly"
run_pull_factory "$ignored"
assert_eq "pull_factory does not block on git-ignored files in broker/" "$PF_RC" "0"
assert_contains "pull_factory with only git-ignored files in broker/ proceeds" "$PF_OUT" "already up to date"

# --- 00-build-templates.sh, run for real with stub docker and sbx ------------
stubs="$tmp/stubs"
mkdir -p "$stubs"
cat > "$stubs/docker" <<'STUB'
#!/bin/sh
# docker build -t TAG -f DOCKERFILE [ARGS...] CONTEXT: record the broker's
# Dockerfile, context path and a copy of the context (gone after the run).
case "$1" in
  build)
    tag="$3" dockerfile="$5"
    for a in "$@"; do ctx="$a"; done
    case "$tag" in factory-broker:*)
      rm -rf "$CAPTURE" && mkdir -p "$CAPTURE" && cp -R "$ctx/." "$CAPTURE/"
      printf '%s\n' "$dockerfile" > "$CAPTURE.dockerfile"
      printf '%s\n' "$ctx" > "$CAPTURE.context" ;;
    esac ;;
  image) [ "$2" != inspect ] || echo "sha256:stub" ;;
esac
STUB
cat > "$stubs/sbx" <<STUB
#!/bin/sh
[ "\$1" != version ] || echo "sbx version: v$(sed -n 's/^readonly SBX_VERSION="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/lib/common.sh") stub"
STUB
chmod +x "$stubs/docker" "$stubs/sbx"

# run_build DIR -> run a copy of the real scripts/00-build-templates.sh (and
# scripts/lib) from inside fixture checkout DIR, so its ROOT is DIR. Output in
# $B_OUT, status in $B_RC, the broker's build context copied to $DIR.ctx.
run_build() {
  rm -rf "$1/scripts"
  mkdir -p "$1/scripts"
  cp -R "$REPO_ROOT/scripts/lib" "$1/scripts/lib"
  cp "$REPO_ROOT/scripts/00-build-templates.sh" "$1/scripts/"
  set +e
  B_OUT="$(CAPTURE="$1.ctx" PATH="$stubs:$PATH" XDG_STATE_HOME="$tmp/state" \
    XDG_CONFIG_HOME="$tmp/config" bash "$1/scripts/00-build-templates.sh" 2>&1)"
  B_RC=$?
  set -e
}

run_build "$ignored"
assert_eq "00-build-templates.sh succeeds on a clean broker/ at the pin (stub docker)" "$B_RC" "0"
diff -r "$pinned_tree" "$ignored.ctx" >/dev/null \
  && ok "00-build-templates.sh builds the broker from exactly the pinned commit's tree" \
  || bad "00-build-templates.sh builds the broker from exactly the pinned commit's tree"
case "$(cat "$ignored.ctx.context")" in
  "$ignored"/*|"$ignored") bad "00-build-templates.sh's broker context is not inside the checkout" ;;
  *) ok "00-build-templates.sh's broker context is not inside the checkout" ;;
esac
assert_eq "00-build-templates.sh takes the broker Dockerfile from the exported context" \
  "$(cat "$ignored.ctx.dockerfile")" "$(cat "$ignored.ctx.context")/Dockerfile"

run_build "$redirected"
assert_eq "00-build-templates.sh refuses a redirected broker/.git" "$B_RC" "1"
assert_contains "00-build-templates.sh's redirect refusal names the problem" "$B_OUT" "does not point at"
[ ! -e "$redirected.ctx" ] && ok "00-build-templates.sh built nothing for a redirected broker/.git" \
  || bad "00-build-templates.sh built nothing for a redirected broker/.git"

run_build "$fsmon"
assert_eq "00-build-templates.sh refuses a broker/.git pointing at an fsmonitor payload" "$B_RC" "1"
[ ! -e "$marker" ] && ok "00-build-templates.sh never ran the fsmonitor payload" \
  || bad "00-build-templates.sh never ran the fsmonitor payload"

run_build "$dirty_tracked"
assert_eq "00-build-templates.sh refuses a dirty broker/" "$B_RC" "1"
assert_contains "00-build-templates.sh's dirty refusal names the problem" "$B_OUT" "uncommitted changes"

# ===========================================================================
# Part 6: an exported ROOT doesn't redirect a direct run of bin/factory.
# Before, bin/factory kept an inherited ROOT, so sourcing roles.sh against
# this empty directory died on its missing broker/tools.txt.
# ===========================================================================
mkdir "$tmp/evil-root"
set +e
evil_out="$(ROOT="$tmp/evil-root" "$REPO_ROOT/bin/factory" help 2>&1)"
evil_rc=$?
set -e
assert_eq "an exported ROOT: a direct 'factory help' still succeeds" "$evil_rc" "0"
case "$evil_out" in
  *"$tmp/evil-root"*) bad "an exported ROOT is never used by a direct run (output mentions it)" ;;
  *) ok "an exported ROOT is never used by a direct run" ;;
esac
assert_empty "an exported ROOT: nothing was written into it" "$(ls -A "$tmp/evil-root")"

# ===========================================================================
# Part 7: factory update --plan.
# ===========================================================================
plan_bin="$tmp/plan-bin"
mkdir -p "$plan_bin"
# A fake sbx: lists $PLAN_SANDBOXES, has no secrets, records nothing else.
cat > "$plan_bin/sbx" <<'STUB'
#!/bin/sh
case "$1" in
  ls) printf '%s\n' $PLAN_SANDBOXES ;;
esac
STUB
chmod +x "$plan_bin/sbx"
export PLAN_SANDBOXES="factory-broker factory-coordinator"

# run_plan DIR [ARGS] -> cmd_update's output with --plan (or ARGS) against
# fixture DIR, in $P_OUT, its status in $P_RC.
run_plan() {
  local dir="$1"
  shift
  set +e
  P_OUT="$( (
    export PATH="$plan_bin:$PATH" XDG_STATE_HOME="$tmp/plan-state" XDG_CONFIG_HOME="$tmp/plan-config"
    # shellcheck disable=SC1091
    . "$FACTORY_SRC/bin/factory"
    ROOT="$dir"
    cmd_update "${@:---plan}"
  ) 2>&1 )"
  P_RC=$?
  set -e
}

planco="$(fresh_checkout plan)"
# The Coordinator's sandbox is up to date (its fingerprint recorded); the
# broker's has none, so it counts as out of date.
(
  export PATH="$plan_bin:$PATH" XDG_STATE_HOME="$tmp/plan-state" XDG_CONFIG_HOME="$tmp/plan-config"
  # shellcheck disable=SC1091
  . "$FACTORY_SRC/bin/factory"
  ROOT="$planco"
  record_sandbox_fingerprint "$COORDINATOR_SANDBOX"
)
run_plan "$planco"
assert_eq "update --plan, nothing incoming: exits 0" "$P_RC" "0"
assert_empty "update --plan, nothing incoming: no commit lines" "$(printf '%s\n' "$P_OUT" | grep '^commit' || true)"
assert_contains "update --plan: the out-of-date broker is to be recreated, with what it loses" \
  "$P_OUT" "$(printf 'recreate\tfactory-broker\tnothing (it keeps no history)')"
assert_empty "update --plan: the up-to-date Coordinator isn't" \
  "$(printf '%s\n' "$P_OUT" | grep "^recreate.factory-coordinator" || true)"
assert_contains "update --plan: no bound project is noted" "$P_OUT" "no project is bound"
assert_contains "update --plan: says up front what every update drops and keeps" \
  "$P_OUT" "$(printf 'note\tevery update stops the dispatch bridge and all agent sandboxes: running jobs end')"
assert_contains "update --plan: the standing note says interrupted jobs, held writes and what is kept" \
  "$P_OUT" "held writes are dropped (queued writes, branches already in your project, saved sessions of sandboxes that aren't recreated"

# Upstream gains a commit touching the Coordinator's kit and the broker's.
mkdir -p "$tmp/seed/kits/coordinator" "$tmp/seed/kits/broker"
echo "x: 1" > "$tmp/seed/kits/coordinator/spec.yaml"
echo "y: 1" > "$tmp/seed/kits/broker/spec.yaml"
git -C "$tmp/seed" add kits
git -C "$tmp/seed" commit -q -m "kits: tighten the Coordinator"
git -C "$tmp/seed" push -q origin HEAD:main
INCOMING="$(git -C "$tmp/seed" rev-parse --short HEAD)"
before="$(git -C "$planco" rev-parse HEAD)"
run_plan "$planco"
assert_eq "update --plan, a commit incoming: exits 0" "$P_RC" "0"
assert_contains "update --plan lists the incoming commit (after a fetch)" \
  "$P_OUT" "$(printf 'commit\t%s\tkits: tighten the Coordinator' "$INCOMING")"
assert_contains "update --plan: the Coordinator may be recreated (its kit changes), labelled a guess" \
  "$P_OUT" "$(printf 'maybe\tfactory-coordinator\tits kit changes (kits/coordinator/spec.yaml) (a guess')"
assert_empty "update --plan: a sandbox already to be recreated isn't listed as maybe too" \
  "$(printf '%s\n' "$P_OUT" | grep "^maybe.factory-broker" || true)"
assert_eq "update --plan doesn't pull: HEAD unchanged" "$(git -C "$planco" rev-parse HEAD)" "$before"
[ ! -e "$planco/kits/coordinator/spec.yaml" ] && ok "update --plan doesn't touch the working tree" \
  || bad "update --plan doesn't touch the working tree"
assert_empty "update --plan: every line is a known kind" \
  "$(printf '%s\n' "$P_OUT" | grep -v -E '^(commit|recreate|maybe|note)	' || true)"

# File names with spaces and glob characters are read whole, never split or expanded.
mkdir -p "$tmp/globdir" && touch "$tmp/globdir/kits-coordinator-expanded"
set +e
G_OUT="$( (
  cd "$tmp/globdir"
  export PATH="$plan_bin:$PATH" XDG_STATE_HOME="$tmp/plan-state" XDG_CONFIG_HOME="$tmp/plan-config"
  # shellcheck disable=SC1091
  . "$FACTORY_SRC/bin/factory"
  ROOT="$planco"
  plan_maybe_sandboxes "$(printf '%s\n' 'kits/coordinator/my spec *.yaml' '*')"
) 2>&1 )"
set -e
assert_eq "plan_maybe_sandboxes reads a name with spaces and '*' whole (one line, the full name)" \
  "$G_OUT" "$(printf 'factory-coordinator\tits kit changes (kits/coordinator/my spec *.yaml)')"

echo "dirty" > "$planco/untracked.txt"
run_plan "$planco"
assert_contains "update --plan notes uncommitted changes (the pull would be skipped)" \
  "$P_OUT" "has uncommitted changes: update skips git pull"
rm -f "$planco/untracked.txt"

git -C "$planco" branch -q --unset-upstream
run_plan "$planco"
assert_eq "update --plan with no upstream: exits 0" "$P_RC" "0"
assert_contains "update --plan with no upstream: notes it" "$P_OUT" "no upstream branch"

run_plan "$planco" --plan --yes
[ "$P_RC" -ne 0 ] && ok "update --plan --yes is refused" || bad "update --plan --yes is refused (got 0)"
assert_contains "update --plan --yes: names the usage" "$P_OUT" "usage: factory update"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
