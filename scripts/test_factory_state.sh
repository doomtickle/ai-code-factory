#!/usr/bin/env bash
set -euo pipefail

# Regression tests for host CLI state handling:
#
# 1. Token minting (scripts/start-broker.sh; start-dispatch's is tested in
#    factory/test_dispatch_start.py): the
#    token's hash is written only after `sbx secret set` succeeds. A failing
#    set stops the script with a message naming the secret, leaves no hash
#    file behind, and leaves an existing hash unchanged (a hash that no longer
#    matches the stored secret would make every broker call a 401, and nothing
#    would re-mint).
# 2. `factory update`: when `sbx rm` fails after the factory was stopped, it
#    says what is left stopped and how to recover ("factory start"), instead of
#    a bare set -e abort. The success path keeps working, and starts the bound
#    project again through Python `factory start --no-dashboard`. (`factory
#    reset` and `start`, with its project switch, are Python now:
#    factory/test_command_runstate.py and factory/test_command_start.py; the
#    image-tag check, setup_done, is in factory/test_sbx.py.)
#
# Drives a fixture checkout (bin/factory + scripts/lib + start-broker.sh,
# stub 03-stop-factory.sh) against a fake sbx on a private PATH. No real sbx,
# docker or network access needed.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SBX_VER="$(sed -n 's/^readonly SBX_VERSION="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/lib/common.sh")"
[ -n "$SBX_VER" ] || { echo "cannot read SBX_VERSION from common.sh" >&2; exit 1; }

tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac; }
assert_not_contains() { case "$2" in *"$3"*) bad "$1 (did not expect '$3', got: $2)" ;; *) ok "$1" ;; esac; }
assert_nonzero() { [ "$2" -ne 0 ] && ok "$1" || bad "$1 (exit status was 0)"; }

