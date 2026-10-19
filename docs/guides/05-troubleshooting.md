# Troubleshooting

When something goes wrong, the factory almost always says what and why. This
guide lists the messages you're most likely to see, what causes each one, and
how to fix it. It starts with the tools that tell you the most: `factory doctor`
and the logs.

## Start here

### Check the machine and the gateway

```bash
factory doctor
```

The doctor checks the platform, tools, Docker, `sbx` and `herdr` versions,
setup and your Claude login. Once the broker sandbox is running, it also runs
the gateway's own checks inside it: policy, tokens and grants, the audit log,
credentials, a live probe, GitHub access per repo, and whether every host the
policy can reach is in a broker kit. Each problem comes with its fix. It changes
nothing.

### Read the logs

```bash
factory logs developer          # every developer lane's transcript
factory logs developer-lane2    # one lane
factory logs broker             # every broker tool call, with the calling role
factory logs dispatch           # every Coordinator tool call and job
```

The broker and dispatch logs print one line per call: the time, the role, the
tool or event, the decision or status, and the reason for a refusal:

```text
14:20:03  developer  npm_fetch  allow
14:21:47  developer  web_fetch  deny  the developer role may not use web_fetch (roles/developer/role.conf BROKER_TOOLS)
```

The raw files are in the state folder, `~/.local/state/ai-code-factory/`
(`$XDG_STATE_HOME/ai-code-factory/` if you set it):

| File | What's in it |
|---|---|
| `dispatch/logs/<lane>.log` | Role transcripts, one per lane |
| `dispatch/audit.jsonl` | Every Coordinator tool call and job end, with cost and context size |
| `dispatch/dispatch.log` | Dispatch bridge errors |
| `broker/audit.jsonl` | Every broker call, with the calling role |
| `broker/broker.log` | Broker server errors |

