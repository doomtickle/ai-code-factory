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
- writes an audit record for every call

Passdesk never runs anything from a caller's workspace: no git hooks or
repo-local git config, no package install scripts, no binaries or
interpreters from the workspace. Code crosses the boundary as git bundles or
plain files; dependencies cross it as lockfile-pinned, hash-verified tarballs
(`npm_fetch`) or wheels (`pip_download`) that the caller installs offline.

The package is `passdesk/`:
- `passdesk/core/`: protocol, auth/grants, policy, content filters, audit,
  workspace file exchange, registry. Knows no tool.
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
