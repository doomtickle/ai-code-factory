# AI Code Factory: Control Plane Directives

## Architecture Overview

Multi-agent coding system driven from a local dashboard, isolated by Docker Sandboxes (`sbx`):
every agent runs in its own microVM with zero access to the host kernel, host Docker
socket, or host credentials. Full picture: README.md.

- **Coordinator:** own microVM, project as workspace, but cannot modify it —
  `kits/coordinator` installs root-owned Claude managed settings denying
  Edit/Write/NotebookEdit/Bash/PowerShell/Monitor/EnterWorktree. It acts only through
  the dispatch bridge (`dispatch/server.py`, host, 127.0.0.1).
- **Bridge's only capabilities:** `claude -p` in an existing role sandbox
  (`sbx exec`, fixed argv); `factory-lane prepare`/`export`
  (`templates/agent/factory-lane`) in an rw role's sandbox (`sbx exec`, fixed argv);
  one fixed, hook-free `git fetch` (`core.hooksPath=/dev/null`,
  `core.fsmonitor=false`, no shell, never `--prune`) of a bundle's `factory/*`
  branches into `refs/remotes/factory-<lane>/`; and a read-only `factory_docs` tool
  serving the factory's own markdown from a `FACTORY_STATE` snapshot `factory start`
  makes, never the repo.
- **Roles are data:** `roles/<name>/role.conf` + `instructions.md` + `kit/`. No role
  mounts the project writable — each gets a scratch dir
  (`FACTORY_STATE/roles/<name>`) plus the project `:ro`. rw roles (`WORKSPACE="rw"`,
  the developer) work in private git clones ("lanes") on their sandbox's own disk and
  hand back only committed `factory/*` branches as bundles the bridge imports; the
  user merges them. `factory-lane`
  initialises a lane's top-level submodules the host has initialised from the host's
  own module store (objects only, no network); ones it hasn't stay empty. A role with `OPTIONAL="1"` in its committed `role.conf` (the
  researcher) is off unless `FACTORY_CONFIG/enabled-roles` names it: `list_roles`
  (`scripts/lib/roles.sh`) and the bridge's `load_roles` leave it out, so it
  gets no sandbox, broker token or grants. The only writable project path any
  role mounts is `PROJECT/.factory/outgoing`. A role runs up to `LANES` jobs at once, each its own
  session/transcript. The bridge passes each role its instructions (snapshotted
  into `FACTORY_STATE` by `factory start`) via `--append-system-prompt`
  (a mixin kit's `agentInstructions` aren't reliably loaded).
- **Broker:** all agent tool traffic (git, package registries, web) goes through the
  Broker microVM (`factory-broker`), reached only as MCP tools via the sbx MCP
  gateway. It grants only the calling role's `BROKER_TOOLS` (by token), applies
  `broker/policy.toml`, filters content both ways, and audit-logs. Its code is the
  `broker/` directory of this repo (it was the separate `passdesk-broker` repo until 24 Oct 2026, history kept); sandbox name
  (`factory-broker`) and paths (`/opt/factory-broker`, `/etc/factory-broker`) are
  unchanged.

## Operational Constraints

- Spin up all agent sandboxes with `sbx run`/`sbx create` only.
- Default network policy stays locked-down; never set it to open.
- Agent kits (`roles/*/kit`, `kits/coordinator`) allow only Anthropic hosts; new
  outbound capability = a Broker tool with policy checks and audit logging, never an
  agent egress rule. Managed settings also deny built-in `WebFetch` (every role) and
  `WebSearch` (every role but researcher, which is optional and off by default): `WebSearch` bypasses the Broker (served by
  the model API), so only the role that can't write the project gets it, to verify
  leads via `web_fetch`. They also set `disableClaudeAiConnectors`, so the user's
  claude.ai account connectors (an outbound path around the Broker) never load.
- The Broker never executes agent-controlled content: no git hooks or repo-local git
  config from the workspace, no package install scripts, no binaries/interpreters from
  the workspace. Code crosses as git bundles or plain files; dependencies cross as
  lockfile-pinned, hash-verified tarballs (`npm_fetch`) or wheels (`pip_download`)
  roles install offline in their own sandbox.
