#!/usr/bin/env bash
set -euo pipefail

# Tests for what a fresh clone sees (bin/factory, scripts/lib/common.sh and roles.sh):
#
# 1. 'factory doctor' and 'factory setup' don't load roles.sh up front, so on a
#    checkout whose broker/ is incomplete (no broker/tools.txt) they still run
#    their checks and report the problem and the exact command that restores
#    it. Any other command still stops with a message.
# 2. With broker/ present doctor says so and carries on with the role checks.
# 3. require_project_commits (factory start): a project repo with no commits is
#    refused by name, before anything is created; repos with commits, and a
#    project that isn't a repo, pass.
# 4. doctor_secret_tool_probe: silent "not found" is fine, a message on stderr
#    is reported.
# 5. sbx_not_signed_in: true only when sbx says "Not authenticated".
# 6. run_or_die: silent on success, the last 20 lines of output on failure.
#
# Fixtures are local git repos and stub commands on a private PATH: no network,
# no sbx, no docker, no keychain.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { # assert_contains LABEL HAYSTACK NEEDLE
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}
assert_not_contains() {
  case "$2" in *"$3"*) bad "$1 (did not expect '$3', got: $2)" ;; *) ok "$1" ;; esac
}

# --- PATH: system tools plus stubs; no sbx, docker or secret-tool --------
sys="$tmp/sys"
mkdir -p "$sys"
for t in bash sh env cat dirname basename readlink grep mkdir sed tr tail head ls rm mv cp chmod sleep id uname \
  git curl jq openssl column python3 awk cut sort mktemp ps wc timeout printf test find seq sw_vers; do
  p="$(command -v "$t" 2>/dev/null)" || continue
  ln -sf "$p" "$sys/$t"
done
stubs="$tmp/stubs"
mkdir -p "$stubs"
for t in sbx docker; do
  printf '#!/bin/sh\necho "%s $*" >> "%s/forbidden"\nexit 1\n' "$t" "$tmp" > "$stubs/$t"
  chmod +x "$stubs/$t"
done