The full list is in [Operating](../../README.md#operating).

### Check what's running

```bash
factory status
```

It shows the project the factory is bound to, whether the broker and dispatch
bridge are running, your Claude login, the roles with their lanes, and each
sandbox's state.

## Starting the factory

### "the factory is not set up yet"

```text
error: the factory is not set up yet: run 'factory setup' first
```

**Cause:** setup hasn't finished on this machine: the images aren't loaded, or
the security baseline isn't applied.
**Fix:** run `factory setup`. It's safe to re-run.

### "sbx 0.45.1 required, found ..."

**Cause:** another `sbx` (often from Homebrew) is first on your `PATH`. Every
script checks the exact pinned version.
**Fix:** `brew uninstall sbx`, or put `~/.local/bin` ahead of `/opt/homebrew/bin`
in `PATH`. Then run `factory doctor`.

### "is not a git repository or a folder of git repositories"

```text
error: /Users/you/code/notes is not a git repository or a folder of git repositories; the developer role(s) work in git clones of it (each commits to factory/* branches you merge). Run 'git init' and commit, or start the factory on the repo itself.
```

**Cause:** the developer works in git clones of the project, so the folder must
be a repository, or hold repositories in its immediate subfolders. Hidden and
symlinked subfolders don't count.
**Fix:** make it a repository with at least one commit, or start the factory on
the repository itself rather than a folder above it:

```bash
git init && git add -A && git commit -m "Initial commit"
factory start
```

### "created with an older layout"

```text
error: these sandboxes were created with an older layout (the project mounted read-write, or no .factory/outgoing) and must be recreated: factory-developer
Run 'factory update': it recreates them (your Claude login, the logs and your project are kept; those roles start a fresh conversation).
```

**Cause:** the developer sandbox dates from before lanes, when it edited your
working tree directly. It now works in its own clones.
**Fix:** run `factory update`. It lists what each recreated sandbox loses and
asks first. From then on, the developer works from committed state, so commit
or stash what you want it to build on.

### "out of date (made from an older image or kit)"

```text
  ! out of date (made from an older image or kit): factory-broker; run 'factory update' to recreate them
```

**Cause:** an image, kit or local setting changed since that sandbox was
created. A sandbox keeps what it was created with, so `factory start` alone
never picks those up.
**Fix:** `factory update`.

### "was created for a different project"

```text
error: factory-developer was created for a different project. To switch projects, see README (Switching projects).
```

**Cause:** sandboxes are bound to the project they were created for. Running
`factory start` in another folder normally offers to switch for you; you see
this message when you decline, or when you run the scripts directly.
**Fix:** `factory reset` removes the agent sandboxes (your login, roles and
settings are kept), then `factory start` in the new project.

### Role sandboxes weren't created

```text
note: no Claude login is stored yet, so these role sandboxes were not created: developer researcher reviewer
```

Or, later, the Coordinator reports:

```text
DENIED: the developer sandbox has not been created; ask the user to run 'factory start' again (role sandboxes are created once a Claude login is stored)
```

**Cause:** `sbx` gives a sandbox the stored Claude login only when it creates
it, so role sandboxes wait until you've signed in.
**Fix:** type `/login` in the Coordinator pane and sign in, then run
`factory start` again (the Host shell pane works).

### "port 18766 is already in use"

```text
error: port 18766 is already in use by another process (pid 51234: /usr/bin/python3 /Users/you/old-copy/dispatch/server.py).
It is not this checkout's dispatch bridge; it may be one started from another copy of the factory repo.
Stop it (e.g. run that copy's scripts/03-stop-factory.sh, or kill the process), then re-run.
```

**Cause:** every copy of the factory repo uses the same ports and sandbox
names, and a dispatch bridge from another copy is still running.
**Fix:** run that copy's `scripts/03-stop-factory.sh`, or kill the pid shown,
then `factory start` again.

### "dispatch bridge exited" or "did not become ready"

**Cause:** the bridge failed to start.
**Fix:** read the error at the end of
`~/.local/state/ai-code-factory/dispatch/dispatch.log`. For the broker's
equivalent ("broker did not become ready"), read `broker/broker.log` in the
same folder.

### Jira warnings on every start

```text
  ! Jira tools are granted but no token is stored in the keychain (see README: Enabling Jira)
```

**Cause:** the Coordinator is granted two read-only Jira tools by default
(`kits/coordinator/broker.conf`).
**Fix:** nothing, if you don't use Jira: the tools just return DENIED with
setup hints. To use them, follow [Enabling Jira](../../README.md#enabling-jira).

## While roles work

### "DENIED" from the dispatch bridge

The bridge refuses requests it can't run, prefixed with `DENIED:`. The
Coordinator usually handles these itself:

- `all 3 lanes of developer are busy (jobs ...); wait for one or cancel it`:
  every lane has a job. It waits, or asks you whether to cancel one.
- `developer can't run on model 'opus-4'; allowed: sonnet, haiku, opus`: the
  model isn't in the role's `MODEL` or `MODELS`.
- `instructions for developer exceed 20000 characters`: the role's
  instructions plus your per-project notes are too long. Shorten the notes.

### "DENIED by broker policy"

Every broker refusal starts with `DENIED by broker policy:` and says which rule
applied. Roles are told to report these, not work around them. Common ones:

| Message (after `DENIED by broker policy:`) | Fix |
|---|---|
| `the developer role may not use web_fetch (roles/developer/role.conf BROKER_TOOLS)` | Working as designed: only the researcher reads the web. Grant a tool in `BROKER_TOOLS` only if you mean to. |
| `host fastify.dev is not in the web allowlist (policy.toml [web].allow_hosts)` | Add the host to `[web].allow_hosts` in your local `policy.toml` and to your local `broker-kit/spec.yaml`, then `factory update` ([Make it yours](04-customize.md)). |
| `repo my-org/my-api is not in the broker allowlist (policy.toml [git].repos)` | Add it to `[git].repos` in your local `policy.toml`, then `factory update`. |
| `agents may only push branches starting with 'factory/'` | Push a `factory/*` branch. Protected branches (`main`, `master`, `release/*`) are always refused. |
| `'.factory/outgoing/x.bundle' does not exist in the workspace` or `factory/x has no commits beyond origin/main` | The developer bundled by hand, into the wrong folder or with the wrong range. It should run `factory-lane bundle "$FACTORY_PROJECT" "$FACTORY_LANE" <rel> factory/x` and pass the printed name to `git_push`. |
| `commit 3f9c2ab01d4e appears to contain secrets (...)` | Remove the secret from the branch's history (the developer can rewrite its `factory/*` branch), then push again. |
| `no Gemini API key found for the broker sandbox; ...` | Optional tool: see [Enabling Gemini Flash queries](../../README.md#enabling-gemini-flash-queries). |

### npm_fetch errors

When some downloads fail, `npm_fetch` says so and gives one line per package:

```text
npm_fetch incomplete: 1 download(s) failed.
  @acme/ui@^2.4.0: HTTP 401 from https://acme.jfrog.io/acme/api/npm/npm-local/@acme/ui/-/@acme/ui-2.4.1.tgz (credential attached): the credential was rejected - check the "factory-npm-registry" keychain entry (README, Enabling a private npm registry)
```

| In the line | Cause and fix |
|---|---|
| `HTTP 401` or `HTTP 403` ... `the credential was rejected` | The private registry token is wrong or expired. Fix the `factory-npm-registry` keychain entry (or `factory secret set npm-registry`), then `factory start`. |
| `no credential for the private npm registry` | No token is stored yet. See [Enabling a private npm registry](../../README.md#enabling-a-private-npm-registry). |
| `DENIED by broker policy: HTTP 302 redirect ... is not in [packages].npm_redirect_hosts` | Add the named host to `[packages].npm_redirect_hosts` in your local `policy.toml` and to your local `broker-kit/spec.yaml`, then `factory update`. |
| `could not reach <host>` | The host isn't in a broker kit. Add it to your local `broker-kit/spec.yaml`, then `factory update`. |
| `more than 5 redirects` or `has no Location header` | A registry-side problem. |

Lockfile problems stop `npm_fetch` before it downloads anything:

- `no yarn.lock or package-lock.json found`: `npm_fetch` only downloads what a
  lockfile pins. Commit one.
- `only yarn v1 lockfiles are supported`: yarn 2 and later aren't supported.
- `package-lock.json lockfileVersion 1 is not supported`: regenerate it with
  npm 7 or later.

### factory-test fell back to host mode

Roles report which mode `factory-test` ran. A fallback looks like this:

```text
factory-test: the clean install failed (above); falling back to a copy of the project's own node_modules
factory-test: mode: host node_modules fallback (the clean install failed) - results reflect the host's installed package versions (including private packages), not a clean yarn.lock install
```

**Cause:** the offline install from `.factory/npm-mirror/` failed, usually
because a package is missing from the mirror: `npm_fetch` hasn't run since the
lockfile changed, or some packages failed to download (above).
**Fix:** ask the Coordinator to have the role call `npm_fetch` again and re-run
the tests. If the lockfile changed on the developer's branch, the developer
copies it to `.factory/outgoing/lock/` and fetches from there. Host-mode
results are still useful, but they reflect your installed versions, not the
lockfile. See [Running tests](../../README.md#running-tests).

Related messages:

- `factory-test: no package mirror at ...: call the broker's npm_fetch tool first`:
  `npm_fetch` hasn't run for this project yet.
- `factory-test: host node_modules can't run here: missing platform packages:`:
  host mode needs the Linux build of a native package (esbuild, rollup, ...)
  at your installed version, and it isn't in the mirror. The message names it;
  run `npm_fetch` again.

### The developer says your changes are missing

**Cause:** the developer works from your committed state. Uncommitted changes
never reach its clones.
**Fix:** commit (or commit to a WIP branch and check it out), then ask again.

### A branch didn't come back

Check the job result's `import_errors`, or the `✗` lines in the developer's
transcript.

- **Nothing to import:** the developer didn't commit to a `factory/*` branch.
  Uncommitted work stays in its lane for the next task there. Ask it to commit.
- **`the exported bundle exceeds 524288000 bytes; not imported`:** the branch
  carries something huge (build output, data files). Ask the developer to
  remove it from the branch.

### A job seems stuck

A job can run for a long time; the Coordinator waits up to 600 seconds per
call and then waits again. Watch its tab, or `factory logs <role>`, to see
whether it's still making progress. If it isn't, ask the Coordinator to stop it:

```prompt
The developer-lane2 job has been stuck on the same failing test for ten
minutes. Cancel it and tell me what it reported so far.
```

The Coordinator calls `cancel_job`, which stops only that job; other lanes keep
running. Anything the lane committed to a `factory/*` branch is still brought
back. If the whole factory is unresponsive, `factory stop` then `factory start`
restarts everything; running jobs are lost, but saved sessions, branches already
brought back and logs are kept.

### Costs keep climbing

Each job result shows `cost_usd` and `context_tokens`. Every turn of a resumed
session re-reads its whole history, so a lane that has grown large makes even a
small task expensive.

- **Session cap:** a lane whose saved session is over the role's
  `MAX_SESSION_TOKENS` (100k by default) starts the next task fresh. The
  result's `session` field says so:
  `fresh session (automatic: the previous one carried ~134k tokens, over the 100k cap; it starts without that history)`.
  Lower the cap in `role.conf` for projects with many unrelated tasks.
- **Fresh sessions:** ask the Coordinator to start unrelated chunks with
  `fresh_session`, putting the facts each one needs in the task.
- **The Coordinator's own context:** use `/compact` or `/clear` in the
  Coordinator pane between unrelated pieces of work.
- **Models:** move the Coordinator to `sonnet` (`/model`) if you hit usage
  limits; the Coordinator already uses cheaper models for mechanical work.
- **See where it goes:** `factory analytics` breaks cost down by role, model,
  task type and day, and lists the most expensive jobs.

## Still stuck

- Run `factory doctor` and fix what it reports.
- Read the transcript of the role involved (`factory logs <role>`) and the
  server logs (`dispatch/dispatch.log`, `broker/broker.log`).
- To start over with clean sandboxes, `factory reset` then `factory start`.
  Your login, roles and settings are kept.
- To switch Claude accounts: `factory reset`, `sbx secret rm anthropic`, then
  `factory start`, `/login`, and `factory start` again.
- On Windows, see the troubleshooting table in
  [Setting up on Windows 11 (WSL2)](../setup-wsl.md#troubleshooting).
