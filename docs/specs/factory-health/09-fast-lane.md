# 09: Fast lane: run tests by risk, and let CI run the full suite

## Diagnosis

- **The factory runs the full suite twice or more per change.** The developer
  runs it once at the end (`roles/developer/instructions.md`, *Tests and
  checks*: "the full suite ... One full run per repo per task"). The reviewer
  runs it again (`roles/reviewer/instructions.md`, *Checks* step 2: "Run the
  full suite once"), and again on every re-review. The full suite includes the
  three headless-Chrome tests (`scripts/test_field_browser.py`,
  `test_field_shown_browser.py`, `test_term_mic_browser.py`), and each one
  scripts a whole page session.
- **This happens for every change, whatever its size.** A one-line copy fix
  pays the same test bill as a bridge change.
- **CI can't catch what the factory stops running yet.** All three browser
  tests are `@unittest.skipUnless(... shutil.which("chrome-headless-shell"))`,
  and the CI runner (`.github/workflows/ci.yml`, `ubuntu-24.04`) doesn't
  install `chrome-headless-shell`. So in CI they **skip silently**. Today the
  factory sandboxes are the only place browser tests ever run. Moving the full
  suite to CI therefore has to start by making CI able to run them.

## Change

### 1. CI runs the browser tests, and can't skip them by accident

- A new job in `ci.yml`, `browser tests`, on `ubuntu-24.04`, runs when
  `changes` reports code. It installs the **same** Chrome for Testing build
  the role images use. Read `CHROME_VERSION` and `CHROME_SHA256_AMD64` from
  `templates/developer/Dockerfile`'s `ARG` lines with a fixed `sed`, so there
  is one pin; don't copy the values. Download the zip, check its SHA-256,
  unzip it, and put the binary on `PATH`. Install the same apt libraries the
  Dockerfile lists. Node comes from `actions/setup-node`, pinned by full
  commit SHA. The role images get Node from their pinned base image, so the
  developer runs `node --version` in its sandbox and pins that exact version
  in the workflow, with a comment saying where it came from.
- That job sets `FACTORY_REQUIRE_BROWSER=1`. Each browser test's
  `skipUnless` becomes a helper that **fails** instead of skipping when that
  variable is set and Chrome or Node is missing. That makes a silent skip in
  CI impossible.
- A new check in `scripts/test_ci_changes.sh`, or a small test: the reviewer
  Dockerfile's `CHROME_VERSION`/`SHA256` equal the developer's, since CI reads
  only the developer's.
- Failing tests are listed in the job's step summary (`$GITHUB_STEP_SUMMARY`):
  test names and their first assertion line only, so you can paste them back
  to the Coordinator.

#### Decision, 13 Nov 2026: the visual browser sections are off the pull-request path

The field test drives the live 3D Thoughtfield, and a CI runner has no GPU, so it is software-rendered and
slow (the `witness` section alone took 597 s on a runner; a whole-file run passed 20 minutes). The owner
decided the browser tests are split by what they protect:

- **Fast set, on every pull request and push** (the `browser shard` jobs): the shown and mic tests and the
  field test's `picking`, `pane` and `nogate` sections, which are behaviour and security checks.
- **Visual set** (the `visual shard` jobs): the field sections `layouts`, `layouts-65`, `crystals`, `witness`,
  `witness-reduced`, `comet`, `comet-reduced`, `arc` and `arc-reduced`. They run **nightly**, **on demand**
  (`workflow_dispatch`) and **on pull requests that touch the Thoughtfield's 3D code** (the `field3d` output of
  the `changes` job: `field-sky.js`, `field-face.js`, `field-order.js`, `field.html`, `field.css`, `witness/`,
  `vendor/three/` and the field browser test), not on every pull request.
- The `browser tests` job requires every fast shard, and every visual shard when they ran; a visual shard
  skipped because the 3D code didn't change is fine. It never passes with everything skipped while code
  changed. `scripts/test_ci_shards.py` checks each field section is in exactly one shard of the two sets.

### 2. Risk tiers decide what the factory tests

Spec 02's contract already has `Risk: low | normal | high`. Add **`trivial`**,
and make the tier decide tests, in the Coordinator briefing and both role
instructions:

