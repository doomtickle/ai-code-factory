# 04: Smaller jobs, with a budget the bridge reports

## Diagnosis

- Of the 21 PRs merged since 5 Nov, **13 added 200+ lines** (code, tests and
  docs; `git diff --shortstat` on each merge) and **9 added over 450**:
  `#140` voice-mic (+1,181, 16 files), `#132` settings-rebuild (+984, 21
  files), `#133` settings-apply-v2 (+714, 18), `#127` field-whats-waiting
  (+708, 18), `#122` coordinator-voice (+521, 30), `#129` (+489, 15), `#139`
  (+472, 10), `#126` (+458, 9), and `#121` Thoughtfield (+11,467, 101 files).
- The Coordinator briefing already says "Break work into small,
  self-contained chunks", but that is advice with no number attached and
  nothing checks it.
- The caps allow long jobs: developer `MAX_TURNS="200"`, and
  `DEFAULT_MAX_JOB_SECONDS = 3600`.
- Big jobs feed every other problem: longer waits (01), more for the reviewer
  to find (02), more tests (05), and bigger sessions that cost more each turn.

## Change

1. **A size budget in the Coordinator briefing** (*Delegating*, replacing the
   "Break work into small..." bullet):

   > Plan each chunk to fit a budget: **at most ~250 changed lines of
   > non-test code, ~8 files, and one user-visible behavior**. Tests and docs
   > don't count toward it. If a feature needs more, split it into chunks
   > that each leave `main` working (the plumbing first, then the behavior,
   > then the UI). List the chunks to the user before starting. Don't go
   > smaller than one coherent change.

2. **The bridge reports size.** In the developer job's result (and the packet,
   and the job-end record via spec 03), add
   `size: {files, lines_added, lines_removed, test_lines_added}` from the
   diffstat it already computes for the packet (`branch_detail()` in
   `dispatch/server.py`; add a `--numstat` variant with the same hardened
   flags rather than parsing the `--stat` text). Add `over_budget: true` when non-test added lines exceed
   `SIZE_BUDGET_LINES` (default 250, overridable per role in `role.conf` as a
   new tuning key, added to `ROLE_OVERRIDE_KEYS` in **both**
   `factory/roles.py` and `scripts/lib/roles.sh`, which a test keeps in sync).
   The bridge only reports this; it never blocks.
3. **The Coordinator acts on it** (briefing): "If a result says
   `over_budget`, tell the user and plan the next chunks smaller; review it on
   the reviewer's default model."
4. **Tighter caps** in the user-side override (`~/.config/ai-code-factory/roles/developer.conf`,
   not the committed `role.conf`, so the team default stays put until the
   numbers from spec 03 back it up):
   `MAX_TURNS="120"`, `MAX_JOB_SECONDS="1800"`. A job that hits a cap stops
   with a clear message today; that message should suggest splitting the task.

## Acceptance criteria

- A unit test: a fake diffstat of 300 non-test lines gives `over_budget: true`;
  200 lines plus 400 test lines gives `false`.
- Test-file detection: a path whose file name starts with `test_` or
  contains `.test.`, or that sits under a `tests/` folder. Put the rule in
  one function so spec 03's analytics can reuse it.
- The `ROLE_OVERRIDE_KEYS` sync test still passes with the new key.
- The README's role.conf key table documents `SIZE_BUDGET_LINES`.

**Out of scope:** the bridge refusing or splitting tasks itself.

## Prompt for the Coordinator

```
Read docs/specs/factory-health/04-job-size.md. Implement "Change" items 1-3 as
one change on factory/size-budget on the developer (role default model);
item 4 is a local setting I'll make myself. This change is itself an example:
plan it within the budget it defines, and if it won't fit, split it and tell
me the chunks first. Review on sonnet, REVISE only for unmet acceptance
criteria or a bug with a reproduction.
```
