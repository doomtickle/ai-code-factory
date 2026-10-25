"""The credentials sbx cannot proxy-inject (Jira, Gemini, the private npm registry token,
the GitLab token and the approvals webhook URL): the named exception in CLAUDE.md
("Credentials"). Port of the helpers in scripts/lib/secrets.sh that `factory secret` uses
(keychain_backend is in factory/preflight.py). The bash stays while scripts/start-broker.sh
sources it; factory/test_secrets.py runs the bash and the Python on the same fakes and
requires the same argv, statuses and messages.

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
import subprocess

from factory import preflight, sbx

# Keep in sync with scripts/lib/secrets.sh (a test compares them).
SECRET_NAMES = ("jira", "gemini", "npm-registry", "gitlab", "approvals-webhook")
# What `factory secret save/forget` accept: the files above, plus "github", the
# broker-scoped sbx secret the egress proxy injects.
KEYCHAIN_NAMES = SECRET_NAMES + ("github",)

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

# Where a program's output goes: passed on to the command's own stream, dropped, or kept
# (returned, never printed: a lookup's output is the value).
PASS, DROP, KEEP = "pass", "drop", "keep"

FOUND, NOT_STORED, UNKNOWN = 0, 1, 2


def secret_name_valid(name):
    """True only for exactly one of SECRET_NAMES."""
    return name in SECRET_NAMES


def keychain_name_valid(name):
    return name in KEYCHAIN_NAMES


def run_tool(ctx, argv, data=None, out=PASS, err=PASS):
    """Run argv with DATA (bytes) on its stdin, or /dev/null, and return (status, stdout,
    stderr) as bytes (empty unless kept). The status is a shell's: 127 not found, 126 not
    runnable, 128+n killed by signal n."""
    pipe = {PASS: subprocess.PIPE, KEEP: subprocess.PIPE, DROP: subprocess.DEVNULL}
    try:
        cp = subprocess.run(list(argv), input=data, stdin=None if data is not None else subprocess.DEVNULL,
                            stdout=pipe[out], stderr=pipe[err], env=dict(ctx.environ))
    except FileNotFoundError:
        status, got_out, got_err = 127, b"", b"%s: command not found\n" % argv[0].encode()
    except OSError as e:
        status, got_out, got_err = 126, b"", ("%s: %s\n" % (argv[0], e.strerror or "cannot run")).encode()
    else:
        status = 128 - cp.returncode if cp.returncode < 0 else cp.returncode
        got_out, got_err = cp.stdout or b"", cp.stderr or b""
    if out == PASS and got_out:
        ctx.stdout.write(got_out.decode("utf-8", "replace"))
    if err == PASS and got_err:
        _to_stderr(ctx, got_err.decode("utf-8", "replace"))
    return status, (got_out if out == KEEP else b""), (got_err if err == KEEP else b"")


def _to_stderr(ctx, text):
    """Write TEXT on stderr after what is already on stdout, as bash's unbuffered echo does."""
    ctx._flush()
    ctx.stderr.write(text)


def _strip(value):
    """What a shell's "$(...)" keeps: the output minus every trailing newline."""
    return value.rstrip(b"\n")


def wincred_script(op, service):
    """The Windows PowerShell script for one Credential Manager operation (lookup, store,
    delete) on SERVICE, or None if OP or SERVICE isn't one of ours. SERVICE is embedded in
    the script, so it must be "factory-" and a valid keychain name, nothing else."""
    if not service.startswith("factory-") or not keychain_name_valid(service[len("factory-"):]):
        return None
    if op == "lookup":
        body = ("try { $c = $v.Retrieve('%s', 'factory') } catch { exit 0 }\n"
                "$c.RetrievePassword(); [Console]::Out.Write($c.Password)" % service)
    elif op == "store":
        body = ("[Console]::InputEncoding = New-Object Text.UTF8Encoding $false\n"
                "$val = [Console]::In.ReadToEnd(); if ($val.Length -eq 0) { exit 3 }\n"
                "try { $v.Remove($v.Retrieve('%s', 'factory')) } catch {}\n"
                "$v.Add((New-Object Windows.Security.Credentials.PasswordCredential('%s', 'factory', $val)))"
                % (service, service))
    elif op == "delete":
        body = "try { $v.Remove($v.Retrieve('%s', 'factory')) } catch {}" % service
    else:
        return None
    return ("# factory-keychain op=%s\n"
            "$ErrorActionPreference = 'Stop'\n"
            "[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false\n"
            "[void][Windows.Security.Credentials.PasswordVault, Windows.Security.Credentials, "
            "ContentType = WindowsRuntime]\n"
            "$v = New-Object Windows.Security.Credentials.PasswordVault\n"
            "%s" % (op, body))


def wincred_run(ctx, op, service, data=None, out=PASS, err=PASS):
    """Run one Credential Manager operation through Windows PowerShell, the value only on
    stdin (DATA) or stdout. (status, stdout, stderr) as run_tool; status 1 without running
    anything for a service or operation that isn't ours, or with no PowerShell."""
    script = wincred_script(op, service)
    ps = preflight.wincred_powershell(ctx)
    if script is None or not ps:
        return 1, b"", b""
    return run_tool(ctx, [ps, "-NoProfile", "-NonInteractive", "-Command", script], data, out, err)


