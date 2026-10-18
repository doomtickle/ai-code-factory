"""Credentials: `get(name)` returns a secret, or None, from an ordered list
of providers.

Built in, in their default order (PASSDESK_CREDENTIAL_PROVIDERS):

  * `proxy`: names the deployment's egress proxy injects itself (today
    `github`, injected by the sbx proxy). Lookup stops here and returns
    None: the gateway never holds these.
  * `file`: the credential's file in the secrets directory (for `jira`,
    `gemini` and `npm-registry`, the path in BROKER_JIRA_SECRET,
    BROKER_GEMINI_SECRET or BROKER_NPM_REGISTRY_SECRET), then the Docker
    secret at /run/secrets/<name>.
  * `env-file`: the file named by PASSDESK_SECRET_<NAME>_FILE (the name
    upper-cased, `-` as `_`). The variable holds a path, never the secret:
    plain environment variables leak into child processes and `docker
    inspect`, so there is no provider that reads a value from one.

A plugin (core/plugins.py) may add providers from its activate() with
`register_provider`, as approval hooks use approvals.set_hook. A plugin
provider is consulted where PASSDESK_CREDENTIAL_PROVIDERS names it, or
after every listed provider if it isn't named.

Every lookup reads fresh, so rotating a file rotates the credential without
a restart. A value loses leading and trailing whitespace (as the old reader's
`.strip()` did); an empty value counts as absent. Secret values never appear in an exception, an
audit record or a repr: errors name the credential and the provider only.

`get`'s refusals (an unusable file, a control character in the value) are
audited by default; pass `on_unusable` to route them elsewhere instead (a
read-only caller like the doctor, which must change nothing on disk).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from . import config
from .audit import audit


class _Injected:
    def __repr__(self) -> str:
        return "credentials.INJECTED"


# A provider returns this for a name something outside the gateway supplies
# (the egress proxy): get() stops looking and returns None.
INJECTED = _Injected()

Provider = Callable[[str], "str | None | _Injected"]


class CredentialError(Exception):
    """A provider failed, or the provider list is misconfigured. Never carries a secret value."""


class _Unusable(Exception):
    """A provider found something under the name it will not use (reason is safe to log)."""


# Credential and provider names: lowercase letters and digits in dash-separated
# words. No `_`, so the env-file variable name maps back to one credential; no
# `/`, `.` or NUL, so a name is always a single file name in a directory.
_NAME_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_MAX_NAME = 64
# Larger than any token or key this gateway uses; a bigger file is a mistake.
MAX_SECRET_BYTES = 64 * 1024
# C0 controls and DEL. A value with one inside it (after surrounding
# whitespace is stripped) is refused: it can't be a header value, and http.client
# would quote the whole value in the ValueError it raises.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

DOCKER_SECRETS_DIR = Path("/run/secrets")

# Names the deployment's egress proxy injects, so the gateway never holds them.
PROXY_INJECTED = frozenset({"github"})


def secrets_dir() -> Path:
    """The deployment's secrets directory (BROKER_SECRETS_DIR), read fresh on
    every call so a test (or a future reload) can repoint it without this
    module needing its own copy."""
    return Path(config.SECRETS_DIR)


def check_name(name: str) -> None:
    if not isinstance(name, str) or len(name) > _MAX_NAME or not _NAME_RE.fullmatch(name):
        raise CredentialError("invalid credential name (lowercase letters, digits and single dashes only)")


def _inside(path: str, root: Path) -> bool:
    root_real = os.path.realpath(root)
    return os.path.commonpath([path, root_real]) == root_real


def _fd_path(fd: int, fallback: str) -> str:
    """The path the kernel resolved `fd` to, from /proc/self/fd; `fallback` where /proc is
    unavailable. os.readlink's result is the kernel's own resolution of the descriptor, not a
    second filesystem walk, so it cannot be redirected by a symlink swap made after the open."""
    link = f"/proc/self/fd/{fd}"
    try:
        return os.readlink(link)
    except OSError:
        return fallback


def read_secret_file(path: Path) -> str | None:
    """The text of a credential file, or None if there is none there.

    Follows symlinks (as the gateway always has; Kubernetes secret volumes
    are symlinks), but the file it reaches must be a regular file of at most
    MAX_SECRET_BYTES holding UTF-8, outside the agent workspace; otherwise
    raises _Unusable. A path that isn't a regular file is refused before it
    is opened (so a device node is never opened), and again on the open
    descriptor; the open is non-blocking, so a FIFO swapped in between
    cannot hang the call.

    The workspace check is on paths: the resolved path before opening, then
    the open descriptor's path from /proc/self/fd, which catches a swap
    between the two (where /proc is unavailable only the first check runs).
    It cannot see the workspace reached another way: a bind mount of it at
    another path, or a hard link to a file in it, is not detected.

    Group- or world-readable files are accepted: Docker secrets are 0444 by
    default (the doctor warns about them in the secrets directory).
    """
    real = os.path.realpath(path)
    if _inside(real, config.WORKSPACE):
        raise _Unusable(f"{path} is inside the agent workspace")
    try:
        st = os.stat(real)
    except OSError:
        return None  # missing or unreadable, as before: the caller reports it as absent
    if not stat.S_ISREG(st.st_mode):
        raise _Unusable(f"{path} is not a regular file")
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Unusable(f"{path} is not a regular file")
        if _inside(_fd_path(fd, real), config.WORKSPACE):
            raise _Unusable(f"{path} is inside the agent workspace")
        data = b""
        while len(data) <= MAX_SECRET_BYTES and (chunk := os.read(fd, MAX_SECRET_BYTES + 1 - len(data))):
            data += chunk
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_SECRET_BYTES:
        raise _Unusable(f"{path} is larger than {MAX_SECRET_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unusable(f"{path} is not UTF-8 text") from None


def _unusable(name: str, provider: str, reason: str) -> None:
    """The default `on_unusable`: today's behaviour, unchanged for every
    caller that doesn't pass its own (see `get`)."""
    try:
        audit({"event": "credential_unusable", "credential": name, "provider": provider, "reason": reason})
    except OSError:
        pass  # outside a running gateway (the doctor, say) there may be no audit log


