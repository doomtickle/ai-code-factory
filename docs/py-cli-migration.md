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
  `factory/roles.py` (role lookups, overrides, enabled roles, role validation), `factory/repos.py`
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
| `factory analytics`, `approvals` (and `approve`), `watch`, `docs`, `dashboard`/`gui`, `config init`, `role new` | 2 |
| Role validation core (`validate_role`, `validate_role_overrides`, `validate_enabled_roles`, `role_lane_names`, `coordinator_broker_tools`; no new command; `role new` uses it) | 3 |
| Bash `in_list` takes one exact word, as Python's `_in_list` (no new command) | done |
| `factory doctor`: `factory/commands/doctor.py`, with the helpers it uses in `factory/preflight.py` (`scripts/lib/preflight.sh` ported, the bash stays for `install-prereqs.sh`), `factory/sbx.py` and `factory/policy.py`; `disabled_role_sandboxes` in `factory/roles.py` | 4 (part 1) |

## Remaining, in order

1. **Wrappers of Python scripts and small commands**: done (PR 2).
2. **Role validation**: done (PR 3). `validate_role`, `validate_role_overrides`,
   `validate_enabled_roles`, `role_lane_names` and `coordinator_broker_tools` are in
   `factory/roles.py` (with `known_broker_tools`), and `role new` calls them. The bash
   copies in `scripts/lib/roles.sh` stay until steps 4, 6 and 7 port their callers
   (`start-broker.sh`, `start-dispatch.sh`, `02-launch-factory.sh`, and `bin/factory`'s
   `doctor`, `start`, `status` and `logs`); `factory/test_roles_validation.py` runs both on the same fixtures
   and requires the same refusals and messages. It replaced `test_factory_role_overrides.sh`.
   `disabled_role_sandboxes` moved to Python with `doctor` (step 4) and has no bash copy.
3. **Tighten bash `in_list`**: done. It returns false when `$1` is empty or holds a space, tab
   or newline, so `EFFORT="low medium"`, `WORKSPACE="rw ro"`, `TEMPLATE="developer reviewer"` and
   `OPTIONAL="0 1"` fail in bash as they do in Python, and `factory/test_roles_validation.py`
   requires the same refusal and message from both. This did not move the security boundary:
   those keys are not overridable.
4. **Read-only inspection**: `doctor` is done (part 1): `factory/test_command_doctor.py` replaces
   the doctor half of `test_factory_doctor.sh`; what tests bash helpers still in `bin/factory`
   (`logs`, `require_project_commits`, `sbx_not_signed_in`, `run_or_die`, `approvals_status_line`)
   stays shell as `scripts/test_factory_helpers.sh` until `status`/`logs`/`start` are ported.
   `status`, `logs` and `packet` remain. `status` waits for #170. The bash `policy_value`,
   `gitlab_*`, `setup_done` and `approvals_summary` stay while `start`/`status` call them;
   `factory/test_policy.py` and `factory/test_preflight.py` run bash and Python on the same
   fixtures.
5. **`secret`** (`save`, `forget`, `set`, `rm`, `ls`, with `scripts/lib/secrets.sh`);
   `test_factory_secret.sh` is the biggest test to port, so it is a pull request by itself.
6. **Run state**: sandbox fingerprints and `stale_sandboxes` (`common.sh`), then
   `stop`, `restart`, `reset`, `chat`, with `03-stop-factory.sh`.
7. **`start`**, with `start-broker.sh`, `start-dispatch.sh` and `02-launch-factory.sh`
   (the riskiest: it creates sandboxes and delivers credentials; the credential rules in
   `CLAUDE.md` apply unchanged, argv lists only).
8. **`setup` and `update`**, with `00-build-templates.sh`, `01-init-security.sh`,
   `install-prereqs.sh`.
9. **Finish**: delete `scripts/lib/*.sh` and the bash tests, leave `bin/factory` as a shim
   with no `PORTED_COMMANDS` list (it always runs Python), drop the parity tests, update
   `scripts/lint.sh`. `templates/agent/factory-lane` and `factory-test` stay bash: they run
   inside the sandboxes, not on the host.

Each pull request: port the commands and the helpers only they use, port their shell
tests (or note why a test must stay shell), update `PORTED_COMMANDS` and `COMMANDS`, delete
the bash function, and keep the output byte-identical unless the pull request says otherwise.
