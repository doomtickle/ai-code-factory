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
  `INTERNAL` next to it holds entry points the bash scripts run (`factory _start-dispatch`):
  dispatched the same way, but not commands a person types, so not in `COMMANDS`,
  `PORTED_COMMANDS` or the usage text.
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
| `factory chat` (and `coordinator`), `stop` (and `down`), `restart`, `reset`: `factory/commands/runstate.py`, with `runs_sandbox` and `coordinator_attached` in `factory/sbx.py`, `Context.confirm` and an `exec` that replaces the process (`os.execvpe`); `03-stop-factory.sh` stays shell; tests in `factory/test_command_runstate.py` and `factory/test_sbx.py` | 6 |
| `factory _start-dispatch` (step 7, part A): `factory/dispatch_start.py`, with `migrate_state` in `factory/state.py` and `policy_toml_string` and `lane_identity_problem` in `factory/policy.py`; `scripts/start-dispatch.sh` is a shim that runs it; tests in `factory/test_dispatch_start.py` and `factory/test_state.py` | 7 (part A) |

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
6. **Run state**: done. `chat`, `stop`, `restart` and `reset` are in
   `factory/commands/runstate.py`.
   - `chat` ends in `ctx.exec` (`os.execvpe`), so the terminal goes straight to `sbx run`. Its
     attached check is `sbx.coordinator_attached` (`ps -axo args=`, a failing `ps` reads as nobody),
     which uses `sbx.runs_sandbox`, the dashboard's rule (`scripts/dashboard_term.py`
     `runs_sandbox`) copied; `factory/test_sbx.py` runs both on the same fixtures and requires the
     same answers. The awk `coordinator_attached` in `common.sh` is deleted, since nothing else
     called it.
   - `stop` and `reset` run `scripts/03-stop-factory.sh` as it is. **It stays shell**: `update` (bin/factory) also runs it, the docs
     tell people to run it by hand, it runs `migrate_state` and needs `jq`, and a port would
     only move its `ps` and `sbx stop` lines without removing any bash. Python takes only its exit
     status. It goes with `common.sh` in step 9.
   - `restart` runs the stop script, then the bash `start` as the fixed argv
     `[<checkout>/bin/factory, start, <project>, --no-dashboard]` (no shell). It moves to a direct
     call when `start` is ported (step 7).
   - `reset` asks through `Context.confirm` (a tty on stdin; `y`, `yes`, any case), as bash did. Bash's
     project switch in `cmd_start` now runs `python3 factory reset --yes`.
   - The bash `rm_failed_after_stop` stays for `cmd_update` (step 8); the Python copy is
     `runstate.rm_failed_after_stop`, and `factory/test_command_runstate.py` compares the two
     messages (reset and update wording, including the trailing space after each name).
   - **Sandbox fingerprints and `stale_sandboxes` are not ported here**: only `start` and `update`
     use them, so they go with steps 7 and 8.
   - Tests: the usage and no-project parts of `test_factory_restart.sh`, the reset parts of
     `test_factory_state.sh`, `test_factory_chat.sh` and Part 6 of `test_factory_layout.sh`
     (`coordinator_attached`) are now Python and deleted. What stays shell: the restart that runs the
     real bash `start` and the real stop script against real sleeping processes
     (`test_factory_restart.sh`), the stale saved layout, and the `cmd_start` project switch calling
     Python `reset --yes` (`test_factory_state.sh` 2c), because they test bash that is not ported.
   - Output is byte-identical (16 scenarios compared old against new: status, stdout, stderr,
     sbx calls and state). One difference, on a failure path: if removing a `project` file fails for a
     reason other than "not there" (a directory in its place, a permission error), Python stops with
     `error: could not remove <path>: <reason>` and exit 1 (a directory reads "Is a directory" on every host);
     bash's `rm -f` printed its own message and, under `set -e`, exited with rm's status. Session
     files are best effort in both.
7. **`start`**, with `start-broker.sh`, `start-dispatch.sh` and `02-launch-factory.sh`
   (the riskiest: it creates sandboxes and delivers credentials; the credential rules in
   `CLAUDE.md` apply unchanged, argv lists only). Done in three parts:
   - **Part A, done: `start-dispatch.sh`.** `factory/dispatch_start.py` (`main(ctx, argv)`) does
     the validation, the lane identity check and snapshot, the snapshots into `FACTORY_STATE`
     (role instructions plus project notes, role overrides, enabled-roles, the docs), `running_pid`
     and `port_holder`, and the bridge restart and readiness wait. `scripts/start-dispatch.sh` is
     now `exec "$(resolve_python)" "$ROOT/factory" _start-dispatch "$@"`, which `02-launch-factory.sh`
     still calls. Snapshots stay atomic where they were (a `.tmp` folder renamed into place), a new
     file takes the source's mode less the umask as `cp` did, and the token goes only to
     `sbx secret set`'s stdin. `migrate_state` is also in Python (`factory/state.py`); the bash one
     stays for `start-broker.sh` and `03-stop-factory.sh`, and `factory/test_state.py` runs both on
     the same fixtures. `policy_toml_string`, `lane_identity_problem` and `write_lane_identity` were
     only this script's, so they are deleted from `common.sh` (the Python rules are in
     `factory/policy.py`; their shell tests are in `factory/test_dispatch_start.py`). The old and
     new scripts were compared on 17 scenarios (fresh, rerun, another project, usage errors, wrong
     sbx, failed mint over an old hash, bad override, bad identity, missing `tools.txt`, a bridge
     that exits or never answers, a failed `mcp add`, a stranger on the port) and 37 identity
     policies: same output, exit status, sbx calls and files, modes included. Deliberate
     differences, all on failure paths: (1) `curl`, `openssl` and `sha256sum`/`shasum` are no longer
     needed, since the readiness probes talk to 127.0.0.1 directly (no proxy) and the token and its
     hash come from `secrets` and `hashlib`, so there is no "not found in PATH" error for them;
     (2) the usage line names `<checkout>/scripts/start-dispatch.sh` where bash printed `$0`;
     (3) a file operation that fails prints `error: <path>: <reason>` and exits 1 (bash printed its
     tool's message, then died under `set -e`); (4) a failing `kill` of an old bridge (here and in
     `migrate_state`) stops with `error: could not stop the dispatch bridge (<pid>): <reason>`.
   - **Part B: `start-broker.sh`** (runs in parallel, adds `"_start-broker"` to `INTERNAL`).
   - **Part C: `02-launch-factory.sh`, `cmd_start`, `onboarding_hints`**, and the sandbox
     fingerprints and `stale_sandboxes` that step 6 left for here.
8. **`setup` and `update`**, with `00-build-templates.sh`, `01-init-security.sh`,
   `install-prereqs.sh`.
9. **Finish**: delete `scripts/lib/*.sh` and the bash tests, leave `bin/factory` as a shim
   with no `PORTED_COMMANDS` list (it always runs Python), drop the parity tests, update
   `scripts/lint.sh`. `templates/agent/factory-lane` and `factory-test` stay bash: they run
   inside the sandboxes, not on the host.

Each pull request: port the commands and the helpers only they use, port their shell
tests (or note why a test must stay shell), update `PORTED_COMMANDS` and `COMMANDS`, delete
the bash function, and keep the output byte-identical unless the pull request says otherwise.
