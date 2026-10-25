"""Reading policy.toml the way the bash checks do: policy_value, gitlab_needs_token and
gitlab_mr_unheld (scripts/lib/common.sh and bin/factory). Not a TOML parser: a line scan
good enough for the values these checks read. The bash copies stay until `start` and
`status` are ported; factory/test_policy.py runs both on the same fixtures.
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
