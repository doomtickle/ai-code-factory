#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/lint.sh
#
# Runs the pinned shellcheck (SHELLCHECK_VERSION in lib/common.sh) on every
# shell script in the repo, failing on warnings and errors. A shellcheck on PATH
# that reports exactly that version is used as is (the developer and reviewer
# images carry one, so roles with no network can lint). Otherwise it downloads
# the pinned one once, checksum-verified, into ~/.cache/ai-code-factory/
# (XDG_CACHE_HOME if set). Runs in CI and works locally on Linux x86_64 and
# macOS (Apple silicon).

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"

# path_shellcheck -> path of a shellcheck on PATH whose version is exactly the pin.
path_shellcheck() {
  local found
  found="$(command -v shellcheck 2>/dev/null)" || return 1
  [ "$("$found" --version 2>/dev/null | sed -n 's/^version: //p')" = "$SHELLCHECK_VERSION" ] || return 1
  printf '%s\n' "$found"
}

if ! bin="$(path_shellcheck)"; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) asset=linux.x86_64 sum="$SHELLCHECK_LINUX_X86_64_SHA256" ;;
    Darwin-arm64) asset=darwin.aarch64 sum="$SHELLCHECK_DARWIN_AARCH64_SHA256" ;;
    *) die "no pinned shellcheck for $(uname -s) $(uname -m)" ;;
  esac
  require_bin curl
  require_bin tar
  require_sha256

  cache="${XDG_CACHE_HOME:-$HOME/.cache}/ai-code-factory"
  bin="$cache/shellcheck-v$SHELLCHECK_VERSION-$asset/shellcheck"
  if [ ! -x "$bin" ]; then
    echo "Downloading shellcheck $SHELLCHECK_VERSION ($asset)..."
    mkdir -p "$cache"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    tarball="$tmp/shellcheck.tar.xz"
    curl -fsSL -o "$tarball" \
      "https://github.com/koalaman/shellcheck/releases/download/v$SHELLCHECK_VERSION/shellcheck-v$SHELLCHECK_VERSION.$asset.tar.xz"
    [ "$(sha256_hash "$tarball")" = "$sum" ] || die "shellcheck download does not match its pinned SHA-256"
    tar -xJf "$tarball" -C "$tmp"
    mkdir -p "$(dirname "$bin")"
    mv "$tmp/shellcheck-v$SHELLCHECK_VERSION/shellcheck" "$bin"
  fi
fi

# Every tracked shell script: *.sh plus the extensionless ones.
files=()
while IFS= read -r f; do files+=("$f"); done < <(
  git -C "$ROOT" -c core.fsmonitor=false ls-files -- '*.sh' bin/factory \
    templates/agent/factory-test templates/agent/factory-lane templates/agent/factory-demo | grep -v '^broker/'
)
echo "shellcheck $SHELLCHECK_VERSION: ${#files[@]} scripts"
cd "$ROOT"
"$bin" --external-sources --severity=warning "${files[@]}"
echo "shellcheck: no warnings"
