# 05: Proportionate tests, and no brittle browser assertions

## Diagnosis

- **The instruction is open-ended.** `roles/developer/instructions.md`,
  *Tests and checks*, first bullet: "Add or update tests for every behavior
  you change, including edge cases and failure paths." The reviewer's step 2
  asks whether tests cover "the new behavior and its failure paths", and a
  missing test is a REVISE reason. Neither says when to stop.
- **Ratios from the PRs merged since 5 Nov** (added lines, test files vs.
  the rest):

  | PR | code | test | test/code |
  |---|---:|---:|---:|
  | #141 no-gate (remove a screen) | 17 | 55 | 3.2 |
  | #131 shown-select | 8 | 32 | 4.0 |
  | #138 field-think | 66 | 179 | 2.7 |
  | #127 field-whats-waiting | 213 | 428 | 2.0 |
  | #140 voice-mic | 458 | 662 | 1.4 |

  Heavy testing is right for the bridge and broker, which are security
  boundaries. It's overkill for removing an entry screen.
- **Brittle browser tests.** `aa23986` ("skip the lower length bound for the
  first always-on post") loosened a headless-Chrome test that asserted
  recorded audio lengths in sample counts (`2 * 16000 * 0.6`) from a fake
  mic that loops. `8defd9d` needed a "longer wrapper timeout". Tests like these
  fail on timing rather than on behavior, and each failure costs a job round.

## Change

1. **Developer instructions** (*Tests and checks*), replacing the first bullet:

   > Test in proportion to risk. Always: the behaviors listed under the task's
   > *Tests required* (or *Done when*), each with one test, plus a failure
   > path a real user or caller can actually hit. For the bridge, broker,
   > `factory-lane`, credentials and anything security-related: also the
   > hostile inputs. Don't add tests for constants, copy text, CSS values, or
   > things the type checker or linter already catches, and don't test the
   > same behavior twice at different layers. Never weaken or delete an
   > existing test to make it pass; a test you think is wrong goes in your
   > report.

2. **A browser-test rule** (developer instructions, and the dashboard section
   of `scripts/CLAUDE.md` if it has one on tests):

   > Browser tests assert what a person would notice: an element appears, a
   > message is sent, a state changes. Don't assert timings, frame counts,
   > sample counts or pixel positions tighter than a person could perceive.
   > Use generous bounds, or drive time with a fake clock. A browser test that
   > fails intermittently is a defect: report it, don't add a retry or widen
   > one bound at a time.

3. **Reviewer instructions** (*Checks*, step 2): "Do tests cover the
   behaviors listed under *Tests required*, plus failure paths a real caller
   can hit? Further edge-case tests are suggestions, except for
   security-boundary code." This matches spec 02's REVISE rule (e).

4. **One-off cleanup (separate, optional chunk):** a reviewer job lists browser
   tests in `scripts/test_*_browser.py` that assert timings, sample counts or
   pixel positions, with file:line, so you can decide which to loosen. The
   reviewer only reports; any fixes go to the developer afterwards.

## Acceptance criteria

- The instruction text is in place. Implement it together with spec 02.
- Nothing about `factory-test`, check budgets or "never weaken a test"
  changes.

**Out of scope:** rewriting existing tests (except via item 4, if you choose to).

## Prompt for the Coordinator

Implemented together with spec 02; use the prompt there. For the optional
cleanup audit:

```
Ask the reviewer (sonnet, read-only) to list browser tests in
scripts/test_*_browser.py and their .js drivers that assert timings, sample
counts, frame counts or pixel positions tightly enough to fail on a slow
sandbox. Report only: file:line, what it asserts, a suggested looser
assertion. No verdict needed. Keep it under 300 words.
```
