# Factory health: specs, 13 Nov 2026

Six specs from a review of `main` at `701efe1`. Each one has a diagnosis with
the evidence, the change, acceptance criteria, and a prompt you can paste into
the Coordinator. Spec 01 is the one written for review: its new tool sits next
to the bridge's security boundary, so it needs your sign-off. The other five
change instructions, docs and reporting.

## What I found

| # | Problem | Main cause (evidence in the spec) |
|---|---------|-----------------------------------|
| 01 | `wait_for_job` drops | The bridge logged only 3 drops in two days, so most are cut between the Coordinator and the gateway, out of the bridge's sight. Each call holds the connection for up to **600 s**, and anything shorter is stretched to 300 s. Two earlier fixes (the 660 s idle timeout and SSE keepalives) depend on every hop between the Coordinator and the bridge passing those keepalives through. The sbx MCP gateway sits in the middle, and nothing guarantees that it does. |
| 02 | Too many review rounds | Tasks don't include acceptance criteria, so the reviewer judges against its own open-ended standard. "Missing test" is a REVISE reason with no limit, and an Opus reviewer is checking a Sonnet developer's work by default. |
| 03 | No benchmarks | `factory analytics` reports cost per *job*, but nothing groups the developer job, its reviews and its REVISE rounds into one *change*, and the verdict is never recorded. That means "rounds per change" can't be measured. |
| 04 | Jobs too big | 13 of the 21 PRs merged since 5 Nov added 200+ lines, and 9 added over 450 (up to 1,181, plus Thoughtfield at 11,467). The "small chunks" rule exists only as advice, nothing measures size, and `MAX_TURNS=200` / `MAX_JOB_SECONDS=3600` allow hour-long jobs. |
| 05 | Paranoid tests | Developer instructions say to test "every behavior you change, including edge cases and failure paths", with no sense of proportion. Test lines run 1.4 to 4x code lines on UI changes, and browser tests assert exact audio lengths (`aa23986`). |
| 06 | **The one you didn't list** | `scripts/CLAUDE.md` has grown from 11 KB to **50 KB** since 25 Oct, and it grew **+26% today** across 16 commits. Every dashboard job loads about 12k tokens of rules, and each rule is one more thing a reviewer can cite. This is my best guess for why today felt worse. |

## Round 2 (13 Nov): the factory grades and improves itself

Specs 01 to 06 are merged and their tests pass on `main` as of `2660e8a`.

| # | What | Depends on |
|---|------|------------|
| 07 | Replay benchmark: 8 past changes as tasks (smoke, full and holdout tiers), a grading job that runs the reference PR's tests as a hidden oracle, and `factory analytics --bench` comparing medians over 3 runs | 03 phase 1 (merged) |
| 08 | Analyst role: read-only, runs weekly at minimum or whenever you ask. The bridge writes an evidence pack to its scratch folder. It labels root causes from a fixed list, proposes one change per run, and the benchmark decides whether it's kept | 07 |

| 09 | Fast lane: a `trivial` risk tier, tests run by risk tier, browser tests left to CI. Includes making CI able to run the browser tests: today they silently skip there | nothing; CI chunk first |

Run 09 first (it speeds up everything after it), then 07, then the smoke benchmark labelled `baseline`, then 08.

## Order (round 1, done)

1. **03 phase 1 first** (record verdicts and per-change metrics), so the
   other fixes can be measured. It's small.
2. **06** (rulebook diet): the biggest likely payoff, and it touches only docs.
3. **02 + 05** together: both change role instructions.
4. **04** (job size).
5. **01** (wait slices), after you approve the bridge change.

## Before you start: get the evidence from your machine

I could only read GitHub, not your state folder. Run this in WSL and paste
the output back to me. It reads logs only and prints counts, not task text:

```bash
S=~/.local/state/ai-code-factory
echo "== dropped streams (bridge saw the client hang up)"
grep -rh "closed the .* stream early" "$S" 2>/dev/null | tail -20
echo "== job ends today: role, status, elapsed s, cost"
grep -h '"job-end"' "$S/dispatch/audit.jsonl" 2>/dev/null | grep "$(date -u +%Y-%m-%d)" \
  | python3 -c 'import sys,json
for l in sys.stdin:
    r=json.loads(l); print(r.get("role"),r.get("lane"),r.get("status"),r.get("elapsed_seconds"),r.get("cost_usd"),r.get("model"))'
echo "== analytics"
factory analytics --json | python3 -c 'import sys,json; r=json.load(sys.stdin); print(json.dumps(r.get("roles"),indent=1)[:3000])'
```

Also copy the exact error the Coordinator shows when a wait drops. Its wording
tells us which hop dropped the call (see spec 01).

## How to hand a spec to the factory

Copy this folder into the repo as `docs/specs/factory-health/`, commit it (the
developer only sees committed state), then paste the spec's **Prompt for the
Coordinator** block into the dashboard terminal. Each prompt names its spec
file, so the Coordinator reads the full spec rather than relying on the prompt
alone.
