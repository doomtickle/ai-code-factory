#!/usr/bin/env bash
set -euo pipefail

# Tests for scripts/ci-changes.sh, which decides which parts of CI a pull request needs. No network.
# 1. The flags for the kinds of change CI sees (docs only, dashboard only, broker, workflow, mixed).
# 2. Anything it can't tell runs everything: no range, an empty list, a bad range, an unknown file.
# 3. A real git range (BASE...HEAD) gives the same answer as the file list.
# 4. The `browser tests` job reads Chrome's pin and apt libraries from the developer Dockerfile
#    (scripts/ci-chrome.sh); the reviewer Dockerfile must carry the same ones.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

script="$REPO_ROOT/scripts/ci-changes.sh"

# flags "file1 file2 ..." -> images/broker/code/docs as one line, e.g. "false false true false".
flags() {
  local out
  out="$(printf '%s\n' "$@" | bash "$script" --stdin)"
  printf '%s' "$out" | sed 's/^[a-z]*=//' | tr '\n' ' ' | sed 's/ $//'
}
ALL="true true true true"

assert_eq "README only: docs tests, no code" "$(flags README.md)" "false false false true"
assert_eq "docs/ guide only" "$(flags docs/guides/01-first-session.md)" "false false false true"
assert_eq "sbx reference only" "$(flags docs/reference/sbx-cli.md)" "false false false true"
assert_eq "CLAUDE.md files only: nothing" "$(flags CLAUDE.md scripts/CLAUDE.md broker/CLAUDE.md)" "false false false false"
assert_eq "broker prose only: no images, no broker" "$(flags broker/docs/decisions.md broker/README.md)" "false false false false"
assert_eq "LICENSE only" "$(flags LICENSE broker/LICENSE)" "false false false false"
assert_eq "prose and README" "$(flags broker/docs/decisions.md README.md)" "false false false true"
assert_eq "dashboard only" "$(flags scripts/dashboard.py scripts/test_dashboard.py scripts/dashboard-ui/theme.css)" "false false true false"
assert_eq "factory Python only" "$(flags factory/repos.py factory/test_repos.py)" "false false true false"
assert_eq "dispatch only" "$(flags dispatch/server.py)" "false false true false"
assert_eq "broker code" "$(flags broker/passdesk/core/policy.py)" "true true true false"
assert_eq "broker Dockerfile" "$(flags broker/Dockerfile)" "true true true false"
assert_eq "broker code and its decision log" "$(flags broker/docs/decisions.md broker/server.py)" "true true true false"
assert_eq "developer template" "$(flags templates/developer/Dockerfile)" "true false true false"
assert_eq "agent tool in templates" "$(flags templates/agent/factory-test)" "true false true false"
assert_eq "kits" "$(flags kits/broker/spec.yaml)" "true false true false"
assert_eq "build code" "$(flags factory/build_templates.py)" "true false true false"
assert_eq "pins" "$(flags scripts/lib/common.sh)" "true false true false"
assert_eq "build script" "$(flags scripts/00-build-templates.sh)" "true false true false"
assert_eq "the workflow itself" "$(flags .github/workflows/ci.yml)" "true true true false"
assert_eq "another workflow" "$(flags .github/workflows/docs.yml)" "false false true false"
assert_eq "a file with no rule runs the suites" "$(flags something/new.txt)" "false false true false"
assert_eq "prose beside code: code wins" "$(flags README.md scripts/dashboard.py)" "false false true true"
assert_eq "an empty list runs everything" "$(flags)" "$ALL"

assert_eq "no arguments runs everything" "$(bash "$script" | sed 's/^[a-z]*=//' | tr '\n' ' ' | sed 's/ $//')" "$ALL"
bash "$script" a b c >/dev/null 2>&1 && bad "three arguments refused" || ok "three arguments refused"

