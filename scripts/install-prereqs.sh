#!/usr/bin/env bash
set -euo pipefail

# Installs the exact sbx and herdr versions pinned in lib/common.sh, verifying
# SHA-256 checksums. Skips a tool that is already at the pinned version.
#   macOS 14+ (Apple silicon): both under $PREFIX (default ~/.local).
#   Ubuntu 24.04+ x86_64, including WSL2: sbx from Docker's .deb via apt (asks
#   for sudo; it installs the AppArmor profile sbx needs), herdr under $PREFIX.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"
# shellcheck source=lib/preflight.sh
. "$(dirname "$0")/lib/preflight.sh"

PREFIX="${PREFIX:-$HOME/.local}"

case "$(uname -s):$(uname -m)" in
  Darwin:arm64) platform=macos ;;
  Linux:x86_64) platform=linux ;;
  *) die "this installer supports macOS (Apple silicon) or Ubuntu Linux (x86_64, including WSL2) only; found $(uname -s)/$(uname -m)" ;;
esac

if [ "$platform" = macos ]; then
  major="$(sw_vers -productVersion | cut -d. -f1)"
  [ "$major" -ge 14 ] || die "macOS 14 (Sonoma) or later required"
  herdr_url="$HERDR_URL"; herdr_sha256="$HERDR_SHA256"
else
  kvm_check || exit 1
  pin="$(sbx_deb_pin)" || exit 1
  sbx_url="${pin% *}"; sbx_sha256="${pin#* }"
  require_bin apt-get
  require_bin sudo
  herdr_url="$HERDR_LINUX_X86_64_URL"; herdr_sha256="$HERDR_LINUX_X86_64_SHA256"
fi

require_bin curl
require_sha256

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# fetch URL SHA256 DEST
fetch() {
  curl -fsSL --retry 3 -o "$3" "$1" || die "download failed: $1"
  [ "$(sha256_hash "$3")" = "$2" ] || die "checksum mismatch for $1"
}

mkdir -p "$PREFIX/bin"

if command -v sbx >/dev/null 2>&1 && [ "$(sbx_installed_version)" = "$SBX_VERSION" ]; then
  echo "sbx $SBX_VERSION already installed: $(command -v sbx)"
elif [ "$platform" = macos ]; then
  echo "Installing sbx $SBX_VERSION..."
  fetch "$SBX_URL" "$SBX_SHA256" "$tmp/sbx.tar.gz"
  dest="$PREFIX/opt/sbx/$SBX_VERSION"
  rm -rf "$dest"
  mkdir -p "$dest"
  tar -xzf "$tmp/sbx.tar.gz" -C "$dest"
  ln -sfn "$dest/Sbx.app/Contents/MacOS/sbx" "$PREFIX/bin/sbx"
else
  echo "Installing sbx $SBX_VERSION (Docker's package; apt asks for your password)..."
  fetch "$sbx_url" "$sbx_sha256" "$tmp/docker-sbx.deb"
  # apt reads local .deb files as the unprivileged _apt user, which can't
  # enter a private temp dir; it then falls back to reading as root and says
  # so. Make the file readable to keep the output quiet.
  chmod 755 "$tmp" && chmod 644 "$tmp/docker-sbx.deb"
  # No recommends: they include systemd-resolved, which can take over
  # /etc/resolv.conf (WSL generates its own) and break DNS. The one that
  # matters, apparmor, is added only where the kernel enforces AppArmor; its
  # profile lets the sandbox shim create user namespaces on Ubuntu 23.10+.
  extra=""
  if [ -d /sys/kernel/security/apparmor ] && ! command -v apparmor_parser >/dev/null 2>&1; then
    extra="apparmor"
  fi
  # --allow-downgrades: the pinned version wins over a newer one from Docker's apt repo.
  # shellcheck disable=SC2086 # $extra is empty or one package name
  sudo apt-get install -y --no-install-recommends --allow-downgrades "$tmp/docker-sbx.deb" $extra
  hash -r
fi

if command -v herdr >/dev/null 2>&1 && [ "$(herdr_installed_version)" = "$HERDR_VERSION" ]; then
  echo "herdr $HERDR_VERSION already installed: $(command -v herdr)"
else
  echo "Installing herdr $HERDR_VERSION..."
  fetch "$herdr_url" "$herdr_sha256" "$tmp/herdr"
  install -m 0755 "$tmp/herdr" "$PREFIX/bin/herdr"
fi

case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) echo "note: add $PREFIX/bin to your PATH, ahead of any other copies of these tools" ;;
esac

# Verify what will actually run from PATH.
PATH="$PREFIX/bin:$PATH" require_sbx
PATH="$PREFIX/bin:$PATH" require_herdr
echo "Prerequisites OK: sbx $SBX_VERSION, herdr $HERDR_VERSION"
