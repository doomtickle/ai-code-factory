#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/ci-changes.sh BASE HEAD     files changed from BASE to HEAD (git diff BASE...HEAD)
#        scripts/ci-changes.sh --stdin       the changed file names, one per line, on stdin
#        scripts/ci-changes.sh               no range: everything runs
#
# The `changes` job of .github/workflows/ci.yml runs this and appends its output to
# $GITHUB_OUTPUT. Prints, one per line:
#
#   images=  the role and broker images need building and testing (templates/, kits/, broker/ code,
#            the build code and its pins, ci.yml)
#   broker=  the broker's own image tests need running (broker/ code, ci.yml)
#   code=    something other than prose changed, so the test suites run
#   docs=    README.md or docs/ changed; scripts/test_docs.py, scripts/test_dashboard.py (it builds
#            the real docs and checks the dashboard's help links against docs/guides/06-dashboard.md),
#            factory/test_command_docs.py's RealRenderTest and scripts/test_sbx_reference.sh read
#            those, so a docs-only change still runs them
#   field3d= the Thoughtfield's 3D code, the browser tests of it, or what they run on changed, so the visual
#            browser shards (the slow ones, software-rendered; see ci.yml's `visual shard` job) run on this
#            pull request. They also run nightly and on demand, whatever this says.
#
# Each is true or false. It is false only when the file list is complete and every file is known to
# be irrelevant. Anything this can't tell (no range, a git failure, an empty list, a file it has no
# rule for) gives true: when unsure, run.

all_true() {
  printf 'images=true\nbroker=true\ncode=true\ndocs=true\nfield3d=true\n'
}

# Prose that no test, build or page reads.
is_inert() {
  case "$1" in
    LICENSE | */LICENSE | CLAUDE.md | */CLAUDE.md | AGENTS.md | */AGENTS.md) return 0 ;;
    broker/docs/* | broker/README.md) return 0 ;;
  esac
  return 1
}

# Prose that scripts/test_docs.py, scripts/test_dashboard.py, RealRenderTest in
# factory/test_command_docs.py and scripts/test_sbx_reference.sh read from the repo.
is_docs() {
  case "$1" in
    README.md | docs/*) return 0 ;;
  esac
  return 1
}

# What the image build, and the tests run in the images, depend on. factory/build_templates.py
# imports other factory modules, but a break in those fails the Python suites; these are the ones
# whose real effect only a docker build shows.
is_image_input() {
  case "$1" in
    templates/* | kits/* | broker/*) return 0 ;;
    factory/build_templates.py | factory/pins.py | factory/sbx.py | factory/__main__.py) return 0 ;;
    scripts/lib/common.sh | scripts/00-build-templates.sh | .dockerignore) return 0 ;;
    .github/workflows/ci.yml) return 0 ;;
  esac
  return 1
}

is_broker_input() {
  case "$1" in
    broker/* | .github/workflows/ci.yml) return 0 ;;
  esac
  return 1
}

# The Thoughtfield's 3D page and what draws it, the browser tests that drive it, and the CI setup they run in
# (Chrome's pin, the shard layout). scripts/test_ci_shards.py checks this list names the files that matter.
is_field3d_input() {
  case "$1" in
    scripts/dashboard-ui/field-sky.js | scripts/dashboard-ui/field-face.js | scripts/dashboard-ui/field-order.js) return 0 ;;
    scripts/dashboard-ui/field.html | scripts/dashboard-ui/field.css) return 0 ;;
    scripts/dashboard-ui/witness/* | scripts/dashboard-ui/vendor/three/*) return 0 ;;
    scripts/test_field_browser.js | scripts/test_field_browser.py | scripts/browser_test_support.py) return 0 ;;
    scripts/ci-chrome.sh | templates/developer/Dockerfile) return 0 ;;
    .github/workflows/ci.yml | .github/actions/browser-shard/*) return 0 ;;
  esac
  return 1
}

classify() { # classify < file names
  local images=false broker=false code=false docs=false field3d=false seen=0 f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    seen=1
    if is_inert "$f"; then
      continue
    fi
    if is_docs "$f"; then
      docs=true
      continue
    fi
    code=true
    ! is_image_input "$f" || images=true
    ! is_broker_input "$f" || broker=true
    ! is_field3d_input "$f" || field3d=true
  done
  if [ "$seen" = 0 ]; then
    all_true
    return
  fi
  printf 'images=%s\nbroker=%s\ncode=%s\ndocs=%s\nfield3d=%s\n' "$images" "$broker" "$code" "$docs" "$field3d"
}

case "$#:${1:-}" in
  1:--stdin) classify ;;
  2:*)
    # No fsmonitor or hooks; --no-renames lists both sides of a rename.
    if names="$(git -c core.fsmonitor=false -c core.hooksPath=/dev/null diff --name-only --no-renames "$1...$2" -- 2>/dev/null)"; then
      printf '%s\n' "$names" | classify
    else
      echo "ci-changes: git diff $1...$2 failed; running everything" >&2
      all_true
    fi
    ;;
  0:) all_true ;;
  *) echo "usage: $0 [BASE HEAD | --stdin]" >&2; exit 2 ;;
esac
