#!/usr/bin/env bash
set -euo pipefail

# Parity test for project_repos, which exists in two copies that must agree:
# factory/repos.py (the Python CLI, which the bridge, dispatch/server.py, imports)
# and templates/agent/factory-lane (inside the sandboxes). Each lists the git repos
# of a project; if they disagreed, a role could be offered a repo the bridge won't
# import, or the reverse. Both (and the bridge's import of the first) run on the
# same fixtures and must print the same list in the same (byte) order, also under a
# non-C locale when the machine has one (factory/test_repos.py has the Python
# side's own cases):
# - a repo, a folder whose .git is a file (worktree/submodule style), a repo
#   named with a leading capital (byte order, not locale order)
# - skipped: a hidden folder with a .git, a symlink to a repo, a folder with no
#   .git, a name containing a newline, a repo nested two levels down
# - a project that is itself a repo (.git a directory, or a file) is just "."

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tmp="$(cd "$tmp" && pwd -P)"

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

via_server() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_ROOT/dispatch" python3 -c '
import sys
from pathlib import Path
import server
for r in server.project_repos(Path(sys.argv[1])):
    print(r)' "$1"
}
via_package() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_ROOT" python3 -c '
import sys
from factory.repos import project_repos
for r in project_repos(sys.argv[1]):
    print(r)' "$1"
}
via_lane() { "$REPO_ROOT/templates/agent/factory-lane" repos "$1"; }

# A locale other than C, if installed: a locale-sorting implementation would then order
# "Capital" and "gitfile" differently.
other_locale="$(locale -a 2>/dev/null | grep -viE '^(c|posix)(\.|$)' | head -n1 || true)"
locales="C"
[ -z "$other_locale" ] || locales="C $other_locale"

# check LABEL PROJECT EXPECTED: each implementation's output, unsorted, must equal EXPECTED
# (which is in byte order) under every locale.
check() {
  local label="$1" project="$2" expected="$3" impl loc
  for loc in $locales; do
    for impl in via_package via_server via_lane; do
      assert_eq "$label: $impl ($loc)" "$(LC_ALL="$loc" "$impl" "$project")" "$expected"
    done
  done
}

# --- a folder of repos -------------------------------------------------------
folder="$tmp/folder"
mkdir -p "$folder/normal/.git" "$folder/Capital/.git" "$folder/gitfile" \
  "$folder/.hidden/.git" "$folder/no-git/src" "$folder/outer/inner/.git" \
  "$folder/real-repo/.git" "$folder/new
line/.git"
echo "gitdir: /elsewhere" > "$folder/gitfile/.git"
ln -s real-repo "$folder/link-to-repo"
check "folder of repos" "$folder" "Capital
gitfile
normal
real-repo"

# --- a project that is itself a repo ----------------------------------------
mkdir -p "$tmp/repo-dir/.git" "$tmp/repo-dir/sub/.git"
check "project with a .git directory" "$tmp/repo-dir" "."
mkdir -p "$tmp/repo-file/sub/.git"
echo "gitdir: /elsewhere" > "$tmp/repo-file/.git"
check "project with a .git file" "$tmp/repo-file" "."

# --- neither -----------------------------------------------------------------
mkdir -p "$tmp/empty/docs"
check "project with no repos" "$tmp/empty" ""

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
