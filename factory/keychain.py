"""Asking the host's keychain whether a credential is stored: port of keychain_has,
keychain_lookup and keychain_search_empty (scripts/lib/secrets.sh). The bash copies stay
until `secret` and `start` are ported; factory/test_keychain.py runs both on the same
stubs. Read-only, and the value is only ever tested for being non-empty: it is never
returned, printed or passed on.
"""

from factory import preflight

# The names `factory secret save` accepts (FACTORY_KEYCHAIN_NAMES in secrets.sh; a test compares).
KEYCHAIN_NAMES = ("jira", "gemini", "npm-registry", "gitlab", "approvals-webhook", "github")

# What keychain_has returns: the shell statuses of the bash function.
FOUND, NOT_STORED, UNDETERMINED = 0, 1, 2

_WINCRED_HEAD = """# factory-keychain op=lookup
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
[void][Windows.Security.Credentials.PasswordVault, Windows.Security.Credentials, ContentType = WindowsRuntime]
$v = New-Object Windows.Security.Credentials.PasswordVault
"""
_WINCRED_LOOKUP = """try { $c = $v.Retrieve('%s', 'factory') } catch { exit 0 }
$c.RetrievePassword(); [Console]::Out.Write($c.Password)"""


def _search_empty(ctx, service):
    """True only if `secret-tool search` exits 0, prints nothing on stderr and lists no item
    for SERVICE; a search that fails in any way is not taken as "empty"."""
    got = ctx.capture(["secret-tool", "search", "service", service])
    if got.err:
        return False
    return got.status == 0 and not any(line.startswith("[") for line in got.out.split("\n"))


def _lookup(ctx, backend, service):
    if backend == "secret-tool":
        got = ctx.capture(["secret-tool", "lookup", "service", service])
        value = got.out.rstrip("\n")
        # A locked item whose unlock prompt was dismissed also comes back as a silent
        # exit 1: list it without unlocking (or prompting) to be sure.
        if got.status == 1 and got.err.rstrip("\n") == "" and value == "":
            return NOT_STORED if _search_empty(ctx, service) else UNDETERMINED
        status = got.status
    elif backend == "wincred":
        if not service.startswith("factory-") or service[len("factory-"):] not in KEYCHAIN_NAMES:
            return UNDETERMINED
        powershell = preflight.wincred_powershell(ctx)
        if not powershell:
            return UNDETERMINED
        got = ctx.capture([powershell, "-NoProfile", "-NonInteractive", "-Command",
                           _WINCRED_HEAD + _WINCRED_LOOKUP % service])
        value = got.out.rstrip("\n")
        status = got.status
        # The lookup script exits 0 with no output when there is no entry.
        if status == 0 and value == "":
            return NOT_STORED
    else:
        return NOT_STORED
    return FOUND if status == 0 and value != "" else UNDETERMINED


def has(ctx, service):
    """FOUND if the keychain holds an entry for SERVICE, NOT_STORED if it definitely
    doesn't, UNDETERMINED if that can't be told (locked or unavailable). On macOS this checks
    existence only, without reading the value (no Keychain prompt)."""
    backend = preflight.keychain_backend(ctx)
    if backend == "macos":
        status = ctx.capture(["security", "find-generic-password", "-s", service]).status
        return FOUND if status == 0 else NOT_STORED if status == 44 else UNDETERMINED
    if backend == "none":
        return NOT_STORED
    return _lookup(ctx, backend, service)
