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
#   the last stdout line is the lane dir. Each lane repo gets a repo-local
#   git identity: factory-<lane> <<lane>@factory.invalid> by default, or the
#   given name and email ("{lane}" replaced, empty = default), taken literally;
#   invalid identities (control characters, "<>", a leading "-", a malformed
#   email) are rejected before anything is changed.
# - export: exit 3 with no factory/* branches and for a branch already on
#   origin; otherwise stdout is a bundle that "git fetch" into the source repo
#   accepts, holding only factory/* refs, whose prerequisites the host has,
#   even after the lane moved its origin/main past the project; "full" has
#   no prerequisites.
# - bundle: writes <base>..<branch> into the project's .factory/outgoing and
#   prints the file name; refuses non-factory branches, unknown branches and
#   bases, and a branch with nothing past its base.
# - invalid lane, rel and ref arguments are rejected.
# - review: checks out a factory ref given in short and in long form.
# - submodules: prepare and review initialise the ones the project's repo has
#   initialised (from its module store, at the pin; a second prepare follows a
#   moved pin) and skip, with a note, the ones it has not; a pin missing from
#   the module store fails.
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
ident() { printf '%s <%s>' "$(git -C "$1" config --local user.name)" "$(git -C "$1" config --local user.email)"; }
assert_eq "prepare: the lane has the default repo-local identity" "$(ident "$lane")" \
  "factory-developer <developer@factory.invalid>"

echo two > "$tmp/single/file.txt"
git -C "$tmp/single" commit -q -am two
run "$LANE" prepare "$tmp/single" developer
assert_eq "prepare again: exit 0, lane dir last" "$RC:$(printf '%s\n' "$OUT" | tail -n 1)" "0:$lane"
assert_eq "prepare again: fetched the new host commit as origin/main" \
  "$(git -C "$lane" rev-parse origin/main)" "$(git -C "$tmp/single" rev-parse main)"

git -C "$tmp/single" checkout -q -b feature
run "$LANE" prepare "$tmp/single" developer
assert_eq "prepare again: origin/HEAD follows the branch checked out in the project" \
  "$(git -C "$lane" symbolic-ref refs/remotes/origin/HEAD)" "refs/remotes/origin/feature"
git -C "$tmp/single" checkout -q main

run "$LANE" prepare "$tmp/multi" developer-lane2
assert_eq "prepare (folder of repos): exit 0, lane dir last" \
  "$RC:$(printf '%s\n' "$OUT" | tail -n 1)" "0:$tmp/lanes/developer-lane2"
assert_eq "prepare (folder of repos): each repo cloned as a subdirectory" \
  "$(cd "$tmp/lanes/developer-lane2" && echo *)" "Z-repo a-repo b-repo worktree"
assert_eq "prepare (folder of repos): every repo has the lane's identity" \
  "$(for r in Z-repo a-repo b-repo worktree; do ident "$tmp/lanes/developer-lane2/$r"; echo; done | sort -u)" \
  "factory-developer-lane2 <developer-lane2@factory.invalid>"

# --- prepare: configured identity ----------------------------------------------
# A lane of its own: re-preparing "developer" would move its origin/HEAD.
ilane="$tmp/lanes/identity"
run "$LANE" prepare "$tmp/single" identity "Acme Bot ({lane})" "bot+{lane}@example.com"
assert_eq "prepare with an identity: exit 0" "$RC" "0"
assert_eq "prepare with an identity: set on a new lane, {lane} replaced" "$(ident "$ilane")" \
  "Acme Bot (identity) <bot+identity@example.com>"
run "$LANE" prepare "$tmp/single" identity '$(touch pwned) `id` "Ünïcode" & co' 'x$(id)@example.com'
assert_eq "prepare with an identity: values are taken literally, never run" \
  "$RC:$(ident "$ilane"):$(find "$ilane" "$PWD" -maxdepth 1 -name '*pwned*' | wc -l | tr -d ' ')" \
  '0:$(touch pwned) `id` "Ünïcode" & co <x$(id)@example.com>:0'
run "$LANE" prepare "$tmp/single" identity "" "team@example.com"
assert_eq "prepare with an identity: an empty name means the default" "$(ident "$ilane")" \
  "factory-identity <team@example.com>"
run "$LANE" prepare "$tmp/single" identity "Team" ""
assert_eq "prepare with an identity: an empty email means the default" "$(ident "$ilane")" \
  "Team <identity@factory.invalid>"
run "$LANE" prepare "$tmp/single" identity "Team"
assert_eq "prepare: a name without an email is a usage error" "$RC:$OUT:$(ident "$ilane")" \
  "1::Team <identity@factory.invalid>"
long="$(printf 'n%.0s' $(seq 129))"
for bad_name in "a
b" "$(printf 'a\tb')" "$(printf 'a\033[31m')" "<x>" "a>b" "-x" "   " "$long"; do
  run "$LANE" prepare "$tmp/single" identity "$bad_name" "ok@example.com"
  assert_eq "invalid name '$bad_name' is rejected, the identity unchanged" "$RC:$OUT:$(ident "$ilane")" \
    "1::Team <identity@factory.invalid>"
