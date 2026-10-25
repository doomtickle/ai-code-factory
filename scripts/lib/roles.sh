# Role helpers. Source after common.sh, with ROOT set to the repo root.
# A role is a directory roles/<name>/ containing role.conf and kit/spec.yaml.
# shellcheck shell=bash

# Broker tool names, from the manifest the broker's tests keep in sync with its
# registry (host scripts run on a Python too old to import the broker).
# broker/ is part of this repository.
[ -f "$ROOT/broker/tools.txt" ] || die "$ROOT/broker/tools.txt is missing: broker/ is incomplete in this checkout (restore it with 'git -C $ROOT checkout HEAD -- broker')"
KNOWN_BROKER_TOOLS="$(sed -e '/^#/d' -e '/^[[:space:]]*$/d' "$ROOT/broker/tools.txt" | tr '\n' ' ')"
readonly KNOWN_BROKER_TOOLS="${KNOWN_BROKER_TOOLS% }"
readonly KNOWN_TEMPLATES="developer reviewer"
readonly RESERVED_ROLE_NAMES="broker coordinator dispatch"

# list_all_roles -> every role in roles/, enabled or not
list_all_roles() {
  local d LC_ALL=C # byte order, as factory/roles.py sorts; a locale puts dev-ops after developer
  for d in "$ROOT"/roles/*/; do
    [ -f "$d/role.conf" ] && basename "$d"
  done
  return 0
}

# list_roles -> the roles this machine runs: every role in roles/ except an
# optional one (OPTIONAL="1" in its committed role.conf) that FACTORY_CONFIG/
# enabled-roles doesn't list. Everything that creates, grants, snapshots or
# reports on roles goes through this, so a disabled role gets no sandbox, no
# broker token or grants, and no entry in the bridge.
list_roles() {
  local r
  for r in $(list_all_roles); do
    ! role_optional "$r" || role_listed_enabled "$r" || continue
    printf '%s\n' "$r"
  done
  return 0
}

# conf_get FILE KEY -> value of KEY="value" in FILE (parsed, never sourced)
conf_get() {
  sed -n "s/^$2=\"\(.*\)\"[[:space:]]*\$/\1/p" "$1" | tail -n 1
}

# --- Optional roles ---------------------------------------------------------------
# A role whose committed role.conf sets OPTIONAL="1" is off by default. A
# machine turns it on by naming it in FACTORY_CONFIG/enabled-roles: one role
# name per line, blank lines and #-comments allowed. That file only switches a
# role on or off: it can't grant tools or change WORKSPACE/TEMPLATE (those stay
# in role.conf, and OPTIONAL itself isn't a local-override key). Absent file =
# every optional role stays off. start-dispatch.sh snapshots the validated list
# into DISPATCH_STATE/enabled-roles for the bridge, so a change applies on the
# next `factory start`.

enabled_roles_file() { printf '%s/enabled-roles' "$FACTORY_CONFIG"; }

# enabled_roles_listed -> the names in enabled-roles, one per line (lines that
# aren't plain role names are skipped here; validate_enabled_roles reports them)
enabled_roles_listed() {
  local f
  f="$(enabled_roles_file)"
  [ -f "$f" ] || return 0
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$f" | grep -E '^[a-z][a-z0-9-]*$' || true
}

role_listed_enabled() { in_list "$1" "$(enabled_roles_listed | tr '\n' ' ')"; }

# role_optional ROLE -> success if ROLE's committed role.conf sets OPTIONAL="1"
role_optional() { [ "$(conf_get "$ROOT/roles/$1/role.conf" OPTIONAL)" = 1 ]; }

# disabled_role_sandboxes -> sandboxes that still exist for optional roles this
# machine has switched off (one per line). Nothing removes them: a role being
# turned off must never delete its sandbox on its own; `factory doctor` names
# them and prints the command to remove each.
disabled_role_sandboxes() {
  local existing r
  existing="$(sbx ls -q 2>/dev/null || true)"
  for r in $(list_all_roles); do
    role_optional "$r" || continue
    ! role_listed_enabled "$r" || continue
    ! printf '%s\n' "$existing" | grep -qx "$(role_sandbox "$r")" || role_sandbox "$r"
  done
  return 0
}

# validate_enabled_roles -> dies unless enabled-roles holds only blank lines,
# comments and names of roles in roles/ (a typo would silently enable nothing).
validate_enabled_roles() {
  local f line trimmed
  f="$(enabled_roles_file)"
  [ -e "$f" ] || return 0
  [ -f "$f" ] || die "$f: not a regular file"
  while IFS= read -r line || [ -n "$line" ]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    case "$trimmed" in ''|'#'*) continue ;; esac
    printf '%s' "$trimmed" | grep -Eq '^[a-z][a-z0-9-]*$' \
      || die "$f: not a role name (one per line, e.g. researcher): $line"
    [ -f "$ROOT/roles/$trimmed/role.conf" ] \
      || die "$f: no role named '$trimmed' in $ROOT/roles (see: factory roles)"
  done < "$f"
}

# --- Local role overrides ------------------------------------------------------------
# FACTORY_CONFIG/roles/<role>.conf: this machine's tuning of a role, layered
# over roles/<role>/role.conf so a user needn't edit their clone. Same
# KEY="value" format, parsed (never sourced), and only the keys below: nothing
# that moves the security boundary (BROKER_TOOLS, WORKSPACE, TEMPLATE, ...).
# validate_role checks the merged values exactly as it checks role.conf's.
# start-dispatch.sh snapshots the overrides for the bridge
# (DISPATCH_STATE/role-overrides), so changes apply on the next `factory start`.
readonly ROLE_OVERRIDE_KEYS="MODEL MODELS EFFORT LANES MAX_SESSION_TOKENS MAX_TURNS MAX_JOB_SECONDS"

role_override_file() { printf '%s/roles/%s.conf' "$FACTORY_CONFIG" "$1"; }

# role_conf_file ROLE KEY -> the file ROLE's KEY is read from: its override if
# that sets KEY (and KEY may be overridden), else roles/ROLE/role.conf
role_conf_file() {
  local o
  o="$(role_override_file "$1")"
  if in_list "$2" "$ROLE_OVERRIDE_KEYS" && [ -f "$o" ] && grep -q "^$2=\".*\"[[:space:]]*\$" "$o"; then
    printf '%s' "$o"
  else
    printf '%s' "$ROOT/roles/$1/role.conf"
  fi
}

# override_note ROLE KEY -> " (set in <override file>)" if ROLE's local
# override sets KEY, else nothing: for error messages
override_note() {
  local f
  f="$(role_conf_file "$1" "$2")"
  [ "$f" = "$ROOT/roles/$1/role.conf" ] || printf ' (set in %s)' "$f"
}

# role_get ROLE KEY -> value of KEY="value" for ROLE: role.conf, with the
# local override layered on top (both parsed, never sourced)
role_get() {
  conf_get "$(role_conf_file "$1" "$2")" "$2"
}

# validate_role_override ROLE -> dies unless ROLE's override (if any) holds
# only blank lines, comments and KEY="value" lines for ROLE_OVERRIDE_KEYS.
validate_role_override() {
  local f line trimmed key
  f="$(role_override_file "$1")"
  [ -e "$f" ] || return 0
  [ -f "$f" ] || die "$f: not a regular file"
  while IFS= read -r line || [ -n "$line" ]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    case "$trimmed" in ''|'#'*) continue ;; esac
    key="$(printf '%s\n' "$line" | sed -n 's/^\([A-Z_][A-Z_]*\)=".*"[[:space:]]*$/\1/p')"
    [ -n "$key" ] || die "$f: not a KEY=\"value\" line: $line"
    in_list "$key" "$ROLE_OVERRIDE_KEYS" \
      || die "$f: $key can't be set in a local override (allowed: $ROLE_OVERRIDE_KEYS); change it in roles/$1/role.conf"
  done < "$f"
}

# validate_role_overrides -> dies if an override names no role in roles/, or if
# enabled-roles (see Optional roles above) is malformed.
validate_role_overrides() {
  local f r
  validate_enabled_roles
  for f in "$FACTORY_CONFIG"/roles/*.conf; do
    [ -e "$f" ] || continue
    r="$(basename "$f" .conf)"
    [ -f "$ROOT/roles/$r/role.conf" ] \
      || die "$f: no role named '$r' in $ROOT/roles (an override is named <role>.conf; see: factory roles)"
  done
  return 0
}

# coordinator_broker_tools -> value of BROKER_TOOLS="..." in
# kits/coordinator/broker.conf (empty if the file is absent). The Coordinator
# is a broker pseudo-role: it calls broker tools directly, with no role in
# between, so it is parsed the same way a role.conf is (parsed, never sourced)
# but lives outside roles/.
coordinator_broker_tools() {
  local f="$ROOT/kits/coordinator/broker.conf"
  [ -f "$f" ] || return 0
  conf_get "$f" BROKER_TOOLS
}

role_sandbox() { printf 'factory-%s' "$1"; }
role_broker_mcp() { printf 'factory-broker-%s' "$1"; }

# in_list WORD "LIST" -> success if WORD is one of the space-separated LIST
in_list() {
  case " $2 " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# Role names must not end in -lane<N>: that is how a role's extra lanes are
# named (role_lane_names), so such a role would collide with another's lane.
valid_role_name() {
  printf '%s' "$1" | grep -Eq '^[a-z][a-z0-9-]{1,30}$' && ! in_list "$1" "$RESERVED_ROLE_NAMES" \
    && ! printf '%s' "$1" | grep -Eq -- '-lane[0-9]+$'
}

# role_lanes ROLE -> how many jobs ROLE may run at once (LANES in role.conf,
# 1 when empty). validate_role rejects values outside 1..8.
role_lanes() {
  local n
  n="$(role_get "$1" LANES)"
  printf '%s' "${n:-1}"
}

# role_lane_names ROLE -> ROLE's lane names, one per line: lane 1 is ROLE
# itself, lane k >= 2 is ROLE-lane<k>. Each lane has its own session and
# transcript (DISPATCH_STATE sessions/<lane>, logs/<lane>.log); all lanes
# share the role's one sandbox.
role_lane_names() {
  local n k
  n="$(role_lanes "$1")"
  printf '%s\n' "$1"
  k=2
  while [ "$k" -le "$n" ]; do
    printf '%s-lane%s\n' "$1" "$k"
    k=$((k + 1))
  done
}

validate_role() {
  local r="$1" t m
  valid_role_name "$r" || die "invalid role name '$r' (lowercase letters, digits, dashes; not ending in -lane<N>; not: $RESERVED_ROLE_NAMES)"
  [ -f "$ROOT/roles/$r/kit/spec.yaml" ] || die "role $r: missing roles/$r/kit/spec.yaml"
  [ -s "$ROOT/roles/$r/instructions.md" ] || die "role $r: missing or empty roles/$r/instructions.md"
  validate_role_override "$r"
  m="$(conf_get "$ROOT/roles/$r/role.conf" OPTIONAL)"
  [ -z "$m" ] || in_list "$m" "0 1" || die "role $r: OPTIONAL must be 1 (off unless enabled-roles names it) or empty"
  in_list "$(role_get "$r" TEMPLATE)" "$KNOWN_TEMPLATES" \
    || die "role $r: TEMPLATE must be one of: $KNOWN_TEMPLATES"
  in_list "$(role_get "$r" WORKSPACE)" "rw ro" || die "role $r: WORKSPACE must be rw or ro"
  for t in $(role_get "$r" BROKER_TOOLS); do
    in_list "$t" "$KNOWN_BROKER_TOOLS" || die "role $r: unknown broker tool '$t' (known: $KNOWN_BROKER_TOOLS)"
  done
  m="$(role_get "$r" MODEL)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[A-Za-z0-9._-]+$' \
    || die "role $r: MODEL must contain only letters, digits, dots, dashes, underscores$(override_note "$r" MODEL)"
  for m in $(role_get "$r" MODELS); do
    printf '%s' "$m" | grep -Eq '^[A-Za-z0-9._-]+$' \
      || die "role $r: MODELS entries must contain only letters, digits, dots, dashes, underscores$(override_note "$r" MODELS)"
  done
  m="$(role_get "$r" EFFORT)"
  [ -z "$m" ] || in_list "$m" "low medium high xhigh" \
    || die "role $r: EFFORT must be low, medium, high or xhigh (or empty)$(override_note "$r" EFFORT)"
  m="$(role_get "$r" LANES)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[1-8]$' \
    || die "role $r: LANES must be a whole number from 1 to 8 (or empty)$(override_note "$r" LANES)"
  m="$(role_get "$r" MAX_SESSION_TOKENS)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[0-9]+$' \
    || die "role $r: MAX_SESSION_TOKENS must be a whole number of tokens (or empty)$(override_note "$r" MAX_SESSION_TOKENS)"
  m="$(role_get "$r" MAX_TURNS)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[0-9]+$' \
    || die "role $r: MAX_TURNS must be a whole number of turns (or empty)$(override_note "$r" MAX_TURNS)"
  m="$(role_get "$r" MAX_JOB_SECONDS)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[0-9]+$' \
    || die "role $r: MAX_JOB_SECONDS must be a whole number of seconds (or empty)$(override_note "$r" MAX_JOB_SECONDS)"
}

# --- Sandbox fingerprints ------------------------------------------------------------
# A sandbox keeps what it was created from: its image, kit, project access and
# whether it has broker access. A digest of those inputs is recorded when the
# sandbox is created; when they change (a rebuilt image, an edited kit),
# `factory start` reports the sandbox as out of date and `factory update`
# recreates it. Grants in broker.conf are left out: the broker re-reads them on
# every call, so only gaining or losing broker access at all needs a new sandbox.
# LANES is left out too: the bridge reads it per job, and lanes share one
# sandbox. Roles with WORKSPACE="rw" also record their mount layout
# (RW_LAYOUT), so a change to how such sandboxes are created marks the old
# ones out of date.

# How 02-launch-factory.sh lays out an rw role's sandbox. lanes-v1: a scratch
# dir as the writable primary workspace, the project read-only, and only
# PROJECT/.factory/outgoing writable; the role works in lane clones on the
# sandbox's own disk. (Before: the project itself, read-write.)
readonly RW_LAYOUT="lanes-v1"

sandbox_fingerprint_file() { printf '%s/sandboxes/%s' "$FACTORY_STATE" "$1"; }

# image_build_id IMAGE -> id of the last build of factory-IMAGE:TEMPLATE_TAG
# (recorded by 00-build-templates.sh), or the tag if none was recorded.
image_build_id() {
  cat "$FACTORY_STATE/images/factory-$1-$TEMPLATE_TAG.id" 2>/dev/null \
    || printf 'factory-%s:%s' "$1" "$TEMPLATE_TAG"
}

# dir_hash DIR -> digest of the file names and contents under DIR ("none" if absent)
dir_hash() {
  local dir="$1" f
  [ -d "$dir" ] || { printf 'none'; return 0; }
  find "$dir" -type f ! -name broker.conf | LC_ALL=C sort | while IFS= read -r f; do
    printf '%s %s\n' "${f#"$dir"/}" "$(sha256_hash "$f")"
  done | sha256_hash
}

yes_no() { if [ -n "$1" ]; then echo yes; else echo no; fi; }

# sandbox_fingerprint NAME -> digest of the inputs NAME is created from
sandbox_fingerprint() {
  local name="$1" role
  case "$name" in
    "$BROKER_SANDBOX")
      # plugins: the PASSDESK_PLUGINS value fixed at creation (the plugin
      # code itself is in the image, so the image id covers its contents).
      printf 'image=%s\nkit=%s\nlocal-kit=%s\nplugins=%s\n' "$(image_build_id broker)" \
        "$(dir_hash "$ROOT/kits/broker")" "$(dir_hash "$FACTORY_CONFIG/broker-kit")" "$(broker_plugins)" ;;
    "$COORDINATOR_SANDBOX")
      printf 'image=%s\nkit=%s\nbroker=%s\n' "$(image_build_id developer)" \
        "$(dir_hash "$ROOT/kits/coordinator")" "$(yes_no "$(coordinator_broker_tools)")" ;;
    *)
      role="${name#factory-}"
      printf 'image=%s\nkit=%s\nworkspace=%s\nbroker=%s\n' "$(image_build_id "$(role_get "$role" TEMPLATE)")" \
        "$(dir_hash "$ROOT/roles/$role/kit")" "$(role_get "$role" WORKSPACE)" \
        "$(yes_no "$(role_get "$role" BROKER_TOOLS)")"
      # Only rw roles: read-only roles' sandboxes keep their fingerprint.
      [ "$(role_get "$role" WORKSPACE)" != rw ] || printf 'layout=%s\n' "$RW_LAYOUT" ;;
  esac | sha256_hash
}

record_sandbox_fingerprint() {
  mkdir -p "$FACTORY_STATE/sandboxes"
  sandbox_fingerprint "$1" > "$(sandbox_fingerprint_file "$1")"
}

# stale_sandboxes -> existing broker, Coordinator and role sandboxes whose
# inputs changed since they were created (or that predate fingerprints), one per line
stale_sandboxes() {
  local existing sb r
  existing="$(sbx ls -q 2>/dev/null || true)"
  for sb in "$BROKER_SANDBOX" "$COORDINATOR_SANDBOX" $(for r in $(list_roles); do role_sandbox "$r"; echo; done); do
    printf '%s\n' "$existing" | grep -qx "$sb" || continue
    [ "$(cat "$(sandbox_fingerprint_file "$sb")" 2>/dev/null || true)" = "$(sandbox_fingerprint "$sb")" ] \
      || printf '%s\n' "$sb"
  done
  return 0
}
