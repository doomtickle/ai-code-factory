# 02: Fewer review rounds, with a contract both roles share

## Diagnosis

- **No definition of done.** The Coordinator briefing asks for "what done
  looks like" in each task, but nothing gives it a structure, so the reviewer
  never sees a checkable list. It reviews against its own standard
  (`roles/reviewer/instructions.md`, *Checks*, step 1: "including edge cases
  and error handling"), and that standard has no limit.
- **"Missing test" has no limit either.** REVISE is allowed for "a missing or
  weakened test", and step 2 asks whether tests cover "the new behavior and its
  failure paths". The reviewer can always name one more failure path, so it can
  always justify one more round. (Spec 05 covers the test side.)
- **The reviewer is stronger than the developer by default.** `roles/reviewer/role.conf`
  runs Opus and `roles/developer/role.conf` runs Sonnet. A stronger model
  reviewing a weaker one's work finds more, and each finding is a round.
- **The developer never sees the reviewer's checklist.** It finds out what
  "required" means only by getting a REVISE.
- **Rounds aren't measured.** The verdict isn't in the job-end audit record, so
  there's no data on how often REVISE happens or why (spec 03).

The guardrails that already exist are good, so keep them: the two-REVISE cap
in the Coordinator briefing, "don't widen a re-review", and suggestions never
blocking.

## Change

1. **Task contract.** In the Coordinator briefing (*Delegating*), require
   every developer task to end with this block, and every review task to
   repeat it word for word:

   ```
   Done when:
   - <observable behavior 1>
   - <observable behavior 2>
   Tests required: <the behaviors that need a test; "the Done-when items" by default>
   Out of scope: <what not to do, including edge cases deliberately left out>
   Risk: low | normal | high   (high = auth, input handling, secrets, concurrency)
   ```

2. **Reviewer REVISE rule** (`roles/reviewer/instructions.md`, *Verdict*):
   replace the first bullet with:

   > REVISE only for: (a) a *Done when* item that isn't met; (b) a bug with a
   > concrete reproduction (input → wrong output) in code the change touches;
   > (c) a security issue with an attack path; (d) a doc that is now false;
   > (e) a behavior listed under *Tests required* that has no test. Label each
   > required change with its letter. Anything else, including edge cases the
   > task put out of scope and additional tests, goes under Suggestions. If the
   > task has no *Done when* block, review as before but say so in Checks.

3. **Pre-review self-check** (`roles/developer/instructions.md`, *Report*):
   before reporting, the developer re-reads *Done when* and ticks each item in
   a new report line, `Done when: 1 ✓ 2 ✓ ...`, giving a file:line for each.

4. **Model pairing by risk** (Coordinator briefing, *Choosing model and
   effort*): the reviewer runs **Sonnet for Risk low/normal** and its Opus
   default only for Risk high or a diff over the size budget (spec 04). The
   current rule (Sonnet only for "small low-risk diffs") is the wrong default
   for a factory that mostly ships UI.

5. **Re-review scope** stays as it is, with one added sentence: "On a
   re-review, a new finding must be in code the fixes changed, labelled with
   its letter."

## Acceptance criteria

- Coordinator briefing, reviewer instructions and developer instructions
  contain the text above. `scripts/test_kit_specs.py` (or the test that covers
  the briefing) still passes.
- The demo fixtures under `scripts/dashboard-demo/setup/root/roles/` are
  updated to match, if a test compares them.
- The report shapes still start with `VERDICT:` and `Branch:`, since dashboard
  parsing depends on them.

**Out of scope:** bridge code, and changing the REVISE cap.

## How to tell it worked

After about 10 changes, spec 03's per-change report should show mean review
rounds dropping, and REVISE findings mostly labelled (a) or (b), not (e).

## Prompt for the Coordinator

```
Read docs/specs/factory-health/02-review-rounds.md and
docs/specs/factory-health/05-test-proportionality.md. Implement both specs'
"Change" sections as one change on factory/review-contract on the developer:
they both edit roles/developer/instructions.md and roles/reviewer/instructions.md,
so they belong together. Use the developer's default model: these are
instructions that steer every future job, and test fixtures may need to
change. Review on sonnet. REVISE only for unmet acceptance criteria in either
spec.
```
