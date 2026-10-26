#!/usr/bin/env bash
set -euo pipefail

# Regression tests for "factory update" and the broker image's build context.
# broker/ is a plain directory of this repository:
#
# 1. scripts/lib/roles.sh reads broker/tools.txt and dies, naming the file,
#    when it is missing.
# 2. bin/factory's pull_factory fast-forwards the checkout (broker/ included)
#    and refuses - rather than pulling over it - when the checkout has
#    uncommitted changes, tracked or untracked, in broker/ or anywhere else.
#    Git-ignored files (e.g. a __pycache__/ from running the broker's tests)
#    don't block it.
# 3. The broker image's build context (scripts/lib/common.sh's broker_export
#    and prepare_broker_context) is a "git archive" export of the committed
#    broker/ (HEAD), byte-for-byte, never the working tree: an edited,
#    untracked or git-ignored file planted in broker/ doesn't reach it, and a
#    checkout without a committed broker/ is refused. Uncommitted work in
#    broker/ is noted, not refused.
# 4. scripts/00-build-templates.sh is run for real against a fixture, with
#    stub docker and sbx commands, to show it builds from that export.
# 5. An exported ROOT doesn't redirect a direct run of bin/factory.
# 6. `factory update --plan` (what the dashboard's UPDATE key shows before
#    asking to confirm) changes nothing but the remote-tracking refs: it
#    lists incoming commits, the sandboxes update would recreate (and what
#    each loses), the ones the incoming files (including anything under
#    broker/) may make stale, and notes for a dirty checkout or a missing
#    upstream; --plan with --yes is refused.
#
# broker/ is writable by an agent working on this repo's own code like any
# other file, so the build-context rules matter for real.
#
# This test sources the real bin/factory (for pull_factory) and
# scripts/lib/common.sh (for the broker_* helpers) and calls them directly,
# against disposable local fixture repos. There is no other shell test
# harness in this repo (bin/factory is otherwise driven by sbx/docker, which
# this test must not require), so this exercises the git plumbing directly:
# no network access, no sbx, no docker.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_empty() { [ -z "$2" ] && ok "$1" || bad "$1 (expected empty, got: $2)"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { # assert_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}
assert_not_contains() {
  case "$2" in *"$3"*) bad "$1 (did not expect '$3', got: $2)" ;; *) ok "$1" ;; esac
}

# --- fixtures: an "origin" holding the factory with a broker/ directory ----
# tools.txt is what roles.sh reads. Dockerfile, passdesk/app.py and the *.pyc
# ignore rule mirror the real broker's layout for the build-context tests.
git init -q --bare -b main "$tmp/origin.git"
git clone -q "$tmp/origin.git" "$tmp/seed"
git -C "$tmp/seed" checkout -q -b main 2>/dev/null || true
mkdir -p "$tmp/seed/broker/passdesk"
echo "git_fetch" > "$tmp/seed/broker/tools.txt"
echo "FROM scratch" > "$tmp/seed/broker/Dockerfile"
printf '__pycache__/\n*.pyc\n' > "$tmp/seed/broker/.gitignore"
echo "print('reviewed')" > "$tmp/seed/broker/passdesk/app.py"
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
git -C "$tmp/seed" add -A
git -C "$tmp/seed" commit -q -m "broker v1, with a stub bin/factory"
git -C "$tmp/seed" push -q origin HEAD:main

# FACTORY_SRC: a copy of this repo's bin/ and scripts/ with a stub
# broker/tools.txt, for the tests that source bin/factory. Sourcing it always
# loads scripts/lib/roles.sh against its own root (it resets ROOT first),
# which dies without broker/tools.txt; the copy keeps those tests independent
# of the real broker/ (factory-test's copy has no .git). They set ROOT to a
# fixture afterwards.
FACTORY_SRC="$tmp/factory-src"
mkdir -p "$FACTORY_SRC/broker"
cp -R "$REPO_ROOT/bin" "$REPO_ROOT/scripts" "$FACTORY_SRC/"
echo "git_fetch" > "$FACTORY_SRC/broker/tools.txt"

# --- helpers for exercising the real code -----------------------------------

