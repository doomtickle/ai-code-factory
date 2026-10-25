"""Roles: roles/<name>/role.conf, this machine's overrides of it, and which
optional roles are switched on. Port of the lookups in scripts/lib/roles.sh
(list_all_roles, list_roles, role_get, role_lanes, role_optional, ...); the
validation stays in bash until `factory start` is ported.
"""

import os
import re

from factory.conf import read_conf, read_text

# Keys a local override (FACTORY_CONFIG/roles/<role>.conf) may set: tuning only,
# nothing that moves the security boundary. Keep in sync with ROLE_OVERRIDE_KEYS
# in scripts/lib/roles.sh (a test compares them).
ROLE_OVERRIDE_KEYS = ("MODEL", "MODELS", "EFFORT", "LANES", "MAX_SESSION_TOKENS", "MAX_TURNS",
                      "MAX_JOB_SECONDS")

# Keep in sync with RESERVED_ROLE_NAMES in scripts/lib/roles.sh (a test compares them).
RESERVED_ROLE_NAMES = ("broker", "coordinator", "dispatch")

_SPACE = " \t\n\v\f\r"


def valid_role_name(name):
    """Lowercase letters, digits and dashes, 2 to 31 characters, starting with a letter;
    not reserved, and not ending in -lane<N>: that is how a role's extra lanes are
    named, so such a role would collide with another's lane."""
    return (re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name) is not None and name not in RESERVED_ROLE_NAMES
            and re.search(r"-lane[0-9]+$", name) is None)


def role_conf_path(paths, role):
    return os.path.join(paths.root, "roles", role, "role.conf")


def role_override_file(paths, role):
    return os.path.join(paths.config, "roles", role + ".conf")


def enabled_roles_file(paths):
    return os.path.join(paths.config, "enabled-roles")


def parse_enabled_roles(text):
    """The names in an enabled-roles file's text: one per line, surrounding
    whitespace ignored; lines that are not plain role names (comments, typos)
    are skipped here (bash's validate_enabled_roles reports them)."""
    names = []
    for line in text.split("\n"):
        name = line.strip(_SPACE)
        if name and name[0] in "abcdefghijklmnopqrstuvwxyz" and all(
                c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
            names.append(name)
    return names


def enabled_roles_listed(paths):
    path = enabled_roles_file(paths)
    return parse_enabled_roles(read_text(path)) if os.path.isfile(path) else []


def list_all_roles(paths):
    """Every role in roles/ (a folder with a role.conf), enabled or not, in byte order."""
    base = os.path.join(paths.root, "roles")
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return sorted((n for n in names if not n.startswith(".")
                   and os.path.isfile(os.path.join(base, n, "role.conf"))),
                  key=lambda n: n.encode("utf-8", "surrogateescape"))


def role_optional(paths, role):
    """True if the committed role.conf sets OPTIONAL="1"."""
    return read_conf(role_conf_path(paths, role)).get("OPTIONAL") == "1"


def role_listed_enabled(paths, role):
    return role in enabled_roles_listed(paths)


def list_roles(paths):
    """The roles this machine runs: all but optional ones enabled-roles doesn't name."""
    listed = set(enabled_roles_listed(paths))
    return [r for r in list_all_roles(paths) if not role_optional(paths, r) or r in listed]


def role_get(paths, role, key):
    """The value of KEY for `role`: role.conf, with the local override on top for
    the keys in ROLE_OVERRIDE_KEYS. "" if neither sets it."""
    if key in ROLE_OVERRIDE_KEYS:
        path = role_override_file(paths, role)
        override = read_conf(path) if os.path.isfile(path) else {}
        if key in override:
            return override[key]
    return read_conf(role_conf_path(paths, role)).get(key, "")


def role_lanes(paths, role):
    """How many jobs `role` may run at once as written (LANES), "1" when empty.
    Not validated: bash's validate_role rejects values outside 1..8."""
    return role_get(paths, role, "LANES") or "1"
