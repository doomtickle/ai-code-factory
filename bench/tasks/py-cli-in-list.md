---
name: py-cli-in-list
base: 873ed8b70fe229b459d27585201b774197cdaedc
reference: 0348d510303186d8fb0b42a5975378b6ff90ef69
tier: holdout
area: cli
---
Work only from commit 873ed8b70fe229b459d27585201b774197cdaedc (`base`). Do not look at any later history, other branches or refs, or the reflog; solve this from the code as it stands at `base`.

CLI: make bash's `in_list` as strict as the Python role validation.

Goal: `docs/py-cli-migration.md` step 3 lists this as the next item. The factory validates `role.conf` values in two places while the CLI is being ported from bash to Python: `scripts/lib/roles.sh` (`in_list`) and `factory/roles.py` (`_in_list`). They disagree: bash's `in_list` accepts a value of several adjacent words if they appear together in the list (`EFFORT="low medium"` passes against `low medium high`), while Python takes one exact word. A value like `TEMPLATE="developer reviewer"` or `WORKSPACE="rw ro"` should be refused by both, with the same message. Make bash match Python, and turn the test that currently asserts "Python is stricter" into a parity test.

Files: `scripts/lib/roles.sh`, `factory/roles.py` (its docstring only), `factory/test_roles_validation.py`, `docs/py-cli-migration.md` (mark step 3 done, update the table and the note about the difference).

Known facts:
- `in_list WORD "LIST"` succeeds if WORD is one of the space-separated words of LIST. After the change an empty WORD, or one holding a space, tab or newline, is never in the list, in bash and in Python alike; an empty LIST contains nothing.
- These keys are not overridable by a user's tuning file, so this does not move a security boundary. Don't claim it does.
- Shell rules: `set -euo pipefail` stays, `bash -n` clean, shellcheck-clean; bash must stay compatible with macOS's bash 3.2.
- Python code in `factory/` is standard library only and runs on Python 3.9.

Done when:
- In bash, `in_list` returns failure for an empty word and for a word containing a space, tab or newline, and still succeeds for each single exact word of the list.
- A `role.conf` with `TEMPLATE`, `WORKSPACE`, `EFFORT` or `OPTIONAL` set to several words, or to nothing where nothing isn't allowed, is refused by the Python validator and by the bash one with the same error text.
- `docs/py-cli-migration.md` says the tightening is done and no longer says Python is deliberately stricter.
Tests required: in `factory/test_roles_validation.py`, replace the "Python is stricter" test by one that checks the refusal and message for the several-words and empty cases, and add a table test that runs bash `in_list` and Python `_in_list` on the same words and lists (single words, multi-word, empty, tab, newline, empty list) and requires the same answer. Run that file and `bash -n scripts/lib/roles.sh`.
Out of scope: any other bash-to-Python port, `role.conf` keys, the overridable-keys list.
Risk: low
