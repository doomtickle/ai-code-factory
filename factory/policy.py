"""Reading policy.toml the way the bash checks do: policy_value, policy_string,
gitlab_needs_token, gitlab_mr_unheld, jira_cloud_id_set, any_jira_tool_granted and
npm_private_registry_needs_token (scripts/lib/common.sh and bin/factory). Not a TOML parser:
a line scan good enough for the values these checks read. The bash copies stay until `start`
and `setup` are ported; factory/test_policy.py runs both on the same fixtures. Also the lane
git identity rules (policy_toml_string, lane_identity_problem), which only start-dispatch.sh used:
their bash is gone.
"""

import os
import re

from factory import roles
from factory.conf import read_text

_SPACE = " \t\n\v\f\r"


def _scan(text, section, key):
    """The raw value of KEY in [SECTION] in one file's text, or None. An array spread over
    several lines comes back joined on one line (up to its closing "]", comments dropped),
    so a check never mistakes `tools = [` for an empty list."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    header = "[%s]" % section
    key_line = re.compile("[%s]*%s[%s]*=" % (_SPACE, re.escape(key), _SPACE))
    inside = more = False
    joined = ""
    for line in lines:
        if more:
            line = re.sub("#.*", "", line, count=1)
            joined += " " + line
            if "]" in line:
                return joined
            continue
        if line.startswith(header):
            inside = True
            continue
        if line.startswith("["):
            inside = False
        if inside and key_line.match(line):
            line = re.sub("^[^=]*=[%s]*" % _SPACE, "", line, count=1)
            if line[:1] == "[" and "]" not in line:
                joined = re.sub("#.*", "", line, count=1)
                more = True
                continue
            return line
    return None


def policy_value(paths, section, key):
    """The raw value of KEY = ... in [SECTION] from this machine's overlay
    (FACTORY_CONFIG/policy.toml) if it sets KEY, else from broker/policy.toml: the same
    precedence as the broker's load_policy(). None if neither sets it."""
    for path in (os.path.join(paths.config, "policy.toml"), os.path.join(paths.root, "broker", "policy.toml")):
        if os.path.isfile(path):
            value = _scan(read_text(path), section, key)
            if value is not None:
                return value
    return None


def policy_string(paths, section, key):
    """The text between the first pair of double quotes of KEY in [SECTION] ("" if unset
    or not a quoted string)."""
    parts = (policy_value(paths, section, key) or "").split('"')
    return parts[1] if len(parts) > 1 else ""


def jira_cloud_id_set(paths):
    """True if [jira].cloud_id is set to something other than empty or the all-zero
    placeholder UUID from config.example/."""
    return policy_string(paths, "jira", "cloud_id").translate({ord("0"): None, ord("-"): None}) != ""


def any_jira_tool_granted(paths):
    """True if the Coordinator or any role is granted a jira_* broker tool. Other broker
    tools (web_fetch, npm_fetch, ...) need no Jira token."""
    if " jira_" in " %s " % roles.coordinator_broker_tools(paths):
        return True
    return any(" jira_" in " %s " % roles.role_get(paths, r, "BROKER_TOOLS") for r in roles.list_roles(paths))


def npm_private_registry_needs_token(paths):
    """True if a role is granted npm_fetch and the policy names a private npm registry
    (which needs the keychain credential)."""
    granted = any(" npm_fetch " in " %s " % roles.role_get(paths, r, "BROKER_TOOLS") for r in roles.list_roles(paths))
    return granted and policy_string(paths, "packages", "npm_private_registry") != ""


def gitlab_needs_token(paths):
    """True if the policy sets [gitlab].url (a non-empty TOML string): the git tools and
    gitlab_create_mr then need the gitlab credential (a file, as sbx can't inject it)."""
    value = policy_value(paths, "gitlab", "url") or ""
    return re.match("[%s]*(\"[^\"]+\"|'[^']+')" % _SPACE, value) is not None


def gitlab_mr_unheld(paths):
    """Why GitLab merge requests would open without approval although other writes are
    held, or None: gitlab_create_mr is granted and [gitlab].url set, but the local
    [approvals].tools list leaves it out. None too when there is no list or it is empty:
    the gateway holds nothing until the list names a tool."""
    granted = any(" gitlab_create_mr " in " %s " % roles.role_get(paths, r, "BROKER_TOOLS")
                  for r in roles.list_roles(paths))
    if not (granted and gitlab_needs_token(paths)):
        return None
    tools = policy_value(paths, "approvals", "tools")
    if not tools or '"gitlab_create_mr"' in tools:
        return None
    if not tools.translate({ord(c): None for c in "[] \t"}):
        return None
    return '[approvals].tools is set without "gitlab_create_mr"'


_BASIC_STRING = re.compile("\"([^\"\\\\]*)\"[%s]*(#.*)?" % _SPACE)
_LITERAL_STRING = re.compile("'([^']*)'[%s]*(#.*)?" % _SPACE)


def policy_toml_string(paths, section, key):
    """The text of a one-line TOML string value of KEY in [SECTION] (policy_value's precedence):
    "basic" without escapes, or 'literal'. None when KEY is unset; ValueError when it is set to
    anything else (an escape, a number, an unclosed quote)."""
    raw = policy_value(paths, section, key)
    if not raw:
        return None
    m = _BASIC_STRING.fullmatch(raw) or _LITERAL_STRING.fullmatch(raw)
    if m is None:
        raise ValueError(raw)
    return m.group(1)


# Bytes, as bash counts them under LC_ALL=C: a control character is 0-31 or 127, a space is
# one of " \t\n\v\f\r".
_IDENTITY_BAD = re.compile(rb"[\x00-\x1f\x7f<>]")
_IDENTITY_EMAIL = re.compile(rb"[^-@ \t\n\v\f\r\x00-\x1f\x7f<>][^@ \t\n\v\f\r\x00-\x1f\x7f<>]*@[^@ \t\n\v\f\r\x00-\x1f\x7f<>]+")


def lane_identity_problem(name, email):
    """Why NAME <EMAIL> can't be a lane's git identity, or None when it can. Empty means the
    default (factory-<lane>, <lane>@factory.invalid); "{lane}" stands for the lane name. The
    same rules as check_identity in templates/agent/factory-lane and lane_identity_problem in
    dispatch/server.py."""
    def raw(value):
        return value.replace("{lane}", "developer").encode("utf-8", "surrogateescape")

    if name and not (raw(name).strip(_SPACE.encode()) and len(raw(name)) <= 128
                     and not _IDENTITY_BAD.search(raw(name)) and not raw(name).startswith(b"-")):
        return ('author_name must be 1-128 characters, not only spaces, with no control character, "<" or ">", '
                'and not start with "-"')
    if email and not (len(raw(email)) <= 254 and _IDENTITY_EMAIL.fullmatch(raw(email))):
        return ('author_email must look like name@domain: one "@", no spaces, control characters, "<" or ">", '
                'not starting with "-", at most 254 characters')
    return None
