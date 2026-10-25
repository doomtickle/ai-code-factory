#!/usr/bin/env bash
set -euo pipefail

# Usage: 02-launch-factory.sh <project-dir>
#
# Starts the factory for <project-dir>:
#   - the broker VM (network tools for roles) and the dispatch bridge (host)
#   - one sandbox per role in roles/, plus the Coordinator sandbox
#
# The Coordinator's terminal is not opened here: bin/factory ends a normal
# start by running `factory dashboard`, which holds it (or `factory chat`).
# FACTORY_NO_UI=1 (factory start --no-ui) only changes the closing message.
#
# Safe to re-run: existing sandboxes are reused and missing ones are created
# (e.g. after adding a role with `factory role new`).

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

[ $# -eq 1 ] || die "usage: $0 <project-dir>"
[ -d "$1" ] || die "project directory not found: $1"

require_bin jq

require_sbx

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# shellcheck source=lib/roles.sh
. "$ROOT/scripts/lib/roles.sh"
PROJECT="$(cd "$1" && pwd -P)"

roles="$(list_roles)"
[ -n "$roles" ] || die "no roles defined in roles/"
validate_role_overrides
for role in $roles; do validate_role "$role"; done
rw_roles=""
for role in $roles; do
  [ "$(role_get "$role" WORKSPACE)" != rw ] || rw_roles="$rw_roles $role"
done
rw_roles="${rw_roles# }"

# --- Preflight ------------------------------------------------------------------
# Layout before roles/: one broker registration shared by all agents.
if sbx mcp ls -q 2>/dev/null | grep -qx "$BROKER_SANDBOX"; then
  die "found the pre-roles broker setup. Migrate once with:
  sbx rm --force factory-developer factory-reviewer
  sbx mcp rm --force $BROKER_SANDBOX
  sbx secret rm -f mcp:$BROKER_SANDBOX:token; sbx secret rm -f mcp:$BROKER_SANDBOX:token:endpoint
  rm -f '$FACTORY_BROKER_STATE/token.sha256'
then re-run this script."
fi

# rw roles work in git clones of the project's repositories (lanes), so the
# project must be a git repository or a folder of them.
if [ -n "$rw_roles" ] && [ -z "$(project_repos "$PROJECT")" ]; then
  die "$PROJECT is not a git repository or a folder of git repositories; the $rw_roles role(s) work in git clones of it (each commits to factory/* branches you merge). Run 'git init' and commit, or start the factory on the repo itself."
fi

# Existing sandboxes are bound to the project they were created for: the
# project is the Coordinator's workspace, mounted read-only into role
# sandboxes (rw roles also mount its .factory/outgoing).
sandboxes_json="$(sbx ls --json)"
for sb in "$COORDINATOR_SANDBOX" $(for r in $roles; do role_sandbox "$r"; echo; done); do
  ws_list="$(printf '%s' "$sandboxes_json" | jq -r --arg n "$sb" '.sandboxes[] | select(.name == $n) | .workspaces[]')"
  if [ -n "$ws_list" ] && ! printf '%s\n' "$ws_list" \
      | grep -qxF -e "$PROJECT" -e "$PROJECT:ro" -e "$PROJECT/.factory/outgoing"; then
    die "$sb was created for a different project. To switch projects, see README (Switching projects)."
  fi
done

# Shared folders are fixed when a sandbox is created: the broker and read-only
# roles must mount their state dir where it is now (outside the repo), not an
# old location (e.g. .broker/ or .roles/ inside the repo, before state moved).
require_state_outside "$PROJECT"
# rw roles from before lanes mounted the project itself read-write, .git
# included (no scratch dir); a role switched from ro to rw lacks the outgoing
# mount. Their fingerprint (RW_LAYOUT and workspace in lib/roles.sh) marks
# them out of date, so `factory update` recreates them; refuse to run one
# until then.
old_layout=""
for r in $rw_roles; do
  sb="$(role_sandbox "$r")"
  ws_list="$(printf '%s' "$sandboxes_json" | jq -r --arg n "$sb" '.sandboxes[] | select(.name == $n) | .workspaces[]')"
  [ -n "$ws_list" ] || continue
  if ! printf '%s\n' "$ws_list" | grep -qxF "$FACTORY_ROLES_SCRATCH/$r" \
      || ! printf '%s\n' "$ws_list" | grep -qxF "$PROJECT/.factory/outgoing"; then
    old_layout="$old_layout $sb"
  fi
done
[ -z "$old_layout" ] || die "these sandboxes were created with an older layout (the project mounted read-write, or no .factory/outgoing) and must be recreated:$old_layout
Run 'factory update': it recreates them (your Claude login, the logs and your project are kept; those roles start a fresh conversation)."
stale=""
for sb in "$BROKER_SANDBOX" $(for r in $roles; do [ "$(role_get "$r" WORKSPACE)" = ro ] && role_sandbox "$r"; echo; done); do
  if [ "$sb" = "$BROKER_SANDBOX" ]; then want="$FACTORY_BROKER_STATE"; else want="$FACTORY_ROLES_SCRATCH/${sb#factory-}"; fi
  ws_list="$(printf '%s' "$sandboxes_json" | jq -r --arg n "$sb" '.sandboxes[] | select(.name == $n) | .workspaces[]')"
  if [ -n "$ws_list" ] && ! printf '%s\n' "$ws_list" | grep -qxF "$want"; then
    stale="$stale $sb"
  fi
done
[ -z "$stale" ] || die "these sandboxes mount an old state location and must be recreated (your Claude login and the logs are kept; those roles start a fresh conversation):
  sbx rm --force$stale
then re-run."

# PROJECT/.factory/outgoing is the one project path rw roles can write: they
# put git bundles there for the broker's git_push. Create it before the
# sandboxes mount it, git-ignored (the broker writes the same .gitignore).
# Refuse symlinks: mkdir -p and the redirect below would follow them out of
# the project.
if [ -n "$rw_roles" ]; then
  for p in .factory .factory/outgoing .factory/.gitignore; do
    [ ! -L "$PROJECT/$p" ] || die "$PROJECT/$p is a symlink; refusing to use it (remove it, then re-run)"
    case "$p" in
      .factory/.gitignore) [ -e "$PROJECT/$p" ] || printf '*\n' > "$PROJECT/$p" ;;
      *) mkdir -p "$PROJECT/$p" ;;
    esac
  done
