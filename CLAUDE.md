# Passdesk: Security Rules

Passdesk is an MCP gateway: agents get no direct network egress, and every
tool call is checked, filtered and audited here. These rules protect that
boundary.

- Passdesk must never execute agent-controlled content: no git hooks or
  repo-local git config from a caller's workspace, no package install
  scripts, no binaries or interpreters from the workspace. Code crosses the
  boundary as git bundles or plain files. Dependencies cross it as
  lockfile-pinned, hash-verified tarballs (`npm_fetch`) or wheels
  (`pip_download`) that callers install offline in their own sandbox.
- Policy is baked into the image at build time (see `Dockerfile`), never read
  from a caller-writable workspace. A deployment may overlay its own
  `policy.toml` at build time (`policy.local.toml`, merged table by table),
  but the baked-in defaults are never editable at runtime.
- Real credentials (GitHub, Jira, Gemini, a private package registry) must be
  injected out-of-band, never passed into an agent sandbox in plaintext.
  - GitHub works when the deployment's egress proxy injects the token
    directly (sbx); Passdesk itself never needs to see it. A deployment
    with no such proxy (`BROKER_DEPLOYMENT=standalone`) delivers a
    fine-grained `github` token the same way as the file-read credentials
    below (owner decision, `docs/decisions.md`); Passdesk hands it to git
    only through `GIT_CONFIG_*` variables in one child's environment
    (scoped to `https://github.com/`, never argv or disk), never follows a
    redirect with it, and redacts it from everything it returns or records.
  - Credentials nothing can proxy-inject (currently Jira, Gemini, and a
    read-only token for a private npm registry) are read from files inside
    Passdesk's own sandbox (`/etc/factory-broker/secrets/`, a Docker secret
    under `/run/secrets/`, or a file whose path is in
    `PASSDESK_SECRET_<NAME>_FILE`), delivered there by the deployment (never
    via argv, an env flag holding the value, a log, or git). All of them go
    through `passdesk/core/credentials.py`. Passdesk reads each file fresh
    on every call, so rotating the file rotates the credential without
    restarting the server. Do not widen this exception to other credentials
    without updating this rule. A provider a plugin registers with
    `credentials.register_provider` is bound by the same rule: it never
    takes a value from a plain environment variable, and never logs or
    returns a value except to the tool that asked for it.
- Passdesk is standard-library Python only (3.11+). A small dependency
  surface is part of its security argument; do not add a third-party
  dependency without re-justifying this rule.
- This repo is shared, so every setup must be repeatable: pin the base
  container image by digest (`Dockerfile`) and flag anything that cannot be
  pinned.
