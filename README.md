# Passdesk

Passdesk is an MCP gateway that sits between one or more coding agents and
the outside world (GitHub, npm, PyPI, docs sites, Jira, Gemini). Agents never
get direct network egress; every tool call goes through Passdesk, which:

- holds the real credentials (GitHub token, Jira/Gemini/private-registry
  secrets) so no agent sandbox ever sees them
- identifies the caller and grants only the tools that caller has been given
- checks every call against `policy.toml` (allowed hosts, repos, branches,
  package allow/deny lists, size limits)
- filters content in both directions: it blocks secrets in outbound content
  and cleans up responses on the way back (strips terminal escapes, redacts
  secrets, caps size, marks fetched content as untrusted)
- writes an audit record for every call, plus an "intent" record *before*
  running a tool that writes to an external system (`git_push`,
  `github_create_pr`), so the attempt is on record even if the call fails
  partway through. A call that writes externally is refused if that intent
  record can't be written.

Passdesk never runs anything from a caller's workspace: no git hooks or
repo-local git config, no package install scripts, no binaries or
interpreters from the workspace. Code crosses the boundary as git bundles or
plain files; dependencies cross it as lockfile-pinned, hash-verified tarballs
(`npm_fetch`) or wheels (`pip_download`) that the caller installs offline.

Everything in the workspace is treated as hostile and liable to change
between two reads:

- Passdesk never follows a symlink under the workspace: a symlinked exchange
  directory (`.factory/` by default, see `BROKER_EXCHANGE_DIR` below) or one
  of its subdirectories, requirements file, lockfile or bundle is refused,
  even if it points inside the workspace. Writes go to a temp file that is
  renamed into place, so a planted symlink or hard link is replaced, never
  written through.
- Agent-supplied files are read once, as regular files with a size cap.
  `git_push` copies the bundle (at most `[filters].max_bundle_bytes`) into
  broker-private space and runs every git step on that copy; `pip_download`
  hands pip a requirements file it wrote itself from the parsed pins, never
  the caller's file.
- `pip_download` accepts only `name==version --hash=sha256:<hex>` lines
  (extras and environment markers allowed) and refuses every option line
  (`-r`, `--no-binary`, `--find-links`, ...). Generate such a file with
  `uv pip compile --generate-hashes` or `pip-compile --generate-hashes`.
- `git_push` secret-scans every pushed commit on its own (patch, message,
  author and committer), not just the net diff, and scans and counts binary
  files as text. Branch names, PR titles and PR bodies are scanned too, so a
  credential cannot leave as the name of the thing being pushed.
- `web_fetch` refuses a hard-coded list of hosts that the egress proxy or
  another broker tool attaches credentials to (GitHub, Atlassian, the
  Gemini API, the configured private npm registry), even if an operator
  adds one to `policy.toml`'s `[web].allow_hosts` - it's an unauthenticated
  GET and must not become a way to read those hosts without going through
  the tool whose own allowlist (e.g. `[git].repos`) is meant to gate them.

The package is `passdesk/`:
- `passdesk/core/`: protocol, auth/grants, policy, content filters, audit,
  workspace file exchange, registry, the approval point every external
  write passes through (`approvals.py`) and plugin loading (`plugins.py`;
  `PASSDESK_PLUGINS` names modules, the image's `plugins` build context
  holds them). Knows no tool. A deployment may install an approval hook
  that holds pushes and pull requests for a person; the design is in
  `docs/pro-approvals.md` and the hook ships in the commercial
  `passdesk-pro` package.
- `passdesk/tools/`: one module per tool (`web`, `git`, `gemini`, `jira`,
  `npm`, `pip`), each registering its tools with `core.registry`.

`server.py` is the entry point; `run.sh` restarts it on crash; `policy.toml`
is the default policy, baked into the image at build time (see `Dockerfile`)
and never read from a caller-writable workspace. `tools.txt` lists every
tool name in `tools/list` order, for callers whose own tooling can't import
Passdesk directly; `tests/test_registry.py` fails if it drifts from the
registry.

Passdesk is standard-library Python only (3.11+) — a small dependency
surface is part of its security argument.

## Running it

Passdesk has no required deployment: it reads its settings from the
environment and speaks MCP over HTTP, so any container runtime or process
supervisor that can set a few environment variables and point an MCP client
at the port can run it. "Deploying with Docker Sandboxes (sbx)" below is one
such adapter; it's documented in detail because it's the one this repo's own
CI and test fixtures exercise, not because it's the only one.

