# AI Code Factory

A local multi-agent coding setup. You talk to a **Coordinator** agent; it breaks
the work down and delegates it to **role agents** (Developer, Reviewer and
Researcher, or any role you add). Every agent is
Claude Code running in its own Docker Sandboxes (`sbx`) microVM, laid out in
[Herdr](https://herdr.dev) tabs.

```
 You ─▶ Coordinator VM ──MCP──▶ dispatch bridge (host) ──sbx exec──▶ role VMs
        (can't edit files)      list_roles · delegate ·              claude -p, one task at a time
                                wait_for_job · cancel_job                  │
                                                                           │ MCP (per-role tools)
                                                                           ▼
                                                  Broker VM ──▶ GitHub / npm / PyPI / docs
                                                  policy · secret filtering · audit
```

- **Isolation:** agents have no access to the host kernel, the host Docker socket,
  or host credentials. Egress is default-deny: agent VMs can reach only Anthropic.
- **Coordinator:** runs in a VM that opens in your project but can't change it:
  Claude's editing tools and shell are disabled by root-owned managed settings.
  It is briefed on the factory and its roles before any task, whatever the
  project: a root-owned managed `CLAUDE.md` from its kit, plus the dispatch
  bridge's MCP instructions, which list the current roles.
  It acts only through the dispatch bridge's tools, which can do one thing: run
  a task as `claude -p` inside an existing role sandbox (fixed arguments, no
  shell). You never type into role sandboxes and nothing is typed into a
  terminal for you. It also gets direct, read-only access to two broker tools
  (`jira_my_issues`, `jira_get_issue`, see "Enabling Jira"), configured in
  `kits/coordinator/broker.conf` the same way a role's `BROKER_TOOLS` are.
- **Roles:** defined as data in `roles/<name>/`. Each role has its own
  instructions, a kit (Anthropic-only network access), an image, **read-write or
  read-only** access to the project, and a list of the broker tools it may use.
  Roles remember earlier tasks (each keeps a Claude session). You can watch every
  role work live in its Herdr tab. Job results show the job's cost, the
  session's running total and its context size. A session over the role's
  `MAX_SESSION_TOKENS` (100k by default) isn't resumed: the next task starts
  fresh, because every turn of a resumed session re-reads its whole history.
  Roles run with Claude's auto memory off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`):
  their sessions and the Coordinator's tasks carry the context they need.
  `delegate` can wait for the job (`wait_seconds`), so a typical job costs the
  Coordinator one call.
- **Broker:** the only VM with egress for tool traffic (GitHub, npm, PyPI, docs
  sites). It identifies the calling role by token and gives that role only its
  granted tools. For every call it:
  - checks the rules in `broker/policy.toml`
  - blocks secrets in outbound content
  - cleans up responses: strips terminal escapes, redacts secrets, caps size,
    marks web content as untrusted
  - writes to an audit log

  Untrusted content is wrapped in `<<<UNTRUSTED CONTENT id=...>>> ... <<<END
  UNTRUSTED CONTENT id=...>>>` markers with a random id per call, and any
  literal `<<<`/`>>>` already in the fetched content (e.g. Python's `>>>`
  prompt in a code sample, or a marker an attacker tried to forge) is
  replaced with full-width lookalikes (`＜＜＜`/`＞＞＞`) so it can't be mistaken
  for a real marker. If you need the exact original text (to copy a code
  snippet, for instance), get it from the source, not verbatim from tool
  output.
- **Credentials** are injected by the `sbx` egress proxy. Your Claude login and
  an optional GitHub token live in the host `sbx` secret store.

> **Platform:** macOS 14 (Sonoma) or later on Apple silicon, or Ubuntu 24.04+
> on x86_64, including **WSL2 on Windows 11** (step-by-step guide:
> [docs/setup-wsl.md](docs/setup-wsl.md)). `sbx` and `herdr` don't ship for other
> platforms (Linux arm64, native Windows) in a form the factory uses. The Linux
> path is new: it is tested in Ubuntu containers, not yet end to end on a real
> Linux or WSL machine. `factory doctor` checks a machine and says how to fix
> what's missing.

## Requirements

**macOS:**
- macOS 14 (Sonoma) or later on Apple silicon
- Xcode Command Line Tools (`xcode-select --install`), for `/usr/bin/python3` (3.9+)

**Linux (x86_64), including WSL2:**
- Ubuntu 24.04 or later. Docker doesn't support sbx on other distros or on
  Ubuntu derivatives (Mint, Pop!_OS), and `factory setup` refuses them.
- KVM access: `/dev/kvm` present, readable and writable by your user (usually
  via membership in the `kvm` group)
- python3 3.9+, `jq`, `curl`, `openssl`, `git` (`factory doctor` prints the
  `apt-get` line for whatever is missing)
- `sudo`: setup installs Docker's `docker-sbx` package with apt
- **WSL2 on Windows 11:** nested virtualization on, which
  `windows/enable-wsl.ps1` sets up. See [docs/setup-wsl.md](docs/setup-wsl.md).

**Both platforms:**
- Docker (Docker Desktop on macOS, Docker Engine on Linux; used only to build
  the sandbox templates)
- A Docker account (for `sbx login`)
- A Claude Pro or Max subscription
- Optional: a GitHub fine-grained token, only if agents should push or open PRs
- Optional: a Gemini API key, only if the researcher should use `gemini_flash_query`
- Optional: a scoped Atlassian API token, only if the Coordinator should use
  `jira_my_issues` / `jira_get_issue`
- Optional: a read-only token for your private npm registry, only if your
  projects depend on private packages (see "Enabling a private npm registry")

Everything is pinned for repeatable setups:

| Component | Pinned in |
|---|---|
| `sbx` 0.45.1, `herdr` 0.9.1 (+ SHA-256, per platform) | `scripts/lib/common.sh` |
| Sandbox base images (digests) | `templates/*/Dockerfile`, `broker/Dockerfile` |
| ruff 0.16.8 | `templates/reviewer/Dockerfile` |
| yarn 1.22.22 (+ SHA-256) and `factory-test` | `templates/developer/Dockerfile`, `templates/reviewer/Dockerfile` |
| Broker code + policy | baked into `factory-broker:v3` at build time |
| Dispatch bridge runtime | macOS system Python (`/usr/bin/python3`); `python3` on PATH on Linux |

Every script checks the exact `sbx`/`herdr` versions and refuses to run on a mismatch.
The broker and the dispatch bridge are Python standard library only.

## Quick start

`broker/` is the `passdesk-broker` repository as a git submodule; a plain
`git clone` leaves it empty. `factory setup` (and any other `factory`
command) initializes it automatically on first run, so this is optional, but
you can also clone with `--recurse-submodules` or run
`git submodule update --init` yourself first. The broker image is always
built from a `git archive` export of the commit this repo pins `broker/` to,
never from the `broker/` working tree, and `factory setup`/`factory update`
refuse to go on while `broker/` has uncommitted work, is off its pin, or its
`broker/.git` doesn't point at `.git/modules/broker`.

```sh
# Check the machine first; it says how to fix anything missing:
./bin/factory doctor

# Once per machine (installs tools, signs in to Docker, builds images,
# applies the security baseline, and puts `factory` on your PATH):
./bin/factory setup

# Then, for any project:
cd ~/code/my-project
factory start
```

`factory start` runs the whole factory for that folder and opens it in Herdr:

- **coordinator** tab: the Coordinator (top) and a host shell (bottom).
- one tab per role, showing its live transcript.

**The first time**, type `/login` in the Coordinator pane and sign in with your
Claude subscription, then run `factory start` again (the Host shell pane below
the Coordinator works). `sbx` stores the login, but it gives a sandbox the
login only when it creates it, so the first start creates just the Coordinator
and the second creates the role sandboxes. To switch Claude accounts, run
`factory reset` and `sbx secret rm anthropic`, then do the same.
Then tell the Coordinator what you want, for example *"Research how to add
rate limiting to the API, implement it with tests, and get it reviewed."*

To leave the factory running in the background, press **Ctrl+B** then **Q** in
Herdr; run `factory start` again to reopen it.

## Commands

| Command | What it does |
|---|---|
| `factory doctor` | Check the machine is ready; prints the fix for each problem (setup runs it first). Once the broker exists, it also runs the gateway's own doctor inside it: policy, tokens and grants, audit log, credentials, a live probe, GitHub access per repo, and the policy's hosts against the broker kit |
| `factory setup` | One-time setup (safe to re-run) |
| `factory update [--yes]` | After pulling changes or editing kits, images or policy: pulls the latest factory, rebuilds, recreates only the sandboxes the changes affect (it lists them and what each loses, then asks), and restarts |
| `factory start [dir]` | Start the factory for a project (default: current folder) and open Herdr |
| `factory status` | Project, broker, dispatch bridge, login, and sandbox status |
| `factory logs <role>` | Follow a role's transcript; `factory logs broker` / `dispatch` for audit logs |
| `factory roles` | List roles, their project access and network tools |
| `factory role new <name> [like]` | Add a role (copied from `reviewer`, or the role you name) |
| `factory analytics` | Cost, token and performance report from the audit logs (`--json`, `--html`) |
| `factory secret set <name>` | Store a credential (for keychain-less machines); reads from stdin |
| `factory secret rm <name>` | Remove a manually-stored credential |
| `factory secret ls` | List manually-stored credentials |
| `factory stop` | Stop the agents, broker and dispatch bridge (state is kept) |
| `factory reset` | Remove the agent sandboxes to switch projects (your login is kept) |

`factory start` on a different folder offers to switch projects for you.
Inside an existing Herdr pane, `factory start` uses that Herdr session; from a
normal terminal it uses a dedicated Herdr session named `factory`.

> If you already have `sbx` from Homebrew at a different version, either run
> `brew uninstall sbx` or put `~/.local/bin` ahead of `/opt/homebrew/bin` in `PATH`.

### Local settings

The repo is generic: it works on any project, personal or work, and holds
nothing specific to one organization. Settings that belong to you or your
organization, such as a private npm registry, your Jira site and the GitHub
repos agents may push to, live in the factory config folder on your machine,
`~/.config/ai-code-factory/` (`$XDG_CONFIG_HOME/ai-code-factory/` if you set
it). Start from the example:

```sh
mkdir -p ~/.config/ai-code-factory
cp -R config.example/. ~/.config/ai-code-factory/   # then edit, keeping only what you need
factory update
```

- `policy.toml` overrides `broker/policy.toml` table by table: a key it sets
  replaces the repo's value, and keys it leaves out keep it. `factory setup`
  bakes it into the broker image next to the repo's policy, so agents can't
  read or change it.
- `broker-kit/spec.yaml` is a kit adding broker egress hosts that your
  `policy.toml` needs, such as your private registry's host. It applies to the
  broker sandbox only (agent sandboxes still reach only Anthropic), and only
  when that sandbox is created; `factory update` recreates it when the kit changes.
- `projects/<project-folder-name>.md` holds optional notes for one project,
  such as its test command, added to every role's instructions on `factory start`
  (Roles, "Per-project notes"). These need no rebuild.

The folder sits outside the repo on purpose. When the factory works on its own
repo, the developer can write anywhere in it, and a change to a git-ignored
file there wouldn't show up in `git diff`. Settings you don't use are inert:
`npm_fetch` only contacts a private registry for lockfile entries that point
there, so one machine can serve both work and personal projects. If your team
shares settings, keep a copy of the folder somewhere for teammates to copy.

### Enabling git push and pull requests

1. Add the repos agents may use to `[git].repos` in your local `policy.toml`
   (Local settings; for example `"my-org/my-project"`), then run `factory setup` to rebuild.
2. Store a GitHub fine-grained token scoped to those repos (Contents: read/write,
   Pull requests: read/write) **for the broker sandbox only**:
   ```sh
   sbx secret set github --sandbox factory-broker
   ```
Agents may only push branches starting with `factory/`. They can never push to
protected branches (`main`, `master`, `release/*`) and can't force-push.

### Enabling a private npm registry

`npm_fetch` downloads public packages from registry.npmjs.org without any
setup. For packages from a private registry (JFrog Artifactory, GitHub
Packages, Verdaccio, ...), set `[packages].npm_private_registry` to its URL
prefix in your local `policy.toml` and add its host to your local
`broker-kit/spec.yaml` (Local settings). It also needs a read-only token. Like the Gemini key,
it can't be proxy-injected, so it lives in the host OS keychain and is
delivered as a file **inside the broker sandbox only**
(`/etc/factory-broker/secrets/npm-registry`, read fresh on every call).
Without it, `npm_fetch` still mirrors the public packages and lists the
private ones it couldn't fetch.

1. Create a read-only token for the registry. For JFrog, an access or identity
   token with read permission on the npm repository; it is sent as
   `Authorization: Bearer <token>`.
2. Store it in the host keychain under service `factory-npm-registry`. On macOS
   use account `factory`; the broker looks the entry up by service only.
   - macOS: `security add-generic-password -U -s factory-npm-registry -a factory -w`
     (prompts for the value; `-U` replaces an existing entry, which
     `add-generic-password` otherwise refuses with "already exists")
   - Linux: `secret-tool store --label "Factory npm registry token" service factory-npm-registry`
     (replaces any existing value)
   
   No keychain (WSL2, headless Linux)? Run `factory secret set npm-registry` and paste the token.
   To revoke, run `factory secret rm npm-registry`.

   The value decides the auth scheme:

   | Value | Sent as |
   |---|---|
   | `<token>` | `Authorization: Bearer <token>` |
   | `<user>:<token-or-password>` | `Authorization: Basic base64(<user>:<token-or-password>)` |
   | `Bearer ...` or `Basic ...` | used verbatim, e.g. a `.npmrc` `_auth` value as `Basic <_auth>` |
3. Run `factory start` (or `./scripts/start-broker.sh <project-dir>`) again to
   deliver it. To rotate or revoke it, change or delete the keychain entry
   (`security delete-generic-password -s factory-npm-registry`) and re-run, as
   for Gemini below.

To check a token from the host, request a tarball your lockfile pins without
following redirects:
```sh
curl -s -D - -o /dev/null -H "Authorization: Bearer $(security find-generic-password -s factory-npm-registry -w)" \
  "<registry prefix>@scope/name/-/@scope/name-1.0.0.tgz" | grep -iE '^(HTTP|location)'
```
A 200, or a 302 whose `Location` is the registry's storage host, means the
token works. A 401 or 403 means it was rejected.

The registry prefix is the part of your lockfile's `resolved` URLs before
`@scope/name/-/...` and must end in `/`, e.g.
`https://acme.jfrog.io/acme/api/npm/npm-local/`.

**Redirects.** Some registries answer an authorized download with a redirect
to signed cloud storage. JFrog Cloud does this for all but the smallest files,
sending a 302 to an `*.s3.amazonaws.com` URL. `npm_fetch` follows up to 5
redirects, https only. It sends the token only to URLs under the registry
prefix and drops it when a redirect leaves it: a signed URL carries its own
auth, and the token must not reach another host. A redirect may leave the
registry only for a host listed in `[packages].npm_redirect_hosts` in your
local `policy.toml`, and that host must also be in your local
`broker-kit/spec.yaml`. When `npm_fetch` refuses a redirect, its
`DENIED by broker policy` line names the host to add.

**Reading `npm_fetch` errors.** Each failed package gets one line: the HTTP
status, the URL (query strings, where signed URLs keep their credentials,
are redacted), and whether the token was attached.
- `HTTP 401/403 ... (credential attached): the credential was rejected`: fix
  the keychain entry.
- `DENIED by broker policy: HTTP 302 redirect ... host X is not in
  [packages].npm_redirect_hosts`: add X to your local settings.
- `could not reach X`: the host isn't in the broker kit.
- `more than 5 redirects` or `has no Location header`: a registry problem.

### Enabling Gemini Flash queries

The researcher's `gemini_flash_query` tool lets it send a prompt to Gemini Flash
for cheap/fast summarization or triage. This is optional; without a key the tool
just returns a DENIED error. `gemini` is not an `sbx` secret service, so unlike
GitHub the key can't be proxy-injected. Instead it lives in the host OS keychain
and is delivered as a file **inside the broker sandbox only**, never seen by any
agent sandbox (the narrowly-scoped exception documented in this repo's
`CLAUDE.md`, under "Credentials"):

1. Store the key in the keychain, service `factory-gemini`:
   - macOS: `security add-generic-password -s factory-gemini -a factory -w` (prompts for the key)
   - Linux: `secret-tool store --label "Factory Gemini key" service factory-gemini` (prompts for the key)
   
   No keychain (WSL2, headless Linux)? Run `factory secret set gemini` and paste the key.
   To revoke, run `factory secret rm gemini`.
2. Run `factory start` (or `./scripts/start-broker.sh <project-dir>` directly) again to
   deliver it to the broker sandbox. Re-running after changing the keychain entry
   rotates the key the broker uses.

**To revoke** a delivered key, revoke it in Google AI Studio and delete the
keychain entry (macOS: `security delete-generic-password -s factory-gemini`;
Linux: `secret-tool clear service factory-gemini`) and run
`./scripts/start-broker.sh <project-dir>` again - it removes the now-stale
file from the broker sandbox instead of leaving it usable. If the keychain
prompt is denied or the keychain is locked when `start-broker.sh` runs, the
lookup returns nothing and the script treats that the same as a revoked
entry: it still removes the delivered file, so the tool fails closed instead
of continuing to work off the old key.

### Enabling Jira

The Coordinator's `jira_my_issues` and `jira_get_issue` tools give it **read-only**
access to your own assigned issues on a Jira Cloud site. This is optional;
until you set the site and add a token, the tools return DENIED errors with
setup hints.

If you already ran `factory start` before this repo had Jira support, first do the
one-time upgrade below (Operating, "Upgrading") so the Coordinator's sandbox and the
broker image actually pick up the new tools - otherwise the steps below deliver a
credential the broker can't use yet.

1. In your local `policy.toml` (Local settings), set `[jira].site` (e.g.
   `"acme.atlassian.net"`) and `[jira].cloud_id`, the `cloudId` that
   `https://<site>/_edge/tenant_info` returns (public tenant info, not a
   secret). Then run `factory update`.
2. Create a **scoped** API token at id.atlassian.com → Security → API tokens →
   Create API token with scopes, granting only `read:jira-work` and `read:jira-user`.
3. Store it in the host keychain, service `factory-jira`, as `you@example.com:TOKEN`:
   - macOS: `security add-generic-password -s factory-jira -a factory -w` (prompts;
     enter `you@example.com:TOKEN` as the value)
   - Linux: `secret-tool store --label "Factory Jira token" service factory-jira`
     (prompts; enter `you@example.com:TOKEN` as the value)
   
   No keychain (WSL2, headless Linux)? Run `factory secret set jira` and paste `you@example.com:TOKEN`.
   To revoke, run `factory secret rm jira`.
4. Run `factory start` (or `./scripts/start-broker.sh <project-dir>` directly) again to
   deliver it to the broker sandbox.

**To revoke** a delivered token, revoke it at id.atlassian.com → Security →
API tokens and delete the keychain entry (macOS: `security
delete-generic-password -s factory-jira`; Linux: `secret-tool clear service
factory-jira`) and run `./scripts/start-broker.sh <project-dir>` again - it
removes the now-stale file from the broker sandbox instead of leaving it
usable. If the keychain prompt is denied or the keychain is locked when
`start-broker.sh` runs, the lookup returns nothing and the script treats that
the same as a revoked entry: it still removes the delivered file, so the tool
fails closed instead of continuing to work off the old token.

Like every broker credential, the token is never visible to the Coordinator or any
role: the broker reads it from a file inside its own sandbox and returns only the
issue data the tools ask for.

### Enabling approvals (Passdesk Pro)

With the Passdesk Pro plugin installed, every `git_push` and
`github_create_pr` waits for a person: the broker records a card (what,
who, how many commits and files, the secret-scan result) and the agent's
call proceeds once you approve it, or hears your reason if you decline. A
call that waits longer than `[approvals].hold_seconds` (45 by default)
returns to the agent with the approval id and asks it to retry the same
call later; the decision is picked up by the retry.

1. Put the `passdesk_pro` package in `~/.config/ai-code-factory/plugins/`
   (Local settings) and store the license key in the host keychain under
   service `factory-passdesk-license`, as for Gemini above. Optionally store
   a Slack, Discord or ntfy webhook URL under `factory-approvals-webhook` to
   be told when something is waiting.
   
   No keychain (WSL2, headless Linux)? Run `factory secret set passdesk-license` to deliver the
   license key, and `factory secret set approvals-webhook` for the webhook URL.
   Revoke with `factory secret rm passdesk-license` or `factory secret rm approvals-webhook`.
2. Run `factory update`: it bakes the plugin into the broker image, names it
   in `PASSDESK_PLUGINS` when it creates the broker sandbox, and delivers
   the two credentials. `factory doctor` then reports the license.
3. Decide with `factory approvals` (list), `factory approvals approve <id>`
   or `factory approvals decline <id> --reason "..."`; `factory approvals
   watch` prints cards as they arrive. Decisions are plain files in the
   broker state folder, so nothing new listens on the network.

## Roles

Three roles, split by what each is allowed to do rather than by job title:

| Role | Does | Project access | Broker tools |
|---|---|---|---|
| developer | implements the change, writes and runs its tests, updates the docs | read-write | `npm_fetch`, `pip_download`, `git_fetch`, `git_push`, `github_create_pr` |
| reviewer | independent check of correctness, tests, security and docs; runs the tests; APPROVED / REVISE | read-only | `npm_fetch` |
| researcher | looks things up on the web and reports with sources | read-only | `web_fetch`, `gemini_flash_query` |

Models, set per role in `role.conf` `MODEL` (aliases resolve to the newest
model of that tier):

| Agent | Model | Why |
|---|---|---|
| Coordinator | `opus` | Few calls, but its task specs and batching set the cost and quality of all the work below it |
| developer | `sonnet` | Most of the tokens; Sonnet is built for everyday agentic coding |
| reviewer | `opus` | Independent judgment and security review, and a different model from the one that wrote the code |
| researcher | `sonnet` | Judges sources and resists text planted in web pages; low volume |

If you hit your subscription's usage limits, move the Coordinator to `sonnet`
first.

**Per-task model and effort.** `MODEL` is each role's default. The Coordinator
can run a single task on another model from the role's `MODELS` list, and set
claude's reasoning `--effort` (`low`, `medium`, `high`, `xhigh`; `EFFORT` in
`role.conf` sets a role's default). It picks the cheapest setting that will do
the task, e.g. haiku at low effort for a rename or a doc update, and steps up
(opus, or higher effort) for hard problems or after a cheaper run fell short.
The bridge accepts only models listed for the role, and each job's result shows
the model and effort it ran with:

| Role | Default | Coordinator may also pick |
|---|---|---|
| developer | `sonnet` | `haiku`, `opus` |
| reviewer | `opus` | `sonnet` (small, low-risk diffs) |
| researcher | `sonnet` | `opus` (no `haiku`: this role reads untrusted web pages) |

A resumed session keeps the model it was saved with, and each model has its own
prompt cache, so a task on a different model from the role's saved session
starts a fresh session.

The split follows the capabilities that matter:

- **Only the developer can change the project.**
- **The reviewer judges work it didn't write** and can't alter it.
- **Only the researcher reads the web.** Web pages are the main way
  prompt-injection attacks arrive, and anything fetched lands in a role that
  can't modify your project. The developer gets the researcher's findings only
  as a report relayed by the Coordinator.

For a deeper security pass, the Coordinator can ask the reviewer for a
security-only review.

### Running tests

The project folder is shared with your machine, so its `node_modules` holds
your machine's native binaries (on a Mac, e.g. rollup's `darwin-arm64` build),
which don't load in the Linux sandboxes. Roles therefore never install into the
project. Instead:

