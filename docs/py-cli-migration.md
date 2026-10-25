# Moving the host CLI from bash to Python

Plan for doomtickle/passdesk-ops#169 (part of #167). The aim: one `factory` Python
package (standard library only, 3.9+), one entrypoint, `bin/factory` reduced to a
thin shim, one `role.conf` parser and one `project_repos` shared with the dispatch
bridge, and the shell tests ported. It is done one command group per pull request,
bash and Python side by side, and `main` works after every one.

## How it works now

- `factory/` is the package (`python3 -m factory`, or `python3 <checkout>/factory`,
  which is what the shim runs). `factory/cli.py` holds `COMMANDS`, the table of
  ported commands; each lives in `factory/commands/<group>.py` as `run(ctx, args)`.
- `bin/factory` lists the same names in `PORTED_COMMANDS` and `exec`s the package for
  them; every other command still runs the bash. A test keeps the two lists equal.
- Shared core already in Python: `factory/conf.py` (the `KEY="value"` parser),
  `factory/roles.py` (role lookups, overrides, enabled roles), `factory/repos.py`
  (`project_repos`), `factory/context.py` (paths, styles). The dispatch bridge
  imports the first three. Until the bash that uses `scripts/lib/roles.sh` and
  `common.sh` is gone, tests compare the Python against those helpers on the same
  fixtures (`factory/test_*.py`, `scripts/test_project_repos.sh`).
- Tests: `python3 -m unittest discover -s factory -t . -p 'test_*.py'`.
- A ported command's bash function is deleted in the same pull request; the `lib/*.sh`
  helpers go only once nothing in bash calls them.

## Ported

| Command | Pull request |
|---|---|
| `factory roles` | 1 (foundation) |
| `factory analytics`, `watch`, `docs`, `dashboard`/`gui`, `config init`, `role new` | 2 |

## Remaining, in order

1. **`approvals`** (wrapper only, after #170 changes its held-write wording). The rest
   of this step (`analytics`, `docs`, `dashboard`/`gui`, `watch`, `config init`,
   `role new`) is ported (PR 2): the wrappers `exec` their script, so signals reach it,
   and `role new` still calls the bash `validate_role` until step 2 replaces it
   (`factory/roles.py` already has `valid_role_name`, checked against it by a test).
2. **Role validation** (`validate_role`, `validate_role_overrides`,
   `validate_enabled_roles`, `role_lane_names`, `coordinator_broker_tools` from
   `scripts/lib/roles.sh`) into `factory/roles.py`, with the shell tests that cover them
   (`test_factory_role_overrides.sh`) ported. Everything after this depends on it.
3. **Read-only inspection**: `doctor` (with `scripts/lib/preflight.sh`), `status`,
   `logs`, `packet`. `status` waits for #170.
4. **`secret`** (`save`, `forget`, `set`, `rm`, `ls`, with `scripts/lib/secrets.sh`);
   `test_factory_secret.sh` is the biggest test to port, so it is a pull request by itself.
5. **Run state**: sandbox fingerprints and `stale_sandboxes` (`common.sh`), then
   `stop`, `restart`, `reset`, `chat`, with `03-stop-factory.sh`.
6. **`start`**, with `start-broker.sh`, `start-dispatch.sh` and `02-launch-factory.sh`
   (the riskiest: it creates sandboxes and delivers credentials; the credential rules in
   `CLAUDE.md` apply unchanged, argv lists only).
7. **`setup` and `update`**, with `00-build-templates.sh`, `01-init-security.sh`,
   `install-prereqs.sh`.
8. **Finish**: delete `scripts/lib/*.sh` and the bash tests, leave `bin/factory` as a shim
   with no `PORTED_COMMANDS` list (it always runs Python), drop the parity tests, update
   `scripts/lint.sh`. `templates/agent/factory-lane` and `factory-test` stay bash: they run
   inside the sandboxes, not on the host.

Each pull request: port the commands and the helpers only they use, port their shell
tests (or note why a test must stay shell), update `PORTED_COMMANDS` and `COMMANDS`, delete
the bash function, and keep the output byte-identical unless the pull request says otherwise.
