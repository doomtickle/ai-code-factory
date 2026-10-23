#!/usr/bin/env bash
set -euo pipefail

# Tests for exclude_template_claude_md (scripts/lib/common.sh), which keeps the
# Coordinator from loading the sbx base template's CLAUDE.md, against a stub
# sbx whose sandbox /etc/claude-code is a temp dir. No sbx, docker or network.
#
# 1. template_claude_md is the workspace's parent joined with CLAUDE.md.
# 2. The exclude is added to the managed settings, the kit's other keys are
#    kept, and the file stays 0444.
# 3. A second run writes nothing (idempotent); an existing exclude list is
#    kept and the path isn't duplicated.
# 4. A path with spaces and shell metacharacters crosses only as data.
# 5. A sandbox without managed settings fails (the launch then dies).

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
command -v jq >/dev/null || { echo "skip - claudeMdExcludes (needs jq)"; exit 0; }

tmp="$(mktemp -d)"
trap 'chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
tmp="$(cd "$tmp" && pwd -P)"
export XDG_CONFIG_HOME="$tmp/config" XDG_STATE_HOME="$tmp/state"

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }

# shellcheck source=lib/common.sh
. "$REPO_ROOT/scripts/lib/common.sh"

# Stub sbx: `exec -u 0 [-i] NAME CMD...` runs CMD with the sandbox's
# /etc/claude-code mapped to $tmp/vm/etc/claude-code; every call is logged.
vm="$tmp/vm"
mkdir -p "$tmp/bin" "$vm/etc/claude-code"
cat > "$tmp/bin/sbx" <<EOF
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "\$*" >> "$tmp/sbx.log"
[ "\$1" = exec ] || exit 2
shift
while [ "\${1#-}" != "\$1" ]; do case "\$1" in -u) shift 2 ;; *) shift ;; esac; done
shift
args=()
for a in "\$@"; do args+=("\${a//\/etc\/claude-code/$vm/etc/claude-code}"); done
exec "\${args[@]}"
EOF
chmod +x "$tmp/bin/sbx"
PATH="$tmp/bin:$PATH"
settings="$vm/etc/claude-code/managed-settings.json"
writes() { grep -c -- ' -i ' "$tmp/sbx.log" 2>/dev/null || true; }
mode() { if stat -c %a "$1" >/dev/null 2>&1; then stat -c %a "$1"; else stat -f %Lp "$1"; fi; }

# --- 1 -----------------------------------------------------------------------------
assert_eq "path: the workspace's parent" "$(template_claude_md /home/u/code)" "/home/u/CLAUDE.md"
assert_eq "path: a nested workspace" "$(template_claude_md /Users/u/src/proj)" "/Users/u/src/CLAUDE.md"

# --- 2 -----------------------------------------------------------------------------
printf '%s\n' '{"model":"opus","permissions":{"deny":["Bash"]}}' > "$settings"
chmod 0444 "$settings"
exclude_template_claude_md factory-coordinator /home/u/code
assert_eq "add: the exclude is set" "$(jq -c .claudeMdExcludes "$settings")" '["/home/u/CLAUDE.md"]'
assert_eq "add: other keys are kept" "$(jq -c '[.model, .permissions.deny]' "$settings")" '["opus",["Bash"]]'
assert_eq "add: the file stays 0444" "$(mode "$settings")" "444"
assert_eq "add: one write" "$(writes)" "1"

# --- 3 -----------------------------------------------------------------------------
exclude_template_claude_md factory-coordinator /home/u/code
assert_eq "again: nothing is written" "$(writes)" "1"
chmod u+w "$settings"
printf '%s\n' '{"claudeMdExcludes":["/other/CLAUDE.md","/home/u/CLAUDE.md"]}' > "$settings"
exclude_template_claude_md factory-coordinator /home/u/code
assert_eq "existing list: kept, no duplicate" "$(jq -c .claudeMdExcludes "$settings")" '["/other/CLAUDE.md","/home/u/CLAUDE.md"]'

# --- 4 -----------------------------------------------------------------------------
odd='/tmp/a b/$(touch pwned);`id`"x'
exclude_template_claude_md factory-coordinator "$odd/proj"
assert_eq "odd path: stored verbatim" "$(jq -r '.claudeMdExcludes[-1]' "$settings")" "$odd/CLAUDE.md"
assert_eq "odd path: nothing ran" "$(test -e pwned || test -e "$vm/pwned" && echo ran || echo no)" "no"
assert_eq "odd path: never on an sbx command line" "$(grep -c 'pwned' "$tmp/sbx.log" || true)" "0"

# --- 5 -----------------------------------------------------------------------------
chmod u+w "$settings"
rm -f "$settings"
set +e
exclude_template_claude_md factory-coordinator /home/u/code 2>/dev/null
rc=$?
set -e
assert_eq "no managed settings: fails" "$rc" "1"
assert_eq "no managed settings: nothing is written" "$(test -e "$settings" && echo written || echo none)" "none"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