1. The role calls the broker's `npm_fetch` tool. The broker reads `yarn.lock`
   (v1) or `package-lock.json` as data and downloads every tarball it pins,
   for all platforms, into `.factory/npm-mirror/`. Each one must come from an
   allowed registry, pass the `[packages]` policy and match its lockfile hash.
   No package manager or package code runs in the broker. The first fetch of a
   typical frontend project is a few hundred MB, and later calls only download
   what changed.
2. The role runs `factory-test -- <command>`, e.g. `factory-test -- npx vitest run`.
   With `-q` it saves the full output to a log and prints only the last 40
   lines (where test runners put the summary and failures) and the log's path.
   If the run fails, it first lists the failure lines from above those 40
   (vitest/jest `FAIL`, pytest `FAILED`, `tsc` and eslint errors, ...), so the
   role rarely has to search the log.
   For one mid-size frontend suite that is 3.7 KB instead of 122 KB in the role's context.
   Roles use it for full-suite runs.
   `factory-test` (baked into the developer and reviewer images) works in a
   copy of the project on the sandbox's own disk, so nothing is written to the
   project and the read-only reviewer can run tests too. It sets `CI=true` so
   runners like Vitest and Jest don't start in watch mode. Snapshots or
   coverage the tests write stay in the copy. Every run says which mode it used:
   - **clean** (tried first): syncs the project, minus `node_modules` and
     `.git`, and installs dependencies offline from the mirror, exactly as the
     lockfile pins them, with install scripts disabled. It reinstalls only
     when the lockfile changes.
   - **host fallback** (if the clean install fails, e.g. a package is missing
     from the mirror; or force it with `-m host`): copies the project together
     with your machine's own `node_modules`. For each package with
     platform-specific optional dependencies (esbuild, rollup, swc, ...), it
     unpacks the sandbox's build from the mirror, at the installed version,
     next to your machine's build, nested copies included. Your real
     `node_modules` is never modified. Results then reflect your installed
     versions, not a clean lockfile install, and the output says so. If a
     needed build isn't in the mirror, it names the package and version.