fi

"$ROOT/scripts/start-broker.sh" "$PROJECT"
"$ROOT/scripts/start-dispatch.sh" "$PROJECT"

# --- Sandboxes --------------------------------------------------------------------
# create_sandbox NAME TEMPLATE KIT-DIR "MCP-LIST" WORKSPACE-MODE
# MCP-LIST is space-separated (empty for none); one --static-mcp per entry.
# sbx requires a writable primary workspace. Modes:
#   project (the Coordinator only): the project is the primary workspace; its
#     kit's managed settings keep the agent from changing it.
#   rw: a scratch dir is primary, the project is mounted :ro, and only
#     PROJECT/.factory/outgoing (nested on top) is writable. The role works in
#     git clones on the sandbox's own disk (lanes, templates/agent/factory-lane);
#     the dispatch bridge imports their factory/* branches as git bundles.
#     Keep in sync with RW_LAYOUT in lib/roles.sh.
#   ro: a scratch dir is primary and the project is mounted :ro.
create_sandbox() {
  local name="$1" template="$2" kit="$3" mcp_list="$4" mode="$5" scratch mcp
  sandbox_exists "$name" && return 0
  echo "Creating sandbox $name..."
  set -- --name "$name" --pull never -t "factory-$template:$TEMPLATE_TAG" --kit "$kit"
  for mcp in $mcp_list; do
    set -- "$@" --static-mcp "$mcp"
  done
  scratch="$FACTORY_ROLES_SCRATCH/${name#factory-}"
  case "$mode" in
    project) sbx create "$@" claude "$PROJECT" >/dev/null ;;
    rw)
      mkdir -p "$scratch"
      sbx create "$@" claude "$scratch" "$PROJECT:ro" "$PROJECT/.factory/outgoing" >/dev/null ;;
    ro)
      mkdir -p "$scratch"
      sbx create "$@" claude "$scratch" "$PROJECT:ro" >/dev/null ;;
    *) die "create_sandbox: unknown workspace mode '$mode'" ;;
  esac
  record_sandbox_fingerprint "$name"
}

