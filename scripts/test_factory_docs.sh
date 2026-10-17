#!/usr/bin/env bash
set -euo pipefail

# Tests for "factory docs [--print]" (cmd_docs and docs_opener in bin/factory):
#
# 1. --print, or a stdout that isn't a terminal, prints the HTML scripts/docs.py
#    renders (docs.py --root <repo>, no --out) and writes no file.
# 2. On a terminal it writes $FACTORY_STATE/docs/index.html (docs.py --out) and
#    opens it with the platform's opener: open (macOS), wslview (WSL, detected
#    from /proc/version or WSL_DISTRO_NAME), else xdg-open. With no opener it
#    prints the path and still succeeds.
# 3. An unknown flag is a usage error, and docs.py never runs.
# 4. It needs no sbx, docker or herdr (stubs that record any call stay
#    silent) and no broker/ submodule: a fixture checkout with neither works,
#    so it runs on a fresh clone before 'factory setup'.
# 5. If the real scripts/docs.py exists, one real render through
#    'factory docs --print' produces HTML.
#
# Parts 1, 3 and 4 run a fixture copy of bin/factory and scripts/lib/ as a
# subprocess, with a stub scripts/docs.py. Part 2 sources the real bin/factory
# (whose main is guarded) to override its terminal check (stdout_is_tty) and WSL
# check (is_wsl), with a stub uname on PATH for macOS. Every run uses a PATH of
# stubs plus links to the few system tools it needs, so the machine's real
# openers, sbx and docker are never reachable.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# The physical path: on macOS the temp dir is under /var, a symlink to
# /private/var, and bin/factory resolves its checkout with pwd -P.
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

# --- system tools: only these are on PATH (no sbx, docker, openers) -----------
sys="$tmp/sys"
mkdir -p "$sys"
for t in bash sh env python3 cat dirname readlink grep mkdir sed tr tail head ls rm mv cp chmod sleep id; do
  p="$(command -v "$t")" || { echo "test needs $t" >&2; exit 1; }
  ln -s "$p" "$sys/$t"
done
real_uname="$(command -v uname)"

# --- stubs ----------------------------------------------------------------------
# Forbidden tools record any call. Openers record theirs in $tmp/opened.
stubs="$tmp/stubs"
mkdir -p "$stubs"
for t in sbx docker herdr; do
  printf '#!/bin/sh\necho "%s $*" >> "%s/forbidden"\nexit 1\n' "$t" "$tmp" > "$stubs/$t"
  chmod +x "$stubs/$t"
done
openers="$tmp/openers"
mkdir -p "$openers"
for t in open wslview xdg-open; do
  printf '#!/bin/sh\necho "%s $*" > "%s/opened.tmp" && mv "%s/opened.tmp" "%s/opened"\n' \
    "$t" "$tmp" "$tmp" "$tmp" > "$openers/$t"
  chmod +x "$openers/$t"
done
# Windows interop for WSL: cmd.exe answers %TEMP% (with a CRLF, as on Windows),
# wslpath maps C:\Users\me\Temp <-> $tmp/winc/Users/me/Temp, and explorer.exe
# records what it was asked to open (exiting 1, as the real one does).
mkdir -p "$tmp/winc/Users/me/Temp"
printf '#!/bin/sh\nprintf "C:\\\\Users\\\\me\\\\Temp\\r\\n"\n' > "$openers/cmd.exe"
cat > "$openers/wslpath" <<WSLPATH
#!/bin/sh
case "\$1" in
  -u) echo "$tmp/winc/Users/me/Temp" ;;
  -w) printf 'C:\\\\Users\\\\me\\\\Temp\\\\%s\\n' "\${2##*/}" ;;
esac
WSLPATH
printf '#!/bin/sh\nprintf "%%s\\\\n" "explorer.exe $*" > "%s/opened.tmp" && mv "%s/opened.tmp" "%s/opened"\nexit 1\n' \
  "$tmp" "$tmp" "$tmp" > "$openers/explorer.exe"
chmod +x "$openers/cmd.exe" "$openers/wslpath" "$openers/explorer.exe"
# uname: Linux unless FAKE_UNAME says otherwise.
printf '#!/bin/sh\nif [ "${1:-}" = -s ] && [ -n "${FAKE_UNAME:-}" ]; then echo "$FAKE_UNAME"; else exec "%s" "$@"; fi\n' \
  "$real_uname" > "$stubs/uname"