**Per-project notes.** Roles learn a project's quirks, such as its non-watch
test command, from an optional `~/.config/ai-code-factory/projects/<project-folder-name>.md`.
`factory start` appends it to every role's instructions. It lives outside the
project, so projects need no factory files, and roles can't change it. For example:
```markdown
- Tests: `factory-test -q -- npx vitest run` (`npm test` starts watch mode).
- Type check: `factory-test -q -- npx tsc --noEmit`.
```

The images also include yarn 1.22.22, so `yarn` scripts and git hooks that
call `yarn` work. Python projects still use `pip_download` and `uv` directly.

Each role is `roles/<name>/role.conf` (description, image, `WORKSPACE=rw|ro`,
`BROKER_TOOLS`, `MODEL`, `MODELS`, `EFFORT`, `MAX_SESSION_TOKENS`), `roles/<name>/instructions.md` (passed to every task
as part of the system prompt) and `roles/<name>/kit/spec.yaml` (its network policy). Read-only roles
get a writable scratch directory in `~/.local/state/ai-code-factory/roles/<name>/` and the project mounted `:ro`.

**Add a role:**
```sh
factory role new designer                # copies reviewer (read-only; only network tool: npm_fetch)
factory role new docs-writer developer   # or copy any existing role
```
Edit its `role.conf` and instructions, then run `factory start` again. It creates
the sandbox and a tab, and the Coordinator sees the new role in `list_roles`.

