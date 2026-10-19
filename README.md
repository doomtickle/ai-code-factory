# AI Code Factory

[![CI](https://github.com/doomtickle/ai-code-factory/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/doomtickle/ai-code-factory/actions/workflows/ci.yml)
Docs site: <https://doomtickle.github.io/ai-code-factory/> (the same page `factory docs` opens locally)

A local multi-agent coding setup. You talk to a **Coordinator** agent; it breaks
the work down and delegates it to **role agents** (Developer, Reviewer and
Researcher, or any role you add). Every agent is
Claude Code running in its own Docker Sandboxes (`sbx`) microVM, laid out in
[Herdr](https://herdr.dev) tabs.

```
 You ─▶ Coordinator VM ──MCP──▶ dispatch bridge (host) ──sbx exec──▶ role VMs
        (can't edit files)      list_roles · delegate ·              claude -p, one task per lane
                                wait_for_job · cancel_job            (developer: in git clones)
                                      ▲                                    │
          your project ◀── git fetch ─┘ factory/* branches as bundles      │ MCP (per-role tools)
          (refs/remotes/factory-<lane>/)                                   ▼
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
  shell), and, for the developer, bring the branches it committed back into
  your project (see "Getting the developer's work"). The bridge also gives it
  the factory's own docs (`factory_docs`: this README and the guides, copied
  when the factory starts), so you can ask it about the factory itself from
  any project, e.g. *"how do I enable git push?"* or *"what does this
  `factory start` error mean?"*. It answers from the docs and tells you the
  command to run on your machine; it can't run it. You never type into role
  sandboxes and nothing is typed into a terminal for you. It can also be granted
  broker tools directly, with no role in between, in `kits/coordinator/broker.conf`
  (the same way a role's `BROKER_TOOLS` work); none by default. The read-only
  Jira tools are the opt-in example (see "Enabling Jira").
- **Roles:** defined as data in `roles/<name>/`. Each role has its own
  instructions, a kit (Anthropic-only network access), an image, a list of the
  broker tools it may use, and a number of **lanes**: how many tasks it runs at
  once (`LANES`, e.g. 3 for the developer). No role can write to your project:
  every role sandbox mounts it read-only. The developer (`WORKSPACE="rw"`)
  works in git clones of it on its sandbox's own disk, one per lane, and hands
  back committed `factory/*` branches. Read-only roles (`WORKSPACE="ro"`)
  just read it. Roles remember earlier tasks (each lane keeps a Claude
  session). You can watch every role work live in its Herdr tab. Job results show the job's cost, the
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

For a browsable version of these docs, with step-by-step guides, run
`./bin/factory docs` (it works offline, before setup).

`broker/` is the `passdesk-broker` repository as a git submodule; a plain
`git clone` leaves it empty. `factory setup` (and any other `factory`
command but `factory docs` and `factory help`) initializes it automatically on first run, so this is optional, but
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
- one tab per role, showing its live transcripts (all its lanes).

The folder must be a git repository, or a folder whose subfolders are git
repositories: the developer works in clones of them, from what is committed
(see "Getting the developer's work").

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

**Without Herdr:** `factory start --no-ui` starts everything but the terminal
layout. Talk to the Coordinator with `sbx run --name factory-coordinator` in
any terminal (type `/login` there the first time, then `factory start --no-ui`
again), and watch every lane with `factory watch` in another.

## Commands

| Command | What it does |
|---|---|
| `factory doctor` | Check the machine is ready; prints the fix for each problem (setup runs it first). Once the broker exists, it also runs the gateway's own doctor inside it: policy, tokens and grants, audit log, credentials, a live probe, GitHub access per repo, the policy's hosts against the broker kit, and (Passdesk Pro) a warning for any approval that expired with no decision |
| `factory setup` | One-time setup (safe to re-run) |
| `factory update [--yes]` | After pulling changes or editing kits, images or policy: pulls the latest factory, rebuilds, recreates only the sandboxes the changes affect (it lists them and what each loses, then asks), and restarts |
| `factory start [dir] [--no-ui]` | Start the factory for a project (default: current folder) and open Herdr. `--no-ui` (or `FACTORY_NO_UI=1`) skips Herdr: talk to the Coordinator with `sbx run --name factory-coordinator` in any terminal and follow roles with `factory logs <role>` |
| `factory status` | Project, broker, dispatch bridge, login, roles (with their lanes), sandbox status, and (Passdesk Pro) pending approvals and the oldest one's age |
| `factory logs <role>` | Follow a role's transcripts, all its lanes (`factory logs developer-lane2` for one lane); `factory logs broker` / `dispatch` for audit logs |
| `factory watch` | Live terminal view of the lanes: one pane per active lane (a job running, or finished in the last ten minutes) with the job's status, elapsed time, model and cost in its title and the transcript rendered for reading (tool calls condensed, results dimmed, text wrapped); idle lanes take one status line each. A digit focuses one lane, `a` shows every lane, `p` shows a lane's latest review packet, `q` quits. `--once` prints one plain-text frame |
| `factory packet [<job-id> \| latest \| <lane> \| --list]` | A job's review packet: the task, each branch's commits and diffstat against your checked-out branch, the `factory-test` modes it reported, the broker calls its role made, cost, and the role's report with its APPROVED / REVISE verdict. Written after every job; `--list` shows recent ones |
| `factory roles` | List roles, their project access, lanes and network tools |
| `factory role new <name> [like]` | Add a role (copied from `reviewer`, or the role you name) |
| `factory docs [--print]` | Open these docs, with guides, as a web page in your browser (offline, works before setup); `--print` writes the HTML to stdout instead |
| `factory approvals [approve <id> \| decline <id> --reason "..." \| watch]` | List, decide and watch Passdesk Pro approval cards (see "Enabling approvals") |
| `factory analytics` | Cost, token and performance report from the audit logs (`--json`, `--html`) |
| `factory secret save <name>` | Store a credential **once** in this machine's keychain (`github`, `jira`, `gemini`, `npm-registry`, `passdesk-license`, `approvals-webhook`); every broker start delivers it, including after `factory update` or `factory reset` |
| `factory secret forget <name>` | Delete a saved credential from the keychain and from the running broker |
| `factory secret set <name>` | Deliver a credential to the running broker only, without saving it (lost when the broker is recreated); reads from stdin |
| `factory secret rm <name>` | Remove a credential delivered with `set` |
| `factory secret ls` | List credentials delivered with `set` |
| `factory stop` | Stop the agents, broker and dispatch bridge (state is kept) |
| `factory reset` | Remove the agent sandboxes to switch projects (your login is kept) |

**Storing credentials once:** `factory secret save <name>` keeps the value in this
machine's keychain under `factory-<name>`: the macOS Keychain, the Linux keyring
(`secret-tool`), or, on WSL2, the **Windows Credential Manager** (encrypted under
your Windows login, no unlock prompt). From then on every broker start delivers it,
so recreating the broker (`factory update`, `factory reset`) needs nothing retyped.
The GitHub token is saved the same way (`factory secret save github`) and restored
as the broker-scoped sbx secret on every start. To rotate a credential, save it
again; to revoke it, `factory secret forget <name>`.

**`factory secret set`** delivers a value to the *current* running broker sandbox
only, and requires the broker to be running first. Any operation that recreates the broker (`factory reset`,
or `factory update` if the broker image changes) drops those values; the run that creates the new
broker sandbox (`factory start`, or `factory update` itself) lists which to set again.

`factory secret rm <name>` only removes the broker sandbox's copy; if the host
keychain still has an entry for `factory-<name>`, it warns that the next
`factory start` will deliver that entry again, and how to clear it too.

Inside an existing Herdr pane, `factory start` uses that Herdr session; from a
normal terminal it uses a dedicated Herdr session named `factory`.

> If you already have `sbx` from Homebrew at a different version, either run
> `brew uninstall sbx` or put `~/.local/bin` ahead of `/opt/homebrew/bin` in `PATH`.

### Switching projects

The factory serves one project at a time: its sandboxes are created for that
folder. `factory start <other-folder>` notices and offers to switch: it removes
the agent sandboxes (as `factory reset` does) and creates new ones for the new
folder. Your Claude login, settings and logs are kept; the roles' conversations
are not. Branches already imported into the old project (`factory-<lane>/...`)
stay there. To do it by hand, run `factory reset`, then `factory start <other-folder>`.

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
repo, the developer's branches can change anything in it once you merge them,
and the broker writes into the project's `.factory/`; settings must stay out of
reach of both. Settings you don't use are inert:
`npm_fetch` only contacts a private registry for lockfile entries that point
there, so one machine can serve both work and personal projects. If your team
shares settings, keep a copy of the folder somewhere for teammates to copy.

### Enabling git push and pull requests

1. Add the repos agents may use to `[git].repos` in your local `policy.toml`
   (Local settings; for example `"my-org/my-project"`), then run `factory update` to rebuild the broker image and recreate the broker sandbox.
2. Create a GitHub fine-grained token scoped to those repos (Contents: read/write,
   Pull requests: read/write, Workflows: read/write) and save it once:
   ```sh
   factory secret save github
   ```
   GitHub rejects pushes that change `.github/workflows/` files without the Workflows
   permission. It goes into this machine's keychain (`factory-github`), and every broker start
   registers it as an sbx secret **for the broker sandbox only**, so it survives
   `factory update` and `factory reset`. The same token keeps working until its
   GitHub expiry date; when it expires, create a new one and save it again.
   (Without a keychain: `sbx secret set github --sandbox factory-broker`, which
   has to be repeated whenever the broker sandbox is recreated.) Never store it
   globally (`sbx secret set github` without `--sandbox`): sbx would then inject it
   for every sandbox. If a token is missing it, update the token on GitHub and run
   `factory secret save github` again.
Agents may only push branches starting with `factory/`. They can never push to
protected branches (`main`, `master`, `release/*`) and can't force-push. The
developer runs `factory-lane bundle` in its sandbox, which writes the branch
(only the commits past its base) as a bundle into `.factory/outgoing/` in the
project (the one project folder it can write) and prints the file name it
then hands to `git_push`.

### Letting the reviewer file issues

The reviewer has `github_create_issue`, which stays inert until you list the
repos it may file in:

1. Add them to `[github].issue_repos` in your local `policy.toml`, e.g.
   `issue_repos = ["my-org/tracker"]`. List **private** repos only: an issue
   in a public repo publishes whatever an agent writes. It is a separate list
   from `[git].repos` on purpose.
2. Give the GitHub token **Issues: read/write** on those repos, save it again
   (`factory secret save github`), and run `factory update`.

It only files issues (existing labels, no assignees, no edits or closing),
and every issue is secret-scanned and audited. To see each one before it is
filed, add `"github_create_issue"` to `[approvals] tools` (Passdesk Pro).

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
   
   Or, on any machine including WSL2: `factory secret save npm-registry` and paste the token
   (see *Storing credentials once* above). To revoke: `factory secret forget npm-registry`.

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
for cheap/fast summarization or triage. It is an opt-in: the researcher is not
granted it by default, and without a key the tool just returns a DENIED error.
`gemini` is not an `sbx` secret service, so unlike
GitHub the key can't be proxy-injected. Instead it lives in the host OS keychain
and is delivered as a file **inside the broker sandbox only**, never seen by any
agent sandbox (the narrowly-scoped exception documented in this repo's
`CLAUDE.md`, under "Credentials"):

1. Grant the tool: add `gemini_flash_query` to `BROKER_TOOLS` in
   `roles/researcher/role.conf` (so it reads
   `BROKER_TOOLS="web_fetch gemini_flash_query"`). Grants are read on every
   start, so no sandbox is recreated.
2. Store the key in the keychain, service `factory-gemini`:
   - macOS: `security add-generic-password -s factory-gemini -a factory -w` (prompts for the key)
   - Linux: `secret-tool store --label "Factory Gemini key" service factory-gemini` (prompts for the key)
   
   Or, on any machine including WSL2: `factory secret save gemini` and paste the key
   (see *Storing credentials once* above). To revoke: `factory secret forget gemini`.
3. Run `factory start` (or `./scripts/start-broker.sh <project-dir>` directly) again to
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
access to your own assigned issues on a Jira Cloud site. They are an opt-in:
the Coordinator is granted no broker tools by default, and until you set the
site and add a token, the tools return DENIED errors with setup hints.

1. Grant the tools: in `kits/coordinator/broker.conf` set
   `BROKER_TOOLS="jira_my_issues jira_get_issue"`. In your local `policy.toml`
   (Local settings), set `[jira].site` (e.g. `"acme.atlassian.net"`) and
   `[jira].cloud_id`, the `cloudId` that `https://<site>/_edge/tenant_info`
   returns (public tenant info, not a secret). Then run `factory update`: it
   rebuilds the broker image and recreates the Coordinator's sandbox, which
   gains broker access only when it is created (its conversation history is
   discarded, as `factory update` says before it asks).
2. Create a **scoped** API token at id.atlassian.com → Security → API tokens →
   Create API token with scopes, granting only `read:jira-work` and `read:jira-user`.
3. Store it in the host keychain, service `factory-jira`, as `you@example.com:TOKEN`:
   - macOS: `security add-generic-password -s factory-jira -a factory -w` (prompts;
     enter `you@example.com:TOKEN` as the value)
   - Linux: `secret-tool store --label "Factory Jira token" service factory-jira`
     (prompts; enter `you@example.com:TOKEN` as the value)
   
   Or, on any machine including WSL2: `factory secret save jira` and paste `you@example.com:TOKEN`
   (see *Storing credentials once* above). To revoke: `factory secret forget jira`.
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
2. Run `factory update`: it bakes the plugin into the broker image and names it
   in `PASSDESK_PLUGINS` when it creates the broker sandbox. `factory doctor`
   then reports the license.
3. From the keychain, the credentials are delivered automatically when the broker
   starts. On any machine, including WSL2: `factory secret save passdesk-license`
   and paste the license key, then `factory secret save approvals-webhook` and paste the webhook URL
   (see *Storing credentials once* above). Revoke with `factory secret forget passdesk-license`
   or `factory secret forget approvals-webhook`.
4. Decide with `factory approvals` (list), `factory approvals approve <id>`
   or `factory approvals decline <id> --reason "..."`; `factory approvals
   watch` prints cards as they arrive. Decisions are plain files in the
   broker state folder, so nothing new listens on the network.
5. `factory status` shows a line with the pending count and the oldest
   card's age once there is at least one; `factory doctor` warns (without
   failing) about any card that expired with no decision, pointing back at
   `factory approvals`. Both are silent when the plugin isn't in use.

## Roles

Three roles, split by what each is allowed to do rather than by job title:

| Role | Does | Project access | Lanes | Broker tools |
|---|---|---|---|---|
| developer | implements the change, writes and runs its tests, updates the docs | read-only; works in its own clones, hands back `factory/*` branches | 3 | `npm_fetch`, `pip_download`, `git_fetch`, `git_push`, `github_create_pr` |
| reviewer | independent check of correctness, tests, security and docs; runs the tests; APPROVED / REVISE; files issues when asked | read-only | 2 | `npm_fetch`, `github_create_issue` |
| researcher | looks things up on the web and reports with sources | read-only | 1 | `web_fetch` (`gemini_flash_query` as an opt-in) |

**Lanes.** A role with `LANES="3"` runs up to three tasks at once, in lanes
named `developer`, `developer-lane2` and `developer-lane3`. Each lane has its
own Claude session and transcript, and, for the developer, its own clones;
all lanes share the role's one sandbox. The Coordinator gives independent
chunks to separate lanes, each on its own branch, and sends a follow-up (a
REVISE, say) back to the lane that did the work. `LANES` is read per task (1
to 8; empty means 1), so changing it needs no new sandbox; `factory start`
updates the role's tab. Several lanes share the sandbox's CPU and memory and
your Claude subscription's usage limits, so more lanes isn't always faster.

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
the task, e.g. haiku at low effort for a doc-only change or a push (never for
code, tests or configuration), and steps up
(opus, or higher effort) for hard problems or after a cheaper run fell short.
The bridge accepts only models listed for the role, and each job's result shows
the model and effort it ran with:

| Role | Default | Coordinator may also pick |
|---|---|---|
| developer | `sonnet` | `haiku` (prose and push chores only, never code), `opus` |
| reviewer | `opus` | `sonnet` (small, low-risk diffs) |
| researcher | `sonnet` | `opus` (no `haiku`: this role reads untrusted web pages) |

A resumed session keeps the model it was saved with, and each model has its own
prompt cache, so a task on a different model from the role's saved session
starts a fresh session.

The split follows the capabilities that matter:

- **No role can change your project.** The developer proposes changes as
  branches; you merge them.
- **The reviewer judges work it didn't write** and can't alter it.
- **Only the researcher reads the web.** Web pages are the main way
  prompt-injection attacks arrive, and anything fetched lands in a role that
  can't modify your project. The developer gets the researcher's findings only
  as a report relayed by the Coordinator. Pages come through the broker's
  `web_fetch` (allowlisted hosts, untrusted-content markers, audit log). The
  researcher also keeps Claude Code's built-in `WebSearch`, which the model API
  serves: it runs outside the broker, so its results are neither allowlisted
  nor audited, and the role's instructions treat them as leads to verify
  through `web_fetch`, never as sources (its report lists them under
  "Unverified"). The role kits deny the built-in `WebFetch` for every role (the
  sandboxes have no egress for it) and `WebSearch` for the developer and
  reviewer, through root-owned managed settings like the Coordinator's.

For a deeper security pass, the Coordinator can ask the reviewer for a
security-only review.

### Getting the developer's work

The developer works in a lane: git clones of your project's repositories on
its sandbox's own disk, cloned from the read-only project and fetched again
before every task. It bases new work on your checked-out branch (or the base
the task names) and commits to a `factory/<topic>` branch. It sees only what
is committed: ask it to work on uncommitted changes and it will say they're
missing, so commit first.

After each developer job, whether it finished, failed or was cancelled, the
dispatch bridge brings the lane's committed `factory/*` branches into your
repository as remote-tracking branches named after the lane:

```sh
git branch -r | grep factory-                        # factory-developer/factory/rate-limit, ...
git log -p main..factory-developer/factory/rate-limit
git merge factory-developer/factory/rate-limit      # or: git switch -c rate-limit factory-developer/factory/rate-limit
```

The job's result lists them (`branches`: repo, ref and commit), and the
Coordinator tells you which one to merge once it is reviewed. The reviewer
checks out the same ref in its own sandbox. When the project is a folder of
repositories, each branch arrives in the repository it belongs to.

Nothing writes your working tree outside the git-ignored `.factory/` (the
developer's `.factory/outgoing/`, the broker's downloads), and nothing writes
your `.git` except that import: the bridge asks the developer's sandbox for a
git bundle of the lane's `factory/*` branches (minus what your repository
already has: the sandbox reads your repository's own ref tips, not its clone's
`origin/*` refs, which the developer may have moved by importing a `git_fetch`
bundle), then runs one fixed `git fetch` of it into
`refs/remotes/factory-<lane>/`, with hooks and `core.fsmonitor` forced off.
If that fetch still finds a base commit missing, the bridge asks once more for
the branches' whole history and says so in the lane's transcript.
No git hook or config from the sandbox can run on your machine. It never prunes, so a branch stays until you
delete it (`git branch -rd factory-developer/factory/rate-limit`). Work the
developer didn't commit stays in its lane for the next task there.

### Running tests

The project folder is shared with your machine, so its `node_modules` holds
your machine's native binaries (on a Mac, e.g. rollup's `darwin-arm64` build),
which don't load in the Linux sandboxes. Roles therefore never install into the
project. Instead:

1. The role calls the broker's `npm_fetch` tool. The broker reads `yarn.lock`
   (v1) or `package-lock.json` as data and downloads every tarball it pins,
   for all platforms, into `.factory/npm-mirror/`. The broker reads the
   lockfile from your project, so a developer that changed one in its clone
   copies it (with its `package.json`) to `.factory/outgoing/lock/` and
   fetches from there. Each one must come from an
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
   project and the read-only reviewer can run tests too. The developer runs it
   from its lane, the reviewer from its checkout of the branch under review;
   both use the mirror in the project's `.factory/npm-mirror/`. It sets `CI=true` so
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
Notes for one role only go in `projects/<project-folder-name>.<role>.md`
(e.g. `my-api.developer.md` for the docs style, `my-api.reviewer.md` for what
to be strict about); `factory start` appends them after the shared notes, to
that role alone, so the others don't carry them in every prompt.
Keep them short: a role's instructions plus these notes must stay under 20,000
characters, or the bridge refuses that role's tasks (`instructions for <role>
exceed 20000 characters`).

The images also include yarn 1.22.22, so `yarn` scripts and git hooks that
call `yarn` work. Python projects still use `pip_download` and `uv` directly.

Each role is `roles/<name>/role.conf` (description, image, `WORKSPACE=rw|ro`, `LANES`,
`BROKER_TOOLS`, `MODEL`, `MODELS`, `EFFORT`, `MAX_SESSION_TOKENS`, `MAX_TURNS`), `roles/<name>/instructions.md` (passed to every task
as part of the system prompt) and `roles/<name>/kit/spec.yaml` (its network policy). Every role
gets a writable scratch directory in `~/.local/state/ai-code-factory/roles/<name>/` and the project mounted `:ro`.
`rw` roles also get the project's `.factory/outgoing/` writable (bundles for `git_push`,
lockfiles for `npm_fetch`), keep their clones in the sandbox, and need the project to
be a git repository or a folder of them.

**Add a role:**
```sh
factory role new designer                # copies reviewer (read-only; network tools: npm_fetch, github_create_issue)
factory role new docs-writer developer   # or copy any existing role
```
Edit its `role.conf` and instructions, then run `factory start` again. It creates
the sandbox and a tab, and the Coordinator sees the new role in `list_roles`.

**Change a role:** edit instructions, `BROKER_TOOLS`, `LANES`, `MODEL`, `MODELS`, `EFFORT`, `MAX_SESSION_TOKENS` or `MAX_TURNS` and run `factory start`
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
| `git_push` | Takes a bundle the developer wrote from its clone to `.factory/outgoing/` (size-capped, copied once into the broker), checks branch rules, secret-scans every commit's patch (binary files included), message and author/committer, pushes |
| `github_create_pr` | Opens a (draft) PR; title and body are secret-scanned |
| `gemini_flash_query` | Sends a prompt to Gemini Flash for cheap/fast summarization or triage; prompt is secret-scanned and size-limited; output marked untrusted |
| `jira_my_issues` | Lists Jira Cloud issues assigned to the caller (`status`: `todo`/`in_progress`/`all`, default not-Done); read-only; output marked untrusted |
| `jira_get_issue` | One Jira Cloud issue the caller is assigned to: summary, status, priority, description, recent comments; read-only; output marked untrusted |

The broker never runs anything from the agent-writable workspace. Git network
operations happen in private mirrors inside the broker VM, code moves in and out
as git bundles, and npm install scripts are disabled. `.factory/` in the project
is git-ignored automatically (`factory start` creates it, with
`.factory/outgoing/`, and refuses to run if either is a symlink).

## Analytics

`factory analytics` aggregates `dispatch/audit.jsonl` (job-end records: cost,
duration, context size, model), `broker/audit.jsonl` (individual broker tool
calls) and `dispatch/sessions/<lane>.json` (the current session totals of each lane of each role)
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
  - `dispatch/logs/<lane>.log`: role transcripts, one per lane (`developer.log`,
    `developer-lane2.log`, ...)
  - `dispatch/sessions/<lane>` (session id) and `<lane>.json` (its total cost
    and context size after the last job)
  - `dispatch/imports/`: bundles being imported from a developer lane (deleted
    once imported)
  - `dispatch/packets/<job-id>.md`: the job's review packet (`factory packet`)
  - `broker/audit.jsonl`: every broker call, with the calling role
  - Server errors: `dispatch/dispatch.log` and `broker/broker.log`
- **Why state is outside the repo:** the broker re-reads its tool grants
  (`broker/grants.json`) and token hashes on every call. Inside the repo they would
  be one merged developer branch away from being changed when the factory works
  on its own repo, and the broker writes into the project's `.factory/`. Only the broker sandbox and
  the dispatch bridge see the state folder. `factory start` moves state from the
  old in-repo location (`.broker/`, `.dispatch/`, `.roles/`) the first time, and
  tells you which sandboxes to recreate.
- **Working on the factory with the factory:** point it at this repo as usual.
  Review a developer branch (`git log -p main..factory-developer/factory/<topic>`)
  before you merge it, and before restarting anything: `scripts/`, `bin/factory` and
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
  - **A role:** its lanes' saved sessions; their next tasks start fresh. For
    the developer, also its lane clones: anything it didn't commit to a
    `factory/*` branch (committed branches are already in your project).
  - **Broker:** a GitHub token stored with `--sandbox factory-broker`, because
    `sbx rm` deletes secrets scoped to the removed sandbox. Re-add it
    afterwards; `factory start` reminds you.

  Sandboxes created before fingerprints existed count as out of date once.

  **Upgrading to lanes:** the developer sandbox used to mount your project
  read-write and edit your working tree directly. Now it mounts the project
  read-only and works in its own clones, so its sandbox must be recreated:
  `factory start` refuses to run with the old one and asks for
  `factory update`, which recreates it. That discards its conversation history
  and any changes it made directly in the project that it hadn't pushed;
  there are none to lose, because it edited your live working tree, where
  its work already is. From then on it works from committed state: commit
  (or stash) what you want it to build on. The same `factory update` rebuilds
  the developer and reviewer images (they gain `factory-lane`), restarts the
  dispatch bridge, and also recreates the Coordinator, because its briefing
  changed, so the Coordinator's conversation history is discarded too.
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
- **Tests:** CI (`.github/workflows/ci.yml`) runs all of these on every pull
  request and push to main: on Linux with Python 3.9 and 3.14, on macOS 14 with
  the system bash and Python, plus shellcheck and an image build. To run them
  yourself (use `/usr/bin/python3` on macOS):
  - shell lint: `scripts/lint.sh` (downloads the pinned shellcheck once)
  - dispatch: `python3 -m unittest discover -s dispatch -v`
  - host scripts: `python3 -m unittest discover -s scripts -p 'test_*.py' -v`
  - shell tests: `bash scripts/test_factory_update.sh`, and likewise
    `test_factory_secret.sh`, `test_factory_docs.sh` and `test_factory_lane.sh`
  - images, without sbx: `scripts/00-build-templates.sh --build-only`
  - broker: `docker run --rm --network none -v "$PWD/broker:/b:ro" -w /b factory-broker:v3 python3 -B -m unittest discover -s tests -t .`
    (`broker/` is the `passdesk-broker` submodule; run `git submodule update --init`
    first if it's empty)
- **Docs site:** `.github/workflows/docs.yml` publishes the `factory docs` page
  to GitHub Pages whenever the docs change on main, with links to repo files
  pointing at that commit on GitHub (`scripts/docs.py --link-base`).
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
roles/<name>/role.conf       Role definition: description, image, rw/ro, lanes, broker tools
roles/<name>/instructions.md Role instructions (added to each task's system prompt)
roles/<name>/kit/spec.yaml   Role kit: Anthropic-only egress
kits/coordinator/spec.yaml   Coordinator kit
kits/coordinator/broker.conf Coordinator's direct broker tool grants (none by default; Jira is the opt-in)
kits/broker/spec.yaml        Broker kit: GitHub, registries, docs hosts
broker/                      passdesk-broker, as a git submodule: MCP server (runs in the broker VM), policy, tests
broker/passdesk/core/        Gateway core: protocol, auth and grants, policy, filters, audit
broker/passdesk/tools/       One module per broker tool (listed in broker/tools.txt)
dispatch/                    Dispatch bridge (runs on the host), tests
docs/broker-extraction.md    Roadmap for making the broker a standalone gateway
docs/setup-wsl.md            Step-by-step setup on Windows 11 with WSL2
docs/guides/*.md             Task-oriented guides (rendered by factory docs)
docs/mac-quickstart.html     One-page Mac quickstart
windows/enable-wsl.ps1       Windows side of that setup: WSL, Ubuntu, nested virtualization
templates/<image>/           Pinned images: developer, reviewer (+ruff)
templates/agent/factory-test Runs tests in a sandbox-local copy (baked into developer and reviewer)
templates/agent/factory-lane Lane clones: prepare, export bundles, bundle a branch for git_push, check out a branch to review
config.example/              Example local settings for ~/.config/ai-code-factory/
scripts/lib/common.sh        Pinned versions, checksums, names, shared checks
scripts/lib/roles.sh         Role parsing and validation
scripts/lib/secrets.sh       keychains (macOS, secret-tool, Windows Credential Manager), factory secret
scripts/lib/preflight.sh     Platform checks shared by the installer and factory doctor
scripts/install-prereqs.sh   Install pinned sbx + herdr
scripts/00-build-templates.sh
scripts/01-init-security.sh
scripts/02-launch-factory.sh <project-dir>
scripts/03-stop-factory.sh
scripts/new-role.sh <name> [<role-to-copy>]
scripts/start-broker.sh, scripts/start-dispatch.sh   (called by the launch script)
scripts/analytics.py, scripts/test_analytics.py      Cost/performance report (factory analytics)
scripts/approvals.py         Passdesk Pro approval cards (factory approvals)
scripts/docs.py              Builds the docs site (factory docs)
scripts/lint.sh              Pinned shellcheck over every shell script (CI)
.github/workflows/            CI (ci.yml) and the docs site on GitHub Pages (docs.yml)
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
