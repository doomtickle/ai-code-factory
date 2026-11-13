# 06: Put `scripts/CLAUDE.md` on a diet

You didn't list this one, but I think it's the biggest factor in today's
slowdown.

## Diagnosis

`scripts/CLAUDE.md` ("Dashboard Security Rules: read before changing
dashboard code") has grown like a ratchet:

| main at | scripts/CLAUDE.md | root CLAUDE.md |
|---|---:|---:|
| 25 Oct | 11 KB | 13 KB |
| 30 Oct | 29 KB | 14 KB |
| 5 Nov | 36 KB | 15 KB |
| 12 Nov, noon | 40 KB | 15 KB |
| **12 Nov, end of day** | **50 KB** | 15 KB |

16 commits touched it on 12 Nov: nearly every feature job added its own rules,
docs and "decision record" lines. It's now 415 lines, almost all under one
"Dashboard rules" heading, and many of the rules are narrow (`#191 /api/lane?name=&job= path; STAGED, WRITTEN and FAILED send the
cu:select hint...`).

Why that hurts every job in the area most of your recent work is in:

- **Context and cost:** about 12k tokens loaded into every developer *and*
  reviewer session that touches `scripts/`, re-read on every turn, before any
  work starts.
- **Review rounds:** every rule is something a reviewer can check and cite.
  50 KB of rules guarantees findings.
- **Attention:** the handful of real security rules (127.0.0.1 only, the
  per-run token, no acting from the display page, `Origin` checks, no shell,
  confirm before host commands) get the same weight as layout notes for one
  pop-up.
- **Feedback loop:** each job adds rules, which makes the next job harder,
  which produces more rules.

## Change

1. **Split the file** (one docs-only change):
   - `scripts/CLAUDE.md` keeps **only normative rules a reviewer must enforce**
     on the dashboard's security boundary and the cross-cutting invariants,
     written as short imperatives. **Target: 8 KB or less.**
   - Move everything else into `docs/decisions/dashboard.md`: decision
     records, per-feature behavior, history, the reasons behind each rule
     ("owner decision 12 Nov..."). It's linked from `scripts/CLAUDE.md` with
     one line ("for the history and per-feature notes, see ...") and is
     **not** auto-loaded.
   - Nothing is deleted. Every sentence goes to one of the two files.
2. **A rule to stop the ratchet** (root `CLAUDE.md`, *Other constraints*, and
   the developer instructions' *Docs* bullet):

   > Don't add to a `CLAUDE.md` unless the task asks you to or the change
   > creates a new security rule. Decision records and feature notes go in
   > `docs/decisions/<area>.md`.

3. **A guard:** a test (`scripts/test_docs.py` or a new one) that fails if any
   `CLAUDE.md` in the repo exceeds a size cap (root 16 KB, `scripts/` 10 KB),
   so growth becomes a deliberate decision.
4. Apply the same split to the root `CLAUDE.md` later, if spec 03's numbers
   show it matters. It's 15 KB and steady, so lower priority.

## Acceptance criteria

- `scripts/CLAUDE.md` is 8 KB or less (10 KB hard cap via the test).
- Every line removed from it appears in `docs/decisions/dashboard.md`. The
  reviewer checks this with a diff, so nothing is lost.
- Each security rule in the current file (the ones about binding,
  tokens/keys, `Origin`, cookies, fixed argv/no shell, confirm steps, the
  config-write path) is still in `scripts/CLAUDE.md`. The reviewer lists them.
- The docs site build (`python3 scripts/docs.py`) and `scripts/test_docs.py`
  pass.

**Out of scope:** changing any rule's meaning. This is a move, not a rewrite.

## Prompt for the Coordinator

```
Read docs/specs/factory-health/06-rulebook-diet.md. Implement "Change" items
1-3 as one docs-and-test change on factory/rulebook-diet, on the developer
with effort high: deciding which rules are security-normative needs care.
Then a security-focused review on the reviewer's default model: confirm no
security rule left scripts/CLAUDE.md and nothing was dropped. REVISE only for
a lost or weakened security rule, lost text, or an unmet acceptance criterion.
```
