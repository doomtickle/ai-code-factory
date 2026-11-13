# 03: Measure the factory: per-change metrics, then a replay benchmark

## Diagnosis

`scripts/analytics.py` (`factory analytics`) already aggregates the job-end
records in `dispatch/audit.jsonl`, with cost, elapsed time, tokens per role,
anomalies and trends. Two things are missing:

- **The unit is the job, not the change.** One feature is a developer job, a
  review, REVISE jobs and re-reviews, but nothing links them. The job-end
  record (`Job.summary()`) has `branches` for developer jobs only, and the
  review job's branch is only in its free-text `task`, truncated to 200
  characters.
- **The verdict isn't recorded.** APPROVED or REVISE is only in the report
  text, so "rounds per change" and "first-pass approval rate" can't be
  computed.

So you can see that today cost more, but not whether the factory got
*worse*, because the mix of work changes from day to day.

## Phase 1: per-change metrics (do this first; no new components)

1. **Record more in `job-end`** (`dispatch/server.py`, `Job.summary()` or the
   audit call in `run_job`):
   - `verdict`: `APPROVED` / `REVISE` / absent, parsed from the report's first
     line (the reviewer's report shape already fixes it).
   - `change`: the `factory/<topic>` branch name. For developer jobs it comes
     from `branches[].ref`. For reviewer jobs, take the first
     `factory-[a-z0-9-]+/factory/[a-z0-9-]+` match in the full task before
     truncating it.
   - `turns`: `num_turns` from the stream-json result event, if present.
   - `diff_lines` / `diff_files`: totals from the diffstat the packet already
     computes (developer jobs).
2. **`factory analytics --changes`**: group job-end records by `change` and
   print one row per change: jobs, review rounds (count of REVISE), first-pass
   approval (yes/no), wall time from the first job's start to the last job's
   end, total cost, diff lines/files, and the test-lines share if the diffstat
   allows it. Then print a summary: median and p90 of each, and a 7-day vs
   previous-7-day comparison.
3. Old records without these fields are counted as "unknown", never dropped.

**Acceptance criteria (phase 1)**
- Unit tests in `dispatch/test_server.py` for verdict parsing (APPROVED,
  REVISE, missing, and the word appearing later in the report) and for change
  extraction from a reviewer task.
- `scripts/test_analytics.py` covers `--changes` with a fixture of two changes,
  one with a REVISE round.
- `--json` output stays strict JSON.
- No task text beyond the branch name is added to the audit log (the
  CLAUDE.md rule: short records, never full task text).

## Phase 2: replay benchmark (after phase 1 has a week of data)

A fixed set of past changes the factory re-does on demand, so a prompt, model
or instruction change can be compared like for like.

1. **The set:** 6 to 10 merged PRs from this repo's history, mixing sizes and
   areas: one bridge change, one broker change, two dashboard UI changes, one
   docs-only change, one test-heavy change. Good candidates from
   history: `#134` (factory-test logs, small), `#131` (shown-select, tiny),
   `#139` (voice-listen, medium), `#133` (settings-apply-v2, large). Each task
   is a file `bench/tasks/<name>.md` with: the base commit (the merge's first
   parent), the original task, rewritten with a *Done when* block (spec 02),
   and the check commands that define pass.
2. **The runner:** a Coordinator session started with a fixed prompt:
   *"Run benchmark task X: delegate it with fresh_session, review it, follow
   the REVISE rules, report the change name."* That way the run goes through
   exactly the path being measured, and **no new host component acts on
   agents** (the bridge stays the only one, as CLAUDE.md requires). Phase 1's
   `--changes` report then scores the run.
3. **Scoring per task:** checks pass (y/n), rounds, wall time, cost, diff
   size vs. the original PR's size. Run each task **3 times** and compare
   medians: agents are non-deterministic, and one run proves nothing.
4. **Tiers:** a *smoke* set of 3 tasks to run after any instruction or model
   change, and the full set weekly or before adopting a change.
5. **Branch hygiene:** benchmark branches are named `factory/bench-<task>-<n>`
   and are never merged. Document the cleanup command.

An unattended runner (a host script that calls the bridge directly) would be
a **new host component acting on agents**. That needs an owner decision and a
CLAUDE.md update first, so it is out of scope here.

## Prompt for the Coordinator (phase 1 only)

```
Read docs/specs/factory-health/03-benchmarks.md. Implement "Phase 1" only
(items 1-3) as one change on factory/change-metrics on the developer (role
default model). Meet the phase 1 acceptance criteria exactly; phase 2 is out
of scope. Review on sonnet, REVISE only for unmet criteria, a bug with a
reproduction, or an audit-log privacy issue.
```