done
for bad_email in "a b@example.com" "no-at" "a@b@c" "-a@b" "a@" "@b" "a@b
c" "<a@b>" "$(printf 'a\001@b')"; do
  run "$LANE" prepare "$tmp/single" identity "Team" "$bad_email"
  assert_eq "invalid email '$bad_email' is rejected, the identity unchanged" "$RC:$OUT:$(ident "$ilane")" \
    "1::Team <identity@factory.invalid>"
done

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

# The role moved the lane's origin/main past the project (importing a git_fetch
# bundle into origin/main does that) and built on it. The bundle must leave out
# only what the project really has, or the project can't import it.
git -C "$lane" checkout -q -b factory/moved factory/feature
echo upstream > "$lane/upstream.txt"
git -C "$lane" add upstream.txt
git -C "$lane" commit -q -m upstream
git -C "$lane" update-ref refs/remotes/origin/main HEAD
echo five > "$lane/file.txt"
git -C "$lane" commit -q -am five
set +e
"$LANE" export "$tmp/single" developer . > "$tmp/lane2.bundle" 2> "$tmp/err"
RC=$?
set -e
assert_eq "export (origin moved past the project): exit 0" "$RC" "0"
run git -C "$tmp/single" bundle verify "$tmp/lane2.bundle"
assert_eq "export (origin moved past the project): the project has the bundle's prerequisites" "$RC" "0"
run git -c core.hooksPath=/dev/null -c core.fsmonitor=false -C "$tmp/single" fetch --no-tags \
  --no-write-fetch-head --quiet "$tmp/lane2.bundle" '+refs/heads/factory/*:refs/remotes/factory-developer/factory/*'
assert_eq "export (origin moved past the project): the project imports it" "$RC" "0"
assert_eq "export (origin moved past the project): the fetched ref is the lane's commit" \
  "$(git -C "$tmp/single" rev-parse refs/remotes/factory-developer/factory/moved)" \
  "$(git -C "$lane" rev-parse factory/moved)"
run "$LANE" export "$tmp/single" developer .
assert_eq "export: once the project imported everything -> exit 3" "$RC:$OUT" "3:"

set +e
"$LANE" export "$tmp/single" developer . full > "$tmp/full.bundle" 2> "$tmp/err"
RC=$?
set -e
assert_eq "export full: exit 0 even when the project has everything" "$RC" "0"
run git -C "$tmp/single" bundle verify "$tmp/full.bundle"
assert_eq "export full: the bundle verifies" "$RC" "0"
case "$OUT$ERR" in
  *requires*) bad "export full: the bundle has no prerequisites" ;;
  *) ok "export full: the bundle has no prerequisites" ;;
esac
run "$LANE" export "$tmp/single" developer . partial
assert_eq "export: a fourth argument other than 'full' is rejected" "$RC" "1"

# --- bundle ------------------------------------------------------------------
run "$LANE" bundle "$tmp/single" developer . factory/moved
assert_eq "bundle: no .factory/outgoing in the project -> exit 1" "$RC" "1"
assert_contains "bundle: ...and says so" "$ERR" "is missing"
mkdir -p "$tmp/single/.factory/outgoing"
run "$LANE" bundle "$tmp/single" developer . factory/moved
assert_eq "bundle: exit 0 and prints the file name" "$RC:$OUT" "0:moved.bundle"
assert_contains "bundle: written to the project's .factory/outgoing" \
  "$(head -c 20 "$tmp/single/.factory/outgoing/moved.bundle")" "# v2 git bundle"
assert_eq "bundle: holds refs/heads/factory/moved" \
  "$(git -C "$tmp/single" bundle list-heads "$tmp/single/.factory/outgoing/moved.bundle" | awk '{print $2}')" \
  "refs/heads/factory/moved"
# origin/HEAD follows the branch the project has checked out when the lane was
# prepared: "feature" above.
assert_contains "bundle: the default base is origin/HEAD" "$ERR" "origin/feature..factory/moved"
run "$LANE" bundle "$tmp/single" developer . factory/moved main
assert_eq "bundle: an explicit base branch" "$RC:$OUT" "0:moved.bundle"
assert_contains "bundle: reports the range" "$ERR" "1 commit(s), origin/main..factory/moved"
run "$LANE" bundle "$tmp/single" developer . other
assert_eq "bundle: a branch outside factory/ is refused" "$RC" "1"
assert_contains "bundle: ...and says why" "$ERR" "only factory/* branches"
run "$LANE" bundle "$tmp/single" developer . factory/nope
assert_eq "bundle: an unknown branch -> exit 1" "$RC" "1"
run "$LANE" bundle "$tmp/single" developer . factory/moved nope
assert_eq "bundle: an unknown base -> exit 1" "$RC" "1"
git -C "$lane" checkout -q -b factory/empty origin/main
run "$LANE" bundle "$tmp/single" developer . factory/empty main
assert_eq "bundle: a branch with nothing past its base -> exit 1" "$RC" "1"
assert_contains "bundle: ...and says so" "$ERR" "no commits beyond"
git -C "$tmp/lanes/developer-lane2/a-repo" checkout -q -b factory/multi-topic origin/main
echo m > "$tmp/lanes/developer-lane2/a-repo/m.txt"
git -C "$tmp/lanes/developer-lane2/a-repo" add m.txt
git -C "$tmp/lanes/developer-lane2/a-repo" commit -q -m m
mkdir -p "$tmp/multi/.factory/outgoing"
run "$LANE" bundle "$tmp/multi" developer-lane2 a-repo factory/multi-topic
assert_eq "bundle (folder of repos): the name carries the repo" "$RC:$OUT" "0:a-repo--multi-topic.bundle"
run "$LANE" bundle "$tmp/multi" developer-lane2 nope factory/multi-topic
assert_eq "bundle: an unknown repo -> exit 1" "$RC" "1"

