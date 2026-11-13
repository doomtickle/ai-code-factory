"""Turn a unittest log into a short Markdown list of failures: each test's name and the first line of its error.

  python3 scripts/ci_failure_summary.py < unittest.log >> "$GITHUB_STEP_SUMMARY"

The `browser tests` job of .github/workflows/ci.yml uses this, so the list can be pasted back to the Coordinator.
Prints nothing when no test failed or errored. Standard library only.
"""

import re
import sys

HEADER = re.compile(r"^(FAIL|ERROR): (\S+) \((.+)\)$")
RULE = re.compile(r"^(=|-){20,}$")


def failures(log):
    """[(kind, test id, first error line)] in the order unittest reported them."""
    found, current, in_traceback = [], None, False
    for line in log.splitlines():
        match = HEADER.match(line)
        if match:
            current = [match.group(1), match.group(3), ""]
            found.append(current)
            in_traceback = False
        elif current is None:
            continue
        elif RULE.match(line):
            if line.startswith("="):
                current = None
        elif line.startswith("Traceback"):
            in_traceback = True
        elif in_traceback and not current[2] and line and not line[0].isspace():
            # Frames are indented; the first unindented line after "Traceback" is the exception.
            current[2] = line
    return [tuple(f) for f in found]


def render(found):
    if not found:
        return ""
    out = [f"### {len(found)} browser test failure{'s' if len(found) != 1 else ''}", ""]
    for kind, test, first in found:
        out.append(f"- `{test}` ({kind.lower()}): {first or 'no error line in the log'}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render(failures(sys.stdin.read())))
