"""The credentials sbx cannot proxy-inject (Jira, Gemini, the private npm registry token,
the GitLab token and the approvals webhook URL): the named exception in CLAUDE.md
("Credentials"). Port of the helpers in scripts/lib/secrets.sh that `factory secret` uses;
the keychain itself is factory/keychain.py (keychain_backend is in factory/preflight.py), and
running a program with a value on its stdin factory/programs.py. The bash stays while
scripts/start-broker.sh sources it; factory/test_secrets.py runs the bash and the Python on
the same fakes and requires the same argv, statuses and messages.

A value is bytes, and it goes only to a program's stdin (the keychain tool, powershell.exe,
`sbx exec -i` into /etc/factory-broker/secrets/ in the broker sandbox, `sbx secret set`).
It is never put in argv, the environment, a message, an exception, a log or a file on the
host. Every program is run from an argv list, never through a shell on the host.

The marker files at $FACTORY_STATE/secrets-manual/<name> (empty, never the value) say that
`factory secret set` delivered a name, so a broker start leaves it alone. They sit under
FACTORY_STATE, never mounted into a sandbox, so a compromised broker can't plant one to
block a revocation.
"""

import os

from factory import keychain, preflight, sbx
from factory.keychain import FOUND, NOT_STORED
from factory.programs import DROP, PASS, run_tool, to_stderr

# Keep in sync with scripts/lib/secrets.sh (a test compares them).
SECRET_NAMES = ("jira", "gemini", "npm-registry", "gitlab", "approvals-webhook")

SECRETS_DIR = "/etc/factory-broker/secrets"

# Run by `sh -c` inside the broker sandbox, with the name as $1 and the value on stdin:
# umask 077, write to a .tmp file and rename it into place. When sbx exec runs as root
# (an old broker image) the chowns hand the directory and file to "agent", who the gateway
# runs as; as "agent" they are skipped. Byte for byte the script of secret_deliver_file.
DELIVER_SCRIPT = (
    'umask 077 && dir="/etc/factory-broker/secrets" && mkdir -p "$dir" \\\n'
    '     && { [ "$(id -u)" != 0 ] || chown agent:agent "$dir"; } \\\n'
    '     && dest="$dir/$1" && cat > "$dest.tmp" && mv "$dest.tmp" "$dest" \\\n'
    '     && { [ "$(id -u)" != 0 ] || chown agent:agent "$dest"; }')


def secret_name_valid(name):
    """True only for exactly one of SECRET_NAMES."""
    return name in SECRET_NAMES


def keychain_unavailable_warning(ctx, service, what):
    """One warning on stderr: SERVICE couldn't be read, so WHAT is left as it is, and how
    to fix that on this machine's backend. Never a value."""
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        hint = "unlock the Keychain (security unlock-keychain) and allow access when asked"
    elif backend == "secret-tool":
        hint = ("unlock your login keyring (log in to the desktop session, or open Passwords and Keys) and check "
                "that a Secret Service such as gnome-keyring-daemon is running (try: secret-tool lookup service %s)"
                % service)
    elif backend == "wincred":
        hint = "check that Windows PowerShell runs from WSL (powershell.exe) and can reach the Credential Manager"
    else:
        hint = "check this machine's keychain"
    to_stderr(ctx, "warning: could not read %s from the keychain (locked or unavailable); leaving %s as it is. "
                     "To fix: %s, then run 'factory start' again\n" % (service, what, hint))


def github_sync_from_keychain(ctx):
    """If the keychain holds factory-github, (re)register it as the broker-scoped sbx secret
    (never global). With no entry, whatever sbx has is left alone; so it is when the keychain
    can't be read, with a warning. Never fatal."""
    status, value = keychain.lookup(ctx, "factory-github")
    if status == NOT_STORED:
        return
    if status != FOUND:
        keychain_unavailable_warning(ctx, "factory-github", "the broker's GitHub token")
        return
    argv = ["sbx", "secret", "set", "-f", "github", "--sandbox", sbx.BROKER_SANDBOX]
    if run_tool(ctx, argv, value, out=DROP, err=DROP)[0] == 0:
        ctx.say("GitHub token for %s restored from the keychain (factory-github)" % sbx.BROKER_SANDBOX)
    else:
        to_stderr(ctx, "warning: could not register the GitHub token from the keychain for %s; "
                         "run 'factory secret save github' again\n" % sbx.BROKER_SANDBOX)


def deliver_file(ctx, name, value, out=PASS):
    """Write VALUE into SECRETS_DIR/NAME in the broker sandbox, over `sbx exec -i` stdin; True
    on success. NAME must be valid (it is the file name)."""
    if not secret_name_valid(name):
        return False
    argv = ["sbx", "exec", "-i", sbx.BROKER_SANDBOX, "sh", "-c", DELIVER_SCRIPT, "_", name]
    return run_tool(ctx, argv, value, out=out)[0] == 0


def revoke_file(ctx, name, out=PASS, err=PASS):
    """Remove SECRETS_DIR/NAME in the broker sandbox; True on success."""
    if not secret_name_valid(name):
        return False
    argv = ["sbx", "exec", sbx.BROKER_SANDBOX, "rm", "-f", "%s/%s" % (SECRETS_DIR, name)]
    return run_tool(ctx, argv, out=out, err=err)[0] == 0


def manual_dir(paths):
    return os.path.join(paths.state, "secrets-manual")


def manual_names(paths):
    """The names marked as managed by `factory secret set`, sorted."""
    folder = manual_dir(paths)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names if not n.startswith(".") and os.path.exists(os.path.join(folder, n)))


def is_manual(paths, name):
    return os.path.exists(os.path.join(manual_dir(paths), name))


def mark_manual(paths, name):
    """Create NAME's marker (an empty file, never the value); True on success."""
    folder = manual_dir(paths)
    try:
        os.makedirs(folder, exist_ok=True)
        os.chmod(folder, 0o700)
        with open(os.path.join(folder, name), "wb"):
            pass
    except OSError:
        return False
    return True


def unmark_manual(paths, name):
    try:
        os.remove(os.path.join(manual_dir(paths), name))
    except FileNotFoundError:
        pass


def sync_from_keychain(ctx, name, quiet=False):
    """Deliver the keychain's factory-NAME to the broker sandbox or, if the keychain
    definitely has none, revoke the broker's file; leave it alone (one warning) if the
    keychain can't be read, and entirely (one note) if NAME is marked manual. QUIET drops
    the note and what sbx prints on stdout (bash's >/dev/null). Returns 2 if a revocation
    failed (the file may still be active), else 0, a failed delivery included."""
    service = "factory-" + name
    if is_manual(ctx.paths, name):
        if not quiet:
            ctx.say("note: %s is managed by 'factory secret set/rm'; leaving it as is" % name)
        return 0
    out = DROP if quiet else PASS
    status, value = keychain.lookup(ctx, service)
    if status == NOT_STORED:
        if not revoke_file(ctx, name, out=out):
            to_stderr(ctx, "error: could not remove the %s credential file from the broker sandbox; "
                             "it may still be active even though its keychain entry is gone\n" % name)
            return 2
        return 0
    if status != FOUND:
        keychain_unavailable_warning(ctx, service, "the broker's %s credential" % name)
        return 0
    if not deliver_file(ctx, name, value, out=out):
        to_stderr(ctx, "warning: could not deliver the %s credential to the broker sandbox "
                         "(an old broker image? see README, Upgrading, for the rebuild steps)\n" % name)
    return 0
