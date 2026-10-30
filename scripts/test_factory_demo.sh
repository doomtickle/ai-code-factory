#!/usr/bin/env bash
set -euo pipefail

# Tests for templates/agent/factory-demo: it starts the real demo dashboard
# from this checkout on loopback (no network, no browser), and `stop` must
# signal only the PID it recorded, only while that is still its dashboard.
# Linux only (/proc), like the role images it ships in.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEMO="$REPO_ROOT/templates/agent/factory-demo"
tmp="$(cd "$(mktemp -d)" && pwd -P)"
decoys=()
cleanup() {
  FACTORY_SCRATCH="$tmp/scratch" FACTORY_LANE=t "$DEMO" stop > /dev/null 2>&1 || true
  for p in ${decoys[@]+"${decoys[@]}"}; do kill "$p" 2> /dev/null || true; done
  rm -rf "$tmp"
}
trap cleanup EXIT

pass=0; fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }

if [ ! -d /proc/self ]; then echo "skip - no /proc here"; exit 0; fi

mkdir -p "$tmp/scratch"
export FACTORY_SCRATCH="$tmp/scratch" FACTORY_LANE=t
state="$tmp/scratch/factory-demo/t"

bash -n "$DEMO" && ok "factory-demo parses" || bad "factory-demo parses"

out="$("$DEMO" 2>&1)" && rc=0 || rc=$?
[ "$rc" = 2 ] && case "$out" in *"Usage: factory-demo"*) true ;; *) false ;; esac \
  && ok "no argument prints usage" || bad "no argument prints usage ($rc): $out"

out="$(FACTORY_SCRATCH='' "$DEMO" start 2>&1)" && rc=0 || rc=$?
[ "$rc" = 1 ] && ok "start needs FACTORY_SCRATCH" || bad "start needs FACTORY_SCRATCH ($rc): $out"

out="$("$DEMO" start "$tmp" 2>&1)" && rc=0 || rc=$?
[ "$rc" = 1 ] && case "$out" in *"no scripts/dashboard.py"*) true ;; *) false ;; esac \
  && ok "start refuses a folder without scripts/dashboard.py" || bad "start refuses a folder without scripts/dashboard.py ($rc): $out"

out="$("$DEMO" url 2>&1)" && rc=0 || rc=$?
[ "$rc" = 1 ] && ok "url with nothing running fails" || bad "url with nothing running fails ($rc)"
out="$("$DEMO" stop 2>&1)" && rc=0 || rc=$?
[ "$rc" = 0 ] && ok "stop with nothing running is harmless" || bad "stop with nothing running is harmless ($rc): $out"

# A process whose command line merely MENTIONS the demo, standing in for the role's own Claude CLI.
bash -c 'sleep 300' "scripts/dashboard.py --demo" > /dev/null 2>&1 &
decoys+=("$!")
bystander="$!"

url="$("$DEMO" start "$REPO_ROOT")" && rc=0 || rc=$?
case "$url" in http://127.0.0.1:*/*) ok "start prints a loopback URL" ;; *) bad "start prints a loopback URL ($rc): $url" ;; esac
[ "$("$DEMO" url)" = "$url" ] && ok "url prints the same URL" || bad "url prints the same URL"
read -r pid _ < "$state/pid"
kill -0 "$pid" 2> /dev/null && ok "the recorded PID is alive" || bad "the recorded PID is alive"
[ "$("$DEMO" start "$REPO_ROOT" 2> /dev/null)" = "$url" ] && ok "a second start reuses the running demo" || bad "a second start reuses the running demo"

"$DEMO" stop > /dev/null && ok "stop succeeds" || bad "stop succeeds"
kill -0 "$pid" 2> /dev/null && bad "stop ended the dashboard" || ok "stop ended the dashboard"
kill -0 "$bystander" 2> /dev/null && ok "a process that only mentions the demo was left alone" || bad "a process that only mentions the demo was left alone"
[ ! -e "$state/pid" ] && ok "stop removed the record" || bad "stop removed the record"

# A stale record naming some other live process (PID reuse): stop must not signal it.
sleep 300 > /dev/null 2>&1 &
other="$!"
decoys+=("$other")
mkdir -p "$state"
printf '%s %s %s\n' "$other" "$(awk '{print $22}' "/proc/$other/stat")" "$REPO_ROOT" > "$state/pid"
echo "http://127.0.0.1:1/" > "$state/url"
out="$("$DEMO" stop)" && rc=0 || rc=$?
kill -0 "$other" 2> /dev/null && [ "$rc" = 0 ] && ok "stop does not signal a recorded PID that is not our dashboard" \
  || bad "stop does not signal a recorded PID that is not our dashboard ($rc): $out"
[ ! -e "$state/pid" ] && ok "a stale record is dropped" || bad "a stale record is dropped"

# A recorded start time that no longer matches (PID reused by something else).
printf '%s 1 %s\n' "$bystander" "$REPO_ROOT" > "$state/pid"
"$DEMO" stop > /dev/null
kill -0 "$bystander" 2> /dev/null && ok "stop checks the start time" || bad "stop checks the start time"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
