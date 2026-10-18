# Write an integration (plugin tools)

Passdesk ships tools for GitHub, npm, PyPI, web fetch, Jira and Gemini. A
plugin adds your own: a status page, an internal API, an issue tracker. An
agent sees a plugin tool like any other tool, and the gateway applies the
same checks to it.

## What the gateway does around every plugin tool call

You write the handler. The gateway does the rest, and a plugin can't turn
any of it off:

- **Grants.** A principal can see and call the tool only if its grant lists
  it (`python -m passdesk principal add <name> --tools status_check`, or the
  deployment's own grant file). Calls outside the grant are refused.
- **Arguments.** Only the declared arguments are accepted, as `string` or
  `boolean`. Missing required arguments and unknown ones are refused before
  your handler runs.
- **Outbound secret scanning.** Every string argument is scanned for known
  key and token formats (and credentials the gateway has used) before your
  handler runs. A match refuses the call.
- **Approvals.** A tool declared `writes_externally=True` gets an `intent`
  audit record before it runs, then goes through the approval point with
  the card `{"tool": <name>, <its arguments>, "principal": <caller>}`. With
  Passdesk Pro, list the tool in `[approvals] tools` to hold it for a person.
- **Inbound cleaning.** Whatever your handler returns has terminal escapes
  and control characters stripped, known secret formats redacted and its
  size capped. It's then wrapped as untrusted content, so the agent treats
  it as data, not instructions. A refusal (`Denied`) message is cleaned the
  same way.
- **Audit.** Every call gets a record: time, principal, tool, redacted
  arguments, the decision and a result excerpt.

## Trust: what a plugin is

A plugin is code the operator installs into the gateway's image at build
time, next to the gateway's own. It runs in the gateway's process, with
the gateway's access. The checks above protect the boundary between the
**agent** and the outside world. They don't contain a malicious plugin.
Install only plugins you'd be willing to put in the core, and review them
like core changes.

## Declare the tools

A plugin is a Python package on the image's `plugins` build context. It
declares its tools as two module attributes. The gateway validates and
registers them at start-up, before it calls the plugin's `activate()`, if
the plugin has one. A plugin never calls the registry itself.

```python
from passdesk.core.errors import Denied
from passdesk.core.registry import PluginTool

PASSDESK_TOOL_NAMESPACE = "status"
PASSDESK_TOOLS = {
    "status_check": PluginTool(
        handler=check,                     # check(args: dict, policy: dict) -> str
        description="Report a service's current status from its public status page.",
        properties={"page": {"type": "string", "description": "a configured page name"}},
        required=("page",),
        writes_externally=False,           # True for anything that changes something outside
        hosts=("www.githubstatus.com",),   # every host the handler may contact
    ),
}
```

The rules (a broken declaration stops the gateway at start-up, and the
doctor reports it):

- The namespace is 2 to 16 lowercase letters or digits. Names that read as
  the gateway or its own tools (`git`, `github`, `web`, `jira`, `gemini`,
  `npm`, `pip`, `passdesk`, …) are reserved. One plugin owns a namespace.
- Every tool name is `<namespace>_<words>` in lowercase with `_`, at most 64
  characters, so a plugin can never shadow or replace a core tool.
- Arguments are `string` or `boolean`, each with an optional `description`
  and nothing else. Other schema keywords would be advertised to the agent
  but not enforced, so they're refused.
- At most 32 tools per plugin.

The handler gets a copy of the arguments and the gateway's policy. It
returns text, or raises `Denied("reason")` to refuse. Any other exception
is reported to the agent as an error and audited.

## Configuration

Read your settings from the policy, under `[plugins.<namespace>]` in the
deployment's `policy.local.toml`. Like all policy, it's baked into the
image at build time and never read from a workspace the agent can write.

```toml
[plugins.status]
pages = { github = "www.githubstatus.com" }
```

## Credentials

Read a credential only with `passdesk.core.credentials.get("<name>")`, at
call time, never at import. It comes from the gateway's secrets directory, a
Docker secret at `/run/secrets/<name>`, or a file named by
`PASSDESK_SECRET_<NAME>_FILE` (README, "Configuring credentials"). Never
from a plain environment variable, never from the workspace. Rules:

- Use a name of your own (e.g. `status-api`). Never read another tool's
  credential (`github`, `jira`, `gemini`, `npm-registry`).
- Send it only with `passdesk.core.net.NO_REDIRECT_OPENER`, so a redirect
  can't carry it to another host.
- Don't put it in the text you return, in a `Denied` message or in an
  exception. Known formats are redacted on the way out, but a value that
  matches no pattern isn't. After reading one, call
  `passdesk.core.filters.remember_secret(value)` so the gateway redacts that
  exact value everywhere.

## Network

Contact only the hosts you declare in `hosts`, and check each request's
host against your policy settings before you send it. Declaring hosts
doesn't open anything. It tells the doctor which hosts the deployment's
network allowlist needs (`python -m passdesk.doctor --egress-hosts`, or
`--kit` for an sbx kit). In a Docker Sandboxes deployment, the kit's
allowlist is the network-level backstop: add your hosts there.

## Install

1. Put the package directory in the directory you pass as the image's
   `plugins` build context (`docker buildx build --build-context
   plugins=<dir> ...`; the Dockerfile puts it on `PYTHONPATH`), and name it
   in `PASSDESK_PLUGINS` (comma-separated).
2. Add its `[plugins.<namespace>]` settings to `policy.local.toml`, and
   deliver any credential it reads.
3. Grant its tools to the principals that should have them.
4. Run `python -m passdesk.doctor`. It lists each plugin's declared tools,
   fails an invalid declaration, and counts declared hosts in the egress
   check.

## Example

`examples/plugins/passdesk_status/` is a complete read-only plugin: one
tool, `status_check`, that reads a service's status from its public
Statuspage page. Only hosts that are both in its code (`HOSTS`) and in the
deployment's `[plugins.status].pages` are allowed. Its tests are in
`tests/test_plugin_tools.py`.
