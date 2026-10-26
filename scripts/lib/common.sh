# Shared helpers for factory scripts. Source it; do not execute it.
# shellcheck shell=bash
# The constants below are used by the scripts that source this file.
# shellcheck disable=SC2034

# Exact tool versions the factory is tested against. Bump these deliberately,
# in the same commit as any spec/script changes the new version requires.
readonly SBX_VERSION="0.45.1"

# The macOS (Apple silicon) release artifact for `factory _install-prereqs`
# (scripts/install-prereqs.sh), read from here by factory/pins.py. The SHA-256
# value comes from the GitHub release digest. The Linux (x86_64, including WSL2)
# .deb pins, one per Ubuntu release, are in factory/preflight.py only.
readonly SBX_URL="https://github.com/docker/sbx-releases/releases/download/v${SBX_VERSION}/DockerSandboxes-darwin.tar.gz"
readonly SBX_SHA256="0a7207d1736ef9d109b722cf3d21e2b9ed5840d7667fcbcb7596075265b22023"

# Pinned ShellCheck for scripts/lint.sh (CI and local runs) and the developer and
# reviewer images (`factory _build-templates` reads these through factory/pins.py
# and passes them as build args). Hashes checked against the v0.10.0 release downloads on 2026-10-17. The linux.aarch64 hash (the
# images on Apple-silicon Macs) was computed on the host by the owner from the
# official shellcheck-v0.10.0.linux.aarch64.tar.xz release asset on 2026-10-25.
readonly SHELLCHECK_VERSION="0.10.0"
readonly SHELLCHECK_LINUX_X86_64_SHA256="6c881ab0698e4e6ea235245f22832860544f17ba386442fe7e9d629f8cbedf87"
readonly SHELLCHECK_LINUX_AARCH64_SHA256="324a7e89de8fa2aed0d0c28f3dab59cf84c6d74264022c00c22af665ed1a09bb"
readonly SHELLCHECK_DARWIN_AARCH64_SHA256="bbd2f14826328eee7679da7221f2bc3afb011f6a928b848c80c321f6046ddf81"

# Factory sandbox, template and MCP names. Role sandboxes are factory-<role>.
readonly BROKER_SANDBOX="factory-broker"
readonly COORDINATOR_SANDBOX="factory-coordinator"
readonly DISPATCH_MCP="factory-dispatch"
readonly TEMPLATE_TAG="v3"
# Host loopback ports (127.0.0.1 only): the broker VM's published port, and the
# dispatch bridge that runs on the host.
readonly BROKER_PORT="18765"
readonly DISPATCH_PORT="18766"

die() {
  echo "error: $*" >&2
  exit 1
}

# Runtime state: audit logs, transcripts, sessions, token hashes, per-role tool
# grants, and roles' scratch dirs. It lives outside the repo so no
# agent workspace can reach it, even when the factory works on its own repo
# (the broker re-reads grants and token hashes on every call). One per machine,
# like the sandbox names. Keep in sync with default_state() in scripts/analytics.py.
case "${XDG_STATE_HOME:-}" in
  /*) FACTORY_STATE="$XDG_STATE_HOME/ai-code-factory" ;;
  *) FACTORY_STATE="$HOME/.local/state/ai-code-factory" ;;  # XDG: ignore relative values
esac
readonly FACTORY_STATE
readonly FACTORY_BROKER_STATE="$FACTORY_STATE/broker"
readonly FACTORY_DISPATCH_STATE="$FACTORY_STATE/dispatch"
readonly FACTORY_ROLES_SCRATCH="$FACTORY_STATE/roles"

# This machine's own settings, so the repo stays generic (any project, any
# organization): policy.toml, an overlay on broker/policy.toml baked into the
# broker image, and broker-kit/, a mixin adding the broker egress hosts that
# overlay needs. Outside the repo for the same reason as FACTORY_STATE: a role
# working on the factory's own repo could change a git-ignored file there
# without it showing in git diff. See config.example/.
case "${XDG_CONFIG_HOME:-}" in
  /*) FACTORY_CONFIG="$XDG_CONFIG_HOME/ai-code-factory" ;;
  *) FACTORY_CONFIG="$HOME/.config/ai-code-factory" ;;
esac
readonly FACTORY_CONFIG

# migrate_state ROOT -> one-time move of state from its old in-repo location
# (ROOT/.broker, ROOT/.dispatch, ROOT/.roles) to FACTORY_STATE. Stops a bridge
# still running from the old location first, since it writes to the old paths.
# Sandboxes that mount an old path are caught by the launch preflight.
migrate_state() {
  local root="$1" old new pid
  mkdir -p "$FACTORY_STATE"
  chmod 700 "$FACTORY_STATE"
  pid="$(cat "$root/.dispatch/pid" 2>/dev/null || true)"
  if [ -n "$pid" ] && ps -p "$pid" -o command= 2>/dev/null | grep -qF "$root/dispatch/server.py"; then
    echo "Stopping dispatch bridge ($pid) to move its state out of the repo..."
    kill "$pid"
    sleep 1
  fi
  for old in broker dispatch roles; do
    [ -d "$root/.$old" ] || continue
    new="$FACTORY_STATE/$old"
    [ -e "$new" ] && die "state exists both in $root/.$old and $new; keep one (move or delete the other), then re-run"
    echo "Moving $root/.$old to $new..."
    mv "$root/.$old" "$new"
  done
}

sbx_installed_version() {
  sbx version 2>/dev/null | sed -n 's/^sbx version: v\{0,1\}\([^ ]*\).*/\1/p'
}

