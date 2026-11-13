---
name: settings-apply-v2
base: 7b5e96d039b09a1b34edae22b84b9805d1de3bbb
reference: 06fb296d8ff78d804c324fc1f1e08d12db665b7f
tier: full
area: dashboard
---
Work only from commit 7b5e96d039b09a1b34edae22b84b9805d1de3bbb (`base`). Do not look at any later history, other branches or refs, or the reflog; solve this from the code as it stands at `base`.

Settings: an "Apply now" that refreshes the bridge's settings without a restart.

Goal: owner decision of 12 Nov 2026. Today every change on the dashboard's SETTINGS page (role tuning, project notes, role instructions) says "applies on Restart", and a restart cancels running jobs. The dispatch bridge reads its copy of the role overrides and instructions plus notes from the snapshot `factory start` writes under `FACTORY_STATE/dispatch/` on every delegate, so refreshing that copy is enough to apply them from each role's next job, with nothing restarted and no job cancelled. Add a command that does just that, and a button for it on SETTINGS.

Files: `factory/dispatch_start.py` (snapshot code), `factory/commands/config.py`, `bin/factory` usage text, `scripts/dashboard_controls.py`, `scripts/dashboard.py`, `scripts/dashboard_configwrite.py`, `scripts/dashboard-ui/cu.js` and `cu.css`, their tests (`factory/test_dispatch_start.py`, `factory/test_command_config.py`, `scripts/test_dashboard_controls.py`, `scripts/test_dashboard_demo.py`, `scripts/test_cu_page.js`), `README.md` (command table), `docs/guides/06-dashboard.md`, `scripts/CLAUDE.md`, `CLAUDE.md`, `broker/docs/decisions.md`. Read `scripts/CLAUDE.md` before editing dashboard code: the control unit's prepare/run token, the runner rules and the COPY/confirm rules are security rules.

Known facts:
- The command is `factory config apply`: a subcommand of `factory config`, not a new top-level command. It rebuilds the snapshot of role overrides and instructions (with project notes) in memory, validates it exactly as `factory start` does, and only then writes it. It stops, starts and cancels nothing, and touches no sandbox, broker, job, `enabled-roles` or lane identity. On any bad value it changes nothing and exits 1.
- What it cannot apply, and says still needs a restart: switching an optional role on or off (it needs a sandbox and a broker token that only start makes) and the lane git identity (`[git]` in `policy.toml`).
- The swap must not leave the bridge looking at a gap: today the snapshot code deletes the instructions folder and renames a new one in, so a delegate arriving in between sees "no instructions loaded". Replace each file atomically, remove stale files only after the new ones are in place, and have `factory start` use the same writer.
- Apply and `factory start` can run at the same time (terminal plus dashboard) and must not break each other: a lock in the dispatch state folder held by both, and a stale-file sweep that leaves fresh temp files alone. Apply refuses with a clear message while a start is writing.
- Apply refuses a role whose instructions plus notes pass the bridge's 20,000-character cap, and notes that are not UTF-8, because the bridge would otherwise fail every task for that role. It changes nothing on a refusal.
- Dashboard: the pending-changes bar gains an "Apply now" key. It goes through the same prepare/run token and runner as the existing RESTART, UPDATE and DIAG actions (fixed argv, no shell, stdin `/dev/null`, own session, logged to a 0600 file, one action at a time across all of them, demo mode refuses it, only once the factory is bound to a project). Prepare lists the pending changes it will apply and those that still need a restart, drawn as text; nothing is cancelled; Terminal 0 stays attached; CONFIRM is dark for 2.5 s after the list arrives. When it finishes, the settings caches are dropped so the bar and running values update on the next poll.
- The tags on SETTINGS lines change: tuning keys, notes and instructions say "next job (after Apply)"; which optional roles are on still says "applies on Restart".

Constraints: standard library only; host code runs on Python 3.9. Never write a secret into the snapshot or the logs.

Done when:
- `factory config apply` refreshes the bridge's snapshot so a delegate after it uses the new overrides, instructions and notes; running jobs are untouched; `enabled-roles` and lane identity are untouched.
- An invalid override, an over-long instructions-plus-notes or non-UTF-8 notes makes it refuse with exit 1 and write nothing.
- A delegate arriving mid-swap never sees a missing instructions file; a concurrent `factory start` and Apply do not break each other, and Apply refuses while a start is writing.
- SETTINGS shows an Apply now key; its prepare shows applicable and restart-only changes; CONFIRM runs the command through the existing runner, one action at a time; demo mode refuses it.
- The four setting tags read as described; the README table, dashboard guide, `scripts/CLAUDE.md`, `CLAUDE.md` and a dated decision entry describe the command and its limits.
Tests required: one test per Done-when item: snapshot refresh and refusals and the atomic swap and lock in `factory/test_dispatch_start.py`; the `config apply` command wiring in `factory/test_command_config.py`; prepare/run/refusal/demo in `scripts/test_dashboard_controls.py`; the key and its confirm in `scripts/test_cu_page.js`. Include the hostile case: a stale lock file or symlink in the state folder must not be followed.
Out of scope: applying `enabled-roles`, the lane identity or gateway settings without a restart; changing how `factory start` validates; any other dashboard page.
Risk: high