**Configuration (environment variables, all optional with working defaults):**

| Variable | Default | What it is |
|---|---|---|
| `BROKER_WORKSPACE` | `/nonexistent` | The project folder shared with agents; every workspace-relative path tools accept is rooted here. |
| `BROKER_STATE` | `/nonexistent` | Where tokens, grants (`roles.json`) and the audit log live. |
| `BROKER_POLICY` | `policy.toml` beside the package | The policy file to load (plus `policy.local.toml` beside it, if present, merged table by table). |
| `BROKER_PORT` | `8765` | The port the MCP server listens on. |
| `BROKER_MIRRORS` | `~/mirrors` | Where git tools keep their bare mirror clones. |
| `BROKER_EXCHANGE_DIR` | `.factory` | Name of the workspace subdirectory `git_fetch`, `npm_fetch` and `pip_download` use to hand files to the agent's own sandbox. Must be a single path component: not empty, no `/` or NUL, not `.` or `..`, not absolute; the broker refuses to start the exchange directory otherwise. |
| `BROKER_JIRA_SECRET` | `/etc/factory-broker/secrets/jira` | File holding the Jira credential (`email:token`); see "Configuring credentials". |
| `BROKER_GEMINI_SECRET` | `/etc/factory-broker/secrets/gemini` | File holding the Gemini API key. |
| `BROKER_NPM_REGISTRY_SECRET` | `/etc/factory-broker/secrets/npm-registry` | File holding the private npm registry credential. |
| `PASSDESK_PLUGINS` | `` (empty) | Comma-separated plugin modules to import at start-up (e.g. the commercial `passdesk-pro` approval hook). |

