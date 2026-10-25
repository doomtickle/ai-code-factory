# Broker extraction roadmap

> **24 Oct 2026:** the broker lives in this repository again, as the plain
> directory `broker/` (the former `passdesk-broker` repository folded in with
> its history). The factory no longer has a git submodule or a pin for it;
> the text below is the plan as written when the broker was a separate
> repository and is left as it was.

Goal: make the broker a standalone gateway that any MCP-speaking coding agent
can sit behind, on any sandbox or none, with the factory as one deployment of
it. The broker's security model stays as it is: no agent egress except
through it, per-caller tool grants, policy checks, filtering in both
directions, an audit record for every call, credentials kept out of agent
sandboxes, and nothing from the workspace ever executed.

Each phase is shippable on its own. Phase 0 is done; most of Phases 1 to 3
are still the plan, recorded here so the reasoning survives, though parts of
each have landed (Phase 1's policy tooling partly, Phase 3's approvals
mostly).

## Where the broker is tied to the factory

| Area | Today | Why it blocks reuse |
|---|---|---|
| Identity | "Roles", with token hashes and `grants.json` grants (object form, `{"tools": [...], "source": "..."}`) written by `scripts/start-broker.sh` from `role.conf`/`kits/coordinator/broker.conf`; `core/protocol.py`'s `grant_hint` is generic, reading the `source` the factory sets (see Phase 1, done) | Other deployments have agents, users and CI jobs, not factory roles |
| File exchange | Tools read from and write to `WORKSPACE/.factory/{incoming,outgoing,npm-mirror,wheels}`, relying on sbx sharing the project folder with the broker; one broker serves one project (`start-broker.sh`, "bound to") | Agents elsewhere don't share a filesystem with the broker |
| Credentials | GitHub works because the sbx proxy injects the token; Jira, Gemini and the private npm registry use the keychain → stdin → file exception (CLAUDE.md) | Needs secret managers and short-lived, narrowly scoped tokens |
| Policy | One TOML baked into the image plus a local overlay, shared by every caller; its hosts are copied by hand into `kits/broker/spec.yaml` | Needs per-caller policy, schema validation, and an egress allowlist generated from the policy |
| Deployment | `broker/run.sh` restart loop on an sbx base image; `start-broker.sh` does sbx run, port publishing and `sbx mcp add` | Needs to run in plain Docker or Kubernetes, with sbx as one adapter |
| Audit | Local JSONL with fsync; the canonical caller field is `principal`, with `role` kept as a duplicate alias (`scripts/analytics.py` reads `principal`, falling back to `role` for older lines) | Needs a documented schema, pluggable sinks, tamper evidence |
| Transport | Standard-library HTTP server, bearer token, no TLS, listening on all interfaces inside the VM | Fine behind sbx's loopback port; not on a network |

## Phase 0: refactor in place (done)

No change in what agents see. The `tools/list` output is byte-identical to
before, apart from the removed `npm_ci`.

- `broker/server.py` (1,219 lines) is now a thin entry point. The code lives in the `broker/agentbroker/` package (renamed `passdesk/` in Phase 2, see Open decisions):
  - `core/`: `config`, `errors`, `policy`, `filters`, `audit`, `workspace`, `net`, `registry`, `auth`, `protocol`. It knows no tool.
  - `tools/`: `web`, `git` (with GitHub PRs), `gemini`, `jira`, `npm`, `pip`. Each module registers its tools with `core.registry.register`. Import order in `tools/__init__.py` sets the `tools/list` order.
- Settings live in `core/config.py` and are read as `config.NAME` at call time, so tests can point them at temp dirs.
- Inside the broker the caller is a *principal* (`auth.authenticate`, `auth.principal_tools`). The audit field is `principal`, with `role` kept as a duplicate alias (see Phase 1, done) until `scripts/analytics.py` no longer needs it.
- `npm_ci` is gone. It installed into the project folder, which CLAUDE.md forbids, and nothing granted it. `npm_fetch` plus `factory-test` replaces it.
- `broker/tools.txt` lists the tool names, and `scripts/lib/roles.sh` reads its known-tools list from that file. Host scripts run on the system Python (3.9), which can't import the broker (3.11+, `tomllib`), so a manifest stands in. `tests/test_registry.py` fails if the manifest drifts from the registry.
- `broker/test_server.py` is split into `broker/tests/`, one file per area. The run command is unchanged (README, Operating).