# sbx_reference_problem FILE -> prints why the saved sbx CLI reference
# (docs/reference/sbx-cli.md, written by scripts/gen-sbx-reference.sh) is not
# usable and returns 0; prints nothing and returns 1 when it is current. The
# file carries a stamp line: <!-- sbx-reference: version=X generated=DATE -->,
# or <!-- sbx-reference: not-generated --> while it is the committed placeholder.
sbx_reference_problem() {
  local file="$1" stamp version
  if [ ! -f "$file" ]; then
    echo "$file is missing; run scripts/gen-sbx-reference.sh on the host"
    return 0
  fi
  stamp="$(grep -m1 '^<!-- sbx-reference: ' "$file" || true)"
  case "$stamp" in
    '<!-- sbx-reference: not-generated'*)
      echo "the sbx CLI reference has not been generated yet; run scripts/gen-sbx-reference.sh on the host (agents otherwise guess sbx flags)"
      return 0 ;;
  esac
  version="$(printf '%s\n' "$stamp" | sed -n 's/^<!-- sbx-reference: version=\([^ ]*\) .*/\1/p')"
  if [ -z "$version" ]; then
    echo "the sbx CLI reference has no version stamp; regenerate it with scripts/gen-sbx-reference.sh"
    return 0
  fi
  if [ "$version" != "$SBX_VERSION" ]; then
    echo "the sbx CLI reference is for sbx $version but the pinned sbx is $SBX_VERSION; regenerate it with scripts/gen-sbx-reference.sh"
    return 0
  fi
  return 1
}

require_bin() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' not found in PATH (see README.md)"
}

require_sha256() {
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
    || die "'sha256sum' or 'shasum' not found in PATH (see README.md)"
}

# sha256_hash [FILE...] -> hex digest, reading stdin if no FILE is given.
# Prefers sha256sum (Linux); falls back to shasum -a 256 (macOS, which does
# not ship sha256sum).
sha256_hash() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$@" | cut -d' ' -f1
  else
    die "'sha256sum' or 'shasum' not found in PATH (see README.md)"
  fi
}

# `sbx version` prints e.g. "sbx version: v0.45.1 <commit>"
require_sbx() {
  require_bin sbx
  local got
  got="$(sbx_installed_version)"
  [ "$got" = "$SBX_VERSION" ] || die "sbx $SBX_VERSION required, found '${got:-unknown}' (see README.md)"
}

# sandbox_running NAME -> success if NAME exists and sbx reports it running
sandbox_running() {
  [ "$(sbx ls 2>/dev/null | awk -v s="$1" '$1 == s { print $3 }')" = running ]
}

# sbx_not_signed_in -> success only when sbx itself says it is not signed in
# to Docker ("Not authenticated", the message the image load gives). Any other
# answer, including an sbx that can't be asked, is "unknown" and fails, so the
# caller carries on and the later failure still gets handled.
sbx_not_signed_in() {
  local out
  out="$(sbx template ls 2>&1 </dev/null || true)"
  printf '%s\n' "$out" | grep -qi "not authenticated"
}

# broker_plugins -> comma-separated module names of the broker plugins in
# the local config folder (FACTORY_CONFIG/plugins/<module>/); empty when
# there are none. `factory _build-templates` bakes the directory into the broker
# image; factory/fingerprints.py has the Python copy, which the broker start
# passes as PASSDESK_PLUGINS and records in the broker's fingerprint. This one
# is for that fingerprint in sandbox_fingerprint (roles.sh), which `factory
# update` (stale_sandboxes) still computes in bash.
broker_plugins() {
  local d names="" LC_ALL=C # byte order, as factory/fingerprints.py sorts; glibc's en_US.UTF-8 puts Zed after alpha
  for d in "$FACTORY_CONFIG"/plugins/*/; do
    [ -d "$d" ] || continue
    d="${d%/}"; d="${d##*/}"
    case "$d" in __pycache__|.*) continue ;; esac
    names="$names${names:+,}$d"
  done
  printf '%s' "$names"
}

# resolve_python -> path to a Python 3.9+ interpreter (printed to stdout).
# On macOS this is always the system Python (Xcode Command Line Tools), the
# pinned runtime for the dispatch bridge and other host-side Python tools.
resolve_python() {
  local python hint
  if [ "$(uname -s)" = "Darwin" ]; then
    python=/usr/bin/python3
    hint="install the Xcode Command Line Tools: xcode-select --install"
  else
    python="$(command -v python3 || true)"
    hint="install python3 3.9+ (e.g. \`sudo apt install python3\`)"
  fi
  [ -n "$python" ] || die "python3 not found in PATH ($hint)"
  "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null \
    || die "$python 3.9+ is required ($hint)"
  printf '%s' "$python"
}

# --- git on the factory's own checkout ----------------------------------------------
# Config forced on every git command the factory runs against its own
# checkout: no fsmonitor command, no hooks. (The broker image build is Python,
# factory/build_templates.py, which exports the committed broker/ the same way.)
BROKER_GIT_SAFE=(-c core.fsmonitor=false -c core.hooksPath=/dev/null)
