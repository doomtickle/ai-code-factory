#!/usr/bin/env bash
set -euo pipefail

# Usage: new-role.sh <name> [<existing-role-to-copy>]
#
# Scaffolds roles/<name>/ by copying an existing role (default: reviewer,
# which is read-only and whose only network tool is npm_fetch: the safest
# starting point). Edit the new role.conf and instructions.md, then
# re-run 02-launch-factory.sh: it creates the sandbox and a Herdr tab, and the
# Coordinator sees the role in list_roles.

# shellcheck source=lib/common.sh
. "$(dirname "$0")/lib/common.sh"
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# shellcheck source=lib/roles.sh
. "$ROOT/scripts/lib/roles.sh"

[ $# -ge 1 ] && [ $# -le 2 ] || die "usage: $0 <name> [<existing-role-to-copy>]"
name="$1"
from="${2:-reviewer}"

valid_role_name "$name" || die "invalid role name '$name' (lowercase letters, digits, dashes; not ending in -lane<N>; not: $RESERVED_ROLE_NAMES)"
[ ! -e "$ROOT/roles/$name" ] || die "roles/$name already exists"
[ -f "$ROOT/roles/$from/role.conf" ] || die "no role named '$from' to copy"

cp -R "$ROOT/roles/$from" "$ROOT/roles/$name"
spec="$ROOT/roles/$name/kit/spec.yaml"
sed -i.bak -e "s/^name: .*/name: factory-$name-kit/" \
           -e "s/^displayName: .*/displayName: Factory $name/" "$spec"
sed -i.bak -e "s/^DESCRIPTION=.*/DESCRIPTION=\"TODO: one line describing what the $name does\"/" \
  "$ROOT/roles/$name/role.conf"
# A copy of an optional role (OPTIONAL="1", with its comment) is a normal role.
sed -i.bak -e '/^# Off by default/,/^OPTIONAL=/d' "$ROOT/roles/$name/role.conf"
rm -f "$spec.bak" "$ROOT/roles/$name/role.conf.bak"
validate_role "$name"
sbx kit validate "$ROOT/roles/$name/kit" >/dev/null

cat <<MSG
Created roles/$name (copied from $from). Next:
  1. Edit roles/$name/role.conf: DESCRIPTION, WORKSPACE (rw/ro), BROKER_TOOLS, LANES, MAX_TURNS, MODEL and MODELS (optional).
  2. Edit its instructions in roles/$name/instructions.md.
  3. Run 'factory start' again.
MSG
