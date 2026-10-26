#!/usr/bin/env bash
set -euo pipefail

# Tests for which shellcheck scripts/lint.sh uses, and that the image build
# passes the pin through:
#
# 1. A shellcheck on PATH reporting exactly SHELLCHECK_VERSION is used, with
#    no download (the developer/reviewer images carry one; roles have no egress).
# 2. One reporting another version, or none on PATH, falls through to the
#    download path, which still checks the pinned SHA-256.
# 3. templates/agent/extract-tar-member (how the images unpack the release
#    without an xz program) takes only the named regular-file member.
# 4. The Dockerfiles take the pin as build args with no defaults (that
#    `factory _build-templates` passes them from lib/common.sh is
#    factory/test_build_templates.py).
#
# A fake shellcheck and a stub curl on a private PATH in a throwaway checkout:
# no network, no real shellcheck.

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

pin="$(sed -n 's/^readonly SHELLCHECK_VERSION="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/lib/common.sh")"
[ -n "$pin" ] && ok "SHELLCHECK_VERSION is pinned in lib/common.sh ($pin)" || bad "SHELLCHECK_VERSION is pinned in lib/common.sh"

# A checkout holding just lint.sh, its lib and one tracked script.
repo="$tmp/repo"
mkdir -p "$repo/scripts"
cp -R "$REPO_ROOT/scripts/lib" "$repo/scripts/lib"
cp "$REPO_ROOT/scripts/lint.sh" "$repo/scripts/"
printf '#!/usr/bin/env bash\necho hi\n' > "$repo/scripts/a.sh"
git -C "$repo" init -q
git -C "$repo" add -A

# Stubs. The fake shellcheck reports $FAKE_SC_VERSION and logs every other call.
stubs="$tmp/stubs"
mkdir -p "$stubs"
cat > "$stubs/shellcheck" <<'STUB'
#!/bin/sh
if [ "$1" = "--version" ]; then
  printf 'ShellCheck - shell script analysis tool\nversion: %s\nlicense: GNU General Public License, version 3\n' "$FAKE_SC_VERSION"
  exit 0
fi
echo "$*" >> "$STUB_LOG/shellcheck-run"
STUB
cat > "$stubs/curl" <<'STUB'
#!/bin/sh
echo "$*" >> "$STUB_LOG/curl-run"
out=""
while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
[ -z "$out" ] || echo "not a shellcheck tarball" > "$out"
STUB
chmod +x "$stubs/shellcheck" "$stubs/curl"

# run_lint LABEL VERSION [nopath]: output in $OUT, status in $RC, stub logs in
# $tmp/log. "nopath" leaves the fake shellcheck off PATH.
run_lint() {
  rm -rf "$tmp/log" "$tmp/cache"
  mkdir -p "$tmp/log"
  local path="$stubs:$PATH"
  [ "${3:-}" != nopath ] || path="$tmp/curl-only:$PATH"
  set +e
  OUT="$(cd "$repo" && STUB_LOG="$tmp/log" FAKE_SC_VERSION="$2" PATH="$path" \
    XDG_CACHE_HOME="$tmp/cache" bash scripts/lint.sh 2>&1)"
  RC=$?
  set -e
}

# 1. Matching version on PATH.
run_lint match "$pin"
assert_eq "matching shellcheck on PATH: lint passes" "$RC" "0"
assert_contains "matching shellcheck on PATH: it lints the repo's scripts" "$(cat "$tmp/log/shellcheck-run" 2>/dev/null)" "scripts/a.sh"
assert_contains "matching shellcheck on PATH: output reports no warnings" "$OUT" "shellcheck: no warnings"
[ ! -e "$tmp/log/curl-run" ] && ok "matching shellcheck on PATH: nothing is downloaded" \
  || bad "matching shellcheck on PATH: nothing is downloaded (curl ran: $(cat "$tmp/log/curl-run"))"
[ ! -e "$tmp/cache" ] && ok "matching shellcheck on PATH: the cache is untouched" || bad "matching shellcheck on PATH: the cache is untouched"

