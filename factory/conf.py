"""The KEY="value" format of role.conf, local role overrides and the lane identity
snapshot. Parsed, never sourced or executed. The one parser: bin/factory's bash
(scripts/lib/roles.sh conf_get), the dispatch bridge and the Python CLI agree with it.
"""

import re

# Same line as `sed -n 's/^KEY="\(.*\)"[[:space:]]*$/\1/p'`: ASCII whitespace
# only after the closing quote, and (see parse_conf) lines end at "\n" only.
_LINE = re.compile(r'([A-Z_]+)="(.*)"[ \t\v\f\r]*', re.ASCII)


def match_conf_line(line):
    """(key, value) if `line` is a KEY="value" line, else None."""
    m = _LINE.fullmatch(line)
    return (m.group(1), m.group(2)) if m else None


def parse_conf(text):
    """Every KEY="value" line of `text` as a dict; the last line for a key wins
    (conf_get takes `tail -n 1`). Other lines (comments, blanks, anything else) are skipped."""
    values = {}
    for line in text.split("\n"):
        pair = match_conf_line(line)
        if pair:
            values[pair[0]] = pair[1]
    return values


def read_text(path):
    """The file's text, or "" if it is absent. Bytes that are not UTF-8 survive
    as surrogates instead of raising."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except (FileNotFoundError, NotADirectoryError):
        return ""


def read_conf(path):
    """parse_conf of the file at `path` ({} if it is absent)."""
    return parse_conf(read_text(path))


def conf_get(path, key):
    """The value of KEY="value" in the file ("" if absent)."""
    return read_conf(path).get(key, "")
