"""Reading policy.toml the way the bash checks did: policy_value, policy_string, git_push_enabled,
git_repos, git_repos_problems, github_repos_listed, gitlab_needs_token, gitlab_mr_unheld, jira_cloud_id_set, any_jira_tool_granted
and npm_private_registry_needs_token (once in scripts/lib/common.sh and bin/factory; their bash is
gone, and factory/test_policy.py checks the answers it gave). Not a TOML parser: a line scan good
enough for the values these checks read. Also the lane git identity rules (policy_toml_string,
lane_identity_problem).
"""

import os
import re
from urllib.parse import urlsplit

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


def policy_value_source(paths, section, key):
    """(raw value, "local" | "repo") for KEY in [SECTION], or (None, None) when neither file sets
    it: policy_value's precedence, plus which file the value came from."""
    for source, path in (("local", os.path.join(paths.config, "policy.toml")),
                         ("repo", os.path.join(paths.root, "broker", "policy.toml"))):
        if os.path.isfile(path):
            value = _scan(read_text(path), section, key)
            if value is not None:
                return value, source
    return None, None


def policy_value(paths, section, key):
    """The raw value of KEY = ... in [SECTION] from this machine's overlay
    (FACTORY_CONFIG/policy.toml) if it sets KEY, else from broker/policy.toml: the same
    precedence as the broker's load_policy(). None if neither sets it."""
    return policy_value_source(paths, section, key)[0]


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


def git_push_enabled(paths):
    """True if [git].repos is a list with something in it: a "[", later a character that is
    neither "]" nor a space, and after it a "]"."""
    value = policy_value(paths, "git", "repos") or ""
    return re.search(r"\[.*[^\]%s].*\]" % _SPACE, value) is not None


def git_repos(paths):
    """The quoted entries of [git].repos, in order ([] if unset or empty)."""
    value = policy_value(paths, "git", "repos") or ""
    return [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', value)]


_GROUP_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}"
_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_GROUP_RE = re.compile(r"([A-Za-z0-9.-]+(?::[0-9]{1,5})?)((?:/%s){1,21})" % _GROUP_SEGMENT)


def _gitlab_netloc(paths):
    """The host (and port, if not 443) of [gitlab].url, lowercase, or None if it is unset or not a URL."""
    url = policy_string(paths, "gitlab", "url")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not url or not host:
        return None
    return host + ("" if port in (None, 443) else ":%d" % port)


def repo_entry_problem(entry, gitlab_netloc):
    """Why a [git].repos entry with a "*" is not a valid GitLab group entry, or None. The same
    rules as the broker's check_repos_entry (broker/passdesk/tools/git.py), which refuses at
    run time what this refuses at build time: only a final "/*" after "<gitlab host>/<group>",
    never GitHub "owner/*", the whole instance "host/*", "**", an empty or ".." segment.
    GITLAB_NETLOC is [gitlab].url's host, which the entry must name (None: no usable URL).
    Entries without a "*" are the broker's to check (they are names, not patterns)."""
    if "*" not in entry:
        return None
    if not entry.endswith("/*") or "*" in entry[:-1]:
        return ('[git].repos entry "%s": "*" is allowed only as a final "/*" after a GitLab group '
                '("<gitlab host>/<group>/*")' % entry)
    group = entry[:-2]
    first = group.split("/", 1)[0]
    if "." not in first and ":" not in first:
        return ('[git].repos entry "%s": group entries are for GitLab only; list GitHub repos one by one as '
                'owner/name' % entry)
    if "/" not in group:
        return ('[git].repos entry "%s": name a group, not the whole instance ("<gitlab host>/<group>/*")' % entry)
    m = _GROUP_RE.fullmatch(group)
    host_ok = bool(m) and _HOST_RE.fullmatch(m.group(1).split(":")[0].lower()) is not None
    port = m.group(1).partition(":")[2] if m else ""
    if (not m or not host_ok or (port and not 0 < int(port) < 65536) or ".." in group or "/." in group or "/-" in group
            or group.lower().endswith((".git", ".atom"))):
        return ('[git].repos entry "%s": not a valid GitLab group path ("<gitlab host>/<group>[/<subgroup>...]/*", '
                'no empty or ".." segments)' % entry)
    if gitlab_netloc is None:
        return '[git].repos entry "%s" is on GitLab, but [gitlab].url is not set (or is not a URL)' % entry
    if m.group(1).lower() != gitlab_netloc:
        return '[git].repos entry "%s" is not on the configured GitLab (%s)' % (entry, gitlab_netloc)
    return None


def git_repos_problems(paths):
    """[git].repos group entries the broker would refuse, as messages ([] if none)."""
    netloc = _gitlab_netloc(paths)
    return [p for p in (repo_entry_problem(e, netloc) for e in git_repos(paths)) if p]


def describe_repo_entry(entry):
    """An entry as the owner reads it: a group says what it covers."""
    return "%s (any project in the group)" % entry if entry.endswith("/*") else entry


def github_repos_listed(paths):
    """True if some quoted [git].repos entry is a GitHub owner/name: it holds a "/" and its
    first segment has no dot or port (a host-qualified entry is on GitLab)."""
    value = policy_value(paths, "git", "repos") or ""
    for entry in re.findall('"([^"]*)"', value):
        parts = entry.split("/")
        if len(parts) >= 2 and not re.search("[.:]", parts[0]):
            return True
    return False


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
    return toml_string(raw)


def toml_string(raw):
    """The text of a one-line TOML string value as read from a file ("basic" without escapes, or
    'literal', then optionally a # comment); ValueError for anything else. FACTORY_CONFIG/dashboard.toml
    (scripts/dashboard_config.py) reads its strings through this too."""
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
