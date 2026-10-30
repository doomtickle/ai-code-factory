# Passdesk

Passdesk is an MCP gateway that sits between one or more coding agents and
the outside world (GitHub, a self-hosted or gitlab.com GitLab, npm, PyPI,
docs sites, Jira, Gemini). Agents never
get direct network egress; every tool call goes through Passdesk, which:

- holds the real credentials (GitHub and GitLab tokens, Jira/Gemini/private-registry
  secrets) so no agent sandbox ever sees them
- identifies the caller and grants only the tools that caller has been given
- checks every call against `policy.toml` (allowed hosts, repos, branches,
  package allow/deny lists, size limits)
- filters content in both directions: it blocks secrets in outbound content
  and cleans up responses on the way back (strips terminal escapes, redacts
  secrets, caps size, marks fetched content as untrusted)
- writes an audit record for every call, plus an "intent" record *before*
  running a tool that writes to an external system (`git_push`,
  `github_create_pr`, `github_update_pr`, `gitlab_create_mr`, `github_create_issue`), so the attempt is on record even if the call fails
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
  files as text. Branch names and PR and merge request titles and bodies are scanned too, so a
  credential cannot leave as the name of the thing being pushed.
- `web_fetch` refuses a hard-coded list of hosts that the egress proxy or
  another broker tool attaches credentials to (GitHub, Atlassian, the
  Gemini API, the configured private npm registry, the configured GitLab
  `[gitlab].url`), even if an operator
  adds one to `policy.toml`'s `[web].allow_hosts` - it's an unauthenticated
  GET and must not become a way to read those hosts without going through
  the tool whose own allowlist (e.g. `[git].repos`) is meant to gate them.

The package is `passdesk/`:
- `passdesk/core/`: protocol, auth/grants, policy, content filters, audit,
  workspace file exchange, registry, the approval point every external
  write passes through (`approvals.py`), credential lookup
  (`credentials.py`, below) and plugin loading (`plugins.py`;
  `PASSDESK_PLUGINS` names modules, the image's `plugins` build context
  holds them). Knows no tool. A plugin may declare tools of its own, which
  run behind the same grants, argument checks, secret scanning, approvals,
  cleaning and audit as core tools (`docs/plugin-tools.md`, with an example
  in `examples/plugins/`). The hold (`holds.py`, `notify.py`): with
  `[approvals].tools` set, each listed write is staged at once for a
  person's decision ("Staged for approval (id X); nothing has been sent
  yet"), recorded as files in the state directory, and a webhook is told;
  the gateway makes the write itself once approved. The
  design is in `docs/approvals.md`. With a hook installed, `git_push` also stores
  the pushed commits' patch in the state directory
  (`approvals/patches/<sha256>.patch`, capped by `[approvals]
  patch_max_bytes`) for the person approving to read; the card carries only
  its digest and size.
- `passdesk/tools/`: one module per tool (`web`, `git`, `github_issues`,
  `gemini`, `jira`, `npm`, `pip`), each registering its tools with `core.registry`.

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
at the port can run it. Two are documented here: plain `docker compose`
([docs/standalone.md](docs/standalone.md), with an egress guard and an
isolation check), and "Deploying with Docker Sandboxes (sbx)" below, the
one this repo's own CI and test fixtures exercise.

**Configuration (environment variables, all optional with working defaults):**