chmod +x "$stubs/uname"
# opener_path NAME... -> a dir holding just those openers
opener_path() {
  local d="$tmp/op-$*" t
  d="${d// /-}"
  mkdir -p "$d"
  for t in "$@"; do ln -sf "$openers/$t" "$d/$t"; done
  printf '%s' "$d"
}

# --- fixture checkout: bin/factory + scripts/lib, a stub docs.py, no broker/ --
fx="$tmp/checkout"
mkdir -p "$fx/bin" "$fx/scripts/lib"
cp "$REPO_ROOT/bin/factory" "$fx/bin/factory"
cp "$REPO_ROOT"/scripts/lib/*.sh "$fx/scripts/lib/"
cat > "$fx/scripts/docs.py" <<'PY'
import os, sys
with open(os.environ["DOCS_STUB_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
html = "<!doctype html><title>stub docs</title>\n"
if "--out" in sys.argv:
    with open(sys.argv[sys.argv.index("--out") + 1], "w") as f:
        f.write(html)
else:
    sys.stdout.write(html)
PY
export DOCS_STUB_LOG="$tmp/docs-calls"

reset_run() { rm -f "$tmp/opened" "$tmp/docs-calls" "$tmp/forbidden"; rm -rf "$tmp/state"; }
# run_fixture ARGS... -> run the fixture's bin/factory, stdout+stderr to $tmp/out
run_fixture() {
  env -u WSL_DISTRO_NAME PATH="$stubs:$sys" XDG_STATE_HOME="$tmp/state" HOME="$tmp/home" \
    "$fx/bin/factory" "$@" > "$tmp/out" 2>&1
}

# ===========================================================================
# Part 1: --print / non-terminal stdout print the HTML
# ===========================================================================
reset_run
if run_fixture docs --print; then ok "docs --print exits 0"; else bad "docs --print exits 0 ($(cat "$tmp/out"))"; fi
assert_contains "docs --print prints the HTML" "$(cat "$tmp/out")" "<!doctype html><title>stub docs</title>"
assert_eq "docs --print runs docs.py with --root and no --out" "$(cat "$tmp/docs-calls" 2>/dev/null)" "--root $fx"
[ ! -e "$tmp/state/ai-code-factory/docs/index.html" ] && ok "docs --print writes no file" || bad "docs --print writes no file"

reset_run
if run_fixture docs; then ok "docs with non-terminal stdout exits 0"; else bad "docs with non-terminal stdout exits 0 ($(cat "$tmp/out"))"; fi
assert_contains "docs with non-terminal stdout prints the HTML" "$(cat "$tmp/out")" "<title>stub docs</title>"
[ ! -e "$tmp/opened" ] && ok "docs with non-terminal stdout opens nothing" || bad "docs with non-terminal stdout opens nothing"

# ===========================================================================
# Part 3: unknown flag
# ===========================================================================
reset_run
if run_fixture docs --html; then bad "docs --html fails"; else ok "docs --html fails"; fi
assert_contains "docs --html prints the usage" "$(cat "$tmp/out")" "usage: factory docs [--print]"
[ ! -e "$tmp/docs-calls" ] && ok "docs --html never runs docs.py" || bad "docs --html never runs docs.py"

# ===========================================================================
# Part 4: no sbx/docker/herdr, no broker/ submodule
# ===========================================================================
[ ! -e "$fx/broker" ] && ok "fixture: the checkout has no broker/" || bad "fixture: the checkout has no broker/"
reset_run
run_fixture docs --print || true
[ ! -e "$tmp/forbidden" ] && ok "docs never calls sbx, docker or herdr" \
  || bad "docs never calls sbx, docker or herdr (called: $(cat "$tmp/forbidden"))"
assert_contains "docs works with no broker/ submodule" "$(cat "$tmp/out")" "<title>stub docs</title>"
reset_run
run_fixture help || true
assert_contains "factory help lists docs" "$(cat "$tmp/out")" "factory docs [--print]"

# ===========================================================================
# Part 2: terminal mode writes the file and calls the platform's opener
# ===========================================================================
# tty_run UNAME WSL_PROC WSL_DISTRO OPENERS... -> source the real bin/factory
# with stdout_is_tty forced on and is_wsl answering WSL_PROC (yes/no), run
# cmd_docs, and wait for a detached opener to record its call.
tty_run() {
  local fake_uname="$1" proc="$2" distro="$3" ops
  shift 3
  ops="$(opener_path "$@")"
  reset_run
  (
    export XDG_STATE_HOME="$tmp/state" HOME="$tmp/home" PATH="$stubs:$ops:$sys" FAKE_UNAME="$fake_uname"
    if [ -n "$distro" ]; then export WSL_DISTRO_NAME="$distro"; else unset WSL_DISTRO_NAME; fi
    # shellcheck disable=SC1091
    . "$REPO_ROOT/bin/factory"
    ROOT="$fx"
    SCRIPTS="$fx/scripts"
    stdout_is_tty() { return 0; }
    if [ "$proc" = yes ]; then is_wsl() { return 0; }; else is_wsl() { return 1; }; fi
    cmd_docs
  ) > "$tmp/out" 2>&1 || { echo "exit $?" >> "$tmp/out"; }
  local i=0
  while [ ! -e "$tmp/opened" ] && [ $# -gt 0 ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
}
index="$tmp/state/ai-code-factory/docs/index.html"
opened() { cat "$tmp/opened" 2>/dev/null || true; }

tty_run Darwin no "" open wslview xdg-open
assert_contains "macOS: index.html written" "$(cat "$index" 2>/dev/null)" "<title>stub docs</title>"
assert_eq "macOS: docs.py gets --root and --out" "$(cat "$tmp/docs-calls" 2>/dev/null)" "--root $fx --out $index"
assert_eq "macOS: opens with open" "$(opened)" "open $index"
assert_contains "macOS: prints the path" "$(cat "$tmp/out")" "$index"
assert_not_contains "macOS: docs.py output not printed" "$(cat "$tmp/out")" "<!doctype"

tty_run Linux yes "" open wslview xdg-open
assert_eq "WSL (/proc/version): opens with wslview" "$(opened)" "wslview $index"

tty_run Linux no Ubuntu open wslview xdg-open
assert_eq "WSL (WSL_DISTRO_NAME): opens with wslview" "$(opened)" "wslview $index"

tty_run Linux yes "" open xdg-open
assert_eq "WSL without wslview: falls back to xdg-open" "$(opened)" "xdg-open $index"

tty_run Linux yes "" wslview cmd.exe wslpath explorer.exe
assert_eq "WSL with Windows interop: opens the Windows copy with explorer.exe" \
  "$(opened)" 'explorer.exe C:\Users\me\Temp\ai-code-factory-docs.html'
assert_contains "WSL with Windows interop: the page is copied to the Windows temp folder" \
  "$(cat "$tmp/winc/Users/me/Temp/ai-code-factory-docs.html" 2>/dev/null)" "<title>stub docs</title>"
assert_contains "WSL with Windows interop: prints the Windows path to paste" \
  "$(cat "$tmp/out")" 'C:\Users\me\Temp\ai-code-factory-docs.html'

tty_run Linux no "" open wslview xdg-open cmd.exe wslpath explorer.exe
assert_eq "Linux: opens with xdg-open" "$(opened)" "xdg-open $index"
assert_contains "Linux: prints the path" "$(cat "$tmp/out")" "$index"

tty_run Linux no ""
assert_eq "no opener: nothing opened" "$(opened)" ""
assert_not_contains "no opener: exit 0" "$(cat "$tmp/out")" "exit "
assert_contains "no opener: prints the path" "$(cat "$tmp/out")" "Docs written to $index"
assert_contains "no opener: says to open it in a browser" "$(cat "$tmp/out")" "Open that file in a web browser"
assert_contains "no opener: index.html still written" "$(cat "$index" 2>/dev/null)" "<title>stub docs</title>"

if [ -e "$tmp/state/ai-code-factory" ]; then
  assert_eq "state dir is private" "$(ls -ld "$tmp/state/ai-code-factory" | cut -c1-10)" "drwx------"
fi

# ===========================================================================
# Part 5: a real render, when scripts/docs.py exists
# ===========================================================================
if [ -f "$REPO_ROOT/scripts/docs.py" ]; then
  reset_run
  if env PATH="$stubs:$sys" XDG_STATE_HOME="$tmp/state" HOME="$tmp/home" \
      "$REPO_ROOT/bin/factory" docs --print > "$tmp/real.html" 2> "$tmp/real.err"; then
    ok "real render: factory docs --print exits 0"
  else
    bad "real render: factory docs --print exits 0 ($(cat "$tmp/real.err"))"
  fi
  head="$(head -c 200 "$tmp/real.html" | tr '[:upper:]' '[:lower:]')"
  assert_contains "real render: output is HTML" "$head" "<!doctype html"
  assert_contains "real render: output ends the document" "$(tail -c 200 "$tmp/real.html")" "</html>"
else
  printf 'skip - real render (scripts/docs.py not present yet)\n'
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