Left for later phases: the in-protocol transfer backend in `core/workspace.py` (the `.factory/` exchange directory's shared-mount backend is done, including a configurable name; see Phase 1 item 3).

## Phase 1: make it generic

All changes stay in this repo, and the factory keeps working throughout.

1. **Principals and grants (done, passdesk-broker#7 "decouple-identity").** The
   broker reads `STATE/grants.json` if present, else `STATE/roles.json` (same
   format, for deployments that still write that name). A grant is a bare
   list of tool names, or an object `{"tools": [...], "source": "..."}`.
   `scripts/start-broker.sh` writes `grants.json` in the object form, plus a
   `roles.json` in the old bare-list form as a transition shim for a broker
   still on a pin before `grants.json` landed (to drop once that pin has
   settled).
2. **Grant hints out of core (done, passdesk-broker#7).** Each grant can
   carry a `source` string, which the deny message quotes ("see
   `<source>`"); with none, it falls back to a generic "its BROKER_TOOLS
   grant" hint. `grant_hint` in `core/protocol.py` no longer hardcodes
   factory paths - `start-broker.sh` sets `source` to `roles/<name>/role.conf`
   or `kits/coordinator/broker.conf`.
3. **File exchange behind one interface.** Tools ask an exchange object for "a place to put a bundle for this caller" or "the bundle this caller handed me". Two backends:
   - *Shared mount*: today's `.factory/` behavior, for sbx.
   - *In-protocol transfer*: bundles and tarballs move as MCP blob content, with size limits and hashes. This is what lets the broker work with agents that share no filesystem with it.

   Also lifts the one-project-per-broker limit: each principal maps to a workspace.
4. **Credential providers.** One lookup interface (`get(name) -> str | None`, read fresh on every call), with providers for:
   - proxy-injected (sbx: no value needed in the broker)
   - file (today's exception)
   - environment from an external secret manager
   - GitHub App installation tokens: short-lived and scoped per repo, replacing a long-lived PAT
5. **Policy tooling (partly done).**
   - A JSON schema for `policy.toml`, checked at load time (bad keys fail the image build, not a tool call).
   - `broker policy validate`.
   - **Done:** `python3 -m passdesk.doctor --egress-hosts`, run inside the
     broker sandbox, prints every host the baked-in policy can reach.
     `factory doctor` calls it and checks the result against
     `kits/broker/spec.yaml` and `$FACTORY_CONFIG/broker-kit/spec.yaml`
     (`bin/factory`'s "Passdesk (the gateway)" step), so those kit files are
     checked against the policy instead of only synced by hand. The same
     doctor also validates the policy and credential setup live
     (`--live`), short of a schema checked at build time.
6. **Audit schema (partly done, passdesk-broker#7).** The caller field moved
   to `principal`, with `role` kept as a duplicate alias until
   `scripts/analytics.py` no longer needs it (it now reads `principal`,
   falling back to `role` for older lines). Still outstanding: a documented
   event format with a version field, pluggable sinks (syslog, webhook,
   OpenTelemetry), and a hash chain over records so edits and deletions show.

## Phase 2: package it

1. Move the broker into its own repository, still standard-library Python. A tiny dependency surface is part of its security argument. It ships:
   - an image pinned by digest
   - a `docker compose` quickstart
   - TLS, with optional mTLS between gateway and broker
   - MCP conformance tests run against the clients it supports

   **Partly done:** the broker moved to its own repository, `passdesk-broker`
   (history preserved via a subtree split), renamed package and all. The
   factory consumes it as a git submodule at `broker/`, pinned to a commit —
   not yet as an image pinned by digest, since there is no registry to push
   one to. The `docker compose` quickstart, TLS/mTLS and MCP conformance
   tests are still outstanding.
2. **Adapters:**
   - *sbx*: today's `start-broker.sh` logic (token minting into `sbx secret`, `sbx mcp add` per principal, credential delivery), moved into the broker repo.
   - *Generic*: setup docs for MCP clients (Claude Code, Codex, Cursor, others), each tested.

   **Partly done (passdesk-broker#8 "decouple-deploy"):** the pieces a
   non-sbx deployment needs to override are now plain settings rather than
   hardcoded paths - `BROKER_BIND_HOST`, `BROKER_ALLOWED_ORIGINS` and
   `BROKER_EXCHANGE_DIR` (env, see `core/config.py`), and `ARG
   INSTALL_DIR=/opt/factory-broker` (Dockerfile; `run.sh` resolves its own
   directory instead of hardcoding it). `start-broker.sh` itself still lives
   in this repo and keeps every default (`0.0.0.0`, empty origins allowlist,
   `.factory`, `/opt/factory-broker`), so this needed no change here.
3. **The factory consumes the broker** as an image pinned by digest, per CLAUDE.md's pinning rule. It keeps only its role → principal mapping and the sbx adapter configuration. `broker/tools.txt` is generated from the pinned image's tool list at build time.

## Phase 3: extension points

1. **Approvals (mostly done).** A hook before risky actions (push, opening a
   PR, a dependency not seen before, a new host): the core holds the call
   until an external approver answers or a timeout denies it. The core ships
   the hook and a deny-all default; approvers (chat, web) plug in.

   **Done:** the gateway's hold (`passdesk/core/holds.py`, moved into
   core from the former Pro plugin on 2026-10-21) hooks `git_push`,
   `github_create_pr` and `gitlab_create_mr` when `[approvals].tools`
   lists them, writing a pending card to the broker state directory
   and staging the write: the call returns at once and the gateway makes
   it once approved (doomtickle/passdesk-ops#170; until 24 Oct 2026 the
   call first waited `[approvals].hold_seconds`). People decide from the dashboard's control unit
   (APPROVE/DECLINE, including the patch INSPECT view) or from
   `scripts/approvals.py` (`factory approvals`), both writing the same
   decision file. Still outstanding: holds are off by default (no deny-all
   default) and chat-based approvers aren't wired up.
2. **Admin API.** Read-only endpoints for audit queries, current grants and policy version, on a separate listener from the agent-facing MCP endpoint. A fleet view can build on it.
3. **Managed policy.** Policy and grants loaded from a signed bundle, so a central team can roll out changes without rebuilding images. Agents still can't influence the policy source.

## Open decisions

- ~~**License** for the standalone repository.~~ Settled: Apache-2.0.
- **Default file exchange:** in-protocol transfer is portable but slower for large dependency mirrors. Shared mount stays the sbx default.
- ~~**Name:** the package is `agentbroker` for now; the server still calls itself `factory-broker`.~~
  Settled: the project and package are **Passdesk** (`passdesk/`); the MCP
  `serverInfo.name` it reports is `"passdesk"`. The sandbox name
  (`factory-broker`) and its filesystem paths (`/opt/factory-broker`,
  `/etc/factory-broker`) are unchanged — those are the factory's deployment
  choices, not the project's identity.
