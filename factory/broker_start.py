"""Start (or restart) the broker sandbox for a project, register it with the sbx MCP gateway
once per role that has BROKER_TOOLS, and deliver the credentials sbx can't proxy-inject. Port
of scripts/start-broker.sh, now a shim that runs this as `factory _start-broker <project-dir>`;
02-launch-factory.sh calls it, and it is safe to run on its own.

Each role authenticates with its own bearer token, minted once: the value goes only to `sbx
secret set` on its stdin (the gateway sends it as that role's header); the broker sees only its
SHA-256 in tokens/<role>.sha256 and maps it back to the role. grants.json tells the broker which
tools each role may use. Both live in the broker state dir (FACTORY_BROKER_STATE, outside the
repo), shared into the broker sandbox only, never into an agent sandbox.

The credential rules of CLAUDE.md ("Credentials") apply: a value (a broker token, a keychain
entry) goes only to a program's stdin (factory/programs.py), never to argv, the environment, a
message, a log or a file on the host; every program runs from an argv list, never through a
shell on the host. The one shell is the fixed script SERVER_SCRIPT, run inside the broker
sandbox with no value in it.
"""

import hashlib
import json
import os
import secrets as token_source
import subprocess
import time

from factory import fingerprints, preflight, roles, sbx, secrets
from factory.commands.status import BROKER_PORT
from factory.context import FactoryError
from factory.programs import DROP, run_tool, to_stderr
from factory.state import migrate_state, require_state_outside

URL = "http://127.0.0.1:%d/mcp" % BROKER_PORT

# Starts the server if it is not running (kit startup commands do not re-run on restart). The
# pgrep pattern is anchored so it cannot match this sh -c command line itself. Byte for byte the
# script start-broker.sh ran; it takes no arguments.
SERVER_SCRIPT = ('pgrep -f "^/bin/sh /opt/factory-broker/run.sh" >/dev/null '
                 '|| setsid /opt/factory-broker/run.sh </dev/null >/dev/null 2>&1 &')

# The gateway resolves ${token} from the sbx secret store at connect time: not a value.
AUTH_HEADER = "Authorization: Bearer ${token}"

# The names synced from the keychain on every start, in this order.
SYNCED_NAMES = ("jira", "gemini", "npm-registry", "gitlab", "approvals-webhook")

READY_TRIES = 30
_sleep = time.sleep  # a test replaces it


