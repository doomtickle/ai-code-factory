---
name: wait-slices
base: 1f305ea8f763476de87cc6ef749d8ee66a3f67b2
reference: b547e7fd3c8bde45e72d2f1165f96fd02fff8445
tier: holdout
area: bridge
---
Work only from commit 1f305ea8f763476de87cc6ef749d8ee66a3f67b2 (`base`). Do not look at any later history, other branches or refs, or the reflog; solve this from the code as it stands at `base`. Read `CLAUDE.md` first: this is the dispatch bridge.

Bridge: wait in 4-minute slices and add `wait_for_any`.

Goal: the Coordinator's `delegate` and `wait_for_job` calls can block for up to 10 minutes, which is longer than Claude Code's default silent-tool abort (about 330 s), so a normal wait sometimes dies with a timeout error and costs the Coordinator a turn. Make every blocking call short and repeatable, tell the Coordinator how to tell a busy job from a stuck one, and add a way to wait on several jobs at once.

Files: `dispatch/server.py`, `dispatch/test_server.py`, `kits/coordinator/spec.yaml` (the Coordinator's briefing and its comment about the tool timeout), `README.md`, `docs/guides/05-troubleshooting.md`.

Known facts:
- Today the maximum wait is a constant of 600 s and the default for a wait is 300 s. A "still running" reply carries little about the job.
- New cap: no blocking call (`delegate`, `wait_for_job` and the new tool) holds a request longer than one slice of 240 s, a named constant, replacing the old maximum. A wait asked for less than 60 s is stretched to a floor of 60 s; the default wait is the slice. A caller who is still waiting at the end of a slice simply calls again.
- A "still running" reply carries `seconds_since_last_output` (how long since the job's transcript was last written) and `turns_so_far` (assistant messages in the transcript, counted once per message, not once per content block).
- New read-only tool `wait_for_any(job_ids, timeout)`: returns as soon as any one of the listed jobs ends, with that job's result and the status of the others; with a job already finished and a zero timeout it answers at once. It validates its arguments (a list of job ids, a numeric timeout) and, like the other waits, streams keepalives and may block. It is read-only: it can neither start nor cancel anything. Add it to the bridge's tool list and the README's tool listing.
- The Coordinator briefing tells it: a wait returns after at most 4 minutes; "still running" is normal, so call the same wait again with no other reply; a wait that errors or drops does not mean the job failed; for parallel jobs delegate with `wait_seconds: 0` and use `wait_for_any`.
- The kit's raised tool timeout (660000 ms) stays as a safety margin; its comment, the README and the troubleshooting guide must stop saying the bridge holds calls up to 600 s, and must say what it means if the timeout still happens (at about 330 s: an old sandbox or old bridge; at about 660 s: something swallows the keepalives; the job keeps running either way).

Constraints: standard library only, Python 3.9-compatible host code. Don't change `cancel_job`, the other tools' signatures, or the bridge's fixed argv rules.

Done when:
- A wait asked for longer than the slice returns after the slice, still running; one asked for under 60 s waits 60 s; the default is the slice.
- A still-running reply has `seconds_since_last_output` and `turns_so_far`, with turns counted once per message.
- `wait_for_any` returns on the first job to end with that job's result and the others' status, answers at once for a finished job with a zero timeout, rejects bad arguments, and streams keepalives.
- The briefing, README and troubleshooting guide match the new behaviour and no longer mention a 600 s maximum.
Tests required: one test per Done-when item in `dispatch/test_server.py`, including the argument validation of `wait_for_any` and the cap. Run `dispatch/test_server.py` and the docs tests that read the touched files.
Out of scope: changing how jobs run, the dashboard, `cancel_job`, a client-side retry loop.
Risk: normal