# --- fixture checkout ------------------------------------------------------------
fx="$tmp/checkout"
mkdir -p "$fx/bin" "$fx/scripts/lib" "$fx/broker" "$fx/kits"
cp "$REPO_ROOT/bin/factory" "$fx/bin/factory"
cp -R "$REPO_ROOT/factory" "$fx/factory"
find "$fx/factory" -name __pycache__ -prune -exec rm -rf {} +
cp "$REPO_ROOT"/scripts/lib/*.sh "$fx/scripts/lib/"
cp "$REPO_ROOT/scripts/start-broker.sh" "$fx/scripts/"
cp -R "$REPO_ROOT/roles" "$fx/roles"
cp -R "$REPO_ROOT/kits/coordinator" "$fx/kits/coordinator"
cp "$REPO_ROOT/broker/tools.txt" "$fx/broker/tools.txt"
cat > "$fx/scripts/03-stop-factory.sh" <<'STUB'
#!/bin/sh
echo "stop" >> "$CALLS"
STUB
chmod +x "$fx/scripts/03-stop-factory.sh"

# --- fake sbx and curl -------------------------------------------------------------
# FAKE_SECRETS: the `secret ls` output. FAKE_SET_FAIL=1: `secret set` fails.
# FAKE_RM_FAIL=1: `rm` fails. FAKE_SANDBOXES: `ls -q`. FAKE_TEMPLATES: `template ls -q`.
bin="$tmp/bin"
mkdir -p "$bin"
cat > "$bin/sbx" <<FAKE_SBX
#!/bin/sh
printf '%s\n' "\$*" >> "\$FAKE_SBX_ARGS"
case "\$1" in
  version) echo "sbx version: v$SBX_VER testcommit" ;;
  settings) echo true ;;
  template) printf '%s\n' \$FAKE_TEMPLATES ;;
  ls) printf '%s\n' \$FAKE_SANDBOXES ;;
  rm) [ "\${FAKE_RM_FAIL:-}" != 1 ] || { echo "sbx: boom" >&2; exit 1; } ;;
  ports) echo '[{"host_port":18765}]' ;;
  secret)
    case "\$2" in
      ls) [ -z "\${FAKE_SECRETS:-}" ] || printf '%s\n' "\$FAKE_SECRETS" ;;
      set)
        cat > "\$FAKE_SECRET_VALUE"
        [ "\${FAKE_SET_FAIL:-}" != 1 ] || { echo "sbx: not signed in" >&2; exit 1; } ;;
    esac ;;
esac
exit 0
FAKE_SBX
cat > "$bin/curl" <<'FAKE_CURL'
#!/bin/sh
printf 401
FAKE_CURL
chmod +x "$bin/sbx" "$bin/curl"

export PATH="$bin:$PATH"
export FAKE_SBX_ARGS="$tmp/sbx-args.log"
export FAKE_SECRET_VALUE="$tmp/secret-value"
export CALLS="$tmp/calls.log"
export XDG_STATE_HOME="$tmp/state"
export XDG_CONFIG_HOME="$tmp/config"
export HOME="$tmp/home"
mkdir -p "$HOME"
unset FAKE_SET_FAIL FAKE_RM_FAIL FAKE_SECRETS FAKE_SANDBOXES FAKE_TEMPLATES

broker_state="$tmp/state/ai-code-factory/broker"
dispatch_state="$tmp/state/ai-code-factory/dispatch"
project="$tmp/project"
mkdir -p "$project"
reset_run() { : > "$FAKE_SBX_ARGS"; : > "$CALLS"; : > "$FAKE_SECRET_VALUE"; }
run_cmd() { # run_cmd COMMAND... -> F_OUT, F_RC
  set +e
  F_OUT="$("$@" 2>&1 </dev/null)"
  F_RC=$?
  set -e
}
sha_of() { printf '%s' "$1" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -d' ' -f1; }

# ===========================================================================
# Part 1: token minting writes the hash only after `sbx secret set` succeeds.
# ===========================================================================
# 1a. Broker, nothing minted yet, set fails: dies naming a secret, no hash file.
reset_run
rm -rf "$broker_state"
export FAKE_SET_FAIL=1
run_cmd "$fx/scripts/start-broker.sh" "$project"
assert_nonzero "broker mint, failing set: exits non-zero" "$F_RC"
assert_contains "broker mint, failing set: names the secret" "$F_OUT" "mcp:factory-broker-"
assert_contains "broker mint, failing set: says the hash was not changed" "$F_OUT" "token hash was not changed"
assert_eq "broker mint, failing set: no hash file is left behind" \
  "$(find "$broker_state/tokens" -name '*.sha256' 2>/dev/null | wc -l | tr -d ' ')" "0"
assert_not_contains "broker mint, failing set: no sandbox was started" "$(cat "$FAKE_SBX_ARGS")" "run -d"

# 1b. Broker, an older hash exists (its secret is gone from the store), set fails:
# the older hash is unchanged.
reset_run
mkdir -p "$broker_state/tokens"
for r in developer reviewer researcher coordinator; do printf 'oldhash-%s\n' "$r" > "$broker_state/tokens/$r.sha256"; done
run_cmd "$fx/scripts/start-broker.sh" "$project"
assert_nonzero "broker mint over an old hash, failing set: exits non-zero" "$F_RC"
drift=""
for r in developer reviewer researcher coordinator; do
  [ "$(cat "$broker_state/tokens/$r.sha256")" = "oldhash-$r" ] || drift="$drift $r"
done
assert_eq "broker mint over an old hash, failing set: every existing hash is unchanged" "$drift" ""

# 1c. Broker, set succeeds: the hash file holds the hash of the value stored.
reset_run
rm -rf "$broker_state"
unset FAKE_SET_FAIL
run_cmd "$fx/scripts/start-broker.sh" "$project"
dev_hash="$(cat "$broker_state/tokens/developer.sha256" 2>/dev/null || true)"
# The fake's capture file holds the value of the last set; every role's token is
# minted separately, so check the last role in the loop: the hash files are
# 64 hex digits each, and at least one matches the captured value.
captured="$(cat "$FAKE_SECRET_VALUE")"
match=""
for f in "$broker_state"/tokens/*.sha256; do
  [ "$(cat "$f")" = "$(sha_of "$captured")" ] && match=1
done
assert_eq "broker mint, successful set: a hash file matches the stored value" "$match" "1"
case "$dev_hash" in
  [0-9a-f]*) [ "${#dev_hash}" -eq 64 ] && ok "broker mint, successful set: developer hash is a sha256" \
    || bad "broker mint, successful set: developer hash is a sha256 (got '$dev_hash')" ;;
  *) bad "broker mint, successful set: developer hash is a sha256 (got '$dev_hash')" ;;
esac

# ===========================================================================
# Part 2: a failing `sbx rm` after the stop says what is left and how to recover.
# ===========================================================================
mkdir -p "$broker_state" "$dispatch_state"
printf '%s\n' "$project" > "$broker_state/project"
export FAKE_SANDBOXES="factory-broker factory-developer other"

# 2a/2b. factory update --pulled --yes with one stale sandbox. cmd_setup and the
# staleness check are replaced (they need docker and real images); the code
# under test is the stop / rm / restart sequence in cmd_update itself.
# The Python update hands the restart to: records `<checkout>/factory start PROJECT --no-dashboard`.
fake_python="$tmp/fake-python"
printf '#!/bin/sh\nshift\necho "$*" >> "$CALLS"\n' > "$fake_python"
chmod +x "$fake_python"
update_run() { # update_run -> F_OUT, F_RC
  set +e
  F_OUT="$(
    # shellcheck source=/dev/null
    . "$fx/bin/factory"
    cmd_setup() { :; }
    stale_sandboxes() { echo factory-developer; }
    describe_loss() { echo "its scratch folder"; }
    resolve_python() { printf %s "$fake_python"; }
    cmd_update --pulled --yes 2>&1
  )"
  F_RC=$?
  set -e
}
printf '%s\n' "$project" > "$broker_state/project"
reset_run
export FAKE_RM_FAIL=1
update_run
assert_nonzero "update, failing rm: exits non-zero" "$F_RC"
assert_eq "update, failing rm: stopped, and nothing restarted" "$(cat "$CALLS")" "stop"
assert_contains "update, failing rm: names the sandbox it could not remove" "$F_OUT" "factory-developer"
assert_contains "update, failing rm: says what is left stopped" "$F_OUT" "Left stopped"
assert_contains "update, failing rm: gives the recovery command" "$F_OUT" "factory start"
assert_not_contains "update, failing rm: does not claim the removal" "$F_OUT" "removed factory-developer"

reset_run
unset FAKE_RM_FAIL
update_run
assert_eq "update: exits 0 when rm works" "$F_RC" "0"
assert_eq "update: stops, then starts the bound project again, in Python, without the dashboard" "$(cat "$CALLS")" "stop
start $project --no-dashboard"
assert_contains "update: reports the removal" "$F_OUT" "removed factory-developer"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
