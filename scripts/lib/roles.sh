# Role helpers. Source after common.sh, with ROOT set to the repo root.
# A role is a directory roles/<name>/ containing role.conf and kit/spec.yaml.
# shellcheck shell=bash

# Broker tool names, from the manifest the broker's tests keep in sync with its
# registry (host scripts run on a Python too old to import the broker).
[ -f "$ROOT/broker/tools.txt" ] || die "missing $ROOT/broker/tools.txt"
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

valid_role_name() {
  printf '%s' "$1" | grep -Eq '^[a-z][a-z0-9-]{1,30}$' && ! in_list "$1" "$RESERVED_ROLE_NAMES"
}

validate_role() {
  local r="$1" t m
  valid_role_name "$r" || die "invalid role name '$r' (lowercase letters, digits, dashes; not: $RESERVED_ROLE_NAMES)"
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
  m="$(role_get "$r" MAX_SESSION_TOKENS)"
  [ -z "$m" ] || printf '%s' "$m" | grep -Eq '^[0-9]+$' \
    || die "role $r: MAX_SESSION_TOKENS must be a whole number of tokens (or empty)"
}