| Variable | Default | What it is |
|---|---|---|
| `BROKER_WORKSPACE` | `/nonexistent` | The project folder shared with agents; every workspace-relative path tools accept is rooted here. |
| `BROKER_STATE` | `/nonexistent` | Where tokens, grants (`roles.json`) and the audit log live. |
| `BROKER_POLICY` | `policy.toml` beside the package | The policy file to load (plus `policy.local.toml` beside it, if present, merged table by table). |
| `BROKER_PORT` | `8765` | The port the MCP server listens on. |
| `BROKER_BIND_HOST` | `0.0.0.0` | Interface the MCP server binds. Narrow it (e.g. `127.0.0.1`) when nothing outside the host needs to reach the broker directly; the sbx deployment below relies on the default for its port publishing. With `BROKER_DEPLOYMENT=standalone`, a bind that isn't loopback refuses to start unless TLS is on or `BROKER_INSECURE_BIND=1` (see "TLS"). |
| `BROKER_TLS_CERT`, `BROKER_TLS_KEY` | unset | PEM certificate (chain) and its unencrypted private key, both or neither; see "TLS". With them the MCP URL is `https://`. |
| `BROKER_INSECURE_BIND` | unset | Exactly `1` lets a standalone deployment bind off loopback with no TLS, for a port nothing outside a private network can reach (a compose stack's internal network sets it). Any other value is off; ignored in `sbx` mode. |
| `BROKER_ALLOWED_ORIGINS` | `` (empty) | Comma-separated Origin allowlist for the MCP spec's DNS-rebinding protection. A request with no `Origin` header (the normal case for a CLI-driven agent client) is unaffected; a request whose `Origin` isn't in this list gets HTTP 403. Empty means every Origin-bearing request is refused until a deployment opts specific origins in. |
| `BROKER_MIRRORS` | `~/mirrors` | Where git tools keep their bare mirror clones. |
| `BROKER_EXCHANGE_DIR` | `.factory` | Name of the workspace subdirectory `git_fetch`, `npm_fetch` and `pip_download` use to hand files to the agent's own sandbox. Must be a single path component: not empty, no `/` or NUL, not `.` or `..`, not absolute; the broker refuses to start the exchange directory otherwise. |
| `BROKER_SECRETS_DIR` | `/etc/factory-broker/secrets` | Directory the three credential files below default into, and also where any other credential name is looked up. Must be an absolute path with no `..` component; the broker refuses to start otherwise. |
| `BROKER_JIRA_SECRET` | `$BROKER_SECRETS_DIR/jira` | File holding the Jira credential (`email:token`); see "Configuring credentials". |
| `BROKER_GEMINI_SECRET` | `$BROKER_SECRETS_DIR/gemini` | File holding the Gemini API key. |
| `BROKER_NPM_REGISTRY_SECRET` | `$BROKER_SECRETS_DIR/npm-registry` | File holding the private npm registry credential. |
| `BROKER_REF_NAMESPACE` | `refs/factory` | Ref namespace `git_push` stages an incoming branch under in its own bare mirror while it verifies the push and awaits approval (never pushed itself; see `tools/git.py`). Must be a ref path under `refs/` (no `..`, no trailing slash, no empty component), must not be, or sit under, a namespace git/GitHub already use (`refs/heads`, `refs/tags`, `refs/remotes`, `refs/notes`, `refs/stash`), and no component may start with `.` or end in `.lock` (`git check-ref-format`); the broker refuses to start otherwise. |
| `BROKER_PUSH_PREFIX` | `factory/` | Fallback for policy's `[git].push_branch_prefix` when a deployment's own `policy.toml` doesn't set it at all (every `policy.toml` this repo ships does, so this only matters for a deployment's own from-scratch policy). Deliberately fails closed here (unlike upstream, which falls back to `""` — any unprotected branch — when the key is absent); the doctor warns when `[git].push_branch_prefix` is missing from policy as well as when it's set to `""`. Must contain only ref-safe characters, no whitespace or control characters. |
| `BROKER_DEPLOYMENT` | `sbx` | `sbx` (default) or `standalone`. Selects whether `git_push`/`github_create_pr` error hints and the doctor's live GitHub checks name the sbx CLI (`sbx secret set ...`) or stay generic, and the default of `PASSDESK_CREDENTIAL_PROVIDERS` (`standalone` leaves out `proxy`, so the gateway reads its own GitHub token); any other value refuses to start. Also selects which checks `python3 -m passdesk.doctor` runs: see "Checking a deployment". |
| `PASSDESK_PLUGINS` | `` (empty) | Comma-separated plugin modules to import at start-up: integrations that add tools (`docs/plugin-tools.md`) or install their own approval hook. `passdesk_pro`, the retired add-on whose hold is now core's, is refused by name. |
| `PASSDESK_CREDENTIAL_PROVIDERS` | `proxy,file,env-file` (`file,env-file` when `BROKER_DEPLOYMENT=standalone`) | Credential providers in lookup order; see "Configuring credentials". A name that isn't a provider stops the server at start-up. |
| `PASSDESK_SECRET_<NAME>_FILE` | unset | Path of a file holding credential `<NAME>` (upper-cased, `-` as `_`: `PASSDESK_SECRET_NPM_REGISTRY_FILE`). Holds a path, never the secret. |

