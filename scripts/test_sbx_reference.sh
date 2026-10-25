#!/usr/bin/env bash
set -euo pipefail

# Tests for the saved sbx CLI reference (doomtickle/passdesk-ops#148):
#
# 1. scripts/gen-sbx-reference.sh runs only `sbx version` and `sbx ... --help`,
#    stamps the version and date, fences each help text, and covers the
#    subcommands the factory uses.
# 2. It exits 1 (after writing the file) when a help command fails, and writes
#    nothing when sbx is missing.
# 3. sbx_reference_problem (scripts/lib/common.sh; `factory doctor` uses the Python copy,
#    factory/sbx.py, tested in factory/test_sbx.py)
#    reports the committed placeholder, a missing file, a missing stamp and a
#    version that differs from SBX_VERSION, and is silent when current.
#
# A stub sbx on a private PATH stands in for the real one: no network, no sbx.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() {
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac
}
assert_not_contains() {
  case "$2" in *"$3"*) bad "$1 (did not expect '$3', got: $2)" ;; *) ok "$1" ;; esac
}

# shellcheck source=lib/common.sh
. "$REPO_ROOT/scripts/lib/common.sh"

sys="$tmp/sys"
stubs="$tmp/stubs"
mkdir -p "$sys" "$stubs"
for t in bash sh env cat dirname basename grep mkdir sed tr head rm mv chmod date mktemp printf test; do
  p="$(command -v "$t" 2>/dev/null)" || continue
  ln -sf "$p" "$sys/$t"
done

# Stub sbx: logs its argv; `version` prints the pinned format; FAIL_ON makes
# one help command exit 2.
cat > "$stubs/sbx" <<STUB
#!/bin/sh
echo "\$*" >> "$tmp/argv.log"
if [ "\$1" = version ]; then echo "sbx version: v\${STUB_VERSION:-$SBX_VERSION} abc123"; exit 0; fi
if [ -n "\${FAIL_ON:-}" ] && [ "\$*" = "\$FAIL_ON" ]; then echo "unknown command" >&2; exit 2; fi
echo "Usage: sbx \$*"
echo "      --user string   not a real flag"
STUB
chmod +x "$stubs/sbx"

gen() { # gen OUT [ENV...] -> runs the generator on the private PATH; sets $status and $output
  local out="$1"
  shift
  status=0
  output="$(env -i PATH="$stubs:$sys" HOME="$tmp" "$@" bash "$REPO_ROOT/scripts/gen-sbx-reference.sh" "$out" 2>&1)" || status=$?
}

# --- 1. generation -----------------------------------------------------------------
gen "$tmp/out/sbx-cli.md"
assert_eq "generator succeeds" "$status" 0
ref="$(cat "$tmp/out/sbx-cli.md")"
assert_contains "stamp has the version and today's date" "$ref" "<!-- sbx-reference: version=$SBX_VERSION generated=$(date -u +%Y-%m-%d) -->"
assert_contains "title names the version" "$ref" "# sbx CLI reference (sbx $SBX_VERSION)"
for c in "sbx --help" "sbx run --help" "sbx create --help" "sbx exec --help" "sbx ls --help" "sbx rm --help" \
  "sbx stop --help" "sbx ports --help" "sbx login --help" "sbx secret set --help" "sbx secret ls --help" \
  "sbx secret rm --help" "sbx mcp add --help" "sbx mcp ls --help" "sbx mcp rm --help" "sbx policy init --help" \
  "sbx policy check --help" "sbx policy allow --help" "sbx policy ls --help" "sbx policy reset --help" \
  "sbx template ls --help" "sbx template load --help" "sbx settings get --help" "sbx settings set --help" \
  "sbx daemon restart --help" "sbx kit validate --help"; do
  assert_contains "covers $c" "$ref" "## \`$c\`"
done
assert_contains "help text is in a fenced block" "$ref" '````text
Usage: sbx exec --help
      --user string   not a real flag
````'
assert_contains "version output is included" "$ref" "sbx version: v$SBX_VERSION abc123"
# every call the generator made is `version` or ends in --help: nothing else runs
bad_calls="$(grep -vE '^version$|--help$' "$tmp/argv.log" || true)"
assert_eq "only version and --help are run" "$bad_calls" ""
assert_eq "no stray temp files left" "$(ls "$tmp/out")" "sbx-cli.md"
assert_eq "a current file raises no doctor problem" "$(sbx_reference_problem "$tmp/out/sbx-cli.md" || true)" ""
sbx_reference_problem "$tmp/out/sbx-cli.md" >/dev/null && bad "current file reported as a problem" || ok "current file returns 1"

# --- 1b. installed sbx differs from the pin: warn, stamp what is installed -----------
gen "$tmp/drift.md" STUB_VERSION=9.9.9
assert_eq "generator still succeeds on a different version" "$status" 0
assert_contains "warns about the pin" "$output" "installed sbx is 9.9.9 but the factory pins $SBX_VERSION"
assert_contains "stamps the installed version" "$(cat "$tmp/drift.md")" "version=9.9.9 "
msg="$(sbx_reference_problem "$tmp/drift.md")" && ok "drift reported" || bad "drift not reported"
assert_contains "drift message names both versions" "$msg" "for sbx 9.9.9 but the pinned sbx is $SBX_VERSION"

# --- 2. failures ---------------------------------------------------------------------
gen "$tmp/fail.md" FAIL_ON="mcp add --help"
assert_eq "a failed help command exits 1" "$status" 1
assert_contains "the failure is named" "$output" "failed (exit 2): sbx mcp add --help"
assert_contains "the file is still written, marked" "$(cat "$tmp/fail.md")" "This command failed (exit 2)"
assert_contains "later commands still ran" "$(cat "$tmp/fail.md")" 'sbx kit validate --help'

status=0
output="$(env -i PATH="$sys" HOME="$tmp" bash "$REPO_ROOT/scripts/gen-sbx-reference.sh" "$tmp/none.md" 2>&1)" || status=$?
assert_eq "no sbx: exits 1" "$status" 1
assert_contains "no sbx: says so" "$output" "'sbx' not found"
[ ! -e "$tmp/none.md" ] && ok "no sbx: writes nothing" || bad "no sbx: wrote a file"

# --- 3. doctor check -----------------------------------------------------------------
committed="$REPO_ROOT/docs/reference/sbx-cli.md"
msg="$(sbx_reference_problem "$committed")" && ok "committed file reports a problem or is current" || ok "committed file is current"
if grep -q 'not-generated' "$committed"; then
  assert_contains "placeholder is reported" "$msg" "has not been generated yet; run scripts/gen-sbx-reference.sh"
fi
assert_contains "placeholder names the command" "$(cat "$committed")" "scripts/gen-sbx-reference.sh"
printf '<!-- sbx-reference: not-generated -->\n' > "$tmp/ph.md"
assert_contains "placeholder reported" "$(sbx_reference_problem "$tmp/ph.md")" "has not been generated yet"
assert_contains "missing file reported" "$(sbx_reference_problem "$tmp/absent.md")" "is missing"
printf '# no stamp\n' > "$tmp/nostamp.md"
assert_contains "missing stamp reported" "$(sbx_reference_problem "$tmp/nostamp.md")" "no version stamp"

# doctor: the warning, not a failure, is wired into factory/commands/doctor.py
grep -q 'sbx.reference_problem' "$REPO_ROOT/factory/commands/doctor.py" && ok "factory doctor calls the check" || bad "factory doctor does not call the check"
assert_not_contains "doctor treats it as a warning" "$(grep -A2 'sbx.reference_problem' "$REPO_ROOT/factory/commands/doctor.py")" "fail("

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