| Risk | Typical change | Developer writes | Developer runs | Reviewer runs |
|---|---|---|---|---|
| **trivial** | docs, copy text, CSS/layout, comments, config text, a rename, ≤ ~30 lines with no new logic | no tests | lint on changed files | nothing: reads the diff (one screenshot if it's visible) |
| **low** | small logic change in non-boundary code | a test only if behavior changed | the tests it added or changed, plus the matching test file of each changed module | the same set, once |
| **normal** | a feature chunk within the size budget | the *Tests required* tests | same as low | same as low |
| **high** | bridge, broker, `factory-lane`, credentials, auth, input handling, anything in `scripts/CLAUDE.md`'s security rules | as today | full suite once, as today | full suite once, as today |

Rules for every tier:
- **Matching test file** means the project's naming convention: `x/foo.py` →
  `x/test_foo.py`; `dispatch/server.py` → `dispatch/test_server.py`;
  `foo.js` → `test_foo.js` if present. If there is none, run nothing extra
  and say so in the report.
- **Browser tests (`scripts/test_*_browser.*`) never run in the factory**
  unless the change edits one of those test files, and then only that one
  file. They drive the whole `factory dashboard --demo`, so a "touches the
  page" rule would mean almost every dashboard change runs them again.
  Dashboard changes rely on CI for them, which is the point of this spec.
- **Re-reviews** run only the tests for files the fixes touched.
- The report's `Checks:` line names the tier and what was deliberately not
  run: `Checks: tier low; ran dispatch/test_server.py (OK); full suite and
  browser tests left to CI`.
- The Coordinator picks the tier when it writes the task. The reviewer may
  say a tier was too low (a REVISE under rule (a) or (c)). It must not run
  more tests on its own initiative.

### 3. CI failure flow

Coordinator briefing, *After review*:

> The full suite and browser tests run in CI on the pull request. If the user
> says CI failed, ask them to paste the failing test lines from the job
> summary, then send them to the same developer lane as a follow-up (like a
> REVISE), with Risk unchanged. The fix's review runs only the failing tests.

### 4. Spec 05's one-off audit

Spec 05's optional brittle-browser-test audit becomes more useful now. Those
tests will run only in CI, so flaky ones would block your PRs. Run it after
this spec merges.

## Acceptance criteria

- On a PR, the `browser tests` job runs and **passes** the fast browser set
  (the shown and mic tests and the field test's picking, pane and nogate
  sections), which show as passed, not skipped, in the job log. The visual
  sections (the other field sections) run nightly, on demand and on PRs that
  touch the Thoughtfield's 3D code, not on every PR (decision of 13 Nov 2026,
  above); when they run, the job requires them too. A test removing
  Chrome from `PATH` with `FACTORY_REQUIRE_BROWSER=1` set shows the helper
  fails rather than skips.
- The Chrome pin is read from `templates/developer/Dockerfile`, not copied.
  The reviewer/developer pin-match check passes. The new action is pinned by
  full commit SHA, with the tag in a comment (CLAUDE.md).
- Briefing and both role instructions contain the tier table and rules. The
  "full suite once" wording remains only under `high`.
- `scripts/test_ci_changes.sh` passes.

**Out of scope:** splitting or speeding up the browser tests themselves,
caching, and making the factory read CI results directly.

## Do this first, before the spec lands

Nothing here takes effect until it's merged, and the browser tests must keep
running *somewhere* until CI can run them. So keep the factory as it is until
the CI job passes on a PR. The prompt below orders the chunks so CI comes
first.

## Prompt for the Coordinator

```
Read docs/specs/factory-health/09-fast-lane.md. Split it into two chunks, in
this order: (1) the CI browser-tests job, FACTORY_REQUIRE_BROWSER and the
pin-match check (Risk: high, since it touches CI and pins); (2) the risk tiers
in the briefing and both role instructions, and the CI failure flow (Risk:
normal). Don't start chunk 2 until I tell you chunk 1's PR showed the browser
tests passing in CI. Developer on the default model, reviews on sonnet.
REVISE only for unmet acceptance criteria, a bug with a reproduction, or an
unpinned dependency.
```
