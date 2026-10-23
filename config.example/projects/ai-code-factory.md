- `docs/reference/sbx-cli.md` (the owner generates it on the host) is the only
  verified `sbx` reference: a command or flag not in it, or any use while it
  still says "not generated", is unverified.
- Docs pages: build one with `python3 scripts/docs.py --root <lane> --out
  /tmp/docs.html`, then `factory-shot` it.
- Tests: `python3 -m unittest discover -s dispatch`, `python3 -m unittest discover
  -s scripts -p 'test_*.py'` and `bash scripts/test_<name>.sh`, each through
  `factory-test -q --`. Shell scripts must pass `bash -n` and keep `set -euo pipefail`.
