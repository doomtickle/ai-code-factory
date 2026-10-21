#!/usr/bin/env bash
set -euo pipefail

# Regression tests for bin/factory's GitLab checks (doomtickle/passdesk-ops#122):
#
# 1. policy_value joins an array spread over several lines (up to its "]",
#    comments dropped), so `tools = [` is never read as an empty list.
# 2. gitlab_needs_token: [gitlab].url set in either TOML string form; "" and
#    the commented-out example are unset.
# 3. github_repos_listed: an owner/name entry is GitHub; host-qualified
#    entries (a dot or a port in the first segment) are GitLab.
# 4. gitlab_mr_unheld: warns when GitLab is configured but [approvals].tools
#    (one line or several) leaves gitlab_create_mr out; silent for no list
#    or tools = [] (holds off: the gateway holds nothing until the list
#    names a tool), a list that names it, no GitLab, or no role granted
#    the tool.
#
# Sources only those functions from bin/factory, against a fixture config
# folder and broker policy. No sbx, keychain or network.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

# The functions under test, and what they call.
fns="$tmp/fns.sh"
for f in policy_value policy_string git_push_enabled github_repos_listed gitlab_needs_token gitlab_mr_unheld; do
  sed -n "/^$f() {/,/^}/p" "$REPO_ROOT/bin/factory" >> "$fns"
  grep -q "^$f() {" "$fns" || { echo "bin/factory has no $f()"; exit 1; }
done
# shellcheck source=/dev/null
. "$fns"
GRANTED="npm_fetch git_push gitlab_create_mr"
list_roles() { echo developer; }
role_get() { echo "$GRANTED"; }

ROOT="$tmp/checkout"
mkdir -p "$ROOT/broker"
cat > "$ROOT/broker/policy.toml" <<'EOF'
[git]
repos = []

[gitlab]
url = ""
EOF
FACTORY_CONFIG="$tmp/config"
mkdir -p "$FACTORY_CONFIG"
GITLAB=$'[gitlab]\nurl = "https://gitlab.acme.net/"\n'

policy() { printf '%s\n' "$1" > "$FACTORY_CONFIG/policy.toml"; }
unheld() { if why="$(gitlab_mr_unheld)"; then printf 'warn: %s' "$why"; else printf 'silent'; fi; }
yesno() { if "$@"; then printf yes; else printf no; fi; }

# --- 1. policy_value ------------------------------------------------------------
policy $'[approvals]\ntools = [\n  "git_push",   # pushes\n  "github_create_pr",\n]\nhold_seconds = 45'
assert_eq "policy_value: a multi-line array comes back joined" \
  "$(policy_value approvals tools | tr -s ' ')" '[ "git_push", "github_create_pr", ]'
assert_eq "policy_value: the key after it is still read" "$(policy_value approvals hold_seconds)" "45"
policy $'[git]\nrepos = ["a/b"]'
assert_eq "policy_value: one-line values are unchanged" "$(policy_value git repos)" '["a/b"]'
assert_eq "policy_value: falls back to broker/policy.toml" "$(policy_value gitlab url)" '""'

# --- 2. gitlab_needs_token ------------------------------------------------------
policy "$GITLAB"
assert_eq "gitlab_needs_token: double-quoted url" "$(yesno gitlab_needs_token)" yes
policy $'[gitlab]\nurl = \'https://gitlab.acme.net/\''
assert_eq "gitlab_needs_token: single-quoted url" "$(yesno gitlab_needs_token)" yes
policy $'[gitlab]\nurl = ""'
assert_eq "gitlab_needs_token: empty url" "$(yesno gitlab_needs_token)" no
policy $'# [gitlab]\n# url = "https://gitlab.example.com/"'
assert_eq "gitlab_needs_token: the commented-out example (base url = \"\")" "$(yesno gitlab_needs_token)" no

# --- 3. github_repos_listed -----------------------------------------------------
policy $'[git]\nrepos = ["gitlab.acme.net/platform/api", "gitlab.acme.net:8443/g/p"]'
assert_eq "github_repos_listed: GitLab only" "$(yesno github_repos_listed)" no
policy $'[git]\nrepos = ["gitlab.acme.net/platform/api", "my-org/my-project"]'
assert_eq "github_repos_listed: one GitHub repo among them" "$(yesno github_repos_listed)" yes
policy $'[git]\nrepos = []'
assert_eq "github_repos_listed: none" "$(yesno github_repos_listed)" no

# --- 4. gitlab_mr_unheld --------------------------------------------------------
policy "$GITLAB"
assert_eq "unheld: no [approvals].tools (holds off)" "$(unheld)" silent
policy "$GITLAB"$'[approvals]\ntools = ["git_push", "github_create_pr"]'
assert_eq "unheld: one-line list without it" "$(unheld)" 'warn: [approvals].tools is set without "gitlab_create_mr"'
policy "$GITLAB"$'[approvals]\ntools = [\n  "git_push",\n  "github_create_pr",\n]'
assert_eq "unheld: multi-line list without it" "$(unheld)" 'warn: [approvals].tools is set without "gitlab_create_mr"'
policy "$GITLAB"$'[approvals]\ntools = [\n  "git_push",\n  "gitlab_create_mr",\n]'
assert_eq "unheld: multi-line list with it" "$(unheld)" silent
policy "$GITLAB"$'[approvals]\ntools = ["git_push", "gitlab_create_mr"]'
assert_eq "unheld: one-line list with it" "$(unheld)" silent
policy "$GITLAB"$'[approvals]\ntools = []'
assert_eq "unheld: tools = [] (holds off on purpose)" "$(unheld)" silent
policy "$GITLAB"$'[approvals]\ntools = [\n]'
assert_eq "unheld: tools = [ ] over two lines" "$(unheld)" silent
policy $'[approvals]\ntools = ["git_push"]'
assert_eq "unheld: no GitLab configured" "$(unheld)" silent
policy "$GITLAB"$'[approvals]\ntools = ["git_push"]'
GRANTED="npm_fetch git_push"
assert_eq "unheld: no role granted gitlab_create_mr" "$(unheld)" silent
GRANTED="npm_fetch git_push gitlab_create_mr"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
