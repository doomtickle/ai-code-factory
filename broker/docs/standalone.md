# Passdesk standalone: `docker compose` + Claude Code

Run Passdesk on plain Docker, no Docker Sandboxes. One `docker compose up`
gives you the gateway, an egress guard for the agent's model traffic, and a
Claude Code agent whose only ways out are the gateway's tools and that
guard. The agent never holds your GitHub token; every push, pull request,
package download and web fetch is a Passdesk tool call, policy-checked,
secret-scanned and audited. An isolation check you run yourself shows
that GitHub, PyPI, npm and the open web are unreachable from the agent
except through Passdesk.

**Checked on 18 Oct 2026** with Docker Desktop 4.54.0 (Engine 29.1.2,
Compose v2.40.3) on WSL2, Claude Code 2.1.280 from the
`docker/sandbox-templates:claude-code` image. A native Linux engine and
Docker Desktop for Mac are expected to behave the same (the isolation
relies on a documented Docker option) but have not been run through this
page yet; the isolation check is how you confirm it on yours.

## What you need

- **Docker Engine 28.0.0 or newer** (Docker Desktop 4.39 or newer; `docker
  version` shows the engine). The `agents` network uses
  `gateway_mode_ipv4: isolated`, which older engines don't have: without it
  the agent could reach services listening on your machine through the
  bridge address. An older engine rejects the option and nothing starts,
  which is the safe failure.
- **A GitHub repository** and a fine-grained token for it (below). For a
  GitLab project instead, set `[gitlab].url`, list the project host first
  and deliver a `gitlab` project or group access token: README, "Configuring
  credentials", GitLab. This walkthrough uses GitHub.
- **A Claude Pro or Max subscription, or an Anthropic API key**, for the
  agent.
- This repository, cloned: the image is built from it until the published
  image lands (doomtickle/passdesk-ops#59).

## 1. The GitHub token

Create a **fine-grained personal access token** (GitHub: Settings,
Developer settings, Personal access tokens, Fine-grained): repository
access limited to the repositories you will list in the policy, with
exactly two permissions, **Contents: read and write** and **Pull requests:
read and write**. Nothing else. Put it in a file the gateway reads as a
Docker secret:

```sh
mkdir -p secrets
printf '%s\n' 'github_pat_...' > secrets/github
chmod 0600 secrets/github
```

`secrets/` is git-ignored. The gateway reads the file fresh on every call,
so to rotate the token, overwrite the file. The agent container never sees
it: Passdesk hands it to git in one child process's environment, refuses
redirects with it, and redacts it from everything it returns or records
(README, "Configuring credentials").

## 2. The policy

Passdesk's policy is baked into the image. Put your overlay in
`config/policy.toml` (git-ignored) with at least the repositories the
token covers:

```toml
[git]
repos = ["you/your-repo"]
```

and uncomment the `additional_contexts` lines under the `passdesk`
service's `build:` in `compose.yaml`. The defaults for everything else
(allowed docs hosts, package lists, branch rules, the egress guard's model
hosts) are in `policy.toml`; the README explains each table.

## 3. Start it

```sh
mkdir -p workspace            # or PASSDESK_WORKSPACE=/path/to/your/project
docker compose up -d
docker compose ps             # passdesk (healthy), egress-guard, agent
```

The workspace directory is bind-mounted into the agent (as its working
directory) and into the gateway, so the `.factory/` exchange works: a
`git_fetch` lands a bundle in `.factory/incoming/`, the agent pushes by
putting a bundle in `.factory/outgoing/`.

## 4. Give the agent a token

A principal is a name with a tool grant and a token. Mint one for the
agent inside the gateway container (the token is printed once and stored
only as a hash):

```sh
docker compose exec passdesk python3 -m passdesk principal add agent \
  --tools git_fetch git_push github_create_pr web_fetch pip_download npm_fetch
```

Put the token in `.env` next to `compose.yaml` (git-ignored) and recreate
the agent so it gets it:

```sh
echo 'PASSDESK_TOKEN=<the token>' > .env
docker compose up -d agent
```

This token grants only those tools; it is not a GitHub credential. The
agent's shell can read it (it is in the container's environment), which
buys it nothing it didn't already have.

