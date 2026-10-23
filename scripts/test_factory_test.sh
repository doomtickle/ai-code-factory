#!/usr/bin/env bash
set -euo pipefail

# Tests for factory-test's Python mode (templates/agent/factory-test): run
# directly against a disposable fixture, with FACTORY_TEST_ROOT and
# FACTORY_PROJECT pointed at temp dirs. No sbx, docker or network: the "wheel"
# is a stub package zipped here.
#
# Covered:
# - a hash-pinned requirements.txt: the venv is created in the copy, the wheel
#   installs offline, the command imports it, and the "mode:" line says so.
# - the install stamp: a second run doesn't reinstall; a changed requirements
#   file does; the project folder gets no .venv.
# - -r picks another file; requirements.txt wins over requirements-dev.txt.
# - failure paths: no wheel folder, a wheel whose hash doesn't match, an
#   unpinned requirements file, a missing -r file, -r outside the project.
# - a Node lockfile wins over a requirements file; a project with neither
#   keeps "project files only".

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FTEST="$REPO_ROOT/templates/agent/factory-test"

for t in python3 rsync sha256sum; do
  command -v "$t" >/dev/null || { echo "skip - factory-test Python mode (needs $t)"; exit 0; }
done
python3 -m pip --version >/dev/null 2>&1 || { echo "skip - factory-test Python mode (needs pip)"; exit 0; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tmp="$(cd "$tmp" && pwd -P)"
export HOME="$tmp/home" FACTORY_TEST_ROOT="$tmp/root" FACTORY_PROJECT="$tmp/host"
export PIP_DISABLE_PIP_VERSION_CHECK=1
mkdir -p "$HOME"
unset FACTORY_TEST_MODE FACTORY_LANE_ROOT

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok - %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL - %s\n' "$1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$3', got '$2')"; }
assert_contains() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected to contain '$3', got: $2)" ;; esac; }
assert_lacks() { case "$2" in *"$3"*) bad "$1 (did not expect '$3', got: $2)" ;; *) ok "$1" ;; esac; }

# run CMD...: stdout in $OUT, stderr in $ERR, exit status in $RC.
run() {
  set +e
  "$@" > "$tmp/out" 2> "$tmp/err"
  RC=$?
  set -e
  OUT="$(cat "$tmp/out")"
  ERR="$(cat "$tmp/err")"
}

# make_wheel DIR NAME VERSION: a pure-Python wheel with one module, NAME.py,
# exposing VERSION (the RECORD pip insists on included). Prints the wheel's path.
make_wheel() {
  mkdir -p "$1"
  python3 - "$@" <<'PY'
import base64, hashlib, sys, zipfile
dest, name, version = sys.argv[1:4]
info = f"{name}-{version}.dist-info"
files = {
    f"{name}.py": f'VERSION = "{version}"\n',
    f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
    f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
}
digest = lambda text: base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
files[f"{info}/RECORD"] = "".join(f"{n},sha256={digest(t)},{len(t)}\n" for n, t in files.items()) + f"{info}/RECORD,,\n"
path = f"{dest}/{name}-{version}-py3-none-any.whl"
with zipfile.ZipFile(path, "w") as z:
    for n, t in files.items():
        z.writestr(n, t)
print(path)
PY
}

wheels="$FACTORY_PROJECT/.factory/wheels"
whl="$(make_wheel "$wheels" factorystub 1.0)"
sum="$(sha256sum "$whl" | cut -d' ' -f1)"
whl2="$(make_wheel "$wheels" factorystub 2.0)"
sum2="$(sha256sum "$whl2" | cut -d' ' -f1)"

# The project is a lane clone: no .factory/ in it, the wheels are the host project's.
proj="$tmp/lane/app"
mkdir -p "$proj"
echo 'import factorystub; print("stub", factorystub.VERSION)' > "$proj/check.py"
printf 'factorystub==1.0 \\\n    --hash=sha256:%s\n' "$sum" > "$proj/requirements.txt"

# --- 1. install and run ------------------------------------------------------------
run "$FTEST" -C "$proj" -- python3 check.py
assert_eq "python mode: exit 0" "$RC" 0
assert_eq "python mode: the command sees the installed wheel" "$OUT" "stub 1.0"
assert_contains "python mode: the mode line names the venv and the file" "$ERR" "mode: Python venv (requirements.txt"
assert_contains "python mode: the install is announced" "$ERR" "installing requirements.txt offline"
assert_eq "python mode: the project gets no .venv" "$(test -e "$proj/.venv" && echo yes || echo no)" no
copy="$(run "$FTEST" -C "$proj"; echo "$OUT")"
assert_eq "python mode: the venv is in the copy" "$(test -x "$copy/.venv/bin/python" && echo yes)" yes
run "$FTEST" -C "$proj" -- sh -c 'command -v python; echo "$VIRTUAL_ENV"'
assert_eq "python mode: the venv's bin is first on PATH" "$(printf '%s\n' "$OUT" | head -n 1)" "$copy/.venv/bin/python"
assert_eq "python mode: VIRTUAL_ENV is set" "$(printf '%s\n' "$OUT" | tail -n 1)" "$copy/.venv"

