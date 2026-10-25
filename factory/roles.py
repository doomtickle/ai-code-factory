"""Roles: roles/<name>/role.conf, this machine's overrides of it, and which
optional roles are switched on. Port of scripts/lib/roles.sh: the lookups
(list_all_roles, list_roles, role_get, role_lanes, role_optional, ...) and the
validation (validate_role, validate_role_overrides, validate_enabled_roles,
role_lane_names, coordinator_broker_tools). The bash copies stay until the
commands that still call them are ported; tests run both on the same fixtures.

Every config file read here is data: parsed line by line, never sourced or run.
A failed check raises FactoryError with the message bash's `die` prints.
"""

import os
import re

from factory.conf import match_conf_line, read_conf, read_text
from factory.context import FactoryError

# Keys a local override (FACTORY_CONFIG/roles/<role>.conf) may set: tuning only,
# nothing that moves the security boundary. Keep in sync with ROLE_OVERRIDE_KEYS
# in scripts/lib/roles.sh (a test compares them).
ROLE_OVERRIDE_KEYS = ("MODEL", "MODELS", "EFFORT", "LANES", "MAX_SESSION_TOKENS", "MAX_TURNS",
                      "MAX_JOB_SECONDS")

# Keep in sync with RESERVED_ROLE_NAMES in scripts/lib/roles.sh (a test compares them).
RESERVED_ROLE_NAMES = ("broker", "coordinator", "dispatch")

# Keep in sync with KNOWN_TEMPLATES in scripts/lib/roles.sh (a test compares them).
KNOWN_TEMPLATES = ("developer", "reviewer")

_SPACE = " \t\n\v\f\r"
_EFFORTS = ("low", "medium", "high", "xhigh")
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]+")
_ROLE_NAME = re.compile(r"[a-z][a-z0-9-]*")
_WORD = re.compile(r"[^ \t\n]+")  # bash's default word splitting, not str.split()


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
    are skipped here (validate_enabled_roles reports them)."""
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
    Not validated here: validate_role rejects values outside 1..8."""
    return role_get(paths, role, "LANES") or "1"


def _words(value):
    """The words of a config value, split as bash splits on its default IFS."""
    return _WORD.findall(value)


def _in_list(word, listing):
    """True if `word` is one of the space-separated words of `listing`. Bash's in_list
    also matches several adjacent words ("low medium" is in "low medium high"); this
    does not, which only makes the check stricter."""
    return word != "" and word in listing.split(" ")


def known_broker_tools(paths):
    """The broker tool names in broker/tools.txt, joined by spaces: what bash calls
    KNOWN_BROKER_TOOLS (comment lines and blank lines dropped, nothing else trimmed)."""
    path = os.path.join(paths.root, "broker", "tools.txt")
    if not os.path.isfile(path):
        raise FactoryError("%s is missing: broker/ is incomplete in this checkout "
                           "(restore it with 'git -C %s checkout HEAD -- broker')" % (path, paths.root))
    lines = [line for line in read_text(path).split("\n")
             if not line.startswith("#") and line.strip(" \t\v\f\r") != ""]
    return " ".join(lines)


def validate_enabled_roles(paths):
    """Raise unless enabled-roles holds only blank lines, comments and names of roles in
    roles/ (a typo would silently enable nothing)."""
    path = enabled_roles_file(paths)
    if not os.path.exists(path):
        return
    if not os.path.isfile(path):
        raise FactoryError("%s: not a regular file" % path)
    for line in read_text(path).split("\n"):
        name = line.strip(_SPACE)
        if name == "" or name.startswith("#"):
            continue
        if _ROLE_NAME.fullmatch(name) is None:
            raise FactoryError("%s: not a role name (one per line, e.g. researcher): %s" % (path, line))
        if not os.path.isfile(role_conf_path(paths, name)):
            raise FactoryError("%s: no role named '%s' in %s (see: factory roles)"
                               % (path, name, os.path.join(paths.root, "roles")))


def validate_role_override(paths, role):
    """Raise unless `role`'s local override (if any) holds only blank lines, comments and
    KEY="value" lines for ROLE_OVERRIDE_KEYS."""
    path = role_override_file(paths, role)
    if not os.path.exists(path):
        return
    if not os.path.isfile(path):
        raise FactoryError("%s: not a regular file" % path)
    try:
        text = read_text(path)
    except OSError as e:
        raise FactoryError("%s: can't be read (%s)" % (path, e.strerror or e))
    for line in text.split("\n"):
        trimmed = line.lstrip(_SPACE)
        if trimmed == "" or trimmed.startswith("#"):
            continue
        pair = match_conf_line(line)  # the line itself, not the trimmed one: no indentation
        if pair is None:
            raise FactoryError("%s: not a KEY=\"value\" line: %s" % (path, line))
        if pair[0] not in ROLE_OVERRIDE_KEYS:
            raise FactoryError("%s: %s can't be set in a local override (allowed: %s); change it in roles/%s/role.conf"
                               % (path, pair[0], " ".join(ROLE_OVERRIDE_KEYS), role))


