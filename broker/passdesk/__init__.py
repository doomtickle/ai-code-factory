"""Passdesk: an MCP gateway, the only component with outbound network access
for tool traffic.

Agents reach it only as MCP tools; deployments such as Docker Sandboxes (sbx)
and AI Code Factory are adapters on top. Every tool call is authenticated,
checked against policy.toml, content-filtered in both directions, and
written to an append-only JSONL audit log.

Design rules:
  * The workspace is agent-controlled. The broker never executes anything from
    it (no hooks, no install scripts, no repo-local git config). Git network
    operations run in private mirrors; code moves in and out as git bundles.
  * Credentials never pass through here: a deployment's egress proxy may
    inject them (e.g. GitHub), with one documented exception (CLAUDE.md,
    "Credentials"): Jira, Gemini and the private npm registry, which a proxy
    cannot reach. Those are delivered as files outside the workspace and
    BROKER_STATE by the deployment, from wherever it keeps secrets, and are
    read fresh from disk on every call through core/credentials.py.

Layout: core/ is the gateway itself (protocol, auth and grants, policy,
filtering, audit) and knows no tool; tools/ holds one module per capability,
each registering its tools with core.registry.

Standard library only (Python 3.11+ for tomllib).
"""