## 5. Run the isolation check

Before trusting the setup, make the agent's own network prove itself:

```sh
docker compose --profile check run --rm isolation-check
```

It runs `scripts/isolation-check.py` inside the agent container's network
namespace and prints one line per probe. Every line must say `PASS` and
the last line `SUMMARY 36 checks, all passed`. It fails if the agent can
resolve an external name, reach a public IPv4 or IPv6 address on 443,
reach a public DNS resolver, resolve `host.docker.internal`, or reach any
gateway address (an `agents` network with a gateway at all is a failure);
and it fails unless the gateway answers MCP with the agent's token, the
guard tunnels a CONNECT to `api.anthropic.com` (with a real TLS handshake)
and refuses `github.com`, `pypi.org`, `registry.npmjs.org`,
`169.254.169.254` and `host.docker.internal`. Run it again after any
Docker upgrade or compose change. A run on this machine is in
doomtickle/passdesk-ops#58.

## 6. Connect Claude Code

Attach to the agent and register Passdesk as an MCP server (user scope
needs no per-project approval; the header reads the token from the
environment, so the token is not in the config file):

```sh
docker compose exec agent claude mcp add -s user --transport http passdesk \
  http://passdesk:8765/mcp --header 'Authorization: Bearer ${PASSDESK_TOKEN}'
docker compose exec agent claude mcp list      # passdesk: ... - Connected
docker compose exec agent claude               # sign in, then work
```

Sign in with your subscription when `claude` asks (it opens a URL you
visit on your machine; the sign-in and model traffic go through the egress
guard, which forwards only `api.anthropic.com` and the claude.ai sign-in
hosts), or set `ANTHROPIC_API_KEY` in the `agent` service's environment.
The agent's `~/.claude` is a named volume, so the sign-in survives
`docker compose down`. Then ask it to fetch your repo (`git_fetch`), make
a change, push a `factory/...` branch (`git_push`) and open a draft PR
(`github_create_pr`), and read the audit record:

```sh
docker compose exec passdesk tail /var/lib/passdesk/audit.jsonl
docker compose exec passdesk python3 -m passdesk.doctor --live
```

## What is and isn't protected

- **Protected:** the agent has no GitHub token and no route to GitHub,
  PyPI, npm, the web, your machine's services, or the Docker socket. Its
  only ways out are Passdesk's tools (policy, secret scan, audit, and the
  approval hold if you switch it on) and a CONNECT to the model API hosts in
  `[egress_guard].allow_hosts`. The isolation check is the evidence.
- **Not protected:** what the agent sends to the model API itself (that
  tunnel is opaque by design: no TLS interception), and anything you mount
  into the agent container beyond the workspace. The gateway's MCP port is
  plain HTTP because only the `agents` network reaches it
  (`BROKER_INSECURE_BIND=1`); if you ever publish that port or put the
  gateway on another host, turn on TLS instead (README, "TLS").
- **Keep it that way:** no `ports:` on the agent, no `extra_hosts` with
  `host-gateway`, no `/var/run/docker.sock` mount, and never add a host an
  agent could fetch code or packages from to the guard's allowlist.

## Troubleshooting

- `gateway_mode_ipv4` rejected: your engine is older than 28.0.0. Upgrade;
  do not remove the option.
- `secret file not found`: create `secrets/github` (step 1). An empty file
  is accepted and means "no token yet".
- `git_fetch` says DENIED and names `[git].repos`: the overlay in step 2
  isn't baked in; rebuild with `docker compose build passdesk` after
  uncommenting `additional_contexts`.
- The agent can't sign in: `docker compose logs egress-guard` shows every
  refused CONNECT with its target. The default allowlist is for Claude Code
  and Codex CLI; Cursor routes through its own hosts and is supported on
  the host only.
- On a Linux host where your user isn't uid 1000, the gateway (uid 1000 in
  the image) may not be able to write `.factory/` in the workspace: build
  with `--build-arg PASSDESK_UID=$(id -u)` (README, "Images").