# The Coordinator's main workspace is the project so Claude opens there; its kit
# makes it read-only through Claude managed settings (sbx needs a writable main workspace).
# It also gets the broker MCP directly (no role in between) when
# kits/coordinator/broker.conf grants it tools.
coordinator_mcp="$DISPATCH_MCP"
[ -z "$(coordinator_broker_tools)" ] || coordinator_mcp="$coordinator_mcp $(role_broker_mcp coordinator)"
create_sandbox "$COORDINATOR_SANDBOX" developer "$ROOT/kits/coordinator" "$coordinator_mcp" project
exclude_template_claude_md "$COORDINATOR_SANDBOX" "$PROJECT" \
  || die "could not add the sbx template's CLAUDE.md to claudeMdExcludes in $COORDINATOR_SANDBOX's managed settings"
# sbx gives a sandbox the stored Claude login only when it creates it: a role
# sandbox created before the first /login stays logged out, and role sandboxes take
# no input to /login there. So until a login is stored, create only the
# Coordinator (where the user logs in); the next launch creates the roles.
roles_waiting_for_login=""
if claude_login_stored; then
  for role in $roles; do
    mcp=""
    [ -z "$(role_get "$role" BROKER_TOOLS)" ] || mcp="$(role_broker_mcp "$role")"
    create_sandbox "$(role_sandbox "$role")" "$(role_get "$role" TEMPLATE)" \
      "$ROOT/roles/$role/kit" "$mcp" "$(role_get "$role" WORKSPACE)"
  done
else
  for role in $roles; do
    sandbox_exists "$(role_sandbox "$role")" || roles_waiting_for_login="$roles_waiting_for_login $role"
  done
fi

# A Coordinator sandbox created before its kit installed the briefing doesn't
# know it is a factory Coordinator (not recreated automatically: it holds the
# user's conversation history).
if ! sbx exec "$COORDINATOR_SANDBOX" test -f /etc/claude-code/CLAUDE.md >/dev/null 2>&1; then
  echo "note: $COORDINATOR_SANDBOX predates the Coordinator briefing; to install it run:
  sbx rm --force $COORDINATOR_SANDBOX && $0 $(printf '%q' "$PROJECT")"
fi

# Point out sandboxes of removed roles (not deleted automatically: that is the user's call).
for sb in $(sbx ls -q | grep '^factory-' || true); do
  case "$sb" in "$BROKER_SANDBOX"|"$COORDINATOR_SANDBOX") continue ;; esac
  in_list "${sb#factory-}" "$(printf '%s ' $roles)" \
    || echo "note: $sb belongs to a removed role; delete it with: sbx rm --force $sb"
done

if [ -n "$roles_waiting_for_login" ]; then
  echo "note: no Claude login is stored yet, so these role sandboxes were not created:$roles_waiting_for_login
  Type /login in the Coordinator (POWER ON on the dashboard), then run 'factory start' again (or press RESTART on the dashboard)."
fi

if [ "${FACTORY_NO_UI:-}" = 1 ]; then
  echo "Factory ready (no UI): talk to the Coordinator with 'factory chat'; watch every lane with 'factory watch'."
else
  echo "Factory ready: the dashboard ('factory dashboard') is the Coordinator's terminal."
fi
echo "Roles: $(printf '%s ' $roles)"