# fresh_checkout NAME -> path to a fresh clone of origin.git, tracking main,
# under $tmp/NAME.
fresh_checkout() {
  git clone -q "$tmp/origin.git" "$tmp/$1" >/dev/null 2>&1
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

# ===========================================================================
# Part 1: scripts/lib/roles.sh reads broker/tools.txt, and dies without it.
# ===========================================================================
roles_co="$(fresh_checkout roles-co)"
(
  # shellcheck disable=SC1091
  . "$REPO_ROOT/scripts/lib/common.sh"
  ROOT="$roles_co"
  # shellcheck disable=SC1091
  . "$REPO_ROOT/scripts/lib/roles.sh"
) && ok "sourcing roles.sh succeeds with broker/tools.txt" \
  || bad "sourcing roles.sh succeeds with broker/tools.txt"
# The tool names themselves are read by factory/roles.py (role validation).
assert_eq "the tool names come from broker/tools.txt (factory/roles.py known_broker_tools)" \
  "$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_ROOT" python3 -c '
import sys
from factory import roles
from factory.context import Paths
print(roles.known_broker_tools(Paths(sys.argv[1], "", "")))' "$roles_co")" "git_fetch"

rm "$roles_co/broker/tools.txt"
set +e
roles_out="$( (
  # shellcheck disable=SC1091
  . "$REPO_ROOT/scripts/lib/common.sh"
  ROOT="$roles_co"
  # shellcheck disable=SC1091
  . "$REPO_ROOT/scripts/lib/roles.sh"
) 2>&1 )"
roles_rc=$?
set -e
assert_eq "sourcing roles.sh without broker/tools.txt fails" "$roles_rc" "1"
assert_contains "roles.sh names the missing file" "$roles_out" "$roles_co/broker/tools.txt is missing"
assert_not_contains "roles.sh does not mention a submodule" "$roles_out" "submodule"

# ===========================================================================
# Part 2: pull_factory pulls broker/ with the rest, and refuses over
# uncommitted work.
# ===========================================================================
checkout="$(fresh_checkout checkout)"
assert_empty "clean checkout: status is empty before any pull" "$(git -C "$checkout" status --porcelain)"

run_pull_factory "$checkout"
assert_eq "pull_factory on an already-current checkout exits 0" "$PF_RC" "0"
assert_contains "pull_factory on an already-current checkout reports up to date" "$PF_OUT" "already up to date"

# An upstream commit that changes the broker.
echo "print('v2')" > "$tmp/seed/broker/passdesk/app.py"
git -C "$tmp/seed" commit -q -am "broker v2"
git -C "$tmp/seed" push -q origin HEAD:main
run_pull_factory "$checkout"
assert_eq "pull_factory exits 0 pulling a commit that changes broker/" "$PF_RC" "0"
assert_eq "pull_factory brings broker/ to the new content" "$(cat "$checkout/broker/passdesk/app.py")" "print('v2')"
assert_eq "the pull leaves the checkout clean" "$(git -C "$checkout" status --porcelain)" ""
assert_contains "pull_factory re-execs 'factory update --pulled' after a real pull" "$PF_OUT" "stub factory: update --pulled"

# --- a dirty tracked file in broker/ ----------------------------------------
echo "print('v3')" > "$tmp/seed/broker/passdesk/app.py"
git -C "$tmp/seed" commit -q -am "broker v3"
git -C "$tmp/seed" push -q origin HEAD:main
V3="$(git -C "$tmp/seed" rev-parse HEAD)"

dirty_tracked="$(fresh_checkout dirty-tracked)"
git -C "$dirty_tracked" reset -q --hard "HEAD~1"
echo "more" >> "$dirty_tracked/broker/tools.txt"
before="$(git -C "$dirty_tracked" rev-parse HEAD)"
run_pull_factory "$dirty_tracked"
assert_eq "dirty tracked file in broker/: pull_factory exits 0 (refuses, does not die)" "$PF_RC" "0"
assert_contains "dirty tracked file in broker/ is refused with a clear message" "$PF_OUT" "uncommitted changes"
assert_eq "dirty tracked file in broker/: HEAD is left untouched" "$(git -C "$dirty_tracked" rev-parse HEAD)" "$before"

# --- an untracked file in broker/ -------------------------------------------
untracked="$(fresh_checkout untracked)"
git -C "$untracked" reset -q --hard "HEAD~1"
echo "x" > "$untracked/broker/stray.txt"
before="$(git -C "$untracked" rev-parse HEAD)"
run_pull_factory "$untracked"
assert_eq "untracked file in broker/: pull_factory exits 0 (refuses, does not die)" "$PF_RC" "0"
assert_contains "untracked file in broker/ is refused with a clear message" "$PF_OUT" "uncommitted changes"
assert_eq "untracked file in broker/: HEAD is left untouched" "$(git -C "$untracked" rev-parse HEAD)" "$before"