- No role sandbox mounts the project read-write, and nothing a role controls runs on
  the host (a writable `.git` would let hooks/fsmonitor run host commands on the next
  git run). rw roles work in lane clones; work reaches the host only as `factory/*`
  branches the bridge imports with hooks/fsmonitor forced off. The only writable
  project path any role mounts is `.factory/outgoing` (`02-launch-factory.sh` creates
  it, git-ignored, refuses symlinks). The project must be a git repo or folder of git
  repos (`project_repos` in `scripts/lib/common.sh`, `factory/repos.py` (which
  `dispatch/server.py` imports), `factory-lane` — kept identical, `scripts/test_project_repos.sh`
  checks it) for rw roles to run.
- Developer/reviewer images carry a pinned headless Chrome (`chrome-headless-shell`,
  via `templates/agent/factory-shot`/DevTools) for rendering and screenshotting work —
  not a network capability: kits unchanged, the browser reaches only `file://` and
  loopback, `factory-shot` refuses other URLs. Screenshots go to `FACTORY_SCRATCH`,
  never the project or a branch.
- Roles never install dependencies into the project folder (shared with host, whose
  `node_modules` holds host-native binaries). Tests run via `factory-test`
  (`templates/agent/factory-test`) in a sandbox-local copy of the project/lane/review
  checkout, so read-only roles can test too.
- Never type into agent terminals from a host script (no terminal multiplexer,
  `tmux send-keys` or similar): if the sandbox session exits, that terminal is a host
  shell. Agents are driven only through the dispatch bridge; the Coordinator's own
  terminal is only the dashboard's PTY or `factory chat`.
- The dispatch bridge stays minimal: no shell, no arbitrary sbx/git commands,
  only allowlisted roles from `roles/`. Its only git commands: the fixed bundle fetch
  above, a `for-each-ref` listing of imported refs (same flags), and for the review
  packet a read-only `log` + `diff --stat` of each imported branch vs host `HEAD`
  (same flags plus `--no-ext-diff --no-textconv`, since `.gitattributes` could name a
  driver) — never checkout, merge, or write a working tree. The one custom host
  component that acts on agents.
- The dashboard (`scripts/dashboard.py`, `factory dashboard`) is the one host
  component listening for a browser: binds 127.0.0.1 only, requires a per-run token,
  and its main page only displays (reaches no endpoint that acts, holds no key); the
  cookie alone must never authorize an action. Anything new that lets a page act
  must meet the same bar: one fixed argv with no shell, a key in a request header
  (not just the cookie) on a POST or WebSocket with its `Origin` checked, kept out of
  reach of the page that shows agent-written content, and a confirm step before any
  host command. Full rules: **`scripts/CLAUDE.md` — read before changing dashboard
  code** (`scripts/dashboard*.py`, `scripts/patchview.py`).
- Broker policy is baked into its image, never read from the agent-writable
  workspace. Hosts in `broker/policy.toml` `[web].allow_hosts` must also be in
  `kits/broker/spec.yaml`.
- The factory is project/org-agnostic: nothing org-specific (registry URLs, Jira
  sites, repo names) is committed — such settings live in `FACTORY_CONFIG`
  (`scripts/lib/common.sh`, `~/.config/ai-code-factory/`, outside repo/workspace):
  `policy.toml` overlays `broker/policy.toml` (baked into the broker image; its
  `[git]` `author_name`/`author_email` are instead the lane git identity
  `start-dispatch.sh` validates and snapshots for `factory-lane prepare`),
  `broker-kit/` mixes in broker egress hosts, `projects/<name>.md` holds per-project
  notes `start-dispatch.sh` appends to role instructions, and `roles/<role>.conf`
  overrides a role's tuning keys only (`ROLE_OVERRIDE_KEYS` in `scripts/lib/roles.sh`
  and `factory/roles.py`, which `dispatch/server.py` imports; never `BROKER_TOOLS`, `WORKSPACE`, `TEMPLATE` or anything
  else that moves the security boundary), parsed as data and snapshotted by
  `start-dispatch.sh` into `FACTORY_STATE` for the bridge. `enabled-roles` (one
  role name per line, validated, snapshotted the same way) only switches optional
  roles on; it grants nothing and `OPTIONAL` is not an override key. `config.example/` holds
  placeholders only; features needing these settings must be inert when absent.

### Credentials