**Change a role:** edit instructions, `BROKER_TOOLS`, `MODEL`, `MODELS`, `EFFORT` or `MAX_SESSION_TOKENS` and run `factory start`
again. All of these take effect from the next task:
`factory start` copies the instructions into the state folder and the bridge
reads that copy, so a role editing this repo can't change prompts on its own. Kit, image, project access and
whether the role has broker access at all are fixed when the sandbox is created,
so for those run `factory update`, which recreates that role's sandbox. Each role's tab shows the
job session's main model once the job reports it (subagents it spawns may use others).

**Change the Coordinator's model:** unlike a role's `MODEL` (read fresh on every
dispatch), the Coordinator's model is baked into its kit's managed settings
(`kits/coordinator/spec.yaml`, `"model"` in the `/etc/claude-code/managed-settings.json`
written at setup) and only applied when its sandbox is created. It defaults to
`opus` (see the models table above); `/model` in the Coordinator pane can still
switch models for that session. To change the default,
edit that `"model"` value, then run `factory update` to recreate the Coordinator
with the new setting; recreating it discards its conversation history.

**Remove a role:** delete `roles/<role>/` and run `factory start`. It closes the role's
tab and removes its broker access, and it prints the `sbx rm` command for the role's
sandbox (sandboxes are never deleted automatically).

