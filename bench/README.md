# Benchmark

Fixed tasks, each a past change from this repo rewritten as a fresh task. A run
is an ordinary Coordinator job (developer, review, REVISE handling) plus a
grading job, so it measures the real path. Run each task 3 times per label and
compare medians, never single runs. Design: `docs/specs/factory-health/07-benchmark.md`.

## Tasks

| Name | Tier | Area | PR |
|---|---|---|---|
| `shown-select` | smoke | dashboard | #131 |
| `factory-test-logs` | smoke | cli | #134 |
| `held-result-shapes` | smoke | docs | #67 |
| `voice-listen` | full | dashboard | #139 |
| `settings-apply-v2` | full | dashboard | #133 |
| `gitlab-group-repos` | full | broker | #117 |
| `wait-slices` | holdout | bridge | #145 |
| `py-cli-in-list` | holdout | cli | #92 |

## Tiers

- **smoke**: small and fast. Run after any instruction, model or config change.
- **full**: smoke plus the full tasks. Run weekly or before adopting a change.
- **holdout**: never shown to the analyst (spec 08) and never named in its
  evidence. Use them only to confirm a change the analyst proposed, so the
  factory can't tune itself to the test set. The Coordinator runs them only when
  you name the holdout tier.

## Adding a task

Create `bench/tasks/<name>.md`. The name is `[a-z0-9-]`, matches the file name,
and is not another task's name plus a hyphen (see *Reading results*).

```
---
name: <name>
base: <40-hex sha of the merge's first parent>
reference: <40-hex sha of the merge commit>
oracle_ref: <40-hex sha>   # optional, see below
tier: smoke | full | holdout
area: bridge | broker | dashboard | docs | cli
---
<the task, as the Coordinator would delegate it>
```

The body says to work only from `base` and not to look at history past it. It
ends with the contract block: `Done when:`, `Tests required:`, `Out of scope:`
and `Risk:`. Anything the reference commit tested that the task excludes goes in
*Out of scope*, because the grading job drops oracle tests that cover only
excluded work. `scripts/test_bench_tasks.py` validates every task file (front
matter, 40-hex shas, tier, area, and the contract block); run it with
`factory-test -q -- python3 -m unittest discover -s scripts -p 'test_bench_tasks.py'`.

`oracle_ref` is optional. Without it the oracle's test files are copied from
`reference`. With it they are copied from that commit instead. Use it when the
reference's own test is flaky or broken for reasons outside the task: pin it to
the commit that fixed the test. The list of files still comes from
`git diff --name-only <base> <reference>`, so only the content changes. Pin a
sha, never a branch, so later test changes can't alter an old task's oracle.
Check that `reference`'s implementation passes the pinned tests before you rely
on it.

## Running

Say this to the Coordinator:

```
run benchmark <tier> [label]
```

`label` names what is being tested, such as `baseline` or `analyst-0003`. It is
`[a-z0-9-]`. For each task of the tier the Coordinator runs 3 developer jobs on
branches `factory/bench-<name>-<label>-<n>` (n = 1..3) from the task's `base`,
reviews them as for real work, then runs a grading job per branch in a fresh
reviewer session. The grade comes from a fixed rubric: the test files the
reference commit added or changed (from `reference`, or from the task's
`oracle_ref` when it has one) are copied onto the branch and run (the oracle), the normal checks run, and the reviewer scores done-when and scope 1-5
and the size ratio (non-test changed lines against the reference's). The report
ends with one line the bridge parses into the job-end record:

```
GRADE: oracle=pass|fail checks=pass|fail done_when=<1-5> scope=<1-5> size_ratio=<x.x>
```

Bench branches are never merged, never get a PR, and the Coordinator never lists
them among merge suggestions.

## Reading results

```
factory analytics --bench [label ...]
```

Groups `factory/bench-<name>-<label>-<n>` changes by task and label and prints
medians across the runs (`--bench-tasks DIR` points at another task folder).
With two labels it also flags each metric whose ranges across runs don't
overlap; only those count as real differences. Every job on a bench change is
counted, including the grading job. A report starting with `VERDICT: REVISE`
counts as a review round.

The task is the longest task name that matches the branch. With tasks `shown`
and `shown-select`, the branch `factory/bench-shown-select-baseline-1` is filed
under `shown-select`, which is right, but a `shown` run with the label
`select-baseline` has the same branch name and is misfiled there too. So don't
add a task whose name is another task's name plus a hyphen. A branch that
matches no task is skipped with a warning.

## Known limitation

Lanes are full clones, so the reference commit is reachable from the developer's
lane. The task text tells the developer to work only from `base`, and the size
ratio is graded, but nothing enforces it. If scores look too good, the fix is a
`factory-lane prepare --at <sha>` mode that clones history only up to `base`;
that is a separate spec.

## Cleanup

Branches the bridge brings back are imported as
`refs/remotes/factory-<lane>/factory/bench-*`. The bridge never deletes refs,
so remove them yourself, on the host, in each repo that holds them:

```
git for-each-ref --format='%(refname)' 'refs/remotes/factory-*/factory/bench-*' \
  | xargs -r -n1 git update-ref -d
```

Run the `git for-each-ref` part alone first to see what will go.
