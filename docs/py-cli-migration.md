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
- A host script that is ported keeps its path and arguments as a thin shim that `exec`s
  `python3 <checkout>/factory _<name> "$@"`. Those entry points are in `INTERNAL` in
  `factory/cli.py`: dispatched like `COMMANDS`, but not in `COMMANDS`, `PORTED_COMMANDS` or any
  usage text (one entry per line, in alphabetical order).
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
| `factory doctor`: `factory/commands/doctor.py`, with the helpers it uses in `factory/preflight.py` (`scripts/lib/preflight.sh` ported; deleted in step 8 part A), `factory/sbx.py` and `factory/policy.py`; `disabled_role_sandboxes` in `factory/roles.py` | 4 (part 1) |
| `factory status`: `factory/commands/status.py`, with `factory/keychain.py` (`keychain_has` and the lookups it uses), `factory/approvals.py` (the approvals summary and status line, shared with `doctor`), `require_sbx` in `factory/sbx.py` and the Jira/npm token checks in `factory/policy.py`; tests in `factory/test_command_status.py` | 4 (part 3) |
| `factory secret` (`save`, `forget`, `set`, `rm`, `ls`): `factory/commands/secret.py`, with the `scripts/lib/secrets.sh` helpers it uses in `factory/secrets.py` and the keychain ones in `factory/keychain.py` (shared with `status`; the bash stays for `keychain_warn_missing`) | 5 |
| `factory chat` (and `coordinator`), `stop` (and `down`), `restart`, `reset`: `factory/commands/runstate.py`, with `runs_sandbox` and `coordinator_attached` in `factory/sbx.py`, `Context.confirm` and an `exec` that replaces the process (`os.execvpe`); `03-stop-factory.sh` stays shell; tests in `factory/test_command_runstate.py` and `factory/test_sbx.py` | 6 |
| `factory _start-dispatch` (step 7, part A): `factory/dispatch_start.py`, with `migrate_state` in `factory/state.py` and `policy_toml_string` and `lane_identity_problem` in `factory/policy.py`; `scripts/start-dispatch.sh` is a shim that runs it; tests in `factory/test_dispatch_start.py` and `factory/test_state.py` | 7 (part A) |
| `scripts/start-broker.sh` (step 7, part B): `factory/broker_start.py` as `factory _start-broker` (`INTERNAL`), the script a shim; the broker's sandbox fingerprint and `broker_plugins` in `factory/fingerprints.py`; `clear_all_manual` and the `sync_from_keychain` statuses (#179) in `factory/secrets.py`; tests in `factory/test_broker_start.py` | 7 (part B) |
| The Coordinator's and roles' sandbox fingerprints, `record_sandbox_fingerprint` for any sandbox and `stale_sandboxes` (step 7, part C1): `factory/fingerprints.py` (`sandbox_fingerprint`, `record_sandbox_fingerprint`, `stale_sandboxes`); the bash copies in `scripts/lib/roles.sh` stay for their callers; tests in `factory/test_fingerprints.py` | 7 (part C1) |
| `factory start` (and `up`) and `scripts/02-launch-factory.sh` (step 7, part C2): `factory/commands/start.py` (`cmd_start`, `require_project_commits`, `onboarding_hints`, `resolve_layout`; `factory _onboarding-hints` for bash `setup`) and `factory/launch.py` (`factory _launch`, the script a shim; the Coordinator's `claudeMdExcludes` with it); `git_push_enabled` and `github_repos_listed` in `factory/policy.py`; `restart` calls `start` directly; tests in `factory/test_command_start.py` and `factory/test_launch.py` | 7 (part C2) |
| The three setup scripts (step 8, part A): `factory/build_templates.py` (`factory _build-templates`, from `00-build-templates.sh`), `factory/security_init.py` (`factory _init-security`, from `01-init-security.sh`) and `factory/prereqs.py` (`factory _install-prereqs`, from `install-prereqs.sh`), each script a shim that runs its `INTERNAL` entry at the same path; `factory/pins.py`; tests in `factory/test_build_templates.py`, `test_security_init.py`, `test_prereqs.py` | 8 (part A) |
## Remaining, in order

1. **Wrappers of Python scripts and small commands**: done (PR 2).
2. **Role validation**: done (PR 3). `validate_role`, `validate_role_overrides`,
   `validate_enabled_roles`, `role_lane_names` and `coordinator_broker_tools` are in
   `factory/roles.py` (with `known_broker_tools`), and `role new` calls them. The bash
   copies in `scripts/lib/roles.sh` stayed until steps 4, 6 and 7 ported their callers
   (`start-broker.sh`, `start-dispatch.sh`, `02-launch-factory.sh`, and `bin/factory`'s
   `doctor`, `start`, `status` and `logs`) and were deleted with the last of them (step 7, part
   C2); `factory/test_roles_validation.py`, which ran both on the same fixtures, now requires the
   refusals and messages the bash gave. It replaced `test_factory_role_overrides.sh`.
   `disabled_role_sandboxes` moved to Python with `doctor` (step 4) and has no bash copy.
3. **Tighten bash `in_list`**: done. It returns false when `$1` is empty or holds a space, tab
   or newline, so `EFFORT="low medium"`, `WORKSPACE="rw ro"`, `TEMPLATE="developer reviewer"` and
   `OPTIONAL="0 1"` fail in bash as they do in Python, and `factory/test_roles_validation.py`
   requires the same refusal and message from both. This did not move the security boundary:
   those keys are not overridable.
4. **Read-only inspection**: `doctor`, `logs`, `packet` and `status` are done.
   `factory/test_command_doctor.py` replaces the doctor half of `test_factory_doctor.sh`, and
   `factory/test_command_status.py` replaces the status parts of `test_factory_helpers.sh`
   (the approvals line) and `test_factory_layout.sh` (the local-settings line). The bash helpers
   `start` called are tested in Python since it was ported (step 7, part C2:
   `require_project_commits` in `factory/test_command_start.py`, `run_or_die` in
   `factory/test_broker_start.py`); `sbx_not_signed_in`, which `setup` calls, stays shell in
   `scripts/test_factory_helpers.sh`.
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
   `npm_private_registry_needs_token`, `setup_done` and the `secrets.sh` keychain helpers stayed
   while `start` called them and went with it (step 7, part C2), their parity tests becoming
   Python-only tests of what the bash gave; `require_sbx` stays for `03-stop-factory.sh`.
5. **`secret`**: done. `factory/commands/secret.py` holds `save`, `forget`, `set`, `rm` and `ls`;
   `factory/secrets.py` the helpers they use (names, delivery and revocation over `sbx exec`, the
   `factory secret set` markers, the per-name and GitHub keychain sync). The keychain itself
   (lookup, has, store, delete and the Credential Manager scripts) is one copy in
   `factory/keychain.py`, which `status` also uses; running a program with a value on its stdin is
   `factory/programs.py`. `keychain_backend` and `wincred_powershell` were already in
   `factory/preflight.py`, and the version check is `sbx.require_sbx`. A value is bytes and goes only to a program's stdin; the hidden prompt
   turns echo off before it prints. `scripts/lib/secrets.sh` stayed for `start-broker.sh` (which
   step 7 part B ported) and for `bin/factory`'s `keychain_warn_missing` (for `start`), which
   called `keychain_has`; the file went with `start` (step 7, part C2), and `status.warn_missing`
   is the Python `keychain_warn_missing`.
   `factory/test_secrets.py` runs the bash and the Python helpers on the same fakes and requires the
   same statuses, values, messages and argv (the delivery and PowerShell scripts byte for byte);
   it also tests `keychain_warn_missing`, which has no Python copy yet, and that the bash
   helpers part B ported (`secret_clear_all_manual` among them, now `secrets.clear_all_manual`)
   are gone from `secrets.sh`. `factory/test_command_secret.py` replaces `test_factory_secret.sh`; it passed against the
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
   - `restart` runs the stop script, then `start` for the bound project with `--no-dashboard`: at
     first the bash one as a fixed argv, and since step 7 part C2 `start.run`, called directly.
   - `reset` asks through `Context.confirm` (a tty on stdin; `y`, `yes`, any case), as bash did. The
     project switch in `start` runs `reset --yes` (as a program while `start` was bash; called
     directly since step 7 part C2).
   - The bash `rm_failed_after_stop` stays for `cmd_update` (step 8); the Python copy is
     `runstate.rm_failed_after_stop`, and `factory/test_command_runstate.py` compares the two
     messages (reset and update wording, including the trailing space after each name).
   - **Sandbox fingerprints and `stale_sandboxes` are not ported here**: only `start` and `update`
     use them, so they go with steps 7 and 8.
   - Tests: the usage and no-project parts of `test_factory_restart.sh`, the reset parts of
     `test_factory_state.sh`, `test_factory_chat.sh` and Part 6 of `test_factory_layout.sh`
     (`coordinator_attached`) are now Python and deleted. What stayed shell (the restart through the
     shim, the real stop script against real sleeping processes, the stale saved layout, the project
     switch) followed with `start` in step 7 part C2, into `factory/test_command_start.py`.
   - Output is byte-identical (16 scenarios compared old against new: status, stdout, stderr,
     sbx calls and state). One difference, on a failure path: if removing a `project` file fails for a
     reason other than "not there" (a directory in its place, a permission error), Python stops with
     `error: could not remove <path>: <reason>` and exit 1 (a directory reads "Is a directory" on every host);
     bash's `rm -f` printed its own message and, under `set -e`, exited with rm's status. Session
     files are best effort in both.
7. **`start`**, with `start-broker.sh`, `start-dispatch.sh` and `02-launch-factory.sh`
   (the riskiest: it creates sandboxes and delivers credentials; the credential rules in
   `CLAUDE.md` apply unchanged, argv lists only). **Done**, in three parts (C in two).
   - **Part A, done: `start-dispatch.sh`.** `factory/dispatch_start.py` (`main(ctx, argv)`) does
     the validation, the lane identity check and snapshot, the snapshots into `FACTORY_STATE`
     (role instructions plus project notes, role overrides, enabled-roles, the docs), `running_pid`
     and `port_holder`, and the bridge restart and readiness wait. `scripts/start-dispatch.sh` is
     now `exec "$(resolve_python)" "$ROOT/factory" _start-dispatch "$@"`, which `02-launch-factory.sh`
     still calls. Snapshots stay atomic where they were (a `.tmp` folder renamed into place), a new
     file takes the source's mode less the umask as `cp` did, and the token goes only to
     `sbx secret set`'s stdin. `migrate_state` is also in Python (`factory/state.py`); the bash one
     stays for `03-stop-factory.sh`, and `factory/test_state.py` runs both on
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
   - **Part B, done: `start-broker.sh`.** `factory/broker_start.py` does it all, run as
     `factory _start-broker <project-dir>`; the script is a shim that keeps its path and its usage
     check (the message names `$0`). The token hashes, `grants.json` (the same bytes as jq's),
     the `project` file, the broker's fingerprint (`factory/fingerprints.py`, byte for byte
     the bash `sandbox_fingerprint factory-broker`, which `stale_sandboxes` still compares
     against; a test runs both) and the file modes are as before. The server-start `sh -c` script
     is the same fixed string, run inside the broker sandbox. Every value (each minted token,
     each keychain entry) goes only to a program's stdin: `factory/test_broker_start.py` checks
     the argv and stdin of every sbx call and scans every file, argv and environment the fakes
     saw for the values. Compared with the bash on 11 scenarios (fresh, existing, failures,
     bound, `sbx run` failing, token set failing, not ready, a bad Coordinator tool, usage, no
     project folder, state inside the project), the status, stdout, stderr, sbx calls, the stdin of
     every call, the state files and their modes were the same. The bash `secret_sync_from_keychain`,
     `github_sync_from_keychain`, `secret_clear_all_manual`, `secret_deliver_file`,
     `secret_revoke_file`, the marker helpers, `secret_name_valid`, `keychain_unavailable_warning`,
     `keychain_store`, `keychain_delete` and the store and delete scripts of `wincred_run` are
     deleted; `secrets.sh` keeps only what `keychain_has` needs, and their parity tests are now
     Python-only tests of the outputs the bash gave.
   - #179: `secrets.sync_from_keychain` returns `SYNCED` (0), `NOT_DELIVERED` (1, a failed
     delivery or an unreadable keychain) or `REVOKE_FAILED` (2). `factory secret save` prints
     "delivered '<name>' to the running broker" only on `SYNCED`, and exits with the status.
     The broker start keeps its behaviour: only a failed revocation is fatal.
   - Deliberate differences, all on failure paths: (1) `jq`, `openssl` and `sha256sum`/`shasum`
     are no longer needed or checked (`curl` still is); (2) a `factory secret set` marker that
     can't be removed when a new broker sandbox is created stops the start with `error: could
     not remove the 'factory secret set' marker <path>: <reason>`, where bash went on with the
     stale marker; (3) a former role's token hash file that can't be removed (not "not there")
     stops with `error: could not remove <path>: <reason>`, where bash's `rm -f` printed its own
     message and exited with its status; (4) a former role whose name (from `sbx mcp ls`) holds a
     `/` still loses its registration and secrets, but no file is removed for it; (5) a state file or
     folder that can't be written (the fingerprint, a hash, `grants.json`) stops with `error: <path>:
     <reason>` and exit 1, where bash printed the shell's own message (also exit 1); (6) the
     plugin list is in byte order, where bash's glob used the locale's collation. Module names
     can hold capitals and `_`, which glibc's UTF-8 locales order differently (`alpha,Zed`,
     `myother,my_plugin`), so `broker_plugins` in `common.sh` now sorts with `LC_ALL=C` too, for
     `stale_sandboxes`, and the parity test runs the bash under the installed locales. The old
     `start-broker.sh` recorded the fingerprint under the user's locale, so a broker created
     before this change, with such a plugin set, on a Linux host with a UTF-8 locale, is reported
     out of date once: `factory start` warns, and `factory update` recreates it (which drops
     held writes). macOS's UTF-8 locales are believed to collate in code point order, which
     would leave its brokers unaffected (unverified).
   - Not ported here: the Coordinator's and roles' fingerprints and `stale_sandboxes` (part C1 did).
     `migrate_state` is part A's, from `factory/state.py`, and `require_state_outside` moved there
     too (the bash one stays for `02-launch-factory.sh`). Part B's own `migrate_state` copy is
     gone; A's differs from it only on failure paths (it stops the bridge only for an ASCII-digit
     pid, with `os.kill` rather than a `kill` argv, and a failing move is an `error:` line).
     `run_or_die` and the token minting stay in `broker_start.py`.
   - **Part C1 (done): the Coordinator's and roles' sandbox fingerprints, `record_sandbox_fingerprint`
     for any sandbox and `stale_sandboxes`** in `factory/fingerprints.py`, checked digest for digest
     and list for list against the bash on the same fixtures (`factory/test_fingerprints.py`). The
     bash copies in `scripts/lib/roles.sh` stay: `onboarding_hints`, `cmd_update_plan`, `cmd_update`
     and `02-launch-factory.sh` still call them. Like bash, an `sbx ls -q` that fails reads as no
     sandboxes (what it printed is kept); `broker_start.py` records through `record_sandbox_fingerprint`.
   - **Part C2 (done): `factory start`, `onboarding_hints` and `02-launch-factory.sh`.**
     `factory/commands/start.py` is `cmd_start` (`start` and `up` in `COMMANDS` and
     `PORTED_COMMANDS`), `require_project_commits` (the same hook- and fsmonitor-free
     `git rev-parse`), `resolve_layout` and `onboarding_hints` (`hints`; `keychain_warn_missing` is
     `status.warn_missing`, the policy checks are `factory/policy.py`'s, with `git_push_enabled`
     and `github_repos_listed` added). `factory/launch.py` is the launch: validation, the preflight
     refusals, `.factory/outgoing` (still refusing a symlinked `.factory`, `outgoing` or
     `.gitignore`; the folders are made one at a time and checked again, the file `O_EXCL |
     O_NOFOLLOW`), the broker and dispatch starts (`broker_start.main` and `dispatch_start.main`,
     called in-process rather than through their shims), `create_sandbox` (the same `sbx create`
     argv lists, the fingerprint recorded with `fingerprints.record_sandbox_fingerprint`), role
     sandboxes only once the Claude login is stored, the Coordinator's `claudeMdExcludes` (the
     settings cross only on the stdin of the same fixed in-sandbox `sh -c` script, as before) and the
     closing notes. `scripts/02-launch-factory.sh` keeps its path and usage check and runs
     `factory _launch "$@"`. Bash reaches it so: `factory setup` ends with `factory
     _onboarding-hints` (`INTERNAL`); `factory update` runs `FACTORY_UPDATING=1 cmd_setup` as before
     (bash exports the variable to the Python for that call, so its hints leave out the
     out-of-date warning) and then `factory start <project> --no-dashboard`; `factory restart`
     calls `start.run` directly. The project switch asks through `Context.confirm` and calls
     `runstate.reset` with `--yes`.
   - Compared with the bash on 83 scenarios (fresh with and without a login, plain and on a
     terminal, rerun, partial, `--no-ui`, `FACTORY_NO_UI`, `--no-dashboard`, `up`, project switch
     yes/no/no tty, not set up, an old image tag, wrong or missing sbx, repos with no commits, a
     project that is not a repo, usage errors, every `--layout`/`FACTORY_LAYOUT`/saved layout case,
     sandboxes of another project, the old rw layout, old state locations, the pre-roles broker,
     state inside the project, each symlink refusal, a kept `.gitignore`, bad overrides, the
     researcher enabled, a removed role, the briefing note with an odd project path, managed settings
     missing, already excluded, out of order or not JSON, failing `create`, broker and dispatch
     starts, each onboarding hint on and off, a locked keychain, the hints entry point with and
     without `FACTORY_UPDATING`, the script's usage, `restart`, and no `broker/tools.txt`): the same
     status, stdout, stderr, sbx calls with their stdin, and files with their modes, except for
     the failure paths below. The WSL `/mnt/<drive>` warning was checked in the tests only (the bash
     read the real `/proc/version`).
   - Deliberate differences, all on failure paths: (1) `jq` is no longer needed, so there is no
     "'jq' not found" error, and managed settings that are not JSON print only the `error:` line,
     not jq's parse error before it; (2) an `sbx ls --json` that fails or gives no sandbox list
     (or a factory sandbox with no workspace list) stops with `error: could not list the sandboxes
     ('sbx ls --json' failed: ...)` and exit 1, where bash exited with sbx's or jq's status after
     their own messages; (3) a folder or file that can't be made (`.factory`, `outgoing`, a scratch
     folder, `.gitignore`, a fingerprint) stops with `error: <path>: <reason>` and exit 1, where
     bash printed `mkdir`'s or the shell's message (also exit 1); (4) a `.factory` or `outgoing`
     that becomes a symlink between the check and the `mkdir` is refused, where `mkdir -p` would
     have followed it. Two more, not failures: the briefing note names the script by its path in
     the checkout (`<checkout>/scripts/02-launch-factory.sh`), where bash printed `$0` as typed
     when the script was run by hand (from `factory start` they are the same), and quotes the
     project as bash 5's `printf %q` does (macOS's bash 3.2 may quote unusual characters
     differently; unverified); and the managed settings are written as Python's compact JSON,
     the same bytes as `jq -c` for what the kit writes (objects, arrays, strings, booleans,
     integers), where a float, a DEL character or a file holding several JSON values would differ
     (the last is refused).
   - Deleted, since nothing in bash calls them any more: in `bin/factory`, `cmd_start`,
     `setup_done`, `require_project_commits`, `onboarding_hints`, `keychain_warn_missing`,
     `git_push_enabled`, `github_repos_listed`, `any_jira_tool_granted`, `gitlab_needs_token`,
     `gitlab_mr_unheld`, `npm_private_registry_needs_token` and `jira_cloud_id_set` (and its
     sourcing of `preflight.sh` and `secrets.sh`); all of
     `scripts/lib/secrets.sh`; in `common.sh`, `run_or_die` (unused since part B), `resolve_layout`
     with `FACTORY_LAYOUT_FILE`, `require_state_outside`, `project_repos` (`factory/repos.py` and
     `factory-lane` are the two copies `scripts/test_project_repos.sh` compares now),
     `template_claude_md`, `with_claude_md_exclude`, `exclude_template_claude_md`,
     `sandbox_exists`, `policy_value`, `policy_string` and `claude_login_stored`; in `roles.sh`,
     `KNOWN_BROKER_TOOLS`, `KNOWN_TEMPLATES`, `RESERVED_ROLE_NAMES` (the `broker/tools.txt` guard
     stays), `validate_enabled_roles`, `validate_role_override(s)`, `override_note`,
     `validate_role`, `valid_role_name`, `role_broker_mcp`, `role_lanes`, `role_lane_names` and
     `record_sandbox_fingerprint`. Their parity tests (`factory/test_policy.py`, `test_secrets.py`,
     `test_keychain.py`, `test_preflight.py`, `test_roles_validation.py`, `test_roles.py`,
     `test_repos.py`, `test_sbx.py`, `test_fingerprints.py`, and `scripts/test_factory_gitlab.sh`
     and `test_factory_config.sh`, deleted) are now Python-only tests of the outputs the bash gave,
     captured from it before it was deleted; what bash stays is still compared. (Its locale loops
     went: only the bash depended on the locale.) `sbx_reference_problem` and `sandbox_running`
     were already uncalled before this step and are left for step 9.
   - Tests: `scripts/test_factory_layout.sh`, `test_factory_restart.sh` and
     `test_claude_md_exclude.sh` are now `factory/test_command_start.py` and
     `factory/test_launch.py` (the real `03-stop-factory.sh` against real sleeping processes
     included), as are the project switch of `test_factory_state.sh` (2c) and
     `require_project_commits` of `test_factory_helpers.sh`; `run_or_die` is tested in
     `factory/test_broker_start.py`. What stays shell, as it tests bash that stays for step 8:
     `test_factory_state.sh` (the broker token minting and `update`'s failed `sbx rm`; its restart
     now checks the Python `start --no-dashboard` call) and `test_factory_helpers.sh` (the
     incomplete-checkout refusal, now through `update --plan`, and `sbx_not_signed_in`).
8. **`setup` and `update`**, with `00-build-templates.sh`, `01-init-security.sh`,
   `install-prereqs.sh`, in two parts.
   - **Part A (done): the three setup scripts.** Each stays at its path (`bin/factory`'s `cmd_setup`
     and CI, `scripts/00-build-templates.sh --build-only`, call them there) as a shim that checks
     what the bash checked and runs `exec "$(resolve_python)" "$ROOT/factory" _<name>`:
     `_build-templates` (`factory/build_templates.py`; the usage check stays in the shim, so the
     message still names `$0`), `_init-security` (`factory/security_init.py`) and `_install-prereqs`
     (`factory/prereqs.py`). Output, exit status, programs run (argv lists, no shell) and files
     written are what the bash gave: the old scripts and the new ones were run against stub
     `docker`, `sbx`, `curl`, `sudo`, `apt-get` and `sw_vers` on 34 fixtures (the build with and
     without `--build-only`, a policy overlay, plugins, a dirty or missing `broker/`, failing
     builds and loads; the init with the gateway on and off, running sandboxes, a policy
     initialised or not, denied or allowed; the install on macOS and Ubuntu, wrong platform, KVM,
     checksum and download failures), and the status, stdout, stderr, calls and files were the same.
     The deliberate differences are on failure paths only: if `docker image inspect` fails after
     a load, bash left an empty `FACTORY_STATE/images/factory-<image>-v3.id` and Python leaves none
     (so `image_build_id` reads "no id recorded" rather than an empty one); a filesystem error
     (a full disk, an unreadable policy file) is `error: <path>: <reason>` where bash printed
     `cp`'s or `mkdir`'s own message (not in the fixtures).
     `00-build-templates.sh` still builds the broker only from `git archive` of the committed
     `broker/`.
   - **Where the pins live.** `scripts/lib/common.sh` stays the one copy of everything bash still
     reads (`SBX_VERSION`, `SBX_URL`, `SBX_SHA256`, the `SHELLCHECK_*` values `scripts/lint.sh`
     uses): `factory/pins.py` parses its `readonly NAME="value"` lines at run time, expanding
     `${NAME}`, instead of a second copy. It goes with `common.sh` in step 9. The Linux `.deb`
     pins (`SBX_DEB_UBUNTU*`) were only used by `preflight.sh`, so they were deleted from
     `common.sh` and live only in `factory/preflight.py`; `SBX_VERSION` is still in both, compared
     by `factory/test_preflight.py`. The `test_lint.sh` check that `00-build-templates.sh` passes
     each ShellCheck pin as `--build-arg` is now `factory/test_build_templates.py` (the argv
     `docker build` gets, with the values from `common.sh`); the Dockerfile `ARG` checks stay in
     `test_lint.sh`.
   - **`install-prereqs.sh` is Python too.** It could run before a suitable Python is installed,
     but `factory setup` runs `factory doctor` first (which needs Python 3.9+, a README
     prerequisite), and a shim that cannot find one prints `resolve_python`'s hint, the same one
     `bin/factory` gives, so a fresh machine is no worse off.
   - Deleted with part A, since nothing calls them: `scripts/lib/preflight.sh`, and in
     `common.sh` `broker_export`, `prepare_broker_context` and the `SBX_DEB_UBUNTU*` pins.
     `BROKER_GIT_SAFE` stays (`bin/factory` uses it). `test_factory_update.sh` Parts 3 and 4 (the
     broker build context, and `00-build-templates.sh` against stub docker) are
     `factory/test_build_templates.py`; `test_preflight.py` now asserts the answers the
     deleted `preflight.sh` gave.
   - **Part B (left): `setup` and `update`.** What is left in `bin/factory`: `cmd_setup` (with
     `sbx_not_signed_in`; it already ends in `factory _onboarding-hints`), `cmd_update` (with
     `pull_factory`, `describe_loss`, `forget_role_sessions`, `rm_failed_after_stop`, `confirm`,
     `bound_project`; it already restarts through Python `factory start --no-dashboard`),
     `cmd_update_plan` (with `plan_maybe_sandboxes`) and `usage`. They still use the bash
     `stale_sandboxes` and the fingerprint helpers under it in `scripts/lib/roles.sh`
     (`sandbox_fingerprint`, `image_build_id`, `dir_hash`, `yes_no`, `RW_LAYOUT`, `broker_plugins`
     in `common.sh`), and its role lookups (`list_roles`, `role_get`, `role_sandbox`,
     `coordinator_broker_tools`); `factory/fingerprints.py` and `factory/roles.py` are their Python
     copies, so the port calls those and deletes the bash. Part B then calls
     `build_templates.main`, `security_init.main` and `prereqs.main` directly and the three shims
     can go (CI's `--build-only` becomes `factory _build-templates --build-only`). Its tests:
     `test_factory_update.sh` (Parts 1, 2, 5 and 6), and what is left of `test_factory_state.sh`
     and `test_factory_helpers.sh`.
9. **Finish**: delete `scripts/lib/*.sh` and the bash tests, leave `bin/factory` as a shim
   with no `PORTED_COMMANDS` list (it always runs Python), drop the parity tests, update
   `scripts/lint.sh`. `templates/agent/factory-lane` and `factory-test` stay bash: they run
   inside the sandboxes, not on the host.

Each pull request: port the commands and the helpers only they use, port their shell
tests (or note why a test must stay shell), update `PORTED_COMMANDS` and `COMMANDS`, delete
the bash function, and keep the output byte-identical unless the pull request says otherwise.