# --- a git-ignored file in broker/ does not block ---------------------------
ignored="$(fresh_checkout ignored-pyc)"
git -C "$ignored" reset -q --hard "HEAD~1"
mkdir -p "$ignored/broker/passdesk/__pycache__"
echo "planted bytecode" > "$ignored/broker/passdesk/__pycache__/app.cpython-312.pyc"
run_pull_factory "$ignored"
assert_eq "git-ignored files in broker/: pull_factory exits 0" "$PF_RC" "0"
assert_eq "git-ignored files in broker/ don't block the pull" "$(git -C "$ignored" rev-parse HEAD)" "$V3"

# ===========================================================================
# Part 3: the broker build context is an export of the committed broker/.
# ===========================================================================
committed_tree="$tmp/committed-tree"
mkdir "$committed_tree"
git -C "$tmp/seed" archive "$V3:broker" | tar -x -f - -C "$committed_tree"

export_co="$(fresh_checkout export-co)"
mkdir "$tmp/ctx-clean"
run_common broker_export "$export_co" "$tmp/ctx-clean"
assert_eq "broker_export succeeds on a clean checkout" "$C_RC" "0"
diff -r "$committed_tree" "$tmp/ctx-clean" >/dev/null \
  && ok "the exported build context is broker/'s committed tree exactly (Dockerfile at its top)" \
  || bad "the exported build context is broker/'s committed tree exactly"

# A planted ignored .pyc, an edit to a tracked file and an untracked file
# are all in the working tree and none of them in the export.
mkdir -p "$export_co/broker/passdesk/__pycache__"
echo "planted bytecode" > "$export_co/broker/passdesk/__pycache__/app.cpython-312.pyc"
echo "print('planted')" > "$export_co/broker/passdesk/app.py"
echo "planted" > "$export_co/broker/planted.txt"
mkdir "$tmp/ctx-planted"
run_common broker_export "$export_co" "$tmp/ctx-planted"
assert_eq "broker_export succeeds with planted files in the broker/ working tree" "$C_RC" "0"
diff -r "$committed_tree" "$tmp/ctx-planted" >/dev/null \
  && ok "an ignored .pyc, an edited file and an untracked file in broker/ don't reach the build context" \
  || bad "an ignored .pyc, an edited file and an untracked file in broker/ don't reach the build context"

mkdir "$tmp/ctx-prepared"
run_common prepare_broker_context "$export_co" "$tmp/ctx-prepared"
assert_eq "prepare_broker_context accepts uncommitted work in broker/" "$C_RC" "0"
assert_contains "prepare_broker_context notes that uncommitted work is not included" "$C_OUT" "uncommitted changes in $export_co/broker are not included"
diff -r "$committed_tree" "$tmp/ctx-prepared" >/dev/null \
  && ok "prepare_broker_context exports the committed tree" \
  || bad "prepare_broker_context exports the committed tree"

# A deleted tracked file is still in the export.
rm "$export_co/broker/Dockerfile"
mkdir "$tmp/ctx-deleted"
run_common broker_export "$export_co" "$tmp/ctx-deleted"
[ -f "$tmp/ctx-deleted/Dockerfile" ] && ok "a Dockerfile deleted from the working tree is still in the export" \
  || bad "a Dockerfile deleted from the working tree is still in the export"

# A clean checkout prints no note.
clean_co="$(fresh_checkout clean-co)"
mkdir "$tmp/ctx-clean2"
run_common prepare_broker_context "$clean_co" "$tmp/ctx-clean2"
assert_eq "prepare_broker_context on a clean checkout: exits 0" "$C_RC" "0"
assert_empty "prepare_broker_context on a clean checkout: prints nothing" "$C_OUT"

# Work added to the index but not committed is not committed either.
staged_co="$(fresh_checkout staged-co)"
echo "print('staged')" > "$staged_co/broker/passdesk/app.py"
git -C "$staged_co" add broker/passdesk/app.py
mkdir "$tmp/ctx-staged"
run_common broker_export "$staged_co" "$tmp/ctx-staged"
assert_eq "a staged, uncommitted edit to broker/ is not exported" "$(cat "$tmp/ctx-staged/passdesk/app.py")" "print('v3')"

