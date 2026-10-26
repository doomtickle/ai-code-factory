#!/usr/bin/env bash
set -euo pipefail

# Usage: 02-launch-factory.sh <project-dir>
#
# Starts the factory for <project-dir>: the broker VM (network tools for
# roles), the dispatch bridge (host), one sandbox per role in roles/ and the
# Coordinator sandbox. `factory start` does this and then opens the dashboard;
# FACTORY_NO_UI=1 only changes the closing message. Safe to re-run: existing
# sandboxes are reused and missing ones are created. The work is in Python:
# factory/launch.py. The usage check stays here, as it names this script the
# way it was called.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

[ $# -eq 1 ] || die "usage: $0 <project-dir>"

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec "$(resolve_python)" "$ROOT/factory" _launch "$@"