`BROKER_WORKSPACE` and `BROKER_STATE` are required in practice (there is
nothing at `/nonexistent`); every other variable above has a usable default.
`BROKER_DEPLOYMENT`, `BROKER_REF_NAMESPACE`, `BROKER_SECRETS_DIR`,
`BROKER_PUSH_PREFIX`, the TLS pair and the bind address are checked for an
invalid value (an unknown deployment name, a ref namespace that isn't under
`refs/` or collides with one git already uses, a secrets directory that
isn't absolute or contains `..`, a push prefix with whitespace or non-ref
characters, one TLS file without the other or inside the workspace, a
standalone bind off loopback with neither TLS nor `BROKER_INSECURE_BIND=1`)
by `config.validate()`,
called both before the server binds its port and by `python3 -m
passdesk.doctor`, with a one-line error naming the variable; nothing silently
takes a bad value.
Callers authenticate with a bearer token checked against a SHA-256 hash in
`BROKER_STATE/tokens/<principal>.sha256`, and are granted only the tools
listed for them in `BROKER_STATE/roles.json` — minting both is the
deployment's job, not Passdesk's (see `docs/approvals.md` for one
example, or ai-code-factory's start script for another). `python3 -m
passdesk.doctor` (below) checks all of this and prints a fix for whatever is
missing.

**Connecting an MCP client:** once running, Passdesk answers MCP over HTTP
at `http://<host>:<port>/mcp` (`https://` with TLS on) and expects
`Authorization: Bearer <token>` on every call. Point any MCP-capable client
at that URL with a minted token; `tools.txt` lists every tool name for
clients that can't query `tools/list` directly.

## TLS

The bearer token on every call is only as private as the wire it crosses.
On loopback that is the machine itself. Anywhere else, turn TLS on:

```
BROKER_TLS_CERT=/etc/passdesk/tls/cert.pem
BROKER_TLS_KEY=/etc/passdesk/tls/key.pem
```

Passdesk serves `https://` with the stdlib `ssl` module, TLS 1.2 or newer,
and refuses to start if the pair doesn't load; the handshake runs per
connection with a 10-second limit, so a stalled client doesn't hold the
listener. There is no ACME or renewal: bring a certificate from your own CA,
or mint a self-signed one, valid for the names clients will use (the
service name, `localhost` and `127.0.0.1` here):

```
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 \
  -keyout key.pem -out cert.pem -subj /CN=passdesk \
  -addext "subjectAltName=DNS:passdesk,DNS:localhost,IP:127.0.0.1"
chmod 0600 key.pem
```

A client then trusts `cert.pem` (a self-signed certificate is its own CA):
Python's `ssl.create_default_context(cafile="cert.pem")`, `curl --cacert
cert.pem`, or `NODE_EXTRA_CA_CERTS=cert.pem` for a Node client such as
Claude Code. Keep the key out of the agent workspace (the broker refuses a
path inside it) and readable only by the user the broker runs as; the
doctor warns otherwise. `python3 -m passdesk.doctor --live` probes
`https://127.0.0.1:<port>/mcp` trusting `BROKER_TLS_CERT` when TLS is on.

**Standalone mode refuses a non-loopback bind without TLS.** With
`BROKER_DEPLOYMENT=standalone`, a `BROKER_BIND_HOST` that isn't loopback
(`0.0.0.0`, `::`, a hostname, or empty) and no TLS pair stops the server at
start-up with a one-line error. The way out for a port nothing outside a
private network can reach, such as a `docker compose` stack whose `agents`
network is `internal: true`, is `BROKER_INSECURE_BIND=1`; the doctor then
warns instead. The `sbx` deployment is exempt: its default `0.0.0.0` bind
serves only the sandbox's own port publishing and keeps working unchanged.

## Configuring credentials

With sbx, GitHub needs no file: the deployment's egress proxy attaches the
token to Passdesk's outbound request, so Passdesk's own process never sees
it (`sbx secret set github --sandbox factory-broker`; this hint, and the
doctor's live GitHub checks, name that command only when `BROKER_DEPLOYMENT`
is `sbx`, the default — set it to `standalone` outside that deployment).

A standalone deployment (`BROKER_DEPLOYMENT=standalone`) has no such proxy,
so Passdesk holds the GitHub token itself: deliver it like the credentials
below, as `github` (`$BROKER_SECRETS_DIR/github`, `/run/secrets/github` or
`PASSDESK_SECRET_GITHUB_FILE`). Use a fine-grained personal access token
limited to the repositories in `[git].repos`, with only **Contents** and
**Pull requests** read and write. Passdesk refuses a value that isn't
20-255 letters, digits and underscores (every GitHub token format), and
then:

- `git_fetch` and `git_push` give it to git only through
  `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` in that one git
  process's environment: an `http.https://github.com/.extraHeader` of
  `Authorization: Basic base64("x-access-token:<token>")`, so git sends it
  to no other host. It is never on a command line or written to disk; git
  runs with `http.followRedirects=false`, no credential helper, and every
  `GIT_TRACE*`/`GIT_CURL_VERBOSE` switch removed from its environment.
- `github_create_pr`, `github_update_pr` and `github_create_issue` send `Authorization: Bearer
  <token>` to `api.github.com` and refuse redirects, like every
  authenticated call. `github_create_issue` needs the token to have Issues
  read/write on the repos in `[github].issue_repos`; `github_update_pr` needs
  Pull requests read/write on the repos in `[git].repos`, like `github_create_pr`.
- `github_get_issue` and `github_list_issues` are read-only and send the same
  Bearer header (or rely on the proxy) the same way; they need Issues read on
  the repos in `[github].issue_read_repos`, a list of its own (not
  `[git].repos`, not `issue_repos`). Their output is cleaned, size-capped and
  wrapped as untrusted content.
- Once read, the token and its encoded form are redacted (as
  `[REDACTED:live-credential]`) from tool output, errors, the audit log and
  approval cards, and a PR title or body containing it is refused.

The token is still read fresh on every call. With `sbx`, a `github` file is
ignored (the `proxy` provider answers first) unless
`PASSDESK_CREDENTIAL_PROVIDERS` is set to leave `proxy` out.

### GitLab

GitLab works with any deployment, sbx included, and nothing proxy-injects
its token, so Passdesk always holds it (owner decision, `docs/decisions.md`).
Set three things:

1. `[gitlab].url` in your local `policy.toml`: the instance as
   `https://<host>[:port]/`, ending in `/` (a sub-path such as
   `https://example.com/gitlab/` works; `https://gitlab.com/` too). Only
   https, no `..`, `//`, `@`, `%` or `\` in the path, and not a GitHub host. Its host must also be in the
   deployment's egress allowlist (with sbx, your broker kit).
2. The projects in `[git].repos`, host first, next to any GitHub repos:
   `"gitlab.example.com/group/project"` or, with subgroups,
   `"gitlab.example.com/group/sub/project"`. A name whose first segment has
   a dot or a port is a GitLab project and must be on `[gitlab].url`; any
   other is `owner/name` on GitHub. To allow every project in a group, list
   the group with a final `/*`: `"gitlab.example.com/group/*"` (or
   `"gitlab.example.com/group/sub/*"`) covers any project under it, subgroups
   included, matched case-insensitively and on whole path components
   (`group/*` does not cover `group-evil/x`, or `group` itself). The `*` is
   valid only as that final `/*` after a group: not `host/*` (a whole
   instance), not `**`, not in the middle, and GitHub has no `owner/*`
   (list GitHub repos one by one). Like the rest of the allowlist it is
   baked into the image at build time. For example:

   ```toml
   [gitlab]
   url = "https://gitlab.example.com/"   # the trailing "/" is required

   [git]
   repos = ["gitlab.example.com/group/*"]
   ```
3. The `gitlab` credential, delivered like the files below
   (`$BROKER_SECRETS_DIR/gitlab`, `/run/secrets/gitlab` or
   `PASSDESK_SECRET_GITLAB_FILE`): a **project access token** (one project)
   or a **group access token** (every project in the group, for a
   `"host/group/*"` entry) with the **Developer** role and the
   **api** and **write_repository** scopes, and an expiry date. Avoid a
   personal access token: `api` on it reaches everything you can. Passdesk
   refuses a value that isn't 20-255 letters, digits, `_`, `-` or `.`.

The token is handled like a gateway-held GitHub token, scoped to the
`[gitlab].url` prefix instead: git gets an
`http.<gitlab url>.extraHeader` of `Authorization: Basic
base64("oauth2:<token>")` in its environment only, with redirects refused;
`gitlab_create_mr` sends `Authorization: Bearer <token>` to
`<gitlab url>api/v4/` and refuses redirects; it is redacted everywhere once
read. GitHub's token is never sent to GitLab, nor GitLab's to GitHub.
`gitlab_create_mr` opens a draft by default (the title gets GitLab's
`Draft: ` prefix), is held for approval like `github_create_pr` when
`[approvals].tools` lists it, and refuses a GitHub repo, as
`github_create_pr` refuses a GitLab project. `web_fetch` refuses the
`[gitlab].url` host. The doctor checks the policy, the credential and, with
`--live`, that the token can read and push each project (Developer or
higher).

The instance's TLS certificate must be one the broker's system trust store
accepts: a private CA is not supported yet.

Jira, Gemini and a private npm registry credential are read from files
instead (see CLAUDE.md's "Credentials" rule for why), fresh on every call,
at the paths in the table above (`BROKER_JIRA_SECRET`,
`BROKER_GEMINI_SECRET`, `BROKER_NPM_REGISTRY_SECRET`, all three defaulting
under `BROKER_SECRETS_DIR`). Deliver each file with mode 0600 or tighter,
owned by the user the broker runs as; rotating the file rotates the
credential without restarting the broker. With sbx + the AI Code Factory,
these are stored in the host keychain and delivered by
`scripts/start-broker.sh`; any other deployment just needs to write the file
to that path before the broker's first call that needs it.

Every credential is looked up by name (`jira`, `gemini`, `npm-registry`,
`github`, `gitlab`) through `passdesk/core/credentials.py`, which asks
each provider in `PASSDESK_CREDENTIAL_PROVIDERS` in turn and takes the
first value found:

1. `proxy`: names the egress proxy injects (`github`). The lookup stops
   there with no value, so Passdesk never holds them. Not in the default
   list when `BROKER_DEPLOYMENT=standalone`.
2. `file`: the path above for the credential (or
   `$BROKER_SECRETS_DIR/<name>` for any other name), then the Docker
   secret `/run/secrets/<name>`.
3. `env-file`: the file named by `PASSDESK_SECRET_<NAME>_FILE`, which must
   be an absolute path.

There is deliberately no provider that reads the secret itself from an
environment variable: those leak into child processes and `docker inspect`.
A plugin can add providers (a keychain, a vault) by calling
`credentials.register_provider(name, fn)` from its `activate()`; name it in
`PASSDESK_CREDENTIAL_PROVIDERS` to place it, otherwise it is asked last.

A credential file is used only if it is a regular file (symlinks are
followed) of at most 64 KiB of UTF-8, outside the agent workspace (checked
on the resolved path and, where `/proc` exists, on the opened file; a bind
mount of the workspace elsewhere is not detected). Leading and trailing
whitespace is dropped, as before; an empty file counts as absent, and a
value with a control character left inside it (a second line, say) is
refused. A refused file is skipped and recorded in the audit
log as a `credential_unusable` event naming the credential, the provider and
the reason, never the value.

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

## Egress guard

In a deployment without sbx, the agent container's only way out is a
CONNECT proxy that forwards nothing but its model traffic. Passdesk ships
one, in the same image so there is one pin:

```
python3 -m passdesk egress-guard [--bind 0.0.0.0] [--port 3128] [--max-connections 64]
```

Put it on both the agents' internal network and a network with egress, and
set `HTTPS_PROXY=http://egress-guard:3128` (plus
`NO_PROXY=passdesk,localhost,127.0.0.1`) in the agent container; the compose
quickstart (doomtickle/passdesk-ops#58) does exactly that. The guard accepts
`CONNECT host:443` for a host named in `policy.toml`'s
`[egress_guard].allow_hosts` (exact names, lowercased and without a
trailing dot before matching; no wildcards, ports or IP literals, and an
entry that isn't one stops it from starting) and refuses everything else:
every other method, every other port, an IP-literal target, a name not on
the list. It resolves the name itself and refuses the request unless every
address is global unicast (no RFC 1918, loopback, link-local such as
`169.254.169.254`, `100.64/10`, `fc00::/7`, multicast, or an IPv6 address
embedding an IPv4 one), then connects to one of those exact addresses, so
there is no second lookup to rebind. The tunnel is opaque: no TLS
interception, no logging of what crosses it. Every refusal is one line on
stderr with the status, the reason and the target (never a payload), and
every tunnel one `allow` line with the host and address. The request read
has a 10-second limit and an 8 KiB cap, an upstream connect 10 seconds, an
idle tunnel 10 minutes, and connections beyond `--max-connections` get 503.

The default list covers Claude Code (`api.anthropic.com`, plus
`platform.claude.com`, `claude.ai` and `claude.com` for a subscription
sign-in) and Codex CLI (`chatgpt.com` and `auth.openai.com` for a ChatGPT
login, `api.openai.com` for an API key); trim it to the client you run in
your `policy.toml` overlay, and never add a host an agent could fetch code
or packages from. The doctor fails on a bad entry in either mode and, in
standalone mode, lists what the guard forwards.

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
the credential files' presence, mode and format (through
`core/credentials.py`, so a credential delivered only as a Docker secret or
through `PASSDESK_SECRET_<NAME>_FILE` is found even when nothing sits at the
configured path), and, with `--live`, whether the gateway answers and
whether the GitHub token (or, for a GitLab project, the `gitlab` token,
which needs Developer or higher) can read and push each repo in
`[git].repos`. It also checks `[gitlab].url` and that each host-qualified
repo is on it, and counts the GitLab host among the hosts the policy can
reach (`--kit`, `--egress-hosts`).

With `BROKER_DEPLOYMENT=sbx` (the default), `--kit <spec.yaml>` (repeatable)
also checks an sbx kit's network allowlist against every host the policy
can reach; `--egress-hosts` prints that host list for tooling that keeps the
allowlist in sync. With `BROKER_DEPLOYMENT=standalone`, there is no sbx kit,
so the doctor skips that check and instead checks: a GitHub credential is
present (through `core/credentials.py`, never printed; standalone's default
provider order is `file,env-file` — see "Configuring credentials" — so this
reports the real file- or env-file-delivered credential's presence; the
"proxy runs first" fix-hint only fires when a deployment's own
`PASSDESK_CREDENTIAL_PROVIDERS` explicitly puts `proxy` back in front), the
exchange directory is writable, and TLS: with `BROKER_TLS_CERT`/`BROKER_TLS_KEY`
set, that both are regular files OpenSSL loads as a pair and the key isn't
readable by others; without them, a `BROKER_BIND_HOST` off loopback is a
problem (the server refuses to start) or, with `BROKER_INSECURE_BIND=1`, a
warning. In `sbx` mode the TLS files are checked only when set, so the
usual report is unchanged.

`--json` prints the same report as one JSON object for scripts.
`ai-code-factory`'s `factory doctor` runs it for you (in `sbx` mode).

## Running the tests

Directly, with Python 3.11+:

```
python3 -m unittest discover -v
```

Or inside the pinned image, the same way CI builds it (either target, see
"Images" below; the standalone one sets `BROKER_*` defaults the tests don't
expect, so unset them first):

```
docker buildx build --load -t passdesk:dev .
docker run --rm --network none -v "$PWD:/b:ro" -w /b passdesk:dev python3 -m unittest -v

docker buildx build --load --target standalone -t passdesk:dev-standalone .
docker run --rm --network none -v "$PWD:/b:ro" -w /b passdesk:dev-standalone \
  sh -c 'unset BROKER_DEPLOYMENT BROKER_STATE BROKER_WORKSPACE BROKER_SECRETS_DIR; exec python3 -m unittest -v'
```

CI (`.github/workflows/test.yml`) runs exactly this for both targets, plus a
smoke test of the standalone image, on every push and pull request. A
second job would push tagged images (`v*`) to Docker Hub but stays disabled
until the owner adds the `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` repository
secrets.

## Images

The `Dockerfile` has two targets. Both bake `policy.toml` in at build time
(never read from a caller-writable path), take the same two optional named
build contexts, and are pinned to their base image's multi-arch digest:

| Target | Base | Installs to | Runs as | For |
|---|---|---|---|---|
| `sbx` (the default: plain `docker build` with no `--target`) | `docker/sandbox-templates:shell-docker` | `/opt/factory-broker` | `agent` | the Docker Sandboxes deployment below; what ai-code-factory builds. Unchanged. |
| `standalone` (`--target standalone`) | `python:3.13-slim-bookworm` plus `git` and `openssl` from Debian | `/opt/passdesk` | `passdesk` (uid 1000, non-root) | plain Docker, no sbx: `BROKER_DEPLOYMENT=standalone` |

The two build contexts are `factory-config` (a directory holding your
`policy.toml` overlay, baked in as `policy.local.toml` and merged table by
table) and `plugins` (optional plugin packages, loaded only when
`PASSDESK_PLUGINS` names them). Pass them with `--build-context
factory-config=DIR --build-context plugins=DIR`; leave either out and an
empty default (a stage of the same name in the `Dockerfile`) stands in, so
`docker build` with no contexts works.

The standalone image sets `BROKER_DEPLOYMENT=standalone`,
`BROKER_STATE=/var/lib/passdesk` (mount a volume there to keep tokens, the
audit log and git mirrors across container recreation),
`BROKER_WORKSPACE=/workspace` (bind-mount the project there),
`BROKER_SECRETS_DIR=/etc/passdesk/secrets` (Docker secrets under
`/run/secrets/` are found too), and puts `/opt/passdesk` on `PYTHONPATH` so
`python3 -m passdesk principal ...` and `python3 -m passdesk.doctor` work
under `docker exec` from any directory. Its command is `python3
/opt/passdesk/server.py`; there is no `run.sh` restart loop, the container
runtime restarts it. `git` and `openssl` come from the Debian repositories
the pinned base is configured with, at whatever version they serve at build
time: they are the one part of the image not pinned by digest (the build log
prints the versions). The non-root user's uid is a build argument
(`--build-arg PASSDESK_UID=... PASSDESK_GID=...`) for a Linux host whose
user isn't uid 1000; the gateway must be able to write the workspace's
exchange directory. [docs/standalone.md](docs/standalone.md) is the `docker compose` quickstart
for this image, with the isolation check that shows the agent's only ways
out are the gateway and the egress guard.

## Deploying with Docker Sandboxes (sbx)

The Dockerfile's default target (`sbx`, with its `INSTALL_DIR`
`/opt/factory-broker`), the secret paths above and the default
`BROKER_DEPLOYMENT=sbx` all assume this deployment: Passdesk runs as its
own sbx sandbox, built by the AI Code Factory from this repo's
`Dockerfile`, with
its policy overlay passed as the `factory-config` build context and its
secrets delivered to `/etc/factory-broker/secrets/` by
`scripts/start-broker.sh` (which also sets `BROKER_WORKSPACE` and
`BROKER_STATE` and launches `run.sh`). [docs/quickstart.md](docs/quickstart.md)
walks through this end to end: installing the factory, scoping a GitHub
token, starting agents, and reading the audit record. The `--kit
<spec.yaml>` doctor check above is this adapter's network-allowlist reader.

## Used by AI Code Factory

`broker/` lives in the ai-code-factory repository, and the Passdesk image is
built from its committed tree (`git archive HEAD broker/`) using this
directory's `Dockerfile` (the sbx deployment above). Its agents reach the
outside world only through that Passdesk instance, which the factory
configures with its own set of callers, tool grants and policy overlay. See
the repository's README and CLAUDE.md for how it wires Passdesk in (roles,
`BROKER_TOOLS`, credential delivery, egress kits). The gateway code here has
no knowledge of that wiring: it only implements the gateway, and keeps every
factory-specific default overridable rather than required.
