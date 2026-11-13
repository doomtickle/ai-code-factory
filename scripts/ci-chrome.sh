#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/ci-chrome.sh pin NAME [DOCKERFILE]   the value of `ARG NAME=...` (CHROME_VERSION, CHROME_SHA256_AMD64, ...)
#        scripts/ci-chrome.sh apt [DOCKERFILE]        the apt packages the Dockerfile installs for Chrome, one per line
#
# DOCKERFILE defaults to templates/developer/Dockerfile. The `browser shard` jobs of .github/workflows/ci.yml
# installs the role images' Chrome for Testing from these, so there is one pin; scripts/test_ci_changes.sh checks
# the reviewer's Dockerfile says the same.

root="$(cd "$(dirname "$0")/.." && pwd -P)"

case "${1:-}" in
  pin)
    name="${2:?usage: ci-chrome.sh pin NAME [DOCKERFILE]}"
    file="${3:-$root/templates/developer/Dockerfile}"
    case "$name" in *[!A-Z0-9_]*) echo "bad ARG name: $name" >&2; exit 2 ;; esac
    value="$(sed -n "s/^ARG $name=\\([^[:space:]]*\\)\$/\\1/p" "$file")"
    [ -n "$value" ] || { echo "no 'ARG $name=' in $file" >&2; exit 1; }
    [ "$(printf '%s\n' "$value" | wc -l | tr -d ' ')" = 1 ] || { echo "more than one 'ARG $name=' in $file" >&2; exit 1; }
    printf '%s\n' "$value"
    ;;
  apt)
    file="${2:-$root/templates/developer/Dockerfile}"
    # The lines between `apt-get install ... \` and `&& rm -rf /var/lib/apt/lists`, minus those two.
    pkgs="$(sed -n '/apt-get install -y --no-install-recommends/,/rm -rf \/var\/lib\/apt\/lists/p' "$file" | sed '1d;$d' | sed 's/\\$//' | tr -s '[:space:]' '\n' | sed '/^$/d')"
    [ -n "$pkgs" ] || { echo "no apt-get install list in $file" >&2; exit 1; }
    printf '%s\n' "$pkgs"
    ;;
  *)
    echo "usage: ci-chrome.sh pin NAME [DOCKERFILE] | apt [DOCKERFILE]" >&2
    exit 2
    ;;
esac
