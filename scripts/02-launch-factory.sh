#!/usr/bin/env bash
set -euo pipefail

# Usage: 02-launch-factory.sh <project-dir>
#
# Starts the factory for <project-dir>:
#   - the broker VM (network tools for roles) and the dispatch bridge (host)
#   - one sandbox per role in roles/, plus the Coordinator sandbox
#   - a Herdr workspace "code-factory", with a "coordinator" tab where you
#     talk to the Coordinator (plus a host shell below it). Its layout
#     (FACTORY_LAYOUT, default "watch"; bin/factory resolves and passes it -
#     see `factory start --layout`) is one of:
#       watch: a "factory watch" pane beside the Coordinator, live for every
#         role's lanes; no per-role tabs (any left over from "tabs" are closed).
#       tabs: one tab per role, tailing the live transcripts of the tasks the
#         Coordinator delegates to it (every lane's, for a role with LANES > 1);
#         any "Watch" pane left over from "watch" is closed.
# Inside a Herdr pane it uses the current Herdr session. Otherwise it uses the
# session named by FACTORY_HERDR_SESSION (bin/factory sets "factory"), starting
# that session's server if needed; attach with `herdr session attach <name>`.
#
# Safe to re-run: existing sandboxes, tabs and panes are reused, missing ones
# are created (e.g. after adding a role with new-role.sh), and a command is only
# started in a pane that is sitting at a shell prompt.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"

[ $# -eq 1 ] || die "usage: $0 <project-dir>"
[ -d "$1" ] || die "project directory not found: $1"

require_bin jq
# FACTORY_NO_UI=1 (factory start --no-ui): no Herdr at all; the user talks to the
# Coordinator with `sbx run` and follows roles with `factory logs`.
[ "${FACTORY_NO_UI:-}" = 1 ] || require_herdr
require_sbx

# FACTORY_LAYOUT: bin/factory resolves and passes this (--layout, FACTORY_LAYOUT,
# or the saved default); a direct run defaults to "watch". Ignored entirely
# with FACTORY_NO_UI=1 (no Herdr layout to pick).
LAYOUT="${FACTORY_LAYOUT:-watch}"
case "$LAYOUT" in
  watch|tabs) ;;
  *) die "FACTORY_LAYOUT must be 'watch' or 'tabs', got '$LAYOUT'" ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# shellcheck source=lib/roles.sh
. "$ROOT/scripts/lib/roles.sh"
PROJECT="$(cd "$1" && pwd -P)"

readonly WS_LABEL="code-factory"
if [ "${HERDR_ENV:-}" = "1" ]; then
  HERDR_SESSION=""
else
  HERDR_SESSION="${FACTORY_HERDR_SESSION:-factory}"
fi

# hd ARGS... -> herdr against the factory's session
hd() {
  if [ -n "$HERDR_SESSION" ]; then herdr --session "$HERDR_SESSION" "$@"; else herdr "$@"; fi
}
readonly COORDINATOR_TAB="coordinator"

roles="$(list_roles)"
[ -n "$roles" ] || die "no roles defined in roles/"
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
# sbx gives a sandbox the stored Claude login only when it creates it: a role
# sandbox created before the first /login stays logged out, and role panes take
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

# --- Herdr ------------------------------------------------------------------------
# Skipped entirely with FACTORY_NO_UI=1 (the block ends after the removed-role
# tabs are closed; the final "ready" message is chosen at the bottom).
if [ "${FACTORY_NO_UI:-}" != 1 ]; then
if [ -n "$HERDR_SESSION" ] && ! herdr session list 2>/dev/null | grep -q "^$HERDR_SESSION .*running"; then
  echo "Starting Herdr session '$HERDR_SESSION'..."
  nohup herdr --session "$HERDR_SESSION" server >/dev/null 2>&1 </dev/null &
  for _ in $(seq 1 20); do
    herdr session list 2>/dev/null | grep -q "^$HERDR_SESSION .*running" && break
    sleep 0.5
  done
  herdr session list 2>/dev/null | grep -q "^$HERDR_SESSION .*running" \
    || die "could not start Herdr session '$HERDR_SESSION'"
fi

