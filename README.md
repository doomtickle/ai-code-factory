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

- Passdesk never follows a symlink under the workspace: a symlinked
  `.factory/`, `.factory/<dir>`, requirements file, lockfile or bundle is
  refused, even if it points inside the workspace. Writes go to a temp file
  that is renamed into place, so a planted symlink or hard link is replaced,
  never written through.
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
  files as text.
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

## Getting started

[docs/quickstart.md](docs/quickstart.md) takes you from a fresh machine to
agents pushing branches and opening draft pull requests through Passdesk,
with the record to show for it.

## Checking a deployment

```
python3 -m passdesk.doctor --live
```

Run it inside the broker sandbox (where the baked-in policy, the state
directory and the credential files are). It changes nothing and prints a fix
for each problem: the policy's shape (every table and key the tools read),
hosts in `[web].allow_hosts` that the credentialed-host denylist makes dead,
the tokens and grants in the state directory, the audit log, the `.factory/`
exchange directory, the credential files' presence, mode and format, and,
with `--live`, whether the gateway answers and whether the GitHub token can
read and push each repo in `[git].repos`. `--kit <spec.yaml>` (repeatable)
checks that every host the policy can reach is in a deployment's egress
allowlist; `--egress-hosts` prints that host list for tooling that keeps the
allowlist in sync. `ai-code-factory`'s `factory doctor` runs it for you.

## Running the tests

Directly, with Python 3.11+:

```
python3 -m unittest discover -v
```

Or inside the pinned image, the same way the broker's own `Dockerfile` builds it:

```
docker build -t passdesk:dev .
docker run --rm --network none -v "$PWD:/b:ro" -w /b passdesk:dev python3 -m unittest -v
```

## Consumers

ai-code-factory consumes this repository as a git submodule: its agents
reach the outside world only through a Passdesk
instance, which it builds from this repo's `Dockerfile` and configures with
its own set of callers, tool grants and policy overlay. See that repo's
README and CLAUDE.md for how it wires Passdesk in (roles, `BROKER_TOOLS`,
credential delivery, egress kits). This repository has no knowledge of that
wiring — it only implements the gateway.
