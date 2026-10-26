"""The host's keychain, where `factory secret save` keeps the factory's credentials as
factory-<name>: macOS Keychain, Linux secret-tool, or the Windows Credential Manager on WSL2
(preflight.keychain_backend picks one). Port of keychain_lookup, keychain_has,
keychain_search_empty, keychain_store, keychain_delete and the PowerShell scripts of
wincred_run (scripts/lib/secrets.sh). That bash is gone; factory/test_keychain.py and
factory/test_secrets.py check the answers it gave on the same stubs.

A value is bytes. It goes into the keychain only on a program's stdin and comes out only
from `lookup` (for factory/secrets.py to pass on, again on stdin); `has` only tests that it
is non-empty. It is never put in argv, a message or a file on the host.
"""

from factory import preflight
from factory.programs import DROP, KEEP, PASS, run_tool

# The names `factory secret save` accepts (the bash FACTORY_KEYCHAIN_NAMES, in this order):
# the broker credential files (secrets.SECRET_NAMES) and "github", the broker-scoped sbx secret.
KEYCHAIN_NAMES = ("jira", "gemini", "npm-registry", "gitlab", "approvals-webhook", "github")

# What keychain_has and keychain_lookup return: the shell statuses of the bash functions.
FOUND, NOT_STORED, UNDETERMINED = 0, 1, 2

_WINCRED_HEAD = """# factory-keychain op=%s
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
[void][Windows.Security.Credentials.PasswordVault, Windows.Security.Credentials, ContentType = WindowsRuntime]
$v = New-Object Windows.Security.Credentials.PasswordVault
"""
_WINCRED_BODY = {
    "lookup": """try { $c = $v.Retrieve('%(s)s', 'factory') } catch { exit 0 }
$c.RetrievePassword(); [Console]::Out.Write($c.Password)""",
    "store": """[Console]::InputEncoding = New-Object Text.UTF8Encoding $false
$val = [Console]::In.ReadToEnd(); if ($val.Length -eq 0) { exit 3 }
try { $v.Remove($v.Retrieve('%(s)s', 'factory')) } catch {}
$v.Add((New-Object Windows.Security.Credentials.PasswordCredential('%(s)s', 'factory', $val)))""",
    "delete": """try { $v.Remove($v.Retrieve('%(s)s', 'factory')) } catch {}""",
}


def name_valid(name):
    """True only for exactly one of KEYCHAIN_NAMES."""
    return name in KEYCHAIN_NAMES


def wincred_script(op, service):
    """The Windows PowerShell script for one Credential Manager operation (lookup, store,
    delete) on SERVICE, or None if OP or SERVICE isn't one of ours. SERVICE is embedded in
    the script, so it must be "factory-" and a valid keychain name, nothing else."""
    if op not in _WINCRED_BODY or not service.startswith("factory-") or not name_valid(service[len("factory-"):]):
        return None
    return _WINCRED_HEAD % op + _WINCRED_BODY[op] % {"s": service}


def wincred_run(ctx, op, service, data=None, out=PASS, err=PASS):
    """Run one Credential Manager operation through Windows PowerShell, the value only on
    stdin (DATA) or stdout. (status, stdout, stderr) as run_tool; status 1 without running
    anything for a service or operation that isn't ours, or with no PowerShell."""
    script = wincred_script(op, service)
    ps = preflight.wincred_powershell(ctx)
    if script is None or not ps:
        return 1, b"", b""
    return run_tool(ctx, [ps, "-NoProfile", "-NonInteractive", "-Command", script], data, out, err)


def _search_empty(ctx, service):
    """True only if `secret-tool search` exits 0, prints nothing on stderr and lists no item
    for SERVICE; a search that fails in any way is not taken as "empty". Its listing can
    hold "secret = <value>": only the item lines are counted."""
    status, out, err = run_tool(ctx, ["secret-tool", "search", "service", service], out=KEEP, err=KEEP)
    if err:
        return False
    return status == 0 and not any(line.startswith(b"[") for line in out.split(b"\n"))


def lookup(ctx, service):
    """(status, value): FOUND with a non-empty value (minus every trailing newline, as a
    shell's "$(...)"); NOT_STORED when the keychain definitely has none (or there is no
    keychain here); UNDETERMINED when it can't be told (locked, unavailable, the prompt
    denied, anything else), so a locked keychain is never taken for a cleared entry. Nothing
    the backend prints is passed on."""
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        status, value, _ = run_tool(ctx, ["security", "find-generic-password", "-s", service, "-w"], out=KEEP, err=DROP)
        if status == 44:
            return NOT_STORED, b""
    elif backend == "secret-tool":
        status, value, err = run_tool(ctx, ["secret-tool", "lookup", "service", service], out=KEEP, err=KEEP)
        # A locked item whose unlock prompt was dismissed also comes back as a silent exit
        # 1: list it without unlocking (or prompting) to be sure.
        if status == 1 and not err.rstrip(b"\n") and not value.rstrip(b"\n"):
            return (NOT_STORED if _search_empty(ctx, service) else UNDETERMINED), b""
    elif backend == "wincred":
        # The lookup script exits 0 with no output when there is no entry.
        status, value, _ = wincred_run(ctx, "lookup", service, out=KEEP, err=DROP)
        if status == 0 and not value.rstrip(b"\n"):
            return NOT_STORED, b""
    else:
        return NOT_STORED, b""
    value = value.rstrip(b"\n")
    if status == 0 and value:
        return FOUND, value
    return UNDETERMINED, b""


def has(ctx, service):
    """FOUND, NOT_STORED or UNDETERMINED, as lookup. On macOS this checks existence only,
    without reading the value (no Keychain prompt)."""
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        status, _, _ = run_tool(ctx, ["security", "find-generic-password", "-s", service], out=DROP, err=DROP)
        return {0: FOUND, 44: NOT_STORED}.get(status, UNDETERMINED)
    if backend == "none":
        return NOT_STORED
    return lookup(ctx, service)[0]


def store(ctx, service, value):
    """Save VALUE as SERVICE; True on success. Not for macOS: `security` has no stdin form,
    so `factory secret save` lets it prompt for the value itself rather than pass it in argv."""
    backend = preflight.keychain_backend(ctx)
    if backend == "secret-tool":
        argv = ["secret-tool", "store", "--label", "AI Code Factory: %s" % service, "service", service]
        return run_tool(ctx, argv, value)[0] == 0
    if backend == "wincred":
        return wincred_run(ctx, "store", service, value, out=DROP)[0] == 0
    return False


def delete(ctx, service):
    """Remove SERVICE from the keychain (nothing to report if it is absent, or on failure:
    the caller checks with has)."""
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        run_tool(ctx, ["security", "delete-generic-password", "-s", service], out=DROP, err=DROP)
    elif backend == "secret-tool":
        run_tool(ctx, ["secret-tool", "clear", "service", service], err=DROP)
    elif backend == "wincred":
        wincred_run(ctx, "delete", service, out=DROP, err=DROP)
