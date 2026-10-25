# Shared helpers for factory scripts. Source it; do not execute it.
# shellcheck shell=bash
# The constants below are used by the scripts that source this file.
# shellcheck disable=SC2034

# Exact tool versions the factory is tested against. Bump these deliberately,
# in the same commit as any spec/script changes the new version requires.
readonly SBX_VERSION="0.45.1"

# Release artifacts for scripts/install-prereqs.sh: macOS (Apple silicon) and
# Linux (x86_64, including WSL2). SHA-256 values come from the GitHub release
# digests.
readonly SBX_URL="https://github.com/docker/sbx-releases/releases/download/v${SBX_VERSION}/DockerSandboxes-darwin.tar.gz"
readonly SBX_SHA256="0a7207d1736ef9d109b722cf3d21e2b9ed5840d7667fcbcb7596075265b22023"

# Linux x86_64 (Ubuntu, including WSL2): sbx ships as a .deb per Ubuntu
# release. The .deb (not the tarball) is what installs the AppArmor profile
# the sandbox shim needs on Ubuntu 23.10+, at the /usr/libexec path that
# profile names. Hashes are the GitHub release asset digests, checked against
# downloads on 2026-10-14.
readonly SBX_DEB_UBUNTU2404_URL="https://github.com/docker/sbx-releases/releases/download/v${SBX_VERSION}/DockerSandboxes-linux-amd64-ubuntu2404.deb"
readonly SBX_DEB_UBUNTU2404_SHA256="aaceda8ade7bdbb285a8e5b27e29d5af1aeef55c57bf4db892966ea47ecc11c1"
readonly SBX_DEB_UBUNTU2604_URL="https://github.com/docker/sbx-releases/releases/download/v${SBX_VERSION}/DockerSandboxes-linux-amd64-ubuntu2604.deb"
readonly SBX_DEB_UBUNTU2604_SHA256="db12d894a9d92da7c33afb5a4654bb341a92a90189b2095490ed3128d58f9c3a"

# Pinned ShellCheck for scripts/lint.sh (CI and local runs). Hashes checked against
# the v0.10.0 release downloads on 2026-10-17.
readonly SHELLCHECK_VERSION="0.10.0"
readonly SHELLCHECK_LINUX_X86_64_SHA256="6c881ab0698e4e6ea235245f22832860544f17ba386442fe7e9d629f8cbedf87"
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

# run_or_die MESSAGE COMMAND... -> run COMMAND with its output held back. If it
# fails, die with MESSAGE and the last 20 lines of what it printed (stdout and
# stderr), so the reason isn't lost; if it succeeds, print nothing.
run_or_die() {
  local msg="$1" log status=0
  shift
  log="$(mktemp)"
  "$@" >"$log" 2>&1 || status=$?
  if [ "$status" -ne 0 ]; then
    echo "error: $msg (exit status $status)" >&2
    if [ -s "$log" ]; then
      echo "last output of '$*' (up to 20 lines):" >&2
      tail -n 20 "$log" | sed 's/^/  /' >&2
    fi
    rm -f "$log"
    exit 1
  fi
  rm -f "$log"
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

# Older versions remembered a `factory start --layout` choice here ("watch",
# "tabs" or "web"). The dashboard ("web") is the only layout left;
# resolve_layout still reads the file so a stale value can't break a start.
readonly FACTORY_LAYOUT_FILE="$FACTORY_CONFIG/layout"

# resolve_layout FLAG ENV -> "web", the only layout. A FLAG (factory start's
# --layout value), ENV (FACTORY_LAYOUT) or saved FACTORY_LAYOUT_FILE naming a
# layout that no longer exists ("watch", "tabs") gets a one-line warning on
# stderr and "web"; the stale file is removed so the warning shows once. Any
# other FLAG or ENV value dies with the command's usage; any other saved value
# is ignored. Nothing is saved.
resolve_layout() {
  local chosen="${1:-$2}" saved
  case "$chosen" in
    ""|web) ;;
    watch|tabs) echo "warning: the '$chosen' layout is gone; using the dashboard (web)" >&2 ;;
    *) die "usage: factory start [dir] [--no-ui] [--layout web]" ;;
  esac
  saved="$(cat "$FACTORY_LAYOUT_FILE" 2>/dev/null || true)"
  case "$saved" in
    watch|tabs)
      echo "warning: your saved '$saved' layout is gone; using the dashboard (web)" >&2
      rm -f "$FACTORY_LAYOUT_FILE" 2>/dev/null || true ;;
  esac
  printf '%s' "web"
}

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

