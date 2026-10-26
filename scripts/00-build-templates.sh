#!/usr/bin/env bash
set -euo pipefail

# Usage: 00-build-templates.sh [--build-only]
#
# Builds the sandbox templates locally and loads them into the sbx runtime.
# No registry push: each image goes docker build -> tar -> sbx template load.
# --build-only builds the same images (docker only) without sbx: no load, and
# nothing recorded in the state dir. CI uses it. The work is in Python:
# factory/build_templates.py. The usage check stays here, as it names this
# script the way it was called.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

case "${1:-}" in
  "") ;;
  --build-only) ;;
  *) die "usage: $0 [--build-only]" ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec "$(resolve_python)" "$ROOT/factory" _build-templates ${1:+"$1"}
