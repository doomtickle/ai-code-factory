# Shared helpers for factory scripts. Source it; do not execute it.
# shellcheck shell=bash

# Exact tool versions the factory is tested against. Bump these deliberately,
# in the same commit as any spec/script changes the new version requires.
readonly SBX_VERSION="0.45.1"
readonly HERDR_VERSION="0.9.1"

# Release artifacts for scripts/install-prereqs.sh: macOS (Apple silicon) and
# Linux (x86_64, including WSL2). SHA-256 values come from the GitHub release
# digests / herdr.dev manifest.
readonly SBX_URL="https://github.com/docker/sbx-releases/releases/download/v${SBX_VERSION}/DockerSandboxes-darwin.tar.gz"
readonly SBX_SHA256="0a7207d1736ef9d109b722cf3d21e2b9ed5840d7667fcbcb7596075265b22023"
readonly HERDR_URL="https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-macos-aarch64"
readonly HERDR_SHA256="5fc7a7e7adfaca56fa80aa89dcb025693357268dab8285b9ce2d08a2313c89de"

# Linux x86_64 (Ubuntu, including WSL2): sbx ships as a .deb per Ubuntu
# release. The .deb (not the tarball) is what installs the AppArmor profile
# the sandbox shim needs on Ubuntu 23.10+, at the /usr/libexec path that
# profile names. Hashes are the GitHub release asset digests, checked against
# downloads on 2026-10-14.
readonly SBX_DEB_UBUNTU2404_URL="https://github.com/docker/sbx-releases/releases/download/v${SBX_VERSION}/DockerSandboxes-linux-amd64-ubuntu2404.deb"
readonly SBX_DEB_UBUNTU2404_SHA256="aaceda8ade7bdbb285a8e5b27e29d5af1aeef55c57bf4db892966ea47ecc11c1"
readonly SBX_DEB_UBUNTU2604_URL="https://github.com/docker/sbx-releases/releases/download/v${SBX_VERSION}/DockerSandboxes-linux-amd64-ubuntu2604.deb"
readonly SBX_DEB_UBUNTU2604_SHA256="db12d894a9d92da7c33afb5a4654bb341a92a90189b2095490ed3128d58f9c3a"
readonly HERDR_LINUX_X86_64_URL="https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-x86_64"
readonly HERDR_LINUX_X86_64_SHA256="2a02fed16beb651ef006e1d43f048f652ca4dc58ad053cd2d44450563d5c54b7"

# Factory sandbox, template and MCP names. Role sandboxes are factory-<role>.
readonly BROKER_SANDBOX="factory-broker"
readonly COORDINATOR_SANDBOX="factory-coordinator"
readonly DISPATCH_MCP="factory-dispatch"
readonly TEMPLATE_TAG="v2"
# Host loopback ports (127.0.0.1 only): the broker VM's published port, and the
# dispatch bridge that runs on the host.
readonly BROKER_PORT="18765"
readonly DISPATCH_PORT="18766"

die() {
  echo "error: $*" >&2
  exit 1
}

# Runtime state: audit logs, transcripts, sessions, token hashes, per-role tool
# grants, and read-only roles' scratch dirs. It lives outside the repo so no
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

# require_state_outside PROJECT -> die if the state dir is inside PROJECT
# (e.g. XDG_STATE_HOME pointed into it), where agents could edit it.
require_state_outside() {
  case "$FACTORY_STATE/" in
    "$1"/*) die "factory state ($FACTORY_STATE) is inside the project $1, where agents could edit it; set XDG_STATE_HOME elsewhere" ;;
  esac
}

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

herdr_installed_version() {
  herdr --version 2>/dev/null | sed -n 's/^herdr \([^ ]*\).*/\1/p'
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

# `herdr --version` prints e.g. "herdr 0.9.1"
require_herdr() {
  require_bin herdr
  local got
  got="$(herdr_installed_version)"
  [ "$got" = "$HERDR_VERSION" ] || die "herdr $HERDR_VERSION required, found '${got:-unknown}' (see README.md)"
}

sandbox_exists() {
  sbx ls -q 2>/dev/null | grep -qx "$1"
}

# claude_login_stored -> success if sbx holds the global Claude login (the first
# /login in a factory sandbox, or an API key from `sbx secret set anthropic`).
claude_login_stored() {
  sbx secret ls 2>/dev/null | grep -q " anthropic "
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