# project_repos PROJECT -> the git repositories roles with WORKSPACE="rw" work
# on, as paths relative to PROJECT, one per line: "." if PROJECT/.git exists
# (a directory, or a gitfile), else every immediate subdirectory that isn't
# hidden or a symlink, has no newline in its name, and has a .git (directory
# or gitfile) of its own, in byte order. Empty when PROJECT is neither a repo
# nor a folder of repos. Keep identical to project_repos() in factory/repos.py
# and templates/agent/factory-lane; scripts/test_project_repos.sh runs all three
# on one fixture and fails if they differ.
project_repos() {
  local project="$1" d name
  if [ -d "$project/.git" ] || [ -f "$project/.git" ]; then
    printf '.\n'
    return 0
  fi
  for d in "$project"/*; do
    name="${d##*/}"
    case "$name" in .*|*$'\n'*) continue ;; esac
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    [ -d "$d/.git" ] || [ -f "$d/.git" ] || continue
    printf '%s\n' "$name"
  done | LC_ALL=C sort
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

# template_claude_md WORKSPACE -> where the sbx base template writes its own
# CLAUDE.md for a sandbox whose primary workspace is WORKSPACE: the workspace's
# parent (sbx mounts it at its host path, so this is the path inside too).
template_claude_md() { printf '%s/CLAUDE.md\n' "$(dirname "$1")"; }

# with_claude_md_exclude PATH: managed-settings JSON on stdin -> the same with
# PATH in claudeMdExcludes (added once, other keys and entries kept), compact.
with_claude_md_exclude() {
  jq -c --arg p "$1" '.claudeMdExcludes = (((.claudeMdExcludes // []) - [$p]) + [$p])'
}

