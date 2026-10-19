# Build a feature

This guide follows one feature from idea to merged code. You'll write a prompt
that gets good results from the Coordinator, see how it hands the work to the
researcher, developer and reviewer, read a job result, handle a REVISE round,
and merge the developer's branch into your project. If you've set up GitHub
access, you'll also see how to push the branch and open a pull request.

## Commit first

Roles work from your committed state. The developer works in git clones of
your project, fetched again before every task, so it can't see uncommitted
changes. If the feature builds on work in progress, commit it first (a WIP
commit on a branch is fine):

```bash
git switch -c rate-limit-prep
git commit -am "WIP: request context for rate limiting"
```

Then name that branch as the base in your prompt ("start from
`rate-limit-prep`"). Without a named base, the developer branches from the
branch your project had checked out, usually the default branch. If you ask
for work on changes it can't see, the developer says they're missing instead
of guessing.

## Write a good prompt

The Coordinator turns your message into self-contained tasks for roles that
never see your conversation. The more it knows up front, the less each role
spends rediscovering it.

A weak prompt:

```prompt
Add rate limiting.
```

This leaves everything open: which endpoints, what limits, what happens when a
client goes over, how to store counters, how to test it. The Coordinator has
to guess or ask, and the developer has to explore the whole codebase to find
where requests come in.

A good prompt:

```prompt
Add per-client rate limiting to the public API in src/server/.
- Limit: 100 requests per minute per API key, fixed window, in memory
  (we run a single instance).
- Over the limit: respond 429 with a Retry-After header in seconds.
- Apply it in the existing middleware chain in src/server/app.ts, before auth
  failures are logged. Health checks (/healthz) are exempt.
- Tests: cover the limit boundary, window reset and the exempt route.
- Research first whether our framework (Fastify 5) has a maintained plugin
  for this, and use it if it fits.
Get it reviewed, then tell me which branch to merge.
```

It names the goal, the files, the constraints, the edge cases and what "done"
looks like. It also says when outside information is needed, which is the
only time the researcher gets involved.

> **Tip:** You don't need to say which model to use. The Coordinator picks the
> cheapest model and effort that will do each task well, steps up when a run
> falls short, and tells you what it picked so you can overrule it.

## What the Coordinator does

For a prompt like the one above, the typical flow is:

1. **Researcher** (only when outside information is needed): reads the docs
   through the broker's `web_fetch` tool and reports back with sources. Only
   the researcher reads the web.
2. **Developer**: works in a private git clone of your project, creates a
   `factory/<topic>` branch, implements the change with tests, runs the checks
   with `factory-test`, commits, and reports the branch.
3. **Reviewer**: checks out that exact branch in its own sandbox, runs the test
   suite once, reviews correctness, tests, security and docs, and ends with
   **APPROVED** or **REVISE** and a list of required changes.

Each role's tab shows its transcript live while it works. See
[Roles](../../README.md#roles) for what each role may do.

## Read a job result

Each finished job gives the Coordinator a result like this (one line of JSON,
wrapped here; `report` is the role's own summary):

```text
{"job_id": "20261017-142530-9b1e44", "role": "developer", "lane": "developer",
 "status": "done", "elapsed_seconds": 412,
 "task": "Add per-client rate limiting to the public API in src/server/ on branch factory/rate-limit.",
 "model": "claude-sonnet-5", "cost_usd": 0.8312, "session_cost_usd": 0.8312,
 "context_tokens": 48213,
 "branches": [{"repo": ".", "ref": "factory-developer/factory/rate-limit", "commit": "3f9c2ab"}],
 "import_errors": [],
 "report": "Branch factory/rate-limit (3f9c2ab): rate limiter in src/server/rate-limit.ts:1-58, wired in src/server/app.ts:41. Tests: 212 passed (factory-test mode: clean install).",
 "session": "fresh session"}
```

What the fields tell you:

| Field | Meaning |
|---|---|
| `status` | `done`, `failed` or `cancelled` (`running` while it works) |
| `lane` | Which lane ran it: `developer`, `developer-lane2`, ... |
| `model`, `effort` | The model the job ran on, and the effort if one was set |
| `cost_usd` | What this job cost |
| `session_cost_usd` | The running total of the lane's session, earlier jobs included |
| `context_tokens` | How large the lane's session has grown; every later turn re-reads it |
| `branches` | Committed `factory/*` branches now in your project: repo, ref and commit |
| `import_errors` | Anything that could not be brought back |
| `session` | Whether the job continued the lane's session or started fresh, and why |

A reviewer job has the same fields except `branches` and `import_errors`.

The Coordinator summarizes these for you. To see them yourself, every job's
end is recorded in the dispatch audit log (without the report):

```bash
jq -c 'select(.event == "job-end") | {lane, status, cost_usd, context_tokens, branches}' \
  ~/.local/state/ai-code-factory/dispatch/audit.jsonl
```

`factory analytics` turns the same log into a cost and performance report
([Analytics](../../README.md#analytics)).

Every job also leaves a **review packet** on your machine, one Markdown file
with everything you need to judge it: the task, each branch's commits and
diffstat against your checked-out branch, the `factory-test` modes the role
reported, the broker calls its role made meanwhile, the cost, and the role's
report (with its APPROVED or REVISE verdict for a review). The job result
carries its path as `packet`; read it with:

```bash
factory packet                      # the latest job
factory packet 20261017-142530-9b1e44
factory packet developer-lane2      # that lane's latest job
factory packet --list               # recent jobs
```

## REVISE loops

When the reviewer answers REVISE, the Coordinator sends its list back to the
developer, on the same lane, so the developer continues the session that
wrote the branch and keeps committing to `factory/rate-limit`. The reviewer
then re-reviews, re-running only the tests and lint for the files the fixes
touched.

A REVISE round shows up in the developer's transcript as a new job on the
same lane, and the branch arrives again at its new commit:

```text
15:01:12  ⎇ branch factory-developer/factory/rate-limit at 8d04e1f
15:01:12  ■ job 20261017-145502-c07a18 done after 370s
```

If the same issue comes back twice, the Coordinator steps up to a stronger
model or effort. You can steer it yourself at any point:

```prompt
The reviewer's second point (Retry-After rounding) matters, the naming nits
don't. Fix only the Retry-After issue, then re-review.
```

## Get the work

Nothing is merged for you. After every developer job, the dispatch bridge
fetches the lane's committed `factory/*` branches into your repository as
remote-tracking branches named after the lane. Look, then merge:

```bash
git branch -r | grep factory-
git log -p main..factory-developer/factory/rate-limit
git merge factory-developer/factory/rate-limit
```

Or check it out as a local branch first:

```bash
git switch -c rate-limit factory-developer/factory/rate-limit
```

When the project is a folder of repositories, each branch arrives in the
repository it belongs to; run these commands there. Branches stay until you
delete them:

```bash
git branch -rd factory-developer/factory/rate-limit
```

See [Getting the developer's work](../../README.md#getting-the-developers-work)
for how the import works and why no git hook or config from a sandbox can run
on your machine.

> **Warning:** When the factory works on its own repo, review a branch before
> you merge it and before you restart anything. `scripts/`, `bin/factory` and
> `dispatch/server.py` run on your machine with your permissions.

## Push and open a pull request (optional)

Agents can push `factory/*` branches and open draft pull requests through the
broker, once you allow it. Setup is in
[Enabling git push and pull requests](../../README.md#enabling-git-push-and-pull-requests):
list the repos in `[git].repos` in your local `policy.toml`, run
`factory update`, and save a GitHub token once (it is restored for the broker
sandbox only, on every start):

```bash
factory secret save github
```

Then ask:

```prompt
Push factory/rate-limit to my-org/my-api and open a draft PR against main
titled "Per-client rate limiting", with a short summary of the change and
how it was tested.
```

The Coordinator asks you before anything irreversible like a push or a PR. The
developer bundles the branch, and the broker checks the branch name, refuses
protected branches and force-pushes, secret-scans every commit, then pushes.
With the Passdesk Pro plugin, each push and PR also waits for your approval
([Enabling approvals](../../README.md#enabling-approvals-passdesk-pro)).

## Keep the Coordinator's context small

Your conversation with the Coordinator is re-read on every turn. When a feature
is merged and you move on to something unrelated, use `/clear` in the
Coordinator pane (or `/compact` to continue with a summary). The Coordinator
reminds you at natural breakpoints and gives you a status to paste into the
new session.

## Next

- [Parallel work](03-parallel-work.md): split a bigger feature across lanes.
- [Running tests](../../README.md#running-tests): how `factory-test` runs your
  suite without touching your `node_modules`.