# --- fixtures ---------------------------------------------------------------------
# make_checkout DIR [nobroker] -> a git checkout of the factory's bin/ and
# scripts/lib/, committed with a broker/tools.txt unless "nobroker" is given.
make_checkout() {
  local d="$1"
  mkdir -p "$d/bin" "$d/scripts/lib"
  cp "$REPO_ROOT/bin/factory" "$d/bin/factory"
  cp "$REPO_ROOT"/scripts/lib/*.sh "$d/scripts/lib/"
  if [ "${2:-}" != nobroker ]; then
    mkdir -p "$d/broker"
    echo "git_fetch" > "$d/broker/tools.txt"
  fi
  git init -q "$d"
  git -C "$d" add -A
  git -C "$d" commit -q -m "fixture"
}

# run_in DIR ARGS... -> run DIR's bin/factory, output to $tmp/out, status in $RC
RC=0
run_in() {
  local d="$1"
  shift
  rm -f "$tmp/forbidden"
  RC=0
  env PATH="$stubs:$sys" HOME="$tmp/home" XDG_STATE_HOME="$tmp/state" XDG_CONFIG_HOME="$tmp/config" \
    "$d/bin/factory" "$@" > "$tmp/out" 2>&1 || RC=$?
}

# ===========================================================================
# Part 1: a checkout whose broker/ is incomplete
# ===========================================================================
make_checkout "$tmp/fresh" nobroker
[ ! -e "$tmp/fresh/broker/tools.txt" ] && ok "fixture: broker/tools.txt is missing" || bad "fixture: broker/tools.txt is missing"

run_in "$tmp/fresh" doctor
out="$(cat "$tmp/out")"
[ "$RC" -ne 0 ] && ok "doctor exits non-zero when broker/ is missing" || bad "doctor exits non-zero when broker/ is missing"
assert_contains "doctor still runs its first check" "$out" "==> Platform"
assert_contains "doctor has a broker/ step" "$out" "==> Gateway source (broker/)"
assert_not_contains "doctor's broker/ step does not mention a submodule" "$out" "submodule"
assert_contains "doctor says broker/ is incomplete" "$out" "broker/tools.txt is missing: broker/ is incomplete in this checkout"
assert_contains "doctor keeps checking after the failure" "$out" "==> Docker"
assert_contains "doctor counts it as a problem" "$out" "problem(s) found."
assert_not_contains "doctor never hits a missing roles.sh function" "$out" "command not found"
assert_not_contains "doctor builds and signs in to nothing" "$(cat "$tmp/forbidden" 2>/dev/null)" "docker build"

make_checkout "$tmp/fresh2" nobroker
run_in "$tmp/fresh2" setup
out="$(cat "$tmp/out")"
[ "$RC" -ne 0 ] && ok "setup exits non-zero when broker/ is missing" || bad "setup exits non-zero when broker/ is missing"
assert_contains "setup runs the doctor first" "$out" "==> Gateway source (broker/)"
assert_contains "setup then stops with its usual message" "$out" "fix the problems above, then run 'factory setup' again"
assert_not_contains "setup builds nothing" "$(cat "$tmp/forbidden" 2>/dev/null)" "docker build"
assert_not_contains "setup signs in to nothing" "$(cat "$tmp/forbidden" 2>/dev/null)" "sbx login"

make_checkout "$tmp/fresh3" nobroker
run_in "$tmp/fresh3" logs
out="$(cat "$tmp/out")"
[ "$RC" -ne 0 ] && ok "other commands still stop without broker/" || bad "other commands still stop without broker/"
assert_contains "roles.sh says what is missing" "$out" "broker/tools.txt is missing"
assert_contains "roles.sh shows the restore command" "$out" "checkout HEAD -- broker"

# broker/ committed but deleted from the working tree: the command the doctor
# prints restores it.
make_checkout "$tmp/deleted"
rm "$tmp/deleted/broker/tools.txt"
run_in "$tmp/deleted" doctor
out="$(cat "$tmp/out")"
assert_contains "doctor reports a deleted broker/tools.txt" "$out" "broker/tools.txt is missing"
cmd="$(sed -n 's/^ *\(git -C .* checkout HEAD -- broker\)$/\1/p' "$tmp/out" | head -n1)"
case "$cmd" in
  "git -C "*) if (cd "$tmp" && eval "$cmd" >/dev/null 2>&1) && [ -f "$tmp/deleted/broker/tools.txt" ]; then ok "the printed command restores broker/"
              else bad "the printed command restores broker/ ($cmd)"; fi ;;
  *) bad "the printed command was found in doctor's output" ;;
esac

# ===========================================================================
# Part 2: with broker/ present doctor carries on with the role checks
# ===========================================================================
make_checkout "$tmp/plain"
mkdir -p "$tmp/config/ai-code-factory/roles"
run_in "$tmp/plain" doctor
out="$(cat "$tmp/out")"
assert_contains "doctor reports a present broker/" "$out" "✓ broker/ is present"
assert_not_contains "doctor reports no broker/ problem" "$out" "is incomplete"
assert_contains "the role checks run once broker/ is there" "$out" "role overrides are valid"
rm -rf "$tmp/config"

# Role overrides can't be checked without broker/: skipped, not a crash.
make_checkout "$tmp/fresh4" nobroker
mkdir -p "$tmp/config/ai-code-factory/roles"
run_in "$tmp/fresh4" doctor
out="$(cat "$tmp/out")"
assert_contains "role override check is skipped without broker/" "$out" "skipped: needs broker/"
assert_not_contains "role override check doesn't crash without broker/" "$out" "command not found"
rm -rf "$tmp/config"

# ===========================================================================
# Parts 3-6: functions, from a checkout that has a broker/ (so sourcing works)
# ===========================================================================
rdy="$tmp/ready"
make_checkout "$rdy"
fn() { # fn CODE -> run CODE in a subshell with bin/factory sourced, output in $tmp/out
  RC=0
  (
    export PATH="$stubs:$sys" HOME="$tmp/home" XDG_STATE_HOME="$tmp/state" XDG_CONFIG_HOME="$tmp/config"
    # shellcheck disable=SC1091
    . "$rdy/bin/factory"
    eval "$1"
  ) > "$tmp/out" 2>&1 || RC=$?
}

# --- Part 3: require_project_commits ---------------------------------------------
proj="$tmp/proj"
mkdir -p "$proj/empty-repo" "$proj/good-repo" "$proj/not-a-repo"
git init -q "$proj/empty-repo"
git init -q "$proj/good-repo"
git -C "$proj/good-repo" commit -q --allow-empty -m first

fn "require_project_commits '$proj'"
[ "$RC" -ne 0 ] && ok "a folder holding a repo with no commits is refused" || bad "a folder holding a repo with no commits is refused"
assert_contains "the refusal names the empty repo" "$(cat "$tmp/out")" "$proj/empty-repo is a git repository with no commits"
assert_contains "the refusal says how to fix it" "$(cat "$tmp/out")" "commit --allow-empty"
assert_not_contains "the repo with commits is not blamed" "$(cat "$tmp/out")" "good-repo is a git"

fn "require_project_commits '$proj/empty-repo'"
[ "$RC" -ne 0 ] && ok "an empty repo as the project is refused" || bad "an empty repo as the project is refused"
assert_contains "it names the repo" "$(cat "$tmp/out")" "$proj/empty-repo/. is a git repository with no commits"

fn "require_project_commits '$proj/good-repo'"
[ "$RC" -eq 0 ] && ok "a repo with a commit passes" || bad "a repo with a commit passes ($(cat "$tmp/out"))"

fn "require_project_commits '$proj/not-a-repo'"
[ "$RC" -eq 0 ] && ok "a project that isn't a repo passes (nothing to check)" || bad "a project that isn't a repo passes ($(cat "$tmp/out"))"

# --- Part 4: doctor_secret_tool_probe -------------------------------------------
st="$tmp/st"
mkdir -p "$st"
printf '#!/bin/sh\nexit 1\n' > "$st/secret-tool"
chmod +x "$st/secret-tool"
fn "PATH='$st':\$PATH; doctor_secret_tool_probe"
[ "$RC" -eq 0 ] && [ ! -s "$tmp/out" ] && ok "secret-tool that says 'not found' quietly passes the probe" \
  || bad "secret-tool that says 'not found' quietly passes the probe (rc=$RC: $(cat "$tmp/out"))"
printf '#!/bin/sh\necho "secret-tool: Cannot autolaunch D-Bus without X11 \\$DISPLAY" >&2\nexit 1\n' > "$st/secret-tool"
fn "PATH='$st':\$PATH; doctor_secret_tool_probe"
[ "$RC" -ne 0 ] && ok "secret-tool with no keyring service fails the probe" || bad "secret-tool with no keyring service fails the probe"
assert_contains "the probe reports secret-tool's own message" "$(cat "$tmp/out")" "Cannot autolaunch D-Bus"

# doctor wires it in: only when secret-tool is the backend.
run_in "$tmp/fresh4" doctor
assert_not_contains "no credential-store step without secret-tool" "$(cat "$tmp/out")" "Credential store"
mkdir -p "$tmp/bin-st"
cp "$st/secret-tool" "$tmp/bin-st/secret-tool"
RC=0
env PATH="$tmp/bin-st:$stubs:$sys" FACTORY_KEYCHAIN=secret-tool HOME="$tmp/home" XDG_STATE_HOME="$tmp/state" XDG_CONFIG_HOME="$tmp/config" \
  "$tmp/fresh4/bin/factory" doctor > "$tmp/out" 2>&1 || RC=$?
assert_contains "doctor probes secret-tool when present" "$(cat "$tmp/out")" "==> Credential store (secret-tool)"
assert_contains "doctor warns, with the reason, when it can't reach a keyring" "$(cat "$tmp/out")" "can't reach a keyring: secret-tool: Cannot autolaunch D-Bus"
assert_contains "doctor says the way around it" "$(cat "$tmp/out")" "factory secret set <name>"

# --- Part 5: sbx_not_signed_in ------------------------------------------------
sb="$tmp/sb"
mkdir -p "$sb"
printf '#!/bin/sh\necho "Error: Not authenticated. Run sbx login" >&2\nexit 1\n' > "$sb/sbx"
chmod +x "$sb/sbx"
fn "PATH='$sb':\$PATH; sbx_not_signed_in"
[ "$RC" -eq 0 ] && ok "sbx saying 'Not authenticated' is not signed in" || bad "sbx saying 'Not authenticated' is not signed in"
printf '#!/bin/sh\necho "factory-developer"\n' > "$sb/sbx"
fn "PATH='$sb':\$PATH; sbx_not_signed_in"
[ "$RC" -ne 0 ] && ok "sbx answering normally is not reported as signed out" || bad "sbx answering normally is not reported as signed out"
printf '#!/bin/sh\nexit 1\n' > "$sb/sbx"
fn "PATH='$sb':\$PATH; sbx_not_signed_in"
[ "$RC" -ne 0 ] && ok "an sbx that fails for another reason is left to the build" || bad "an sbx that fails for another reason is left to the build"

# --- Part 6: run_or_die ---------------------------------------------------------
loud="$tmp/loud"
mkdir -p "$loud"
printf '#!/bin/sh\nfor i in $(seq 1 25); do echo "line $i" >&2; done\nexit 3\n' > "$loud/fail"
printf '#!/bin/sh\necho noisy\nexit 0\n' > "$loud/succeed"
chmod +x "$loud/fail" "$loud/succeed"
fn "run_or_die 'could not do it' '$loud/fail' a b"
[ "$RC" -ne 0 ] && ok "run_or_die fails when the command does" || bad "run_or_die fails when the command does"
out="$(cat "$tmp/out")"
assert_contains "run_or_die says what failed" "$out" "error: could not do it (exit status 3)"
assert_contains "run_or_die shows the last line" "$out" "line 25"
assert_contains "run_or_die shows the 20th line from the end" "$out" "line 6"
assert_not_contains "run_or_die cuts the earlier lines" "$out" "line 5"
fn "run_or_die 'could not do it' '$loud/succeed'"
[ "$RC" -eq 0 ] && [ ! -s "$tmp/out" ] && ok "run_or_die is silent on success" || bad "run_or_die is silent on success"

# --- Part 7: approval counts in status and doctor -------------------------------
# approvals_summary prints "pending oldest expired_unhandled decided running
# made failed"; pending counts only undecided staged cards, so a decided card
# (the gateway acts on it) is reported apart, never as needing the owner.
fn "approvals_summary() { echo '2 7 0 1 0 0 0'; }; approvals_status_line"
assert_contains "status counts undecided cards as staged" "$(cat "$tmp/out")" "2 staged (oldest 7 min); 1 approved, being written"
fn "approvals_summary() { echo '0 -1 0 1 1 3 2'; }; approvals_status_line"
out="$(cat "$tmp/out")"
assert_contains "decided and running writes, done and failed ones" "$out" "none staged; 2 approved, being written; 3 done; 2 failed - factory approvals"
assert_not_contains "a decided card alone has no oldest age" "$out" "oldest"
assert_not_contains "no retry wording" "$out" "retry"
fn "approvals_summary() { echo '0 -1 0 0 0 0 1'; }; approvals_status_line"
assert_contains "a failed write alone is shown" "$(cat "$tmp/out")" "none staged; 1 failed"
fn "approvals_summary() { echo '0 -1 0 0 0 0 0'; }; approvals_status_line"
[ "$RC" -eq 0 ] && [ ! -s "$tmp/out" ] && ok "no line when nothing is held" || bad "no line when nothing is held"
fn "approvals_summary() { echo '1 3 0'; }; approvals_status_line"
assert_contains "a three-field summary still reads" "$(cat "$tmp/out")" "1 staged (oldest 3 min) - factory approvals"
fn "approvals_summary() { echo '0 -1 0 1'; }; approvals_status_line"
assert_contains "a four-field summary still reads" "$(cat "$tmp/out")" "none staged; 1 approved, being written - factory approvals"
fn "approvals_summary() { echo '0 -1 0 3'; }; approvals_doctor_check"
[ "$RC" -eq 0 ] && [ ! -s "$tmp/out" ] && ok "doctor doesn't warn about decided cards" || bad "doctor doesn't warn about decided cards"
fn "approvals_summary() { echo '1 40 1 0'; }; approvals_doctor_check"
assert_contains "doctor still warns about an expired undecided card" "$(cat "$tmp/out")" "1 expired approval(s) with no decision"

# --- Part 8: optional roles in doctor ---------------------------------------------
# A disabled optional role's leftover sandbox is noted with the command that
# removes it (never run); an enabled-roles typo is a problem; both stay silent
# when there is nothing to say.
mkdir -p "$rdy/roles/researcher/kit"
printf 'OPTIONAL="1"\nTEMPLATE="developer"\nWORKSPACE="ro"\n' > "$rdy/roles/researcher/role.conf"
osb="$tmp/osb"
mkdir -p "$osb"
printf '#!/bin/sh\necho "$*" >> "%s/osb-calls"\n[ "$1 $2" = "ls -q" ] && echo factory-researcher\nexit 0\n' "$tmp" > "$osb/sbx"
chmod +x "$osb/sbx"
rm -rf "$tmp/config"
fn "PATH='$osb':\$PATH; cmd_doctor"
out="$(cat "$tmp/out")"
assert_contains "doctor notes the leftover sandbox of a disabled role" "$out" "factory-researcher exists but its role (researcher) is disabled"
assert_contains "doctor prints the removal command" "$out" "sbx rm --force factory-researcher"
assert_contains "doctor says how to turn the role on again" "$out" "add 'researcher' to $tmp/config/ai-code-factory/enabled-roles"
assert_not_contains "doctor removes nothing itself" "$(cat "$tmp/osb-calls")" "rm"
mkdir -p "$tmp/config/ai-code-factory"
printf 'researcher\n' > "$tmp/config/ai-code-factory/enabled-roles"
fn "PATH='$osb':\$PATH; cmd_doctor"
out="$(cat "$tmp/out")"
assert_contains "doctor reports a valid enabled-roles" "$out" "enabled-roles is valid (researcher)"
assert_not_contains "no leftover note once the role is enabled" "$out" "is disabled"
printf 'reseacher\n' > "$tmp/config/ai-code-factory/enabled-roles"
fn "PATH='$osb':\$PATH; cmd_doctor"
assert_contains "doctor flags an unknown name in enabled-roles" "$(cat "$tmp/out")" "no role named 'reseacher'"
rm -rf "$tmp/config"
sed -i.bak 's/^echo factory-researcher//' /dev/null 2>/dev/null || true
printf '#!/bin/sh\nexit 0\n' > "$osb/sbx"
fn "PATH='$osb':\$PATH; cmd_doctor"
assert_not_contains "no optional-roles step when there is nothing to say" "$(cat "$tmp/out")" "Optional roles"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
