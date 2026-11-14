# 07: Replay benchmark (spec 03, phase 2)

The analyst (spec 08) proposes changes, and this benchmark is what proves or
disproves them. Build this first.

## What exists

Phase 1 is on `main`: job-end records carry `change`, `verdict`, `turns` and
`diff_lines`, and `factory analytics --changes` groups jobs into changes
(`changes_report()` in `scripts/analytics.py`). Nothing replays a fixed task
yet, so two weeks of real work can't be compared fairly: the mix of work is
different every day.

## Change

### 1. Benchmark tasks: `bench/tasks/<name>.md`

Each task is a past change from this repo, rewritten as a fresh task:

```
---
name: shown-select
base: <sha of the merge's first parent>
reference: <sha of the merge commit>
tier: smoke | full | holdout
area: bridge | broker | dashboard | docs | cli
---
<the task, written as the Coordinator would delegate it, ending with the
spec 02 contract: Done when / Tests required / Out of scope / Risk>
```

Start with 8 tasks, picked so the tiers get a mix of areas and sizes:
- **smoke (3):** run after any instruction, model or config change. Small and
  fast: `#131` shown-select, `#134` factory-test logs, and one docs-only task.
- **full (3):** smoke plus these, weekly or before you adopt a change:
  `#139` voice-listen (medium), `#133` settings-apply-v2 (large), plus one
  broker task.
- **holdout (2):** never shown to the analyst (spec 08), and never named in
  its evidence. Use them only to confirm a change the analyst proposed, so the
  factory can't tune itself to the test set.

`bench/README.md` lists the tasks and says how to add one.

### 2. Running a task (no new host component)

A run is an ordinary Coordinator job, so it measures the real path. The
Coordinator briefing gets a short *Benchmark* section:

> When the user says "run benchmark <tier> [label]", read `bench/tasks/`, and
> for each task of that tier, 3 times: delegate it to the developer with
> `fresh_session`, telling it to branch `factory/bench-<name>-<label>-<n>`
> from the task's `base` commit (`git switch -c ... <base>` in its lane), then
> review and handle REVISE exactly as for real work. Then delegate a grading
> job (below). A job that runs a browser test (`scripts/test_*_browser.*`;
> developer, review or grading job alike) runs alone: start it only when no
> other benchmark job is running, and start nothing else until it ends. Run all
> other jobs in parallel lanes and collect them with
> `wait_for_any`. Never merge a bench branch, and never ask the user to.
> Report a one-line table at the end.

`label` names what is being tested, such as `baseline` or `analyst-0003`.
Branch names carry it, so `--changes` can group by it.

### 3. Grading job: reviewer, fresh session

A separate review with a fixed rubric, run after the normal review loop has
finished:

1. Copy the **test files the reference commit added or changed** into the
   review checkout of the bench branch, and run them with `factory-test`. They
   are the hidden oracle: pass or fail. A task may set `oracle_ref` (a 40-hex
   sha) to copy those files from that commit instead of `reference`, for when
   the reference's own test is flaky; the file list still comes from base..reference.
2. Run the project's normal checks. `checks=fail` when a check fails in code or
   tests the change touched. A failure only in code and tests the change didn't
   touch: run that test file once more, and once at `base`. If it passes on the
   rerun or fails the same way at `base`, it is flaky, not the change's: score
   `checks=pass` and name the test and assertion in a `Flaky:` line of the
   report. Otherwise `checks=fail`.
3. Score 1–5 against the task's *Done when* items, plus: scope (didn't touch
   files the task didn't need), size relative to the reference diff, and test
   proportionality (spec 05).
4. Report a fixed block that the bridge parses into the job-end record:
   `GRADE: oracle=pass|fail checks=pass|fail done_when=<1-5> scope=<1-5> size_ratio=<x.x>`.

### 4. Scoring: `factory analytics --bench [label ...]`

Groups `factory/bench-*` changes by task and label and prints, per label, the
**medians across the 3 runs**: oracle pass rate, rounds, wall time, cost,
turns, grade scores, and size ratio. With two labels, it also prints the
difference and flags any metric where the two labels' ranges across runs
don't overlap. Only those count as real differences. Never compare single
runs.

### 5. Cleanup

`bench/README.md` documents removing the imported `refs/remotes/factory-*/factory/bench-*`
refs. The bridge never deletes refs, so this is a command you run yourself.

## Known limitation: the answer is in the lane's history

Lanes are full clones, so the reference commit is reachable from the
developer's lane. The task text tells it not to look past `base`, and the
grade includes the size ratio, but nothing enforces this. If the scores look
too good, the fix is a `factory-lane prepare --at <sha>` mode that clones only
history up to `base`. That changes `factory-lane`, which would be a separate
spec.

## Acceptance criteria

- `bench/tasks/` contains the 8 tasks above, each with valid front matter. A
  test validates every task file: name matches the file, `base` and
  `reference` are 40-hex, tier is one of the three, and the body contains
  `Done when:`.
- The bridge parses a `GRADE:` line into job-end fields (`grade_oracle`,
  `grade_checks`, `grade_done_when`, `grade_scope`, `grade_size_ratio`).
  Unit tests cover a valid line, a missing line, and junk values.
- `factory analytics --bench` has a test with a fixture of two labels × 3
  runs, including one non-overlapping metric.
- The Coordinator briefing has the *Benchmark* section. Bench branches never
  appear in the merge suggestions the Coordinator gives you.

**Out of scope:** an unattended host runner, the `--at` lane mode, and the
analyst.

## Prompt for the Coordinator

```
Read docs/specs/factory-health/07-benchmark.md. This is bigger than the size
budget, so split it and show me the chunks first. My suggestion: (1) bench/
task files plus their validation test, (2) GRADE parsing in the bridge,
(3) factory analytics --bench, (4) the briefing's Benchmark section and
bench/README.md. Developer on the default model, reviews on sonnet. REVISE
only for unmet acceptance criteria, a bug with a reproduction, or a security
issue with an attack path. When all four are merged, run "benchmark smoke
baseline" and show me the table.
```
