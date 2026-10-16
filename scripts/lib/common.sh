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

# broker_plugins -> comma-separated module names of the broker plugins in
# the local config folder (FACTORY_CONFIG/plugins/<module>/), e.g.
# "passdesk_pro"; empty when there are none. 00-build-templates.sh bakes the
# directory into the broker image and start-broker.sh passes this list as
# PASSDESK_PLUGINS when it creates the broker sandbox.
broker_plugins() {
  local d names=""
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

# --- broker/ submodule safety -------------------------------------------------------
# broker/ (the passdesk-broker submodule) is where the Broker's code lives.
# An agent working on the factory's own repo can write into it like any other
# file -- including broker/.git, the gitfile that tells git where broker/'s
# repository is. So nothing here ever lets git discover that repository from
# broker/ itself (no "git -C broker"): a planted broker/.git pointing at a
# fake repository could make any check pass, and a fake repository's config
# (core.fsmonitor, hooks) would run commands on the host. Instead:
#
# - broker/'s git directory is resolved from the superproject
#   (broker_gitdir), and every git command against it goes through
#   broker_git, with an explicit --git-dir/--work-tree and core.fsmonitor and
#   hooks forced off;
# - broker/.git must still be exactly the gitfile git writes
#   (broker_check_gitfile), because "git submodule update" and plain git use
#   by a person in broker/ do follow it;
# - the broker image is built from a "git archive" export of the pinned
#   commit (broker_export), never from the broker/ working tree, so nothing
#   left in the working tree -- edited, untracked or git-ignored (e.g. a
#   planted __pycache__/*.pyc) -- can reach the image.
#
# The main repository's own .git (and so .git/modules/broker, its config and
# hooks) is trusted here; see the passdesk-broker decision log (2026-10-16)
# for that open risk.

# Config forced on every git command that touches broker/ or that a pull
# into ROOT may run inside broker/ (git passes -c settings on to the git
# processes it runs for submodules): no fsmonitor command, no hooks.
BROKER_GIT_SAFE=(-c core.fsmonitor=false -c core.hooksPath=/dev/null)

# broker_gitdir_rel ROOT -> broker/'s git directory as ROOT's own repository
# names it (normally ".git/modules/broker", relative to ROOT), whether or not
# it exists.
broker_gitdir_rel() {
  git "${BROKER_GIT_SAFE[@]}" -C "$1" rev-parse --git-path modules/broker
}

# broker_initialized ROOT -> success if broker/ looks initialized at all
# (it has a broker/.git entry, or ROOT has a broker module directory). A
# plain "git clone" without --recurse-submodules leaves neither.
broker_initialized() {
  local rel
  if [ -e "$1/broker/.git" ] || [ -L "$1/broker/.git" ]; then return 0; fi
  rel="$(broker_gitdir_rel "$1")" || return 1
  case "$rel" in /*) ;; *) rel="$1/$rel" ;; esac
  [ -e "$rel" ]
}

# broker_gitdir ROOT -> absolute path of broker/'s git directory, resolved
# from ROOT's repository, never from broker/.git. Fails (message on stderr)
# if the submodule isn't initialized.
broker_gitdir() {
  local rel
  rel="$(broker_gitdir_rel "$1")" || return 1
  case "$rel" in /*) ;; *) rel="$1/$rel" ;; esac
  [ -d "$rel" ] && [ -f "$rel/HEAD" ] || {
    echo "$1/broker is not initialized (no $rel); run 'git -C $1 submodule update --init'" >&2
    return 1
  }
  (cd "$rel" && pwd -P)
}

# broker_check_gitfile ROOT -> fail (message on stderr) unless ROOT/broker/.git
# is a regular file (not a symlink, not a directory) holding exactly the
# gitfile line git writes for this submodule, e.g. "gitdir: ../.git/modules/broker".
broker_check_gitfile() {
  local f="$1/broker/.git" nl=$'\n' rel want got
  rel="$(broker_gitdir_rel "$1")" || return 1
  case "$rel" in
    /*) echo "$1's broker module directory ($rel) is not inside the checkout; unsupported layout (e.g. a linked worktree), refusing" >&2
      return 1 ;;
  esac
  want="gitdir: ../$rel"
  if [ -L "$f" ] || [ ! -f "$f" ]; then
    echo "$f is not a regular gitfile; refusing to use broker/ (expected a file containing '$want')" >&2
    return 1
  fi
  # Compare the whole file, trailing newline included ("$(cat)" would strip
  # any number of them).
  got="$(cat -- "$f"; printf x)"
  got="${got%x}"
  [ "$got" = "$want$nl" ] || {
    echo "$f does not point at $rel (it says '${got%"$nl"}'); refusing to use broker/ - someone redirected it" >&2
    return 1
  }
}

# broker_git ROOT ARGS... -> run "git ARGS..." against broker/'s real
# repository (see above), after checking broker/.git.
broker_git() {
  local root="$1" gitdir
  shift
  gitdir="$(broker_gitdir "$root")" || return 1
  broker_check_gitfile "$root" || return 1
  git "${BROKER_GIT_SAFE[@]}" --git-dir="$gitdir" --work-tree="$root/broker" "$@"
}

# broker_dirty ROOT -> "git status --porcelain --ignored" output for ROOT/broker:
# uncommitted changes, untracked files, and (as "!! " lines) git-ignored
# files. Empty if clean. Fails if broker/ isn't initialized or broker/.git
# is wrong. --ignore-submodules=all: broker/ has no submodules of its own,
# and status must not go running git inside nested repositories.
broker_dirty() {
  broker_git "$1" status --porcelain --untracked-files=all --ignored=matching --ignore-submodules=all
}

# broker_blocking_changes DIRTY -> DIRTY (broker_dirty's output) without its
# git-ignored ("!! ") lines. Ignored files (e.g. a __pycache__/ left by
# running the broker's tests) don't block anything: the image is built from
# broker_export, which never includes the working tree, and a pull or
# submodule update leaves them alone.
broker_blocking_changes() {
  printf '%s\n' "$1" | grep -v '^!! ' | sed '/^$/d' || true
}

# broker_pin ROOT -> the commit ROOT's superproject HEAD pins broker/ to.
broker_pin() {
  git "${BROKER_GIT_SAFE[@]}" -C "$1" rev-parse HEAD:broker
}

# broker_head ROOT -> broker/'s actually checked-out commit.
broker_head() {
  broker_git "$1" rev-parse HEAD
}

# require_broker_at_pin ROOT -> die unless ROOT/broker is initialized, its
# gitfile points where it should, it has no uncommitted or untracked changes,
# and it is checked out exactly at the superproject's pin. The image itself
# comes from broker_export, so this is about not building while someone has
# unreviewed work in broker/, not about what gets copied.
require_broker_at_pin() {
  local root="$1" dirty pin head
  dirty="$(broker_dirty "$root" 2>&1)" || die "refusing to build the broker image: $dirty"
  dirty="$(broker_blocking_changes "$dirty")"
  [ -z "$dirty" ] || die "$root/broker has uncommitted changes (including untracked files); refusing to build from it:
$dirty"
  pin="$(broker_pin "$root")" || die "cannot read the broker pin from $root's HEAD"
  head="$(broker_head "$root" 2>&1)" || die "refusing to build the broker image: $head"
  [ "$head" = "$pin" ] \
    || die "$root/broker is checked out at $head, not the pinned commit $pin; run 'git -C $root submodule update --init' (or investigate why it moved) before building"
}

# broker_export ROOT DEST -> write the tree of the pinned broker commit into
# the empty directory DEST, straight from broker/'s repository with
# "git archive" (no working tree, no checkout, no filters). This is the
# broker image's build context. Dies if broker/ isn't initialized, its
# gitfile is wrong, or the pinned commit isn't in its repository.
broker_export() {
  local root="$1" dest="$2" pin err
  pin="$(broker_pin "$root")" || die "cannot read the broker pin from $root's HEAD"
  if ! err="$(broker_git "$root" cat-file -e "$pin^{commit}" 2>&1)"; then
    [ -n "$err" ] || err="pinned commit $pin is not in the broker repository; run 'git -C $root submodule update --init'"
    die "refusing to export the broker: $err"
  fi
  broker_git "$root" archive --format=tar "$pin" | tar -x -f - -C "$dest" \
    || die "exporting broker commit $pin from $root failed"
}

# prepare_broker_context ROOT DEST -> refuse unless broker/ is clean and at
# its pin (require_broker_at_pin), then export the pin into DEST
# (broker_export). 00-build-templates.sh builds the broker image from DEST.
prepare_broker_context() {
  require_broker_at_pin "$1"
  broker_export "$1" "$2"
  [ -f "$2/Dockerfile" ] || die "the pinned broker commit has no Dockerfile"
}
