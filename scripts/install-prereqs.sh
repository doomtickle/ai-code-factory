#!/usr/bin/env bash
set -euo pipefail

# Installs the exact sbx version pinned in lib/common.sh, verifying its SHA-256
# checksum. Skips it if it is already at the pinned version.
#   macOS 14+ (Apple silicon): under $PREFIX (default ~/.local).
#   Ubuntu 24.04+ x86_64, including WSL2: sbx from Docker's .deb via apt (asks
#   for sudo; it installs the AppArmor profile sbx needs).
# The work is in Python: factory/prereqs.py, which needs Python 3.9+ first
# (`factory setup` has checked that with `factory doctor` by now).

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec "$(resolve_python)" "$ROOT/factory" _install-prereqs