# pane_idle PANE -> success if the pane's shell is at its prompt (nothing in the foreground)
pane_idle() {
  hd pane process-info --pane "$1" \
    | jq -e '.result.process_info | .foreground_process_group_id == .shell_pid' >/dev/null
}

# run_if_idle PANE COMMAND
run_if_idle() {
  if pane_idle "$1"; then
    hd pane run "$1" "$2"
  fi
}

# stop_stale_tail PANE LOG... -> stop a transcript tail that follows other
# files than LOG... (e.g. the in-repo .dispatch/logs/ from before state moved
# out, or a role's lanes before its LANES changed), so the pane is idle and
# run_if_idle restarts it on LOG.... Only a plain `tail` is stopped, by pid;
# nothing is typed into the pane.
stop_stale_tail() {
  local pane="$1" pid
  shift
  pid="$(hd pane process-info --pane "$pane" | jq -r --argjson logs "$(jq -n '$ARGS.positional' --args "$@")" \
    '.result.process_info.foreground_processes[] | select(.name == "tail" and .argv[-($logs | length):] != $logs) | .pid' \
    | head -n 1 || true)"
  [ -n "$pid" ] || return 0
  echo "Restarting transcript tail in pane $pane (it follows other log paths)..."
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    pane_idle "$pane" && return 0
    sleep 1
  done
}

# Workspace: reuse, preferring the one this script runs in; close duplicates.
existing="$(hd workspace list | jq -r --arg l "$WS_LABEL" '.result.workspaces[] | select(.label == $l) | .workspace_id')"
ws=""
for id in $existing; do
  [ "$id" = "${HERDR_WORKSPACE_ID:-}" ] && ws="$id"
done
[ -n "$ws" ] || ws="$(printf '%s\n' "$existing" | sed -n 1p)"
for id in $existing; do
  if [ "$id" != "$ws" ] && [ "$id" != "${HERDR_WORKSPACE_ID:-}" ]; then
    echo "Closing duplicate $WS_LABEL workspace $id..."
    hd workspace close "$id" >/dev/null
  fi
done
if [ -z "$ws" ]; then
  echo "Creating $WS_LABEL workspace..."
  ws_json="$(hd workspace create --cwd "$PROJECT" --label "$WS_LABEL" --focus)"
  ws="$(printf '%s' "$ws_json" | jq -r '.result.workspace.workspace_id')"
  hd tab rename "$(printf '%s' "$ws_json" | jq -r '.result.tab.tab_id')" "$COORDINATOR_TAB" >/dev/null
fi

# tab_pane LABEL -> first pane of the tab with that label, creating the tab if needed
tab_pane() {
  local tab pane
  tab="$(hd tab list --workspace "$ws" | jq -r --arg l "$1" '[.result.tabs[] | select(.label == $l)][0].tab_id // empty')"
  if [ -z "$tab" ]; then
    hd tab create --workspace "$ws" --cwd "$PROJECT" --label "$1" --no-focus \
      | jq -r '.result.root_pane.pane_id'
    return
  fi
  pane="$(hd pane list --workspace "$ws" | jq -r --arg t "$tab" '[.result.panes[] | select(.tab_id == $t)][0].pane_id // empty')"
  [ -n "$pane" ] || die "tab $1 has no panes; close it (hd tab close) and re-run"
  printf '%s' "$pane"
}

# Coordinator tab: the Coordinator sandbox, with a host shell below it (and,
# under "watch", a Watch pane beside it - added below).
coordinator="$(tab_pane "$COORDINATOR_TAB")"
hd pane rename "$coordinator" "Coordinator (VM)" >/dev/null
coord_tab="$(hd pane get "$coordinator" | jq -r '.result.pane.tab_id')"
host_shell="$(hd pane list --workspace "$ws" \
  | jq -r --arg t "$coord_tab" '[.result.panes[] | select(.tab_id == $t and .label == "Host shell")][0].pane_id // empty')"
if [ -z "$host_shell" ]; then
  host_shell="$(hd pane split "$coordinator" --direction down --cwd "$PROJECT" --no-focus | jq -r '.result.pane.pane_id')"
  hd pane rename "$host_shell" "Host shell" >/dev/null