- The GitHub token is scoped to the Broker sandbox (`sbx secret set github --sandbox
  factory-broker`), never global. `scripts/start-broker.sh` re-registers a host-keychain
  `factory-github` entry, broker-scoped and over stdin, on every broker start
  (`github_sync_from_keychain`), so recreating the broker never drops it.
- Real credentials (GitHub, Anthropic) are injected out-of-band by the host egress
  proxy, never passed into a sandbox in plaintext. Register via `sbx secret set
  <service>` (global scope default); never commit them to git or pass them as env
  flags, CLI args, or files inside a sandbox.
- **Named exception:** `sbx` can't proxy-inject Jira, Gemini, a read-only private npm
  registry token (`[packages].npm_private_registry`), a GitLab project access token
  for `[gitlab].url` (owner decision 2026-10-20, doomtickle/passdesk-ops#120 — sbx has
  no `gitlab` service), or the approvals webhook URL the gateway posts held cards to.
  These may be held as plaintext files, **only in the broker sandbox**: kept in the
  host OS keychain (macOS Keychain / Linux `secret-tool` / the Windows Credential
  Manager on WSL2, reached through `powershell.exe` with the value only on stdin;
  `factory secret save`) and delivered by `scripts/start-broker.sh` over `sbx exec`
  **stdin** into `/etc/factory-broker/secrets/` inside the broker sandbox — never
  argv, an env flag, a log, or git. No keychain (headless Linux): `factory secret set
  <name>` delivers the same way until the broker is recreated. That path is outside
  both the agent-visible workspace and `BROKER_STATE` (the broker state dir, a host
  bind mount), so no plaintext ever lands on host disk. The broker
  (`broker/passdesk/`) reads the file fresh every
  call, so rotating the keychain entry and re-running `start-broker.sh` rotates the
  credential. Do not widen this exception to other credentials or sandboxes without
  updating this rule.
- Claude authentication: agents use a Claude Pro/Max subscription. The first `/login`
  in any factory sandbox is stored by sbx as the global `anthropic` secret; every
  sandbox created after uses it (sbx adds the login only at creation, so
  `02-launch-factory.sh` creates role sandboxes only once it's stored). The sandbox
  credentials file isn't verified as a placeholder — treat it as a usable login:
  never copy anything out of `~/.claude` in a sandbox or into the repo. API-key auth
  via `sbx secret set anthropic` remains the proxy-injected alternative.

## Other constraints

- Job state: the dispatch bridge keeps its job list in `FACTORY_STATE/dispatch/jobs.json`
  (short records, never full task text or secrets; atomic write, `O_NOFOLLOW`,
  validated on load, a bad file is skipped with a log line). A job recorded as
  running with no live process reloads as `interrupted`, and no lane may show RUN
  without a live job (dispatch closes dangling transcripts at start; `watch.py` and
  the dashboard treat a down bridge as interrupted). Lane sessions live on the role
  sandbox's own disk, so `factory update` losing them when it recreates that sandbox
  is expected; `describe_loss` and the standing note in `cmd_update_plan` must say so,
  and match what `cmd_update` really does.
- Runtime state (audit logs, transcripts, token hashes, tool grants, roles' scratch
  dirs) lives in `FACTORY_STATE` (`scripts/lib/common.sh`,
  `~/.local/state/ai-code-factory/`), never in the repo or any agent workspace: the
  broker re-reads grants/hashes on every call, and the factory may work on its own
  repo.
- Scripts must be POSIX-compliant Bash with `set -euo pipefail`. The host CLI is moving
  to Python (`factory/`, standard library only, 3.9+; plan and order in
  `docs/py-cli-migration.md`): port a command there rather than extending its bash, keep
  `bin/factory`'s `PORTED_COMMANDS` equal to `factory/cli.py`'s `COMMANDS`, call programs
  with argv lists (never `shell=True`), and keep the `role.conf` parser and `project_repos`
  single (`factory/conf.py`, `factory/repos.py`; the dispatch bridge imports them).
- This repo is shared with the team, so every setup must be repeatable: pin container
  images by digest, tools by exact version (with checksums for downloads), and keep
  pins in `scripts/lib/common.sh` or the relevant Dockerfile. In `.github/workflows/`,
  pin actions by full commit SHA (tag in a comment), runners by OS version, and Python
  by exact version. Flag anything that cannot be pinned.