def validate_role_overrides(paths):
    """Raise if enabled-roles is malformed, or an override names no role in roles/."""
    validate_enabled_roles(paths)
    folder = os.path.join(paths.config, "roles")
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in sorted((n for n in names if n.endswith(".conf") and not n.startswith(".")),
                       key=lambda n: n.encode("utf-8", "surrogateescape")):
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            continue
        role = name[:-len(".conf")]
        if not os.path.isfile(role_conf_path(paths, role)):
            raise FactoryError("%s: no role named '%s' in %s (an override is named <role>.conf; see: factory roles)"
                               % (path, role, os.path.join(paths.root, "roles")))


def coordinator_broker_tools(paths):
    """BROKER_TOOLS in kits/coordinator/broker.conf ("" if the file is absent). The
    Coordinator calls broker tools directly, with no role in between, so its grant is
    read like a role.conf's but lives outside roles/."""
    path = os.path.join(paths.root, "kits", "coordinator", "broker.conf")
    return read_conf(path).get("BROKER_TOOLS", "") if os.path.isfile(path) else ""


def _lane_count(value):
    """How many lanes bash's role_lane_names counts up to for `value`: an integer with optional
    sign and surrounding spaces or tabs (only those: \\v, \\f and \\r are trimmed by some bash
    versions' `[ -le ]` and not others, so bash spells the pattern out); anything else (and
    anything past a 64-bit integer) leaves the role just its first lane."""
    m = re.fullmatch(r"[ \t]*([+-]?[0-9]+)[ \t]*", value)
    if m is None or abs(int(m.group(1))) >= 2 ** 63:
        return 1
    return int(m.group(1))


def role_lane_names(paths, role):
    """`role`'s lane names: lane 1 is `role` itself, lane k >= 2 is role-lane<k>. Each lane has
    its own session and transcript; all lanes share the role's one sandbox. LANES is not
    checked here (validate_role bounds it to 1..8), so validate before listing."""
    return [role] + ["%s-lane%d" % (role, k) for k in range(2, _lane_count(role_lanes(paths, role)) + 1)]


def validate_role(paths, role):
    """Raise unless `role` is a well-formed role: name, kit, instructions, local override and
    every value of the merged role.conf (the override is layered on top; see role_get)."""
    known_tools = known_broker_tools(paths)
    if not valid_role_name(role):
        raise FactoryError("invalid role name '%s' (lowercase letters, digits, dashes; not ending in -lane<N>; "
                           "not: %s)" % (role, " ".join(RESERVED_ROLE_NAMES)))
    folder = os.path.join(paths.root, "roles", role)
    if not os.path.isfile(os.path.join(folder, "kit", "spec.yaml")):
        raise FactoryError("role %s: missing roles/%s/kit/spec.yaml" % (role, role))
    try:
        has_instructions = os.path.getsize(os.path.join(folder, "instructions.md")) > 0
    except OSError:
        has_instructions = False
    if not has_instructions:
        raise FactoryError("role %s: missing or empty roles/%s/instructions.md" % (role, role))
    validate_role_override(paths, role)

    def get(key):
        return role_get(paths, role, key)

    def note(key):
        # " (set in <override file>)" when the local override supplies the value
        path = role_override_file(paths, role)
        if key in ROLE_OVERRIDE_KEYS and os.path.isfile(path) and key in read_conf(path):
            return " (set in %s)" % path
        return ""

    optional = read_conf(role_conf_path(paths, role)).get("OPTIONAL", "")
    if optional != "" and not _in_list(optional, "0 1"):
        raise FactoryError("role %s: OPTIONAL must be 1 (off unless enabled-roles names it) or empty" % role)
    if not _in_list(get("TEMPLATE"), " ".join(KNOWN_TEMPLATES)):
        raise FactoryError("role %s: TEMPLATE must be one of: %s" % (role, " ".join(KNOWN_TEMPLATES)))
    if not _in_list(get("WORKSPACE"), "rw ro"):
        raise FactoryError("role %s: WORKSPACE must be rw or ro" % role)
    for tool in _words(get("BROKER_TOOLS")):
        if not _in_list(tool, known_tools):
            raise FactoryError("role %s: unknown broker tool '%s' (known: %s)" % (role, tool, known_tools))
    model = get("MODEL")
    if model != "" and _MODEL_NAME.fullmatch(model) is None:
        raise FactoryError("role %s: MODEL must contain only letters, digits, dots, dashes, underscores%s"
                           % (role, note("MODEL")))
    for model in _words(get("MODELS")):
        if _MODEL_NAME.fullmatch(model) is None:
            raise FactoryError("role %s: MODELS entries must contain only letters, digits, dots, dashes, "
                               "underscores%s" % (role, note("MODELS")))
    effort = get("EFFORT")
    if effort != "" and not _in_list(effort, " ".join(_EFFORTS)):
        raise FactoryError("role %s: EFFORT must be low, medium, high or xhigh (or empty)%s" % (role, note("EFFORT")))
    lanes = get("LANES")
    if lanes != "" and re.fullmatch(r"[1-8]", lanes) is None:
        raise FactoryError("role %s: LANES must be a whole number from 1 to 8 (or empty)%s" % (role, note("LANES")))
    for key, what in (("MAX_SESSION_TOKENS", "tokens"), ("MAX_TURNS", "turns"), ("MAX_JOB_SECONDS", "seconds")):
        value = get(key)
        if value != "" and re.fullmatch(r"[0-9]+", value) is None:
            raise FactoryError("role %s: %s must be a whole number of %s (or empty)%s" % (role, key, what, note(key)))