# --- built-in providers ---------------------------------------------------------------

def _proxy(name: str):
    return INJECTED if name in PROXY_INJECTED else None


def _secret_path(name: str) -> Path:
    """Where the `file` provider looks first: the configured path for the
    credentials that have one, else <secrets dir>/<name>."""
    configured = {"jira": config.JIRA_SECRET_PATH, "gemini": config.GEMINI_SECRET_PATH,
                  "npm-registry": config.NPM_REGISTRY_SECRET_PATH}
    return configured.get(name) or secrets_dir() / name


def _file(name: str, on_unusable: Callable[[str, str, str], None] = _unusable) -> str | None:
    for path in (_secret_path(name), DOCKER_SECRETS_DIR / name):
        try:
            value = read_secret_file(path)
        except _Unusable as e:
            on_unusable(name, "file", str(e))
            continue
        if value is not None and value.strip():
            return value
    return None


def env_file_var(name: str) -> str:
    return "PASSDESK_SECRET_" + name.upper().replace("-", "_") + "_FILE"


def _env_file(name: str) -> str | None:
    var = env_file_var(name)
    path = os.environ.get(var, "")
    if not path:
        return None
    if not os.path.isabs(path):
        raise _Unusable(f"{var} is not an absolute path")
    return read_secret_file(Path(path))


BUILTIN: dict[str, Provider] = {"proxy": _proxy, "file": _file, "env-file": _env_file}
_registered: dict[str, Provider] = {}


def register_provider(name: str, fn: Provider | None) -> None:
    """Add (or, with None, remove) a plugin's provider under `name`.

    `fn(credential_name)` returns the secret, None if it has none (lookup
    goes on to the next provider), or INJECTED if something outside the
    gateway supplies it (lookup stops, get() returns None). A built-in
    provider cannot be replaced.
    """
    if not isinstance(name, str) or len(name) > _MAX_NAME or not _NAME_RE.fullmatch(name):
        raise ValueError("invalid credential provider name")
    if name in BUILTIN:
        raise ValueError(f"{name!r} is a built-in credential provider and cannot be replaced")
    if fn is None:
        _registered.pop(name, None)
    else:
        _registered[name] = fn


def provider_order() -> list[str]:
    """Provider names in lookup order. Raises CredentialError for a listed name that isn't a provider."""
    order: list[str] = []
    for name in (n.strip() for n in config.CREDENTIAL_PROVIDERS.split(",")):
        if not name or name in order:
            continue
        if name not in BUILTIN and name not in _registered:
            raise CredentialError(f"unknown credential provider {name!r} in PASSDESK_CREDENTIAL_PROVIDERS")
        order.append(name)
    return order + [n for n in _registered if n not in order]


def get(name: str, on_unusable: Callable[[str, str, str], None] = _unusable) -> str | None:
    """The secret called `name` from the first provider that has one, or None.

    Raises CredentialError for an invalid name, a misconfigured provider
    list, or a provider that fails; the error never carries a value.

    `on_unusable(credential_name, provider, reason)` runs for every refusal
    along the way (default: audit it, as every caller used to). `reason`
    never carries a secret value. A caller that must change nothing on disk
    (the doctor) can pass a callback that just collects `reason` instead.
    """
    check_name(name)
    for provider in provider_order():
        fn = BUILTIN.get(provider) or _registered[provider]
        try:
            value = fn(name, on_unusable) if fn is _file else fn(name)
        except _Unusable as e:
            on_unusable(name, provider, str(e))
            continue
        except Exception:  # noqa: BLE001 - a plugin's message might hold the value
            raise CredentialError(f"credential provider {provider!r} failed to look up {name!r}") from None
        if value is INJECTED:
            return None
        if value is None:
            continue
        if not isinstance(value, str):
            raise CredentialError(f"credential provider {provider!r} returned a non-string for {name!r}")
        value = value.strip()
        if not value:
            continue
        if _CONTROL_RE.search(value):
            on_unusable(name, provider, "the value contains control characters")
            continue
        return value
    return None