## Broker tools

| Tool | What it does |
|---|---|
| `web_fetch` | HTTPS GET from `[web].allow_hosts`; redirects re-checked; HTML → text; output marked untrusted |
| `npm_fetch` | Downloads every tarball `yarn.lock` (v1) or `package-lock.json` pins into `.factory/npm-mirror/` (no code runs); each is policy-checked and verified against its lockfile hash; roles install offline with `factory-test` |
| `pip_download` | Downloads binary wheels for a hash-pinned `requirements.txt` (every line `name==version --hash=sha256:...`, no option lines; generate it with `uv pip compile --generate-hashes`) into `.factory/wheels/` (no code runs); pip only sees a copy the broker writes itself; agent installs offline with `uv` |
| `git_fetch` | Fetches a branch into a private mirror and hands it to the agent as a bundle in `.factory/incoming/` |
| `git_push` | Takes a bundle from `.factory/outgoing/` (size-capped, copied once into the broker), checks branch rules, secret-scans every commit's patch (binary files included), message and author/committer, pushes |
| `github_create_pr` | Opens a (draft) PR; title and body are secret-scanned |
| `gemini_flash_query` | Sends a prompt to Gemini Flash for cheap/fast summarization or triage; prompt is secret-scanned and size-limited; output marked untrusted |
| `jira_my_issues` | Lists Jira Cloud issues assigned to the caller (`status`: `todo`/`in_progress`/`all`, default not-Done); read-only; output marked untrusted |
| `jira_get_issue` | One Jira Cloud issue the caller is assigned to: summary, status, priority, description, recent comments; read-only; output marked untrusted |

