#!/usr/bin/env bash
set -euo pipefail

# Tests for templates/agent/factory-lane (the sandbox side of the developer's
# private git clones, "lanes") and factory-test's npm mirror lookup via
# FACTORY_PROJECT. factory-lane is run directly on the host against disposable
# local git repos, with FACTORY_LANES_ROOT and HOME pointed at temp dirs: no
# sbx, no docker, no network.
#
# Covered:
# - repos: a single repo, and a folder of repos (hidden dirs, symlinks and
#   non-repos skipped, byte-order sorted).
# - prepare: clones, then fetches (a new host commit shows up as origin/main);
#   the last stdout line is the lane dir.
# - export: exit 3 with no factory/* branches and for a branch already on
#   origin; otherwise stdout is a bundle that "git fetch" into the source repo
#   accepts, holding only factory/* refs, whose prerequisites the host has.
# - invalid lane, rel and ref arguments are rejected.
# - review: checks out a factory ref given in short and in long form.
# - factory-test: with no mirror above the source dir, it looks in
#   $FACTORY_PROJECT/.factory/npm-mirror (only the "no package mirror"
#   message path: an actual install would need yarn/npm and packages).

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LANE="$REPO_ROOT/templates/agent/factory-lane"
FTEST="$REPO_ROOT/templates/agent/factory-test"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tmp="$(cd "$tmp" && pwd -P)"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export HOME="$tmp/home" FACTORY_LANES_ROOT="$tmp/lanes"
export GIT_CONFIG_NOSYSTEM=1
mkdir -p "$HOME"
git config --global init.defaultBranch main

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { # assert_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}

# run CMD...: stdout in $OUT, stderr in $ERR, exit status in $RC.
run() {
  set +e
  "$@" > "$tmp/out" 2> "$tmp/err"
  RC=$?
  set -e
  OUT="$(cat "$tmp/out")"
  ERR="$(cat "$tmp/err")"
}

mkrepo() { # mkrepo DIR: a repo with one commit on main
  git init -q "$1"
  echo one > "$1/file.txt"
  git -C "$1" add file.txt
  git -C "$1" commit -q -m one
}

# --- repos -------------------------------------------------------------------
mkrepo "$tmp/single"
run "$LANE" repos "$tmp/single"
assert_eq "repos: a single repo is '.'" "$RC:$OUT" "0:."

mkdir "$tmp/multi"
mkrepo "$tmp/multi/b-repo"
mkrepo "$tmp/multi/a-repo"
mkrepo "$tmp/multi/Z-repo"
mkrepo "$tmp/multi/.hidden"
mkdir "$tmp/multi/not-a-repo"
echo x > "$tmp/multi/plain-file"
mkrepo "$tmp/elsewhere"
ln -s "$tmp/elsewhere" "$tmp/multi/linked"
git -C "$tmp/multi/a-repo" worktree add -q "$tmp/multi/worktree" -b wt
run "$LANE" repos "$tmp/multi"
assert_eq "repos: a folder of repos, sorted, skipping hidden, symlinked and non-repo dirs (a .git file counts)" \
  "$RC:$(printf '%s,' $OUT)" "0:Z-repo,a-repo,b-repo,worktree,"

run "$LANE" repos "$tmp/multi/not-a-repo"
assert_eq "repos: no repos prints nothing" "$RC:$OUT" "0:"
run "$LANE" repos relative/path
assert_eq "repos: a relative project path is rejected" "$RC" "1"
run "$LANE" repos "$tmp/missing"
assert_eq "repos: a missing project is rejected" "$RC" "1"

# --- prepare -----------------------------------------------------------------
run "$LANE" prepare "$tmp/single" developer
assert_eq "prepare: exit 0" "$RC" "0"
assert_eq "prepare: last stdout line is the lane dir" "$(printf '%s\n' "$OUT" | tail -n 1)" "$tmp/lanes/developer"
lane="$tmp/lanes/developer"
assert_eq "prepare: the lane is a clone of the project" \
  "$(git -C "$lane" rev-parse HEAD)" "$(git -C "$tmp/single" rev-parse HEAD)"
assert_eq "prepare: origin is the project" "$(git -C "$lane" remote get-url origin)" "$tmp/single"

echo two > "$tmp/single/file.txt"
git -C "$tmp/single" commit -q -am two
run "$LANE" prepare "$tmp/single" developer
assert_eq "prepare again: exit 0, lane dir last" "$RC:$(printf '%s\n' "$OUT" | tail -n 1)" "0:$lane"
assert_eq "prepare again: fetched the new host commit as origin/main" \
  "$(git -C "$lane" rev-parse origin/main)" "$(git -C "$tmp/single" rev-parse main)"

run "$LANE" prepare "$tmp/multi" developer-lane2
assert_eq "prepare (folder of repos): exit 0, lane dir last" \
  "$RC:$(printf '%s\n' "$OUT" | tail -n 1)" "0:$tmp/lanes/developer-lane2"
assert_eq "prepare (folder of repos): each repo cloned as a subdirectory" \
  "$(cd "$tmp/lanes/developer-lane2" && echo *)" "Z-repo a-repo b-repo worktree"

run "$LANE" prepare "$tmp/multi/not-a-repo" developer-lane3
assert_eq "prepare: a project without repos fails" "$RC" "1"

# --- export ------------------------------------------------------------------
run "$LANE" export "$tmp/single" developer .
assert_eq "export: no factory/* branches -> exit 3, nothing on stdout" "$RC:$OUT" "3:"

git -C "$lane" checkout -q -b factory/already origin/main
run "$LANE" export "$tmp/single" developer .
assert_eq "export: a factory branch already on origin -> exit 3, nothing on stdout" "$RC:$OUT" "3:"

git -C "$lane" checkout -q -b factory/feature origin/main
echo three > "$lane/file.txt"
git -C "$lane" commit -q -am three
git -C "$lane" checkout -q -b other
echo four > "$lane/file.txt"
git -C "$lane" commit -q -am four
set +e
"$LANE" export "$tmp/single" developer . > "$tmp/lane.bundle" 2> "$tmp/err"
RC=$?
set -e
assert_eq "export: new work -> exit 0" "$RC" "0"
assert_contains "export: stdout is a git bundle" "$(head -c 20 "$tmp/lane.bundle")" "# v2 git bundle"
assert_eq "export: the bundle holds only factory/* refs (not other, not factory/already)" \
  "$(git -C "$tmp/single" bundle list-heads "$tmp/lane.bundle" | awk '{print $2}')" "refs/heads/factory/feature"
run git -C "$tmp/single" bundle verify "$tmp/lane.bundle"
assert_eq "export: the host has the bundle's prerequisites" "$RC" "0"
assert_contains "export: the bundle has prerequisites (it leaves out origin's history)" "$OUT$ERR" "requires"
run git -c core.hooksPath=/dev/null -c core.fsmonitor=false -C "$tmp/single" fetch --no-tags \
  --no-write-fetch-head --quiet "$tmp/lane.bundle" '+refs/heads/factory/*:refs/remotes/factory-developer/factory/*'
assert_eq "export: git fetch into the source repo accepts the bundle" "$RC" "0"
assert_eq "export: the fetched ref is the lane's commit" \
  "$(git -C "$tmp/single" rev-parse refs/remotes/factory-developer/factory/feature)" \
  "$(git -C "$lane" rev-parse factory/feature)"

run "$LANE" export "$tmp/multi" developer-lane2 a-repo
assert_eq "export (folder of repos): no factory branches -> exit 3" "$RC" "3"
run "$LANE" export "$tmp/single" developer-lane9 .
assert_eq "export: a lane that was never prepared -> exit 3" "$RC:$OUT" "3:"

# --- invalid arguments -------------------------------------------------------
for bad_lane in Developer d "-x" "dev/../x" "dev_1" "a$(printf 'b%.0s' $(seq 41))" "dev
x"; do
  run "$LANE" prepare "$tmp/single" "$bad_lane"
  assert_eq "invalid lane '$bad_lane' is rejected" "$RC:$OUT" "1:"
done
for bad_rel in ../single "" .hidden linked not-a-repo /tmp; do
  run "$LANE" export "$tmp/multi" developer-lane2 "$bad_rel"
  assert_eq "invalid rel '$bad_rel' is rejected" "$RC:$OUT" "1:"
done
run "$LANE" export "$tmp/single" developer a-repo
assert_eq "invalid rel 'a-repo' for a single repo is rejected" "$RC:$OUT" "1:"
for bad_ref in "-x" "--upload-pack=touch" "factory-developer/../x" "a b" 'a;b' 'x@{1}' "refs/heads/x.lock" \
               "factory-developer/factory/nope" "refs/remotes/factory-developer/factory/nope" "a//b" "/x"; do
  run "$LANE" review "$tmp/single" "$bad_ref"
  assert_eq "invalid or unknown ref '$bad_ref' is rejected" "$RC:$OUT" "1:"
done
run "$LANE" export "$tmp/single" developer
assert_eq "export: missing rel is a usage error" "$RC" "1"
run "$LANE" frobnicate
assert_eq "an unknown command is rejected" "$RC" "1"
run "$LANE" -h
assert_contains "-h prints the usage" "$OUT" "factory-lane prepare <project> <lane>"

# --- review ------------------------------------------------------------------
run "$LANE" review "$tmp/single" factory-developer/factory/feature
assert_eq "review (short ref): exit 0" "$RC" "0"
rdir="$(printf '%s\n' "$OUT" | tail -n 1)"
assert_contains "review: the checkout is under \$HOME/review" "$rdir" "$HOME/review/"
assert_eq "review: HEAD is the factory branch's commit" \
  "$(git -C "$rdir" rev-parse HEAD)" "$(git -C "$lane" rev-parse factory/feature)"
assert_eq "review: on branch review" "$(git -C "$rdir" symbolic-ref --short HEAD)" "review"
assert_eq "review: origin/main...HEAD is the change" \
  "$(git -C "$rdir" log --format=%s origin/main..HEAD)" "three"

echo scribble > "$rdir/file.txt"
echo junk > "$rdir/untracked.txt"
run "$LANE" review "$tmp/single" refs/remotes/factory-developer/factory/feature
assert_eq "review (long ref): exit 0, same checkout reused" "$RC:$(printf '%s\n' "$OUT" | tail -n 1)" "0:$rdir"
assert_eq "review again: local edits discarded" "$(cat "$rdir/file.txt"):$(test -e "$rdir/untracked.txt" && echo kept || echo gone)" "three:gone"

run "$LANE" review "$tmp/multi" main a-repo
assert_eq "review (folder of repos, rel a-repo, branch main): exit 0" "$RC" "0"
assert_eq "review: rel's checkout is at its main" \
  "$(git -C "$(printf '%s\n' "$OUT" | tail -n 1)" rev-parse HEAD)" "$(git -C "$tmp/multi/a-repo" rev-parse main)"
run "$LANE" review "$tmp/multi" main
assert_eq "review: rel defaults to '.', which a folder of repos lacks" "$RC" "1"

# --- factory-test: npm mirror via FACTORY_PROJECT ----------------------------
mkdir -p "$tmp/ftlane/app"
echo '{"name":"app","version":"1.0.0"}' > "$tmp/ftlane/app/package.json"
: > "$tmp/ftlane/app/yarn.lock"
run env FACTORY_PROJECT="$tmp/ftproject" FACTORY_TEST_ROOT="$tmp/ftroot" "$FTEST" -m clean -C "$tmp/ftlane/app"
assert_eq "factory-test: fails without a mirror" "$RC" "1"
assert_contains "factory-test: looks for the mirror in \$FACTORY_PROJECT when none is above the source" \
  "$ERR" "no package mirror at $tmp/ftproject/.factory/npm-mirror"
mkdir -p "$tmp/ftlane/.factory/npm-mirror"
run env FACTORY_PROJECT="$tmp/ftproject" FACTORY_TEST_ROOT="$tmp/ftroot" "$FTEST" -m host -C "$tmp/ftlane/app"
assert_contains "factory-test: host mode without node_modules anywhere still says so" \
  "$ERR" "host mode needs the project's node_modules"

# Host mode in a lane clone: node_modules come from the same path in the project.
if command -v node >/dev/null && command -v rsync >/dev/null; then
  mkdir -p "$tmp/ftproject/app/node_modules/dep" "$tmp/ftproject/app/pkgs/x/node_modules/inner"
  echo '{"name":"dep","version":"1.0.0"}' > "$tmp/ftproject/app/node_modules/dep/package.json"
  echo '{"name":"inner","version":"1.0.0"}' > "$tmp/ftproject/app/pkgs/x/node_modules/inner/package.json"
  echo 'host-only' > "$tmp/ftproject/app/host-only.txt"
  run env FACTORY_PROJECT="$tmp/ftproject" FACTORY_LANE_ROOT="$tmp/ftlane" FACTORY_TEST_ROOT="$tmp/ftroot" \
    "$FTEST" -m host -C "$tmp/ftlane/app"
  copy="$(printf '%s\n' "$OUT" | tail -n 1)"
  assert_eq "factory-test host mode in a lane: exit 0" "$RC" "0"
  assert_eq "factory-test host mode in a lane: the copy has the project's node_modules, nested ones too" \
    "$(test -f "$copy/node_modules/dep/package.json" && test -f "$copy/pkgs/x/node_modules/inner/package.json" && echo yes)" "yes"
  assert_eq "factory-test host mode in a lane: the copy has the lane's files, not the project's" \
    "$(test -f "$copy/yarn.lock" && test ! -e "$copy/host-only.txt" && echo yes)" "yes"
  assert_eq "factory-test host mode in a lane: the project is untouched" \
    "$(ls -A "$tmp/ftproject/app/node_modules")" "dep"
  run env FACTORY_PROJECT="$tmp/ftproject" FACTORY_LANE_ROOT="$tmp/elsewhere" FACTORY_TEST_ROOT="$tmp/ftroot" \
    "$FTEST" -m host -C "$tmp/ftlane/app"
  assert_contains "factory-test host mode: a source outside FACTORY_LANE_ROOT doesn't borrow node_modules" \
    "$ERR" "host mode needs the project's node_modules"
else
  echo "skip - factory-test host mode in a lane (needs node and rsync)"
fi

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