# --- 2. the stamp ------------------------------------------------------------------
run "$FTEST" -C "$proj" -- python3 check.py
assert_lacks "stamp: an unchanged requirements file isn't reinstalled" "$ERR" "installing"
assert_eq "stamp: still works" "$OUT" "stub 1.0"
printf 'factorystub==2.0 \\\n    --hash=sha256:%s\n' "$sum2" > "$proj/requirements.txt"
run "$FTEST" -C "$proj" -- python3 check.py
assert_contains "stamp: a changed requirements file reinstalls" "$ERR" "installing requirements.txt"
assert_eq "stamp: the new version is installed" "$OUT" "stub 2.0"

# --- 3. which file ------------------------------------------------------------------
printf 'factorystub==1.0 \\\n    --hash=sha256:%s\n' "$sum" > "$proj/requirements-dev.txt"
run "$FTEST" -C "$proj" -- python3 check.py
assert_eq "choice: requirements.txt wins over requirements-dev.txt" "$OUT" "stub 2.0"
run "$FTEST" -C "$proj" -r requirements-dev.txt -- python3 check.py
assert_contains "choice: -r names the other file" "$ERR" "mode: Python venv (requirements-dev.txt"
assert_eq "choice: -r installs that file" "$OUT" "stub 1.0"
rm "$proj/requirements.txt"
run "$FTEST" -C "$proj" -- python3 check.py
assert_eq "choice: requirements-dev.txt is the fallback" "$OUT" "stub 1.0"

# --- 4. failures -------------------------------------------------------------------
run "$FTEST" -C "$proj" -r nope.txt -- true
assert_eq "failure: a missing -r file is refused" "$RC" 1
assert_contains "failure: names the missing file" "$ERR" "no such requirements file"
run "$FTEST" -C "$proj" -r ../other.txt -- true
assert_eq "failure: -r outside the project is refused" "$RC" 1
assert_contains "failure: says why" "$ERR" "inside"
echo 'factorystub==1.0' > "$proj/loose.txt"
run "$FTEST" -C "$proj" -r loose.txt -- true
assert_eq "failure: an unpinned -r file is refused" "$RC" 1
assert_contains "failure: asks for hashes" "$ERR" "no --hash= pins"

bad_proj="$tmp/lane/bad"
mkdir -p "$bad_proj"
printf 'factorystub==1.0 \\\n    --hash=sha256:%064d\n' 0 > "$bad_proj/requirements.txt"
run "$FTEST" -C "$bad_proj" -- echo should-not-run
assert_eq "failure: a wheel whose hash doesn't match fails the run" "$RC" 1
assert_lacks "failure: the command doesn't run after a failed install" "$OUT" "should-not-run"
assert_contains "failure: points at pip_download" "$ERR" "pip_download"
assert_eq "failure: no half-made venv is left" "$(find "$FACTORY_TEST_ROOT" -path '*bad*' -name '.venv' | wc -l | tr -d ' ')" 0

printf 'factorystub==1.0 \\\n    --hash=sha256:%s\n' "$sum" > "$bad_proj/requirements.txt"
FACTORY_PROJECT="$tmp/nowhere" run "$FTEST" -C "$bad_proj" -- echo should-not-run
assert_eq "failure: no wheel folder fails the run" "$RC" 1
assert_contains "failure: names pip_download" "$ERR" "no wheel folder"

unpinned="$tmp/lane/unpinned"
mkdir -p "$unpinned"
echo factorystub==1.0 > "$unpinned/requirements.txt"
run "$FTEST" -C "$unpinned" -- echo ran
assert_eq "unpinned requirements.txt: project files only" "$OUT" "ran"
assert_contains "unpinned requirements.txt: says it was ignored" "$ERR" "ignoring requirements.txt"
assert_contains "unpinned requirements.txt: mode line" "$ERR" "mode: project files only"

# --- 5. other projects -------------------------------------------------------------
node_proj="$tmp/lane/node"
mkdir -p "$node_proj"
cp "$proj/requirements-dev.txt" "$node_proj/requirements.txt"
echo '{"name":"x","version":"1.0.0","lockfileVersion":3,"requires":true,"packages":{"":{"name":"x","version":"1.0.0"}}}' > "$node_proj/package-lock.json"
echo '{"name":"x","version":"1.0.0"}' > "$node_proj/package.json"
run "$FTEST" -C "$node_proj" -- true
assert_contains "node lockfile wins over a requirements file: the npm path runs" "$ERR" "npm_fetch"
assert_lacks "node lockfile wins: no venv made" "$ERR" "Python venv"

plain="$tmp/lane/plain"
mkdir -p "$plain"
echo 'package main' > "$plain/main.go"
run "$FTEST" -C "$plain" -- ls
assert_contains "no lockfile, no requirements: project files only" "$ERR" "mode: project files only"
assert_eq "no lockfile, no requirements: the files are copied" "$OUT" "main.go"

echo "---"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
