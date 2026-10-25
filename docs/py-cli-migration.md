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
| `factory logs` (and `log`), `packet` (and `packets`): step 4, part 2 | 4 (part) |
| Bash `in_list` takes one exact word, as Python's `_in_list` (no new command) | done |
| `factory doctor`: `factory/commands/doctor.py`, with the helpers it uses in `factory/preflight.py` (`scripts/lib/preflight.sh` ported, the bash stays for `install-prereqs.sh`), `factory/sbx.py` and `factory/policy.py`; `disabled_role_sandboxes` in `factory/roles.py` | 4 (part 1) |
| `factory status`: `factory/commands/status.py`, with `factory/keychain.py` (`keychain_has` and the lookups it uses), `factory/approvals.py` (the approvals summary and status line, shared with `doctor`), `require_sbx` in `factory/sbx.py` and the Jira/npm token checks in `factory/policy.py`; tests in `factory/test_command_status.py` | 4 (part 3) |
| `factory secret` (`save`, `forget`, `set`, `rm`, `ls`): `factory/commands/secret.py`, with the `scripts/lib/secrets.sh` helpers it uses in `factory/secrets.py` and the keychain ones in `factory/keychain.py` (shared with `status`; the bash stays for `start-broker.sh` and `keychain_warn_missing`) | 5 |

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
4. **Read-only inspection**: `doctor`, `logs`, `packet` and `status` are done.
   `factory/test_command_doctor.py` replaces the doctor half of `test_factory_doctor.sh`, and
   `factory/test_command_status.py` replaces the status parts of `test_factory_helpers.sh`
   (the approvals line) and `test_factory_layout.sh` (the local-settings line). What tests bash
   helpers `start` still calls (`require_project_commits`, `sbx_not_signed_in`, `run_or_die`)
   stays shell as `scripts/test_factory_helpers.sh` until `start` is ported.
   `logs` and `packet` live in `factory/commands/logs.py` and `packet.py`; `logs` hands following
   to `tail -F` (it already survives rotation and heads each file) and formats the audit lines in
   Python, so it no longer needs `jq`.
   `status` keeps the bash output byte for byte (checked against the bash it replaced on about 30
   fixtures, plain and on a terminal, including exit codes and stderr), with these deliberate
   differences (1) `sbx version` failing outright is reported as "sbx 0.45.1 required, found
   'unknown'" where bash exited silently (`require_sbx`); (2) the approvals line now only knows
   the staged-only flow of #170 (staged, approved/being written, done, failed; no held or waiting
   lane state, no `hold_seconds`); (3) when `sbx ls --json` fails or returns something that is not
   a sandbox list, bash exited silently (under `pipefail`, with sbx's own exit status and whatever
   sbx and jq wrote to stderr), while Python stops with "error: could not list the sandboxes ..."
   and exit 1; (4) `running`/`stopped` are coloured only in the status column, where the old `sed`
   coloured the first match anywhere on the line. Its helpers went with it: `cmd_status`, `lanes_label`,
   `approvals_summary`, `approvals_status_line` (bin/factory) and `local_settings_present`
   (`common.sh`) are deleted, since nothing else in bash called them. The bash `policy_value`,
   `policy_string`, `gitlab_*`, `jira_cloud_id_set`, `any_jira_tool_granted`,
   `npm_private_registry_needs_token`, `setup_done`, `require_sbx` and the `secrets.sh` keychain
   helpers stay while `start` calls them; `factory/test_policy.py`,
   `test_sbx.py`, `test_keychain.py` and `test_preflight.py` run bash and Python on the same
   fixtures.
5. **`secret`**: done. `factory/commands/secret.py` holds `save`, `forget`, `set`, `rm` and `ls`;
   `factory/secrets.py` the helpers they use (names, delivery and revocation over `sbx exec`, the
   `factory secret set` markers, the per-name and GitHub keychain sync). The keychain itself
   (lookup, has, store, delete and the Credential Manager scripts) is one copy in
   `factory/keychain.py`, which `status` also uses; running a program with a value on its stdin is
   `factory/programs.py`. `keychain_backend` and `wincred_powershell` were already in
   `factory/preflight.py`, and the version check is `sbx.require_sbx`. A value is bytes and goes only to a program's stdin; the hidden prompt
   turns echo off before it prints. `scripts/lib/secrets.sh` stays: `start-broker.sh` calls
   `secret_sync_from_keychain`, `github_sync_from_keychain` and `secret_clear_all_manual` on every
   start, and `bin/factory`'s `keychain_warn_missing` (for `start`/`status`) calls `keychain_has`.
   `factory/test_secrets.py` runs the bash and the Python helpers on the same fakes and requires the
   same statuses, values, messages and argv (the delivery and PowerShell scripts byte for byte);
   it also tests `secret_clear_all_manual` and `keychain_warn_missing`, which have no Python copy
   yet. `factory/test_command_secret.py` replaces `test_factory_secret.sh`; it passed against the
   bash `cmd_secret` before that was deleted. Deliberate differences, all on failure paths:
   - when the marker can't be written, bash also printed `mkdir`'s or the shell's own error before
     its `error:` line; Python prints only the `error:` line;
   - a program that is not installed is reported as `<name>: command not found`, without bash's
     `line N:` prefix;
   - what a program prints is passed on once it exits rather than as it runs (no visible change:
     the programs involved print at most a line);
   - a NUL byte in a value is kept (bash's `$(...)` dropped it with a warning);
   - delivery and revocation check the name again themselves (bash relied on its callers).
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