# A checkout without a committed broker/ is refused, and nothing is written.
nobroker="$tmp/nobroker"
git init -q "$nobroker"
git -C "$nobroker" commit -q --allow-empty -m "no broker"
mkdir -p "$nobroker/broker" && echo "git_fetch" > "$nobroker/broker/tools.txt"
mkdir "$tmp/ctx-nobroker"
run_common broker_export "$nobroker" "$tmp/ctx-nobroker"
assert_eq "broker_export refuses a checkout whose broker/ is not committed" "$C_RC" "1"
assert_contains "broker_export's refusal names the problem" "$C_OUT" "broker/tools.txt is not in the committed tree"
assert_empty "broker_export wrote nothing for an uncommitted broker/" "$(ls -A "$tmp/ctx-nobroker")"

# ===========================================================================
# Part 4: 00-build-templates.sh, run for real with stub docker and sbx.
# ===========================================================================
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
# scripts/lib) from inside fixture checkout DIR, so its ROOT is DIR. Output
# in $B_OUT, status in $B_RC, the broker's build context copied to $DIR.ctx.
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

build_co="$(fresh_checkout build-co)"
mkdir -p "$build_co/broker/passdesk/__pycache__"
echo "planted bytecode" > "$build_co/broker/passdesk/__pycache__/app.cpython-312.pyc"
run_build "$build_co"
assert_eq "00-build-templates.sh succeeds on a checkout with a committed broker/ (stub docker)" "$B_RC" "0"
diff -r "$committed_tree" "$build_co.ctx" >/dev/null \
  && ok "00-build-templates.sh builds the broker from exactly the committed broker/ tree" \
  || bad "00-build-templates.sh builds the broker from exactly the committed broker/ tree"
case "$(cat "$build_co.ctx.context")" in
  "$build_co"/*|"$build_co") bad "00-build-templates.sh's broker context is not inside the checkout" ;;
  *) ok "00-build-templates.sh's broker context is not inside the checkout" ;;
esac
assert_eq "00-build-templates.sh takes the broker Dockerfile from the exported context" \
  "$(cat "$build_co.ctx.dockerfile")" "$(cat "$build_co.ctx.context")/Dockerfile"
assert_not_contains "00-build-templates.sh doesn't mention a submodule or pin" "$B_OUT" "submodule"

echo "print('edited')" > "$build_co/broker/passdesk/app.py"
echo "planted" > "$build_co/broker/planted.txt"
run_build "$build_co"
assert_eq "00-build-templates.sh still builds with uncommitted work in broker/" "$B_RC" "0"
assert_contains "00-build-templates.sh notes that it is not in the image" "$B_OUT" "uncommitted changes in $build_co/broker are not included"
diff -r "$committed_tree" "$build_co.ctx" >/dev/null \
  && ok "uncommitted work in broker/ doesn't reach the broker image's build context" \
  || bad "uncommitted work in broker/ doesn't reach the broker image's build context"

rm -rf "$build_co.ctx"
run_build "$nobroker"
assert_eq "00-build-templates.sh refuses a checkout whose broker/ is not committed" "$B_RC" "1"
assert_contains "00-build-templates.sh's refusal names the problem" "$B_OUT" "not in the committed tree"
[ ! -e "$nobroker.ctx" ] && ok "00-build-templates.sh built nothing without a committed broker/" \
  || bad "00-build-templates.sh built nothing without a committed broker/"

# ===========================================================================
# Part 5: an exported ROOT doesn't redirect a direct run of bin/factory.
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
# Part 6: factory update --plan.
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
  # What factory/fingerprints.py record_sandbox_fingerprint writes (the bash copy is gone).
  mkdir -p "$FACTORY_STATE/sandboxes"
  sandbox_fingerprint "$COORDINATOR_SANDBOX" > "$(sandbox_fingerprint_file "$COORDINATOR_SANDBOX")"
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

# A change under broker/ makes the broker sandbox a candidate for recreation.
set +e
B_PLAN="$( (
  export PATH="$plan_bin:$PATH" XDG_STATE_HOME="$tmp/plan-state" XDG_CONFIG_HOME="$tmp/plan-config"
  # shellcheck disable=SC1091
  . "$FACTORY_SRC/bin/factory"
  ROOT="$planco"
  plan_maybe_sandboxes "$(printf '%s\n' 'broker/passdesk/app.py')"
) 2>&1 )"
set -e
assert_eq "plan_maybe_sandboxes: a changed file under broker/ may recreate the broker" \
  "$B_PLAN" "$(printf "factory-broker\tthe broker's code or kit changes (broker/passdesk/app.py)")"

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