# exclude_template_claude_md SANDBOX WORKSPACE: keep Claude Code in SANDBOX
# from loading the base template's CLAUDE.md (shell persistence, network
# troubleshooting, port publishing: ~2,750 words that don't apply to a factory
# agent). Only the Coordinator needs it: its cwd is the project, under that
# file's folder; roles' cwds (lanes, review checkouts, the :ro project) aren't.
# Adds the path to the root-owned managed settings the kit wrote, and leaves
# them 0444. Idempotent, so it runs on every launch and fixes an existing
# sandbox without recreating it. The path only crosses as a jq argument and
# the JSON over stdin, never inside a command line.
exclude_template_claude_md() {
  local name="$1" path current merged
  path="$(template_claude_md "$2")"
  current="$(sbx exec -u 0 "$name" cat /etc/claude-code/managed-settings.json)" || return 1
  merged="$(printf '%s' "$current" | with_claude_md_exclude "$path")" || return 1
  [ "$merged" != "$(printf '%s' "$current" | jq -c .)" ] || return 0
  printf '%s\n' "$merged" | sbx exec -u 0 -i "$name" sh -c \
    'f=/etc/claude-code/managed-settings.json; cat > "$f.tmp" && chmod 0444 "$f.tmp" && mv -f "$f.tmp" "$f"'
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

sandbox_exists() {
  sbx ls -q 2>/dev/null | grep -qx "$1"
}

# coordinator_attached -> success if some process on this machine is already
# running `sbx run` on $COORDINATOR_SANDBOX (`factory chat` or the
# dashboard's Terminal 0). A second one would start a second Coordinator
# in the same VM. It matches `sbx [global flags] run ...` with the name as
# `--name NAME`, `--name=NAME` or a bare argument before any `--` (`sbx run`
# takes a sandbox name positionally too; there is no short form of --name),
# erring towards a match. Same rule as runs_sandbox in
# scripts/dashboard_term.py.
coordinator_attached() {
  ps -axo args= 2>/dev/null | awk -v n="$COORDINATOR_SANDBOX" '
    {
      b = $1; sub(/.*\//, "", b)
      if (b != "sbx") next
      i = 2
      while (i <= NF && substr($i, 1, 1) == "-") i++
      if ($i != "run") next
      for (j = i + 1; j <= NF && $j != "--"; j++)
        if ($j == n || $j == "--name=" n) found = 1
    }
    END { exit !found }'
}

# sandbox_running NAME -> success if NAME exists and sbx reports it running
sandbox_running() {
  [ "$(sbx ls 2>/dev/null | awk -v s="$1" '$1 == s { print $3 }')" = running ]
}

# local_settings_present -> success if the config folder holds any local
# settings: policy.toml or enabled-roles, or a broker-kit/, roles/, projects/ or
# plugins/ folder.
local_settings_present() {
  local d
  [ -f "$FACTORY_CONFIG/policy.toml" ] && return 0
  [ -f "$FACTORY_CONFIG/enabled-roles" ] && return 0
  for d in broker-kit roles projects plugins; do
    [ -d "$FACTORY_CONFIG/$d" ] && return 0
  done
  return 1
}

# policy_value SECTION KEY -> the raw value of KEY = ... in [SECTION], from this
# machine's overlay ($FACTORY_CONFIG/policy.toml) if it sets KEY, else from
# broker/policy.toml - the same precedence as the broker's load_policy().
# Best-effort awk, not a TOML parser: good enough for the values these checks
# read. An array spread over several lines comes back joined on one line (up
# to its closing "]", comments dropped), so a check never mistakes
# `tools = [` for an empty list.
policy_value() {
  local f v
  for f in "$FACTORY_CONFIG/policy.toml" "$ROOT/broker/policy.toml"; do
    [ -f "$f" ] || continue
    if v="$(awk -v s="[$1]" -v k="$2" '
        more { sub(/#.*/, ""); v = v " " $0; if (index($0, "]")) { print v; found = 1; exit } next }
        index($0, s) == 1 { f = 1; next }
        /^\[/ { f = 0 }
        f && $0 ~ "^[[:space:]]*"k"[[:space:]]*=" {
          sub(/^[^=]*=[[:space:]]*/, "")
          if (substr($0, 1, 1) == "[" && !index($0, "]")) { sub(/#.*/, ""); v = $0; more = 1; next }
          print; found = 1; exit
        }
        END { exit !found }' "$f")"; then
      printf '%s\n' "$v"
      return 0
    fi
  done
}

# policy_string SECTION KEY -> the string value of KEY = "..." in [SECTION]
policy_string() {
  policy_value "$1" "$2" | awk -F'"' '{ print $2 }'
}

# policy_toml_string SECTION KEY -> the text of a one-line TOML string value
# of KEY in [SECTION] (policy_value's precedence): "basic" without escapes or
# 'literal'. Fails when KEY is unset; prints "?" and fails when it is set to
# anything else (an escape, a number, an unclosed quote).
policy_toml_string() {
  local raw re_basic='^"([^"\\]*)"[[:space:]]*(#.*)?$' re_literal="^'([^']*)'[[:space:]]*(#.*)?\$"
  raw="$(policy_value "$1" "$2")"
  [ -n "$raw" ] || return 1
  if [[ "$raw" =~ $re_basic ]] || [[ "$raw" =~ $re_literal ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    echo "?"
    return 1
  fi
}

# lane_identity_problem NAME EMAIL -> why NAME <EMAIL> can't be a lane's git
# identity, or nothing (and failure) when it can. Empty means the default
# (factory-<lane>, <lane>@factory.invalid); "{lane}" stands for the lane name.
# The same rules as check_identity in templates/agent/factory-lane and
# lane_identity_problem in dispatch/server.py.
lane_identity_problem() {
  local LC_ALL=C name="${1//\{lane\}/developer}" email="${2//\{lane\}/developer}" bad='[[:cntrl:]<>]'
  local email_re='^[^-@[:space:][:cntrl:]<>][^@[:space:][:cntrl:]<>]*@[^@[:space:][:cntrl:]<>]+$'
  if [ -n "$1" ] && ! { [ -n "${name//[[:space:]]/}" ] && [ "${#name}" -le 128 ] \
      && ! [[ "$name" =~ $bad ]] && [ "${name#-}" = "$name" ]; }; then
    echo "author_name must be 1-128 characters, not only spaces, with no control character, \"<\" or \">\", and not start with \"-\""
  elif [ -n "$2" ] && ! { [ "${#email}" -le 254 ] && [[ "$email" =~ $email_re ]]; }; then
    echo "author_email must look like name@domain: one \"@\", no spaces, control characters, \"<\" or \">\", not starting with \"-\", at most 254 characters"
  else
    return 1
  fi
}

# write_lane_identity FILE -> the lanes' git identity ([git] author_name and
# author_email in policy.toml) as AUTHOR_NAME="..." / AUTHOR_EMAIL="..." lines
# in FILE, for the dispatch bridge to pass to `factory-lane prepare`. Neither
# set: no FILE (factory-lane's default). Refuses (dies, leaving no FILE) on an
# invalid value.
write_lane_identity() {
  local name="" email="" why
  rm -f "$1"
  name="$(policy_toml_string git author_name)" || [ "$name" != "?" ] \
    || die "[git] author_name in policy.toml must be a one-line string (\"...\" without escapes, or '...')"
  email="$(policy_toml_string git author_email)" || [ "$email" != "?" ] \
    || die "[git] author_email in policy.toml must be a one-line string (\"...\" without escapes, or '...')"
  if why="$(lane_identity_problem "$name" "$email")"; then
    die "[git] in policy.toml: $why"
  fi
  if [ -n "$name$email" ]; then
    printf 'AUTHOR_NAME="%s"\nAUTHOR_EMAIL="%s"\n' "$name" "$email" > "$1"
  fi
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

# claude_login_stored -> success if sbx holds the global Claude login (the first
# /login in a factory sandbox, or an API key from `sbx secret set anthropic`).
claude_login_stored() {
  sbx secret ls 2>/dev/null | grep -q " anthropic "
}

# broker_plugins -> comma-separated module names of the broker plugins in
# the local config folder (FACTORY_CONFIG/plugins/<module>/); empty when
# there are none. 00-build-templates.sh bakes the
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

# --- the broker image's build context -----------------------------------------------
# broker/ is a plain directory of this repository. An agent working on the
# factory's own repo can write into the working tree like any other file, so
# the broker image is never built from it: it is built from "git archive" of
# the committed tree (HEAD), so nothing left in the working tree -- edited,
# untracked or git-ignored (e.g. a planted __pycache__/*.pyc) -- can reach
# the image. Only changes committed to ROOT's HEAD ship.
#
# The repository's own .git (its config and hooks) is trusted here; see the
# passdesk-broker decision log (2026-10-16) for that open risk.

# Config forced on every git command the factory runs against its own
# checkout: no fsmonitor command, no hooks.
BROKER_GIT_SAFE=(-c core.fsmonitor=false -c core.hooksPath=/dev/null)

# broker_export ROOT DEST -> write the committed tree of ROOT's broker/ (at
# ROOT's HEAD) into DEST, so DEST/Dockerfile is the broker's Dockerfile.
broker_export() {
  local root="$1" dest="$2" err
  if ! err="$(git "${BROKER_GIT_SAFE[@]}" -C "$root" cat-file -e 'HEAD:broker/tools.txt' 2>&1)"; then
    die "refusing to export the broker: broker/tools.txt is not in the committed tree of $root (${err:-HEAD has no broker/ directory})"
  fi
  git "${BROKER_GIT_SAFE[@]}" -C "$root" archive --format=tar HEAD broker/ | tar -x -f - -C "$dest" --strip-components=1 \
    || die "exporting the committed broker/ from $root failed"
}

# prepare_broker_context ROOT DEST -> export the committed broker/ into DEST
# (broker_export). 00-build-templates.sh builds the broker image from DEST.
# Uncommitted work in broker/ is not refused, but is noted: it is not in the
# image.
prepare_broker_context() {
  local dirty
  broker_export "$1" "$2"
  [ -f "$2/Dockerfile" ] || die "the committed broker/ has no Dockerfile"
  dirty="$(git "${BROKER_GIT_SAFE[@]}" -C "$1" status --porcelain --untracked-files=all -- broker 2>/dev/null || true)"
  if [ -n "$dirty" ]; then
    echo "note: building the broker image from the committed broker/ (HEAD); uncommitted changes in $1/broker are not included" >&2
  fi
}
