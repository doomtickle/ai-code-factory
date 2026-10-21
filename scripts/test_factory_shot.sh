#!/usr/bin/env bash
set -euo pipefail

# Tests for templates/agent/factory-shot's argument handling: everything it
# decides before it would start Chrome. The rendering itself needs the role
# image (chrome-headless-shell) and is exercised by hand when the pin changes.
# No network, no browser: FACTORY_SHOT_CHROME points at a stub that must
# never be reached by these cases.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHOT="$REPO_ROOT/templates/agent/factory-shot"
tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0; fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }

printf '#!/bin/sh\necho reached > "%s/reached"; exit 1\n' "$tmp" > "$tmp/fake-chrome"
chmod +x "$tmp/fake-chrome"
export FACTORY_SHOT_CHROME="$tmp/fake-chrome"
printf '<p>hi</p>' > "$tmp/page.html"

run() { # run NAME EXPECTED_EXIT FRAGMENT ARGS...
  local name="$1" want="$2" frag="$3" rc out; shift 3
  rm -f "$tmp/reached"
  out="$(cd "$tmp" && node "$SHOT" "$@" 2>&1)" && rc=0 || rc=$?
  if [ "$rc" != "$want" ]; then bad "$name (exit $rc, wanted $want): $out"; return; fi
  case "$out" in *"$frag"*) ;; *) bad "$name: output lacks '$frag': $out"; return ;; esac
  [ ! -e "$tmp/reached" ] || { bad "$name: Chrome was started"; return; }
  ok "$name"
}

node --check "$SHOT" && ok "factory-shot parses" || bad "factory-shot parses"
run "no arguments prints usage" 2 "factory-shot <page> <out.png>"
run "one argument prints usage" 2 "factory-shot <page> <out.png>" page.html
run "unknown flag prints usage" 2 "factory-shot <page> <out.png>" page.html out.png --bogus
run "output must be .png" 1 "must end in .png" page.html out.jpg
run "missing file" 1 "no such file" nope.html out.png
run "https is refused" 1 "reaches nothing else" https://example.com/ out.png
run "http to another host is refused" 1 "reaches nothing else" http://example.com/ out.png
run "http with userinfo is refused" 1 "reaches nothing else" http://user:pw@127.0.0.1:8000/ out.png
run "file scheme spelled out is refused" 1 "reaches nothing else" file:///etc/hostname out.png
run "width must be a number" 1 "whole number" page.html out.png --width wide
run "width out of range" 1 "--width 100..4000" page.html out.png --width 50
run "height out of range" 1 "--width 100..4000" page.html out.png --height 99999

# A page that passes every check reaches the browser; with the stub, the
# failure is reported as Chrome's, not as a crash.
rm -f "$tmp/reached"
out="$(cd "$tmp" && node "$SHOT" page.html out.png 2>&1)" && rc=0 || rc=$?
[ "$rc" = 1 ] && [ -e "$tmp/reached" ] && ok "a valid file page starts the browser" \
  || bad "a valid file page starts the browser (exit $rc): $out"
out="$(cd "$tmp" && FACTORY_SHOT_CHROME=/nonexistent/chrome node "$SHOT" page.html out.png 2>&1)" && rc=0 || rc=$?
[ "$rc" = 1 ] && case "$out" in *"could not start"*) ok "a missing browser is a clean error" ;; *) bad "a missing browser is a clean error: $out" ;; esac

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