fi
# A reused host shell may still be in another folder (e.g. after switching projects).
host_cwd="$(hd pane get "$host_shell" | jq -r '.result.pane.cwd')"
if [ "$host_cwd" != "$PROJECT" ] && pane_idle "$host_shell"; then
  hd pane run "$host_shell" "cd $(printf '%q' "$PROJECT")"
fi
run_if_idle "$coordinator" "sbx run --name $COORDINATOR_SANDBOX"

# "watch" layout: a "factory watch" pane to the right of the Coordinator,
# full-width host shell below (splitting off the Coordinator pane itself, the
# only one guaranteed to exist both on a fresh tab and on one already split
# down for the host shell - so this lands the same way whichever happened
# first; a true full-height right column isn't reachable once the coordinator
# pane is already split into a top/bottom pair, since a pane split only
# divides that one pane's own cell). Any Watch pane is closed under "tabs".
watch_pane="$(hd pane list --workspace "$ws" \
  | jq -r --arg t "$coord_tab" '[.result.panes[] | select(.tab_id == $t and .label == "Watch")][0].pane_id // empty')"
if [ "$LAYOUT" = watch ]; then
  if [ -z "$watch_pane" ]; then
    watch_pane="$(hd pane split "$coordinator" --direction right --cwd "$PROJECT" --no-focus | jq -r '.result.pane.pane_id')"
    hd pane rename "$watch_pane" "Watch" >/dev/null
  fi
  run_if_idle "$watch_pane" "$(printf '%q' "$ROOT/bin/factory") watch"
elif [ -n "$watch_pane" ]; then
  echo "Closing Watch pane (switched to the tabs layout)..."
  hd pane close "$watch_pane" >/dev/null
fi

# "tabs" layout: one tab per role, tailing the transcripts of all its lanes
# (read-only; nothing is typed into sandboxes). With several files, tail
# heads each chunk with "==> <lane>.log <==". Not created under "watch".
if [ "$LAYOUT" = tabs ]; then
  for role in $roles; do
    pane="$(tab_pane "$role")"
    hd pane rename "$pane" "$role (transcript)" >/dev/null
    logs=()
    for lane in $(role_lane_names "$role"); do
      logs+=("$FACTORY_DISPATCH_STATE/logs/$lane.log")
    done
    stop_stale_tail "$pane" "${logs[@]}"
    run_if_idle "$pane" "clear; tail -n 200 -F$(printf ' %q' "${logs[@]}")"
  done
fi

# Close transcript tabs: under "tabs", only those of roles that no longer
# exist; under "watch", every one (the Watch pane replaces them, and a
# re-run after switching layouts must clean them up).
hd tab list --workspace "$ws" | jq -r '.result.tabs[] | .tab_id + " " + .label' | while read -r tab label; do
  [ "$label" = "$COORDINATOR_TAB" ] && continue
  if [ "$LAYOUT" = tabs ] && in_list "$label" "$(printf '%s ' $roles)"; then
    continue
  fi
  if hd pane list --workspace "$ws" | jq -e --arg t "$tab" --arg l "$label (transcript)" \
      'any(.result.panes[]; .tab_id == $t and .label == $l)' >/dev/null; then
    if [ "$LAYOUT" = watch ]; then
      echo "Closing tab of $label (switched to the watch layout)..."
    else
      echo "Closing tab of removed role $label..."
    fi
    hd tab close "$tab" >/dev/null
  fi
done
fi  # FACTORY_NO_UI

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
  Type /login in the Coordinator, then run 'factory start' again (e.g. in the Host shell pane)."
fi

if [ "${FACTORY_NO_UI:-}" = 1 ]; then
  echo "Factory ready (no UI): talk to the Coordinator with 'sbx run --name $COORDINATOR_SANDBOX'; watch every lane with 'factory watch'."
else
  hd workspace focus "$ws" >/dev/null
  hd tab focus "$coord_tab" >/dev/null
  echo "Factory ready (Herdr ${HERDR_SESSION:+session '$HERDR_SESSION', }workspace $WS_LABEL): talk to the Coordinator in the '$COORDINATOR_TAB' tab."
fi
echo "Roles: $(printf '%s ' $roles)"