# --- submodules ----------------------------------------------------------------
# A submodule the project's repo has initialised is initialised in the lane at
# the superproject's pin, from the project's own module store; one it has not
# is left empty with a note. The fixture's remotes are local paths, which git
# refuses as submodule transports unless allowed.
mkrepo "$tmp/subrepo"
mkrepo "$tmp/subrepo2"
mkrepo "$tmp/subproj"
git -C "$tmp/subproj" -c protocol.file.allow=always submodule add -q "$tmp/subrepo" mod
git -C "$tmp/subproj" -c protocol.file.allow=always submodule add -q "$tmp/subrepo2" other
git -C "$tmp/subproj" commit -q -m "add submodules"
# "other" was never initialised on this host: no module store (the broken
# gitfile it leaves behind is why later commits here name their paths).
rm -rf "$tmp/subproj/.git/modules/other"
pin() { git -C "$tmp/subproj" rev-parse "HEAD:$1"; }

run "$LANE" prepare "$tmp/subproj" subs
assert_eq "submodules: prepare exits 0" "$RC" "0"
sublane="$tmp/lanes/subs"
assert_eq "submodules: an initialised submodule is checked out at the pin" \
  "$(git -C "$sublane/mod" rev-parse HEAD)" "$(pin mod)"
assert_eq "submodules: ...from the project's module store, not the .gitmodules url" \
  "$(git -C "$sublane" config submodule.mod.url)" "$tmp/subproj/.git/modules/mod"
assert_eq "submodules: one with no module store is left empty" "$(ls -A "$sublane/other")" ""
assert_contains "submodules: ...and the skip is named" "$ERR" "submodule other is not initialised"
assert_eq "submodules: the skip is the only note about it (the initialised one is quiet)" \
  "$(printf '%s\n' "$ERR" | grep -c 'submodule mod')" "0"

old_pin="$(pin mod)"
git -C "$tmp/subrepo" commit -q --allow-empty -m "sub two"
git -C "$tmp/subproj/mod" fetch -q origin
git -C "$tmp/subproj/mod" checkout -q FETCH_HEAD
git -C "$tmp/subproj" add mod
git -C "$tmp/subproj" commit -q -m "bump mod"
# prepare fetches; the lane's own branch (and so its pin) moves when the role merges.
run "$LANE" prepare "$tmp/subproj" subs
assert_eq "submodules: a second prepare exits 0 and keeps the lane's own pin" \
  "$RC:$(git -C "$sublane/mod" rev-parse HEAD)" "0:$old_pin"
git -C "$sublane" merge -q --ff-only origin/main
run "$LANE" prepare "$tmp/subproj" subs
assert_eq "submodules: prepare after the pin moved exits 0" "$RC" "0"
assert_eq "submodules: ...and updates the submodule to the new pin" \
  "$(git -C "$sublane/mod" rev-parse HEAD)" "$(pin mod)"
assert_eq "submodules: ...which is not the old one" \
  "$([ "$(pin mod)" != "$old_pin" ] && echo moved)" "moved"

run "$LANE" review "$tmp/subproj" main
assert_eq "submodules: review exits 0" "$RC" "0"
sdir="$(printf '%s\n' "$OUT" | tail -n 1)"
assert_eq "submodules: review initialises the submodule at the pin" \
  "$(git -C "$sdir/mod" rev-parse HEAD)" "$(pin mod)"
assert_contains "submodules: review names the one it skips" "$ERR" "submodule other is not initialised"
run "$LANE" review "$tmp/subproj" main
assert_eq "submodules: review again exits 0" "$RC" "0"

# A pin the module store lacks is an error, not a skip.
git -C "$tmp/subproj" update-index --cacheinfo "160000,$(printf '1%.0s' $(seq 40)),mod"
git -C "$tmp/subproj" commit -q -m "pin nobody has"
run "$LANE" prepare "$tmp/subproj" subs-bad
assert_eq "submodules: a pin missing from the module store fails the prepare" "$RC" "1"
assert_contains "submodules: ...and names the submodule" "$ERR" "can't initialise submodule mod"

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
