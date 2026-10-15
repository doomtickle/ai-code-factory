# Broker extraction roadmap

Goal: make the broker a standalone gateway that any MCP-speaking coding agent
can sit behind, on any sandbox or none, with the factory as one deployment of
it. The broker's security model stays as it is: no agent egress except
through it, per-caller tool grants, policy checks, filtering in both
directions, an audit record for every call, credentials kept out of agent
sandboxes, and nothing from the workspace ever executed.

Each phase is shippable on its own. Phase 0 is done; Phases 1 to 3 are the
plan, recorded here so the reasoning survives.

## Where the broker is tied to the factory

| Area | Today | Why it blocks reuse |
|---|---|---|
| Identity | "Roles", with token hashes and `roles.json` grants written by `scripts/start-broker.sh` from `role.conf`; the Coordinator is special-cased in `core/protocol.py` (`grant_hint`) | Other deployments have agents, users and CI jobs, not factory roles |
| File exchange | Tools read from and write to `WORKSPACE/.factory/{incoming,outgoing,npm-mirror,wheels}`, relying on sbx sharing the project folder with the broker; one broker serves one project (`start-broker.sh`, "bound to") | Agents elsewhere don't share a filesystem with the broker |
| Credentials | GitHub works because the sbx proxy injects the token; Jira, Gemini and the private npm registry use the keychain → stdin → file exception (CLAUDE.md) | Needs secret managers and short-lived, narrowly scoped tokens |
| Policy | One TOML baked into the image plus a local overlay, shared by every caller; its hosts are copied by hand into `kits/broker/spec.yaml` | Needs per-caller policy, schema validation, and an egress allowlist generated from the policy |
| Deployment | `broker/run.sh` restart loop on an sbx base image; `start-broker.sh` does sbx run, port publishing and `sbx mcp add` | Needs to run in plain Docker or Kubernetes, with sbx as one adapter |
| Audit | Local JSONL with fsync; the caller field is named `role` (`scripts/analytics.py` reads it) | Needs a documented schema, pluggable sinks, tamper evidence |
| Transport | Standard-library HTTP server, bearer token, no TLS, listening on all interfaces inside the VM | Fine behind sbx's loopback port; not on a network |

## Phase 0: refactor in place (done)

No change in what agents see. The `tools/list` output is byte-identical to
before, apart from the removed `npm_ci`.

- `broker/server.py` (1,219 lines) is now a thin entry point. The code lives in the `broker/agentbroker/` package:
  - `core/`: `config`, `errors`, `policy`, `filters`, `audit`, `workspace`, `net`, `registry`, `auth`, `protocol`. It knows no tool.
  - `tools/`: `web`, `git` (with GitHub PRs), `gemini`, `jira`, `npm`, `pip`. Each module registers its tools with `core.registry.register`. Import order in `tools/__init__.py` sets the `tools/list` order.
- Settings live in `core/config.py` and are read as `config.NAME` at call time, so tests can point them at temp dirs.
- Inside the broker the caller is a *principal* (`auth.authenticate`, `auth.principal_tools`). Agent-facing messages still say "role". The audit field is still `role`, because analytics reads it.
- `npm_ci` is gone. It installed into the project folder, which CLAUDE.md forbids, and nothing granted it. `npm_fetch` plus `factory-test` replaces it.
- `broker/tools.txt` lists the tool names, and `scripts/lib/roles.sh` reads its known-tools list from that file. Host scripts run on the system Python (3.9), which can't import the broker (3.11+, `tomllib`), so a manifest stands in. `tests/test_registry.py` fails if the manifest drifts from the registry.
- `broker/test_server.py` is split into `broker/tests/`, one file per area. The run command is unchanged (README, Operating).

Left for later phases: `grant_hint` (factory wiring in core), the `role` audit field, and the `.factory/` exchange directories in `core/workspace.py`.

## Phase 1: make it generic

All changes stay in this repo, and the factory keeps working throughout.

1. **Principals and grants.** A versioned grants file replaces bare `roles.json`: principal → tools, plus optional per-principal policy overrides (e.g. a reviewer may fetch but not push). `start-broker.sh` writes the new format. The broker accepts the old one during a deprecation window.
2. **Grant hints out of core.** Each grant can carry a `source` string, which the deny message quotes. The factory sets `roles/<name>/role.conf` or `kits/coordinator/broker.conf`, and `grant_hint` goes away.
3. **File exchange behind one interface.** Tools ask an exchange object for "a place to put a bundle for this caller" or "the bundle this caller handed me". Two backends:
   - *Shared mount*: today's `.factory/` behavior, for sbx.
   - *In-protocol transfer*: bundles and tarballs move as MCP blob content, with size limits and hashes. This is what lets the broker work with agents that share no filesystem with it.

   Also lifts the one-project-per-broker limit: each principal maps to a workspace.
4. **Credential providers.** One lookup interface (`get(name) -> str | None`, read fresh on every call), with providers for:
   - proxy-injected (sbx: no value needed in the broker)
   - file (today's exception)
   - environment from an external secret manager
   - GitHub App installation tokens: short-lived and scoped per repo, replacing a long-lived PAT
5. **Policy tooling.**
   - A JSON schema for `policy.toml`, checked at load time (bad keys fail the image build, not a tool call).
   - `broker policy validate`.
   - `broker egress-hosts`, which prints every host the policy can reach, so `kits/broker/spec.yaml` is generated or checked instead of synced by hand.
6. **Audit schema.** A documented event format with a version field. The caller field moves to `principal`, with `role` kept as an alias until `scripts/analytics.py` reads both. Sinks: file (today), syslog, webhook, OpenTelemetry. Each record carries the previous record's hash, so edits and deletions show.

## Phase 2: package it

1. Move the broker into its own repository, still standard-library Python. A tiny dependency surface is part of its security argument. It ships:
   - an image pinned by digest
   - a `docker compose` quickstart
   - TLS, with optional mTLS between gateway and broker
   - MCP conformance tests run against the clients it supports
2. **Adapters:**
   - *sbx*: today's `start-broker.sh` logic (token minting into `sbx secret`, `sbx mcp add` per principal, credential delivery), moved into the broker repo.
   - *Generic*: setup docs for MCP clients (Claude Code, Codex, Cursor, others), each tested.
3. **The factory consumes the broker** as an image pinned by digest, per CLAUDE.md's pinning rule. It keeps only its role → principal mapping and the sbx adapter configuration. `broker/tools.txt` is generated from the pinned image's tool list at build time.

## Phase 3: extension points

1. **Approvals.** A hook before risky actions (push, opening a PR, a dependency not seen before, a new host): the core holds the call until an external approver answers or a timeout denies it. The core ships the hook and a deny-all default; approvers (chat, web) plug in.
2. **Admin API.** Read-only endpoints for audit queries, current grants and policy version, on a separate listener from the agent-facing MCP endpoint. A fleet view can build on it.
3. **Managed policy.** Policy and grants loaded from a signed bundle, so a central team can roll out changes without rebuilding images. Agents still can't influence the policy source.

## Open decisions

- **License** for the standalone repository.
- **Default file exchange:** in-protocol transfer is portable but slower for large dependency mirrors. Shared mount stays the sbx default.
- **Name:** the package is `agentbroker` for now; the server still calls itself `factory-broker`.
