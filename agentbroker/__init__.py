"""Factory broker: the only component with outbound network access for tool traffic.

Agent sandboxes reach this server exclusively through the sbx MCP gateway.
Every tool call is authenticated, checked against policy.toml, content-filtered
in both directions, and written to an append-only JSONL audit log.

Design rules:
  * The workspace is agent-controlled. The broker never executes anything from
    it (no hooks, no install scripts, no repo-local git config). Git network
    operations run in private mirrors; code moves in and out as git bundles.
  * Credentials never pass through here: the sbx egress proxy injects them,
    with one documented exception (CLAUDE.md, "Credentials"): Jira, Gemini and
    the private npm registry, which the proxy cannot reach. Those are delivered as files outside the
    workspace and BROKER_STATE by scripts/start-broker.sh, from the host OS
    keychain, and are read fresh from disk on every call.

Layout: core/ is the gateway itself (protocol, auth and grants, policy,
filtering, audit) and knows no tool; tools/ holds one module per capability,
each registering its tools with core.registry. docs/broker-extraction.md
describes where this is headed.

Standard library only (Python 3.11+ for tomllib).
"""