# 2. Wrong versions fall through to the download, whose checksum check fails
# on the stub's junk tarball. (Needs a platform with a pinned asset.)
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64 | Darwin-arm64)
    for v in "0.9.0" "${pin}1" "1${pin}" ""; do
      run_lint wrong "$v"
      assert_eq "shellcheck '$v' on PATH: lint falls through and fails" "$RC" "1"
      assert_contains "shellcheck '$v' on PATH: the pinned release is downloaded" "$(cat "$tmp/log/curl-run" 2>/dev/null)" "/v$pin/shellcheck-v$pin."
      assert_contains "shellcheck '$v' on PATH: the download is checksum-verified" "$OUT" "does not match its pinned SHA-256"
      [ ! -e "$tmp/log/shellcheck-run" ] && ok "shellcheck '$v' on PATH: the mismatched binary never lints" \
        || bad "shellcheck '$v' on PATH: the mismatched binary never lints"
    done
    # No shellcheck on PATH at all behaves the same.
    mkdir -p "$tmp/curl-only" && ln -sf "$stubs/curl" "$tmp/curl-only/curl"
    run_lint absent "$pin" nopath
    # The real PATH may carry a matching shellcheck; only assert when it doesn't.
    if ! PATH="$PATH" command -v shellcheck >/dev/null 2>&1; then
      assert_eq "no shellcheck on PATH: lint falls through and fails" "$RC" "1"
      assert_contains "no shellcheck on PATH: the pinned release is downloaded" "$(cat "$tmp/log/curl-run" 2>/dev/null)" "/v$pin/shellcheck-v$pin."
    fi
    ;;
  *) echo "skip - no pinned shellcheck asset for $(uname -s) $(uname -m)" ;;
esac

# 3. extract-tar-member.
X="$REPO_ROOT/templates/agent/extract-tar-member"
if python3 -c 'import lzma' 2>/dev/null; then
  python3 - "$tmp/sc.tar.xz" <<'PY'
import io, sys, tarfile
with tarfile.open(sys.argv[1], "w:xz") as t:
    def add(name, data=b"", type=tarfile.REGTYPE, link=""):
        i = tarfile.TarInfo(name); i.type = type; i.linkname = link
        i.size = len(data) if type == tarfile.REGTYPE else 0
        t.addfile(i, io.BytesIO(data) if type == tarfile.REGTYPE else None)
    add("sc-v1/shellcheck", b"#!/bin/sh\necho the binary\n")
    add("sc-v1/README.txt", b"readme")
    add("sc-v1/link", type=tarfile.SYMTYPE, link="/etc/passwd")
    add("sc-v1/dir", type=tarfile.DIRTYPE)
PY
  mkdir "$tmp/x"
  "$X" "$tmp/sc.tar.xz" sc-v1/shellcheck "$tmp/x/shellcheck" && rc=0 || rc=$?
  assert_eq "extract-tar-member: extracts the named member" "$rc" "0"
  assert_eq "extract-tar-member: contents are the member's" "$(cat "$tmp/x/shellcheck")" "$(printf '#!/bin/sh\necho the binary')"
  [ -x "$tmp/x/shellcheck" ] && ok "extract-tar-member: the result is executable" || bad "extract-tar-member: the result is executable"
  assert_eq "extract-tar-member: extracts nothing else" "$(ls "$tmp/x")" "shellcheck"
  fails() { # fails LABEL FRAGMENT ARGS...
    local label="$1" frag="$2" out rc; shift 2
    out="$("$X" "$@" 2>&1)" && rc=0 || rc=$?
    [ "$rc" != 0 ] && assert_contains "extract-tar-member: $label" "$out" "$frag" || bad "extract-tar-member: $label (exit 0)"
  }
  fails "a missing member is refused" "no member" "$tmp/sc.tar.xz" sc-v1/nope "$tmp/x/a"
  fails "a symlink member is refused" "not a regular file" "$tmp/sc.tar.xz" sc-v1/link "$tmp/x/b"
  fails "a directory member is refused" "not a regular file" "$tmp/sc.tar.xz" sc-v1/dir "$tmp/x/c"
  fails "a partial name is refused" "no member" "$tmp/sc.tar.xz" shellcheck "$tmp/x/d"
  fails "an existing destination is refused" "exists" "$tmp/sc.tar.xz" sc-v1/shellcheck "$tmp/x/shellcheck"
  echo "not xz" > "$tmp/junk"
  fails "a file that is not .tar.xz is refused" "extract-tar-member:" "$tmp/junk" sc-v1/shellcheck "$tmp/x/e"
  fails "wrong usage is refused" "Usage" "$tmp/sc.tar.xz"
  assert_eq "extract-tar-member: refused cases wrote nothing" "$(ls "$tmp/x")" "shellcheck"