The broker never runs anything from the agent-writable workspace. Git network
operations happen in private mirrors inside the broker VM, code moves in and out
as git bundles, and npm install scripts are disabled. `.factory/` in the project
is git-ignored automatically.

## Analytics

`factory analytics` aggregates `dispatch/audit.jsonl` (job-end records: cost,
duration, context size, model), `broker/audit.jsonl` (individual broker tool
calls) and `dispatch/sessions/<role>.json` (each role's current session totals)
in the state folder (see Operating) into a cost and performance report. It only reads these files — nothing is sent
anywhere — and runs entirely on the host, using the same Python resolution as
the dispatch bridge (`resolve_python` in `scripts/lib/common.sh`): the pinned
system Python on macOS, or `python3` from PATH on Linux.

```sh
factory analytics                # human-readable table (default)
factory analytics --json         # for scripting, e.g. factory analytics --json | jq '.roles'
factory analytics --html         # writes dispatch/analytics.html in the state folder and opens it
factory analytics --html > report.html   # not a terminal: prints the HTML page instead
factory analytics --top 20       # list the 20 most expensive jobs instead of 10
```

The report covers: total and per-role cost, average cost and duration per job,
success rate (done vs. failed/cancelled), each role's current context size and
whether it's growing, the most expensive jobs, a coarse cost-by-task-type
breakdown (bucketed by the task's first word, e.g. "add", "fix", "review"),
cost by model (from the `model` each job actually reported — see the
`factory logs <role>` transcripts for the model a specific job used;
`factory roles` only shows each role's *configured* model), cost over the
last 7/30 days, and jobs whose cost or duration is
a statistical outlier for their role. Missing or malformed log lines are
skipped and reported as warnings rather than failing the report; a factory that
hasn't dispatched any jobs yet just shows an empty report.

## Operating

- **Logs:** `factory logs <role>` follows a role; `factory logs broker` and
  `factory logs dispatch` follow the audit logs. The raw files are in the
  state folder, `~/.local/state/ai-code-factory/` (`$XDG_STATE_HOME/ai-code-factory/`
  if you set `XDG_STATE_HOME`):
  - `dispatch/audit.jsonl`: every Coordinator tool call and job
  - `dispatch/logs/<role>.log`: role transcripts
  - `dispatch/sessions/<role>` (session id) and `<role>.json` (its total cost
    and context size after the last job)
  - `broker/audit.jsonl`: every broker call, with the calling role
  - Server errors: `dispatch/dispatch.log` and `broker/broker.log`
- **Why state is outside the repo:** the broker re-reads its tool grants
  (`broker/roles.json`) and token hashes on every call. Inside the repo they would
  be editable by any role that can write to the project, which includes the
  developer when the factory works on its own repo. Only the broker sandbox and
  the dispatch bridge see the state folder. `factory start` moves state from the
  old in-repo location (`.broker/`, `.dispatch/`, `.roles/`) the first time, and
  tells you which sandboxes to recreate.
- **Working on the factory with the factory:** point it at this repo as usual.
  Review `git diff` before restarting anything: `scripts/`, `bin/factory` and
  `dispatch/server.py` run on your Mac with your permissions, and roles, kits and
  broker policy change what the sandboxes may do.
- **Changing broker policy:** edit `broker/policy.toml` (and `kits/broker/spec.yaml` if
  you add a web host) for changes everyone should get, or your local `policy.toml`
  and `broker-kit/` (Local settings) for your own. Then run `factory update`. The
  policy is built into the image, so agents can't change it.
- **Upgrading:** run `factory update`. It pulls this checkout (fast-forward only,
  with its submodules, and only when it has no local changes - a `broker/` pin
  bump by itself never counts as a local change, since `factory update` moves
  `broker/` to the new pin as part of the same pull), runs `factory setup` to
  rebuild the images, then recreates exactly the sandboxes whose inputs changed and restarts
  the factory, dispatch bridge included. A sandbox keeps the image, kit,
  project access and broker access it was created with, so `factory start`
  alone never picks those up. It only says which sandboxes are out of date.
  `factory update` records a fingerprint of those inputs for each sandbox it
  creates. Before it removes anything, it lists each out-of-date sandbox and
  what recreating it discards, then asks (`--yes` skips the question):
  - **Coordinator:** its conversation history.
  - **A role:** its saved session; the next task starts fresh.
  - **Broker:** a GitHub token stored with `--sandbox factory-broker`, because
    `sbx rm` deletes secrets scoped to the removed sandbox. Re-add it
    afterwards; `factory start` reminds you.

  Sandboxes created before fingerprints existed count as out of date once.
- **Coordinator briefing:** the Coordinator learns what the factory is from
  `/etc/claude-code/CLAUDE.md`, written from `kits/coordinator/spec.yaml` when its
  sandbox is created (a mixin kit's `agentInstructions` aren't loaded reliably), and
  from the dispatch bridge's MCP instructions, built from `roles/` on every new
  Coordinator session. After editing the kit's briefing, or `dispatch/server.py`,
  run `factory update`.
- **After a reboot:** `factory start`.
- **One checkout at a time:** every copy of this repo uses the same ports, sandbox
  names and sbx secrets. If `factory start` says port 18766 is already in use,
  a dispatch bridge from another copy is probably still running: run that copy's
  `scripts/03-stop-factory.sh` (or kill the pid shown), then `factory start` again.
- **Rotating tokens:** the sbx daemon caches them. Delete `broker/tokens/<role>.sha256`
  (or `dispatch/token.sha256`) in the state folder, run `factory start`, run `sbx daemon restart`, then
  `factory start` again.
- **Tests:**
  - broker: `docker run --rm --network none -v "$PWD/broker:/b:ro" -w /b factory-broker:v3 python3 -m unittest -v`
    (`broker/` is the `passdesk-broker` submodule; run `git submodule update --init`
    first if it's empty)
  - dispatch: `/usr/bin/python3 -m unittest discover -s dispatch -v` (macOS) or
    `python3 -m unittest discover -s dispatch -v` (Linux)
- **Under the hood:** `factory` wraps the scripts in `scripts/`:
  - `install-prereqs.sh`, `00-build-templates.sh` and `01-init-security.sh` (setup)
  - `02-launch-factory.sh <dir>`, which calls `start-broker.sh` and `start-dispatch.sh`
  - `03-stop-factory.sh` and `new-role.sh`

  They can still be run directly.

### Migrating from the pre-roles setup

If `factory start` reports "found the pre-roles broker setup", run the commands
it prints once. They remove the old Developer and Reviewer sandboxes and the
shared broker registration; the next launch recreates everything. Your Claude
login is kept.

## Layout

```
bin/factory                  The CLI
roles/<name>/role.conf       Role definition: description, image, rw/ro, broker tools
roles/<name>/instructions.md Role instructions (added to each task's system prompt)
roles/<name>/kit/spec.yaml   Role kit: Anthropic-only egress
kits/coordinator/spec.yaml   Coordinator kit
kits/coordinator/broker.conf Coordinator's direct broker tool grants (Jira)
kits/broker/spec.yaml        Broker kit: GitHub, registries, docs hosts
broker/                      passdesk-broker, as a git submodule: MCP server (runs in the broker VM), policy, tests
broker/passdesk/core/        Gateway core: protocol, auth and grants, policy, filters, audit
broker/passdesk/tools/       One module per broker tool (listed in broker/tools.txt)
dispatch/                    Dispatch bridge (runs on the host), tests
docs/broker-extraction.md    Roadmap for making the broker a standalone gateway
docs/setup-wsl.md            Step-by-step setup on Windows 11 with WSL2
windows/enable-wsl.ps1       Windows side of that setup: WSL, Ubuntu, nested virtualization
templates/<image>/           Pinned images: developer, reviewer (+ruff)
templates/agent/factory-test Runs tests in a sandbox-local copy (baked into developer and reviewer)
config.example/              Example local settings for ~/.config/ai-code-factory/
scripts/lib/common.sh        Pinned versions, checksums, names, shared checks
scripts/lib/roles.sh         Role parsing and validation
scripts/lib/preflight.sh     Platform checks shared by the installer and factory doctor
scripts/install-prereqs.sh   Install pinned sbx + herdr
scripts/00-build-templates.sh
scripts/01-init-security.sh
scripts/02-launch-factory.sh <project-dir>
scripts/03-stop-factory.sh
scripts/new-role.sh <name> [<role-to-copy>]
scripts/start-broker.sh, scripts/start-dispatch.sh   (called by the launch script)
scripts/analytics.py, scripts/test_analytics.py      Cost/performance report (factory analytics)
```

## Upgrading a pin

Change pins on purpose, in their own commit, and re-run the full setup to test.

- **Claude base image:** `docker buildx imagetools inspect docker/sandbox-templates:claude-code`,
  then copy the top-level `Digest:` into `templates/developer/Dockerfile` **and**
  `templates/reviewer/Dockerfile`.
- **Broker base image:** same with `docker/sandbox-templates:shell-docker` →
  `broker/Dockerfile` (in the `passdesk-broker` submodule).
- After changing any image, bump `TEMPLATE_TAG` (`v1` → `v2`) in `scripts/lib/common.sh`.
- **sbx / herdr:** update the version, URL and SHA-256 in `scripts/lib/common.sh`,
  for each platform you use:
  - **macOS:** use the `digest` of `DockerSandboxes-darwin.tar.gz` on the GitHub
    release; for herdr, use the `sha256` for `macos-aarch64` in
    https://herdr.dev/latest.json.
  - **Linux (x86_64):** use the `digest` of `DockerSandboxes-linux-amd64-ubuntu2404.deb`
    and `-ubuntu2604.deb` on the GitHub release (`SBX_DEB_UBUNTU2404_*`,
    `SBX_DEB_UBUNTU2604_*`), and of `herdr-linux-x86_64` on the herdr release
    (`HERDR_LINUX_X86_64_*`). Check each against the file you download
    (`sha256sum <file>`); never guess a checksum.
  Kit keys, CLI flags and lifecycle behavior have changed between sbx releases, so
  re-run `sbx kit validate` on every kit and the broker/dispatch lifecycle checks.
- **ruff:** change `RUFF_VERSION` in `templates/reviewer/Dockerfile`.
- **yarn:** change `YARN_VERSION` and `YARN_SHA256` in **both**
  `templates/developer/Dockerfile` and `templates/reviewer/Dockerfile`. Get the
  hash from the file itself:
  `curl -sL https://registry.npmjs.org/yarn/-/yarn-<version>.tgz | shasum -a 256`
  (and check it matches the registry's `dist.integrity`).
