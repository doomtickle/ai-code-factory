- `sbx` is not installed in the factory's sandboxes. Before using or documenting
  an `sbx` command or flag, check `docs/reference/sbx-cli.md` (the owner generates
  it on the host). Anything not in it, or any use while it still says "not
  generated", is unverified: say so in your report instead of guessing. Reviewers:
  flag such a command or flag in the change as unverified.
- Docs pages: build one with `python3 scripts/docs.py --root <lane> --out
  /tmp/docs.html`, then `factory-shot` it.
- Tests: `python3 -m unittest discover -s dispatch`, `python3 -m unittest discover
  -s scripts -p 'test_*.py'` and `bash scripts/test_<name>.sh`, each through
  `factory-test -q --`. Shell scripts must pass `bash -n` and keep `set -euo pipefail`.