else
  echo "skip - python3 has no lzma here"
fi

# 4. Image build wiring.
for t in developer reviewer; do
  df="$REPO_ROOT/templates/$t/Dockerfile"
  for a in SHELLCHECK_VERSION SHELLCHECK_LINUX_X86_64_SHA256 SHELLCHECK_LINUX_AARCH64_SHA256; do
    grep -qx "ARG $a" "$df" && ok "$t Dockerfile: ARG $a has no default" || bad "$t Dockerfile: ARG $a has no default"
  done
  chrome_line="$(grep -n 'chrome-headless-shell --version' "$df" | cut -d: -f1)"
  sc_line="$(grep -n 'shellcheck --version' "$df" | cut -d: -f1)"
  copy_line="$(grep -n '^COPY --chmod=0555 templates/agent/factory-shot' "$df" | cut -d: -f1)"
  [ -n "$chrome_line" ] && [ "$chrome_line" -lt "$sc_line" ] && [ "$sc_line" -lt "$copy_line" ] \
    && ok "$t Dockerfile: the ShellCheck layer sits after Chrome's and before the COPYs" \
    || bad "$t Dockerfile: the ShellCheck layer sits after Chrome's and before the COPYs"
  grep -q 'extract-tar-member' "$df" && ! grep -q 'tar -x' "$df" \
    && ok "$t Dockerfile: unpacks with extract-tar-member, not the xz program" \
    || bad "$t Dockerfile: unpacks with extract-tar-member, not the xz program"
  grep -q 'python3' <(grep '^RUN for t in' "$df") && ok "$t Dockerfile: the tool check includes python3" \
    || bad "$t Dockerfile: the tool check includes python3"
  grep -qx '!templates/agent/extract-tar-member' "$REPO_ROOT/.dockerignore" \
    && ok ".dockerignore lets the build context carry extract-tar-member" \
    || bad ".dockerignore lets the build context carry extract-tar-member"
  grep -q 'sha256sum -c' "$df" && grep -q 'dpkg --print-architecture' "$df" && grep -q '/usr/local/bin/shellcheck' "$df" \
    && ok "$t Dockerfile: ShellCheck is arch-selected, verified and installed" \
    || bad "$t Dockerfile: ShellCheck is arch-selected, verified and installed"
done
grep -q 'entrypoint shellcheck' "$REPO_ROOT/.github/workflows/ci.yml" && grep -q -- '--network none --entrypoint shellcheck' "$REPO_ROOT/.github/workflows/ci.yml" \
  && ok "ci.yml runs shellcheck --version in the role images with no network" \
  || bad "ci.yml runs shellcheck --version in the role images with no network"
aarch64="$(sed -n 's/^readonly SHELLCHECK_LINUX_AARCH64_SHA256="\(.*\)"$/\1/p' "$REPO_ROOT/scripts/lib/common.sh")"
case "$aarch64" in
  *[!0-9a-f]* | "") bad "SHELLCHECK_LINUX_AARCH64_SHA256 is a 64-char hex digest" ;;
  *) [ "${#aarch64}" = 64 ] && ok "SHELLCHECK_LINUX_AARCH64_SHA256 is a 64-char hex digest" || bad "SHELLCHECK_LINUX_AARCH64_SHA256 is a 64-char hex digest" ;;
esac

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
