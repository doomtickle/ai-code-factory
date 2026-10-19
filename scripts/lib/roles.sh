# Role helpers. Source after common.sh, with ROOT set to the repo root.
# A role is a directory roles/<name>/ containing role.conf and kit/spec.yaml.
# shellcheck shell=bash

# Broker tool names, from the manifest the broker's tests keep in sync with its
# registry (host scripts run on a Python too old to import the broker).
# broker/ is a git submodule (passdesk-broker); a plain "git clone" (without
# --recurse-submodules) leaves it an empty directory, so every command sourcing
# this file (not just setup/update) tries once to initialize it before giving up.
# Only a submodule with no trace of initialization: "submodule update" follows
# broker/.git, which an agent can write (see "broker/ submodule safety" in
# common.sh), so a broker/ that has one but lost tools.txt is left to a person.
if [ ! -f "$ROOT/broker/tools.txt" ] && ! broker_initialized "$ROOT"; then
  git "${BROKER_GIT_SAFE[@]}" -C "$ROOT" submodule update --init --quiet -- broker >/dev/null 2>&1 || true
fi
[ -f "$ROOT/broker/tools.txt" ] \
  || die "missing $ROOT/broker/tools.txt - run 'git submodule update --init' in $ROOT"
KNOWN_BROKER_TOOLS="$(sed -e '/^#/d' -e '/^[[:space:]]*$/d' "$ROOT/broker/tools.txt" | tr '\n' ' ')"
readonly KNOWN_BROKER_TOOLS="${KNOWN_BROKER_TOOLS% }"
readonly KNOWN_TEMPLATES="developer reviewer"
readonly RESERVED_ROLE_NAMES="broker coordinator dispatch"

list_roles() {
  local d
  for d in "$ROOT"/roles/*/; do
    [ -f "$d/role.conf" ] && basename "$d"
  done
  return 0
}

# conf_get FILE KEY -> value of KEY="value" in FILE (parsed, never sourced)
conf_get() {
  sed -n "s/^$2=\"\(.*\)\"[[:space:]]*\$/\1/p" "$1" | tail -n 1
}

# role_get ROLE KEY -> value of KEY="value" in role.conf (parsed, never sourced)
role_get() {
  conf_get "$ROOT/roles/$1/role.conf" "$2"
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
  in_list "$(role_get "$r" TEMPLATE)" "$KNOWN_TEMPLATES" \
    || die "role $r: TEMPLATE must be one of: $KNOWN_TEMPLATES"
  in_list "$(role_get "$r" WORKSPACE)" "rw ro" || die "role $r: WORKSPACE must be rw or ro"
  for t in $(role_get "$r" BROKER_TOOLS); do
    in_list "$t" "$KNOWN_BROKER_TOOLS" || die "role $r: unknown broker tool '$t' (known: $KNOWN_BROKER_TOOLS)"
  done
  m="$(role_get "$r" MODEL)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[A-Za-z0-9._-]+$' \
    || die "role $r: MODEL must contain only letters, digits, dots, dashes, underscores"
  for m in $(role_get "$r" MODELS); do
    printf '%s' "$m" | grep -Eq '^[A-Za-z0-9._-]+$' \
      || die "role $r: MODELS entries must contain only letters, digits, dots, dashes, underscores"
  done
  m="$(role_get "$r" EFFORT)"
  [ -z "$m" ] || in_list "$m" "low medium high xhigh" \
    || die "role $r: EFFORT must be low, medium, high or xhigh (or empty)"
  m="$(role_get "$r" LANES)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[1-8]$' \
    || die "role $r: LANES must be a whole number from 1 to 8 (or empty)"
  m="$(role_get "$r" MAX_SESSION_TOKENS)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[0-9]+$' \
    || die "role $r: MAX_SESSION_TOKENS must be a whole number of tokens (or empty)"
  m="$(role_get "$r" MAX_TURNS)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[0-9]+$' \
    || die "role $r: MAX_TURNS must be a whole number of turns (or empty)"
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