# A real range.
repo="$tmp/repo"
git init -q "$repo"
(
  cd "$repo"
  mkdir docs scripts
  echo a > README.md; echo b > scripts/a.py
  git add -A; git commit -qm base
  git branch -M main
  git switch -qc docs-pr
  echo c >> README.md; echo d > docs/x.md
  git add -A; git commit -qm docs
  git switch -qc code-pr main
  echo e >> scripts/a.py
  git add -A; git commit -qm code
  git switch -q main
  echo f > unrelated.md      # main moves on: not part of either PR (three dots)
  git add -A; git commit -qm drift
)
range() { (cd "$repo" && bash "$script" "$1" "$2" | sed 's/^[a-z]*=//' | tr '\n' ' ' | sed 's/ $//'); }
assert_eq "range: a docs PR" "$(range main docs-pr)" "false false false true"
assert_eq "range: a code PR is judged on its own files, not main's drift" "$(range main code-pr)" "false false true false"
assert_eq "range: a bad base runs everything" "$(range deadbeefdeadbeefdeadbeefdeadbeefdeadbeef code-pr 2>/dev/null)" "$ALL"
rename_out="$(cd "$repo" && git switch -qc rename-pr main~1 && mkdir -p docs && git mv scripts/a.py docs/a.py && git commit -qm rename && bash "$script" main~1 rename-pr | sed 's/^[a-z]*=//' | tr '\n' ' ' | sed 's/ $//')"
assert_eq "range: a rename from scripts/ into docs/ counts both paths" "$rename_out" "false false true true"

# Chrome: CI reads only the developer's Dockerfile, so the reviewer's must match it.
chrome="$REPO_ROOT/scripts/ci-chrome.sh"
for name in CHROME_VERSION CHROME_SHA256_AMD64 CHROME_SHA256_ARM64; do
  dev="$(bash "$chrome" pin "$name" "$REPO_ROOT/templates/developer/Dockerfile")"
  assert_eq "reviewer $name equals the developer's" "$(bash "$chrome" pin "$name" "$REPO_ROOT/templates/reviewer/Dockerfile")" "$dev"
done
assert_eq "reviewer's Chrome apt libraries equal the developer's" \
  "$(bash "$chrome" apt "$REPO_ROOT/templates/reviewer/Dockerfile" | tr '\n' ' ')" "$(bash "$chrome" apt "$REPO_ROOT/templates/developer/Dockerfile" | tr '\n' ' ')"
assert_eq "the developer's CHROME_VERSION looks like a version" "$(bash "$chrome" pin CHROME_VERSION | grep -Ec '^[0-9]+(\.[0-9]+){3}$')" "1"
assert_eq "the developer's SHA-256 pins are 64 hex digits" "$(bash "$chrome" pin CHROME_SHA256_AMD64; bash "$chrome" pin CHROME_SHA256_ARM64)" "$(grep -E '^ARG CHROME_SHA256_(AMD64|ARM64)=[0-9a-f]{64}$' "$REPO_ROOT/templates/developer/Dockerfile" | sed 's/^[^=]*=//')"
# The reader refuses what it can't read: a duplicated, a missing or a badly named ARG.
printf 'ARG CHROME_VERSION=1.2.3.4\nARG CHROME_VERSION=5.6.7.8\n' > "$tmp/Dockerfile.dup"
bash "$chrome" pin CHROME_VERSION "$tmp/Dockerfile.dup" >/dev/null 2>&1 && bad "a duplicated ARG refused" || ok "a duplicated ARG refused"
bash "$chrome" pin CHROME_SHA256_AMD64 "$tmp/Dockerfile.dup" >/dev/null 2>&1 && bad "a missing ARG refused" || ok "a missing ARG refused"
bash "$chrome" pin 'a.b' "$tmp/Dockerfile.dup" >/dev/null 2>&1 && bad "a badly named ARG refused" || ok "a badly named ARG refused"
# A drifted reviewer pin is seen by the same comparison.
sed 's/^ARG CHROME_VERSION=.*/ARG CHROME_VERSION=1.2.3.4/' "$REPO_ROOT/templates/reviewer/Dockerfile" > "$tmp/Dockerfile.drift"
[ "$(bash "$chrome" pin CHROME_VERSION "$tmp/Dockerfile.drift")" != "$(bash "$chrome" pin CHROME_VERSION)" ] && ok "a drifted reviewer pin is seen" || bad "a drifted reviewer pin is seen"

printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
