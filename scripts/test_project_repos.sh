#!/usr/bin/env bash
set -euo pipefail

# Parity test for project_repos, which exists in three copies that must agree:
# scripts/lib/common.sh (host shell), dispatch/server.py (the bridge) and
# templates/agent/factory-lane (inside the sandboxes). Each lists the git repos
# of a project; if they disagreed, a role could be offered a repo the bridge
# won't import, or the reverse. All three run on the same fixtures and must
# print the same sorted list:
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

via_common() { bash -c '. "$1/scripts/lib/common.sh"; project_repos "$2"' _ "$REPO_ROOT" "$1"; }
via_server() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_ROOT/dispatch" python3 -c '
import sys
from pathlib import Path
import server
for r in server.project_repos(Path(sys.argv[1])):
    print(r)' "$1"
}
via_lane() { "$REPO_ROOT/templates/agent/factory-lane" repos "$1"; }

# check LABEL PROJECT EXPECTED: each implementation, sorted, must equal EXPECTED.
check() {
  local label="$1" project="$2" expected="$3" impl
  for impl in via_common via_server via_lane; do
    assert_eq "$label: $impl" "$("$impl" "$project" | LC_ALL=C sort)" "$expected"
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

# Same order from each, not just the same set.
assert_eq "folder of repos: common.sh and server.py agree on the order" \
  "$(via_common "$folder")" "$(via_server "$folder")"

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