class Exit(Exception):
    """Stop with this exit status; what went wrong has been printed (bash's set -e or exit)."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def broker_mcp(role):
    return "factory-broker-" + role


def token_secret(role):
    return "mcp:%s:token" % broker_mcp(role)


def _usage(ctx):
    return "usage: %s <project-dir>" % os.path.join(ctx.paths.root, "scripts", "start-broker.sh")


def _check(status):
    """A step that failed stops the start with its exit status, as set -e did."""
    if status != 0:
        raise Exit(status)


def run_or_die(ctx, message, argv):
    """Run ARGV with its output held back; if it fails, print MESSAGE and the last 20 lines it
    printed (stdout and stderr) and stop with exit status 1 (common.sh run_or_die)."""
    got = ctx.capture(argv, merge=True)
    if got.status == 0:
        return
    text = "error: %s (exit status %d)\n" % (message, got.status)
    if got.out:
        lines = got.out.split("\n")
        if got.out.endswith("\n"):
            lines.pop()
        text += "last output of '%s' (up to 20 lines):\n" % " ".join(argv)
        text += "\n".join("  " + line for line in lines[-20:]) + ("\n" if got.out.endswith("\n") else "")
    to_stderr(ctx, text)
    raise Exit(1)


def _write(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def write_grants(ctx, state):
    """Write grants.json from roles/*/role.conf and kits/coordinator/broker.conf, and return the
    broker roles (those with a grant), in order. The broker re-reads it per request; each entry
    names the file that grants it, so a denied principal's error can point there."""
    paths = ctx.paths
    grants, broker_roles = {}, []
    for role in roles.list_roles(paths):
        roles.validate_role(paths, role)
        tools = roles._words(roles.role_get(paths, role, "BROKER_TOOLS"))
        if not tools:
            continue
        broker_roles.append(role)
        grants[role] = {"tools": tools, "source": "roles/%s/role.conf" % role}
    # The Coordinator is a broker pseudo-role (kits/coordinator/broker.conf): it calls broker
    # tools directly, with no role in between, gated the same way against the known tools.
    tools = roles._words(roles.coordinator_broker_tools(paths))
    if tools:
        known = roles.known_broker_tools(paths)
        for tool in tools:
            if not roles._in_list(tool, known):
                raise FactoryError("kits/coordinator/broker.conf: unknown broker tool '%s' (known: %s)" % (tool, known))
        broker_roles.append("coordinator")
        grants["coordinator"] = {"tools": tools, "source": "kits/coordinator/broker.conf"}
    tmp = os.path.join(state, "grants.json.tmp")
    _write(tmp, json.dumps(grants, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, os.path.join(state, "grants.json"))
    return broker_roles


def mint_tokens(ctx, state, broker_roles):
    """Mint a token for each broker role that has no hash or whose secret sbx doesn't hold. Kept
    once minted: the sbx daemon caches header secrets, so a new value only reaches the gateway
    after `sbx daemon restart` (README, "Rotating broker tokens")."""
    stored = ctx.capture(["sbx", "secret", "ls"]).out
    for role in broker_roles:
        secret = token_secret(role)
        hash_file = os.path.join(state, "tokens", role + ".sha256")
        try:
            have_hash = os.path.getsize(hash_file) > 0
        except OSError:
            have_hash = False
        if have_hash and (" %s " % secret) in stored:
            continue
        ctx.say("Minting broker token for %s..." % role)
        token = token_source.token_hex(32).encode()
        token_hash = hashlib.sha256(token).hexdigest()
        # The hash is written only once the secret is stored: a failed set must leave the old
        # hash matching the old secret (-f: without it an existing secret silently stays).
        status = run_tool(ctx, ["sbx", "secret", "set", "-f", secret], token, out=DROP, err=DROP)[0]
        del token
        if status != 0:
            raise FactoryError("could not store the broker token secret '%s' with sbx; the token hash was not "
                               "changed. Check 'sbx secret ls' and that sbx is signed in, then run this again" % secret)
        _write(hash_file, token_hash + "\n")


def create_sandbox(ctx, project, state):
    paths = ctx.paths
    ctx.say("Creating broker sandbox for %s..." % project)
    # This machine's extra broker egress hosts (e.g. a private npm registry), if any.
    kits = ["--kit", os.path.join(paths.root, "kits", "broker")]
    local_kit = os.path.join(paths.config, "broker-kit")
    if os.path.isfile(os.path.join(local_kit, "spec.yaml")):
        ctx.say("Including local broker kit from %s" % local_kit)
        kits += ["--kit", local_kit]
    # Plugins baked into the image (00-build-templates.sh) load only when named here; the
    # folder names under the local plugins folder are the modules.
    plugins = fingerprints.broker_plugins(paths)
    if plugins:
        ctx.say("Enabling broker plugins: %s" % plugins)
        kits += ["-e", "PASSDESK_PLUGINS=" + plugins]
    run_or_die(ctx, "could not create the broker sandbox",
               ["sbx", "run", "-d", "--name", sbx.BROKER_SANDBOX, "--pull", "never",
                "-t", "factory-broker:" + sbx.TEMPLATE_TAG, "-p", "127.0.0.1:%d:8765" % BROKER_PORT,
                "-e", "BROKER_WORKSPACE=" + project, "-e", "BROKER_STATE=" + state,
                "shell"] + kits + [project, state])
    fingerprints.record_broker_fingerprint(paths)
    # A brand new sandbox has none of the credentials delivered, so any marker left from an
    # older one (after 'factory reset' or 'factory update' removed it) is stale: left in place,
    # sync_from_keychain would skip that name forever, "leaving it as is" for a credential that
    # no longer exists anywhere.
    cleared = secrets.clear_all_manual(paths)
    if cleared:
        ctx.say("note: this is a new broker sandbox, so the 'factory secret set' marker(s) for: %s were cleared; "
                "run 'factory secret set <name>' again for each to redeliver it" % " ".join(cleared))


def port_published(ctx):
    """True if `sbx ports --json` lists the broker's host port."""
    got = ctx.capture(["sbx", "ports", sbx.BROKER_SANDBOX, "--json"])
    if got.status != 0:
        return False
    try:
        ports = json.loads(got.out)
    except ValueError:
        return False
    if isinstance(ports, dict):
        ports = list(ports.values())
    if not isinstance(ports, list):
        return False
    return any(isinstance(p, dict) and type(p.get("host_port")) in (int, float) and p["host_port"] == BROKER_PORT
               for p in ports)


def wait_ready(ctx, state):
    """Ready = the server answers and demands a token (401 without one)."""
    ctx.say("Waiting for the broker to answer...")
    code = "000"
    for _ in range(READY_TRIES):
        code = ctx.capture(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "3",
                            "-X", "POST", "-d", "{}", URL]).out
        if code == "401":
            return
        _sleep(1)
    raise FactoryError("broker did not become ready (last HTTP status: %s); see %s"
                       % (code, os.path.join(state, "broker.log")))


def register(ctx, state, broker_roles):
    """Register each broker role with the MCP gateway once, and drop the registrations, tokens
    and secrets of roles that no longer have broker access."""
    registered = ctx.capture(["sbx", "mcp", "ls", "-q"]).out
    lines = registered.split("\n")
    for role in broker_roles:
        name = broker_mcp(role)
        if name not in lines:
            _check(ctx.run(["sbx", "mcp", "add", name, "--url", URL, "--header", AUTH_HEADER, "--skip-auth"],
                           stdout=subprocess.DEVNULL))
    prefix = broker_mcp("")
    for name in [w for line in lines if line.startswith(prefix) for w in line.split()]:
        role = name[len(prefix):] if name.startswith(prefix) else name
        # A NUL (from sbx) can reach neither an argv nor a path: leave that name alone.
        if "\0" in name or roles._in_list(role, " ".join(broker_roles)):
            continue
        ctx.say("Removing broker access for former role %s..." % role)
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        ctx.run(["sbx", "mcp", "rm", "--force", name], **quiet)
        ctx.run(["sbx", "secret", "rm", "-f", "mcp:%s:token" % name], **quiet)
        ctx.run(["sbx", "secret", "rm", "-f", "mcp:%s:token:endpoint" % name], **quiet)
        # The name comes from sbx: never let it reach outside tokens/.
        if "/" in role:
            continue
        hash_file = os.path.join(state, "tokens", role + ".sha256")
        try:
            os.remove(hash_file)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise FactoryError("could not remove %s: %s" % (hash_file, e.strerror or e))


def deliver_credentials(ctx):
    """The credentials the sbx egress proxy cannot inject (the named exception in CLAUDE.md,
    "Credentials"): looked up in the host keychain and piped over `sbx exec` stdin into the
    broker sandbox (secrets.sync_from_keychain). Re-running rotates them, and a deleted keychain
    entry revokes the broker's file; a name `factory secret set` delivered is left alone. Run
    last, after MCP registration. A delivery that fails (a denied keychain prompt, a locked
    keychain, a broker image predating /etc/factory-broker/secrets) only warns, so the rest of
    the factory stays usable; a failed revocation is fatal, as the credential may still be
    active. Then the GitHub token: not a file but the broker-scoped sbx secret the egress proxy
    injects, which 'sbx rm' deletes with the sandbox, so it is restored from the keychain
    (factory-github) on every start; without an entry, a token set by hand is left alone."""
    revocation_failed = False
    for name in SYNCED_NAMES:
        if secrets.sync_from_keychain(ctx, name) == secrets.REVOKE_FAILED:
            revocation_failed = True
    secrets.github_sync_from_keychain(ctx)
    if revocation_failed:
        raise FactoryError("a credential revocation failed above; it may still be active in the broker sandbox - "
                           "see the error(s) above")


def start(ctx, args):
    paths = ctx.paths
    if len(args) != 1:
        raise FactoryError(_usage(ctx))
    if not os.path.isdir(args[0]):
        raise FactoryError("project directory not found: %s" % args[0])
    sbx.require_sbx(ctx)
    if not preflight.which(ctx, "curl"):
        raise FactoryError("'curl' not found in PATH (see README.md)")
    roles.known_broker_tools(paths)  # refuses a checkout with no broker/tools.txt, as roles.sh did
    project = os.path.realpath(args[0])
    state = paths.broker_state

    require_state_outside(paths, project)
    migrate_state(ctx, paths.root)
    os.makedirs(os.path.join(state, "tokens"), exist_ok=True)
    os.chmod(state, 0o700)

    # A broker's workspace is fixed when its sandbox is created.
    bound_file = os.path.join(state, "project")
    if sbx.sandbox_exists(ctx, sbx.BROKER_SANDBOX) and os.path.isfile(bound_file):
        with open(bound_file, encoding="utf-8", errors="surrogateescape") as f:
            bound = f.read().rstrip("\n")
        if bound != project:
            raise FactoryError("the broker is bound to %s. To switch projects, see README (Switching projects)." % bound)
    _write(bound_file, project + "\n")

    broker_roles = write_grants(ctx, state)
    mint_tokens(ctx, state, broker_roles)

    if sbx.sandbox_exists(ctx, sbx.BROKER_SANDBOX):
        ctx.say("Starting existing broker sandbox...")
        run_or_die(ctx, "could not start the broker sandbox", ["sbx", "run", "-d", "--name", sbx.BROKER_SANDBOX])
    else:
        create_sandbox(ctx, project, state)

    _check(ctx.run(["sbx", "exec", sbx.BROKER_SANDBOX, "sh", "-c", SERVER_SCRIPT]))

    # Port publishing does not survive every restart; re-publish if it is gone.
    if not port_published(ctx):
        _check(ctx.run(["sbx", "ports", sbx.BROKER_SANDBOX, "--publish", "127.0.0.1:%d:8765" % BROKER_PORT],
                       stdout=subprocess.DEVNULL))

    wait_ready(ctx, state)
    register(ctx, state, broker_roles)
    deliver_credentials(ctx)

    ctx.say("Broker ready: %s, roles:%s (audit log: %s)"
            % (URL, "".join(" " + r for r in broker_roles) or " none", os.path.join(state, "audit.jsonl")))
    return 0


def main(ctx, args):
    """`factory _start-broker <project-dir>`: what scripts/start-broker.sh runs."""
    try:
        return start(ctx, args)
    except Exit as e:
        return e.status
    except OSError as e:
        # A state file or folder that can't be written: bash's redirection or mkdir failed and
        # set -e stopped it with status 1.
        to_stderr(ctx, "error: %s: %s\n" % (e.filename, e.strerror or e) if e.filename else "error: %s\n" % e)
        return 1