def keychain_search_empty(ctx, service):
    """True only if `secret-tool search` exits 0, prints nothing on stderr and lists no item
    for SERVICE. Its listing can hold "secret = <value>": only the item lines are counted."""
    status, out, err = run_tool(ctx, ["secret-tool", "search", "service", service], out=KEEP, err=KEEP)
    if err:
        return False
    count = sum(1 for line in out.split(b"\n") if line.startswith(b"["))
    return status == 0 and count == 0


def keychain_lookup(ctx, service):
    """(status, value): FOUND with a non-empty value; NOT_STORED when the keychain
    definitely has none (or there is no keychain here); UNKNOWN when it can't be told
    (locked, unavailable, the prompt denied, anything else), so a locked keychain is never
    taken for a cleared entry. Nothing the backend prints is passed on."""
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        status, value, _ = run_tool(ctx, ["security", "find-generic-password", "-s", service, "-w"], out=KEEP, err=DROP)
        if status == 44:
            return NOT_STORED, b""
    elif backend == "secret-tool":
        status, value, err = run_tool(ctx, ["secret-tool", "lookup", "service", service], out=KEEP, err=KEEP)
        # A locked item whose unlock prompt was dismissed also comes back as a silent exit
        # 1: list it without unlocking (or prompting) to be sure.
        if status == 1 and not _strip(err) and not _strip(value):
            return (NOT_STORED if keychain_search_empty(ctx, service) else UNKNOWN), b""
    elif backend == "wincred":
        # The lookup script exits 0 with no output when there is no entry.
        status, value, _ = wincred_run(ctx, "lookup", service, out=KEEP, err=DROP)
        if status == 0 and not _strip(value):
            return NOT_STORED, b""
    else:
        return NOT_STORED, b""
    value = _strip(value)
    if status == 0 and value:
        return FOUND, value
    return UNKNOWN, b""


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
    _to_stderr(ctx, "warning: could not read %s from the keychain (locked or unavailable); leaving %s as it is. "
                     "To fix: %s, then run 'factory start' again\n" % (service, what, hint))


def keychain_has(ctx, service):
    """FOUND, NOT_STORED or UNKNOWN, as keychain_lookup. On macOS this checks existence only,
    without reading the value (no Keychain prompt)."""
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        status, _, _ = run_tool(ctx, ["security", "find-generic-password", "-s", service], out=DROP, err=DROP)
        return {0: FOUND, 44: NOT_STORED}.get(status, UNKNOWN)
    if backend == "none":
        return NOT_STORED
    return keychain_lookup(ctx, service)[0]


def keychain_store(ctx, service, value):
    """Save VALUE as SERVICE; True on success. Not for macOS: `security` has no stdin form,
    so `factory secret save` lets it prompt for the value itself rather than pass it in argv."""
    backend = preflight.keychain_backend(ctx)
    if backend == "secret-tool":
        argv = ["secret-tool", "store", "--label", "AI Code Factory: %s" % service, "service", service]
        return run_tool(ctx, argv, value)[0] == 0
    if backend == "wincred":
        return wincred_run(ctx, "store", service, value, out=DROP)[0] == 0
    return False


def keychain_delete(ctx, service):
    """Remove SERVICE from the keychain (nothing to report if it is absent, or on failure:
    the caller checks with keychain_has)."""
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        run_tool(ctx, ["security", "delete-generic-password", "-s", service], out=DROP, err=DROP)
    elif backend == "secret-tool":
        run_tool(ctx, ["secret-tool", "clear", "service", service], err=DROP)
    elif backend == "wincred":
        wincred_run(ctx, "delete", service, out=DROP, err=DROP)


def github_sync_from_keychain(ctx):
    """If the keychain holds factory-github, (re)register it as the broker-scoped sbx secret
    (never global). With no entry, whatever sbx has is left alone; so it is when the keychain
    can't be read, with a warning. Never fatal."""
    status, value = keychain_lookup(ctx, "factory-github")
    if status == NOT_STORED:
        return
    if status != FOUND:
        keychain_unavailable_warning(ctx, "factory-github", "the broker's GitHub token")
        return
    argv = ["sbx", "secret", "set", "-f", "github", "--sandbox", sbx.BROKER_SANDBOX]
    if run_tool(ctx, argv, value, out=DROP, err=DROP)[0] == 0:
        ctx.say("GitHub token for %s restored from the keychain (factory-github)" % sbx.BROKER_SANDBOX)
    else:
        _to_stderr(ctx, "warning: could not register the GitHub token from the keychain for %s; "
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
    status, value = keychain_lookup(ctx, service)
    if status == NOT_STORED:
        if not revoke_file(ctx, name, out=out):
            _to_stderr(ctx, "error: could not remove the %s credential file from the broker sandbox; "
                             "it may still be active even though its keychain entry is gone\n" % name)
            return 2
        return 0
    if status != FOUND:
        keychain_unavailable_warning(ctx, service, "the broker's %s credential" % name)
        return 0
    if not deliver_file(ctx, name, value, out=out):
        _to_stderr(ctx, "warning: could not deliver the %s credential to the broker sandbox "
                         "(an old broker image? see README, Upgrading, for the rebuild steps)\n" % name)
    return 0