`BROKER_WORKSPACE` and `BROKER_STATE` are required in practice (there is
nothing at `/nonexistent`); every other variable above has a usable default.
Callers authenticate with a bearer token checked against a SHA-256 hash in
`BROKER_STATE/tokens/<principal>.sha256`, and are granted only the tools
listed for them in `BROKER_STATE/roles.json` — minting both is the
deployment's job, not Passdesk's (see `docs/pro-approvals.md` for one
example, or ai-code-factory's start script for another). `python3 -m
passdesk.doctor` (below) checks all of this and prints a fix for whatever is
missing.

**Connecting an MCP client:** once running, Passdesk answers MCP over HTTP
at `http://<host>:<port>/mcp` and expects `Authorization: Bearer <token>` on
every call. Point any MCP-capable client at that URL with a minted token;
`tools.txt` lists every tool name for clients that can't query `tools/list`
directly.

## Configuring credentials

GitHub needs no file: the deployment's egress proxy attaches the token to
Passdesk's outbound request, so Passdesk's own process never sees it (with
sbx: `sbx secret set github --sandbox factory-broker`).

Jira, Gemini and a private npm registry credential are read from files
instead (see CLAUDE.md's "Credentials" rule for why), fresh on every call,
at the paths in the table above (`BROKER_JIRA_SECRET`,
`BROKER_GEMINI_SECRET`, `BROKER_NPM_REGISTRY_SECRET`). Deliver each file
with mode 0600 or tighter, owned by the user the broker runs as; rotating
the file rotates the credential without restarting the broker. With sbx +
the AI Code Factory, these are stored in the host keychain and delivered by
`scripts/start-broker.sh`; any other deployment just needs to write the file
to that path before the broker's first call that needs it.

## Managing principals

A principal is whatever a token was minted for (see `passdesk/core/auth.py`).
`ai-code-factory`'s `scripts/start-broker.sh` mints one per role automatically;
for a standalone (non-factory) deployment, or for a principal the factory
doesn't manage, use the CLI instead:

```
python3 -m passdesk principal add <name> --tools TOOL [TOOL ...]
python3 -m passdesk principal list
python3 -m passdesk principal revoke <name>
```

`add` mints a fresh token, writes its SHA-256 hash to
`BROKER_STATE/tokens/<name>.sha256` and the tool grant to
`BROKER_STATE/grants.json` (mode 0600; `BROKER_STATE` and its `tokens/`
directory are created mode 0700 if missing) — uses the same files and formats
`start-broker.sh` writes (in a factory deployment, `start-broker.sh` owns
`grants.json` and rebuilds it on every start).
The token itself is printed to stdout exactly once and is never written to
disk or logged; capture it immediately (e.g. `token=$(python3 -m passdesk
principal add alice --tools git_fetch)`), there is no way to recover it
afterwards short of `revoke` and `add` again. A principal name must start
with a lowercase letter and contain only lowercase letters, digits, `_` or
`-`; `--tools` accepts only names in the running registry (`tools.txt`).

`list` prints each existing principal and its granted tools, never a hash
or a token, and flags a principal missing a token or a grant (its calls
would fail). `revoke` removes both the token hash and the grant; a token
already handed to an agent stops authenticating immediately.

**Do not use this CLI to manage a factory-managed deployment's own roles.**
In an `ai-code-factory` deployment, `scripts/start-broker.sh` owns
`grants.json` and rebuilds it from scratch (not a read-modify-write) on
every factory start, from `roles/*/role.conf`; a principal the CLI adds in
between factory starts loses its grant (though not its token file) the
next time the factory starts the broker. Use the CLI for a standalone
deployment, or for a principal the factory doesn't manage at all; for a
factory role, change `roles/<role>/role.conf` instead and let
`start-broker.sh` apply it.

## Checking a deployment

```
python3 -m passdesk.doctor --live
```

Run it where the broker itself runs (its container, where the baked-in
policy, the state directory and the credential files are). It changes
nothing and prints a fix for each problem: the policy's shape (every table
and key the tools read), hosts in `[web].allow_hosts` that the
credentialed-host denylist makes dead, the tokens and grants in the state
directory, the audit log, the exchange directory (`BROKER_EXCHANGE_DIR`),
the credential files' presence, mode and format, and, with `--live`,
whether the gateway answers and whether the GitHub token can read and push
each repo in `[git].repos`. `--kit <spec.yaml>` (repeatable) checks an sbx
kit's network allowlist against every host the policy can reach (other
deployments check their own egress policy by hand, or write their own
reader for `--egress-hosts`' output); `--egress-hosts` prints that host
list for tooling that keeps the allowlist in sync. `--json` prints the same
report as one JSON object for scripts. `ai-code-factory`'s `factory doctor`
runs it for you.

## Running the tests

Directly, with Python 3.11+:

```
python3 -m unittest discover -v
```

Or inside the pinned image, the same way CI builds it. The Dockerfile needs
two additional build contexts (`factory-config`, a deployment's policy
overlay; `plugins`, optional plugin packages) that plain `docker build`
doesn't supply, so pass empty ones explicitly:

```
mkdir -p /tmp/passdesk-empty/factory-config /tmp/passdesk-empty/plugins
: > /tmp/passdesk-empty/factory-config/policy.toml
docker buildx build --load -t passdesk:dev \
  --build-context factory-config=/tmp/passdesk-empty/factory-config \
  --build-context plugins=/tmp/passdesk-empty/plugins \
  .
docker run --rm --network none -v "$PWD:/b:ro" -w /b passdesk:dev python3 -m unittest -v
```

CI (`.github/workflows/test.yml`) runs exactly this, on every push and pull
request. A second job would push tagged images (`v*`) to Docker Hub but
stays disabled until the owner adds the
`DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` repository secrets.

## Deploying with Docker Sandboxes (sbx)

The Dockerfile's default `INSTALL_DIR` (`/opt/factory-broker`) and the
secret paths above assume this deployment: Passdesk runs as its own sbx
sandbox, built by the AI Code Factory from this repo's `Dockerfile`, with
its policy overlay passed as the `factory-config` build context and its
secrets delivered to `/etc/factory-broker/secrets/` by
`scripts/start-broker.sh` (which also sets `BROKER_WORKSPACE` and
`BROKER_STATE` and launches `run.sh`). [docs/quickstart.md](docs/quickstart.md)
walks through this end to end: installing the factory, scoping a GitHub
token, starting agents, and reading the audit record. The `--kit
<spec.yaml>` doctor check above is this adapter's network-allowlist reader.

## Used by AI Code Factory

ai-code-factory consumes this repository as a git submodule: its agents
reach the outside world only through a Passdesk instance, which it builds
from this repo's `Dockerfile` (the sbx deployment above) and configures with
its own set of callers, tool grants and policy overlay. See that repo's
README and CLAUDE.md for how it wires Passdesk in (roles, `BROKER_TOOLS`,
credential delivery, egress kits). This repository has no knowledge of that
wiring — it only implements the gateway, and keeps every factory-specific
default overridable rather than required.
