"""The words a lane wears on the dashboard (doomtickle/passdesk-ops#194): the desk placard
(what the job is about), the developer lane a reviewer is checking, and the branch a developer
is on. The factory floor and the lane modules and chips read these one derivation, so the two
views can't disagree. Pure text in, one clean line out; nothing here touches a file or a command.
The JS half (the "DEV 2" tag) is dashboard-ui/lanelabel.js.
"""

from __future__ import annotations

import re

DESK_MAX = 40      # characters of a lane's desk placard (the floor slides what the glass can't show)
BRANCH_MAX = 80    # characters of a branch name on a module header
TOPIC = re.compile(r"(?:(?<=\.\.)|(?<=\borigin/)|(?<![\w./-]))(?:factory-[A-Za-z0-9-]+/)?factory/([A-Za-z0-9][A-Za-z0-9._-]*)")
DEV_REF = re.compile(r"(?<![\w-])factory-(developer(?:-lane[0-9]+)?)/factory/")
TASK_LABEL = re.compile(r"^(?:(?:repo|repository|project)\s*:\s*[^\s;]*[^\s.;][.;]?\s*|(?:task|goal)\s*:\s*)", re.IGNORECASE)


def branch_topic(text: str) -> str:
    """The <topic> of the first factory/<topic> branch a text names (also as
    factory-<lane>/factory/<topic>, origin/factory/<topic> or main...factory/<topic>),
    else "". The one place that knows what a branch looks like."""
    m = TOPIC.search(str(text or ""))
    return m.group(1).rstrip("._-") if m else ""


def desk_label(brief: str) -> str:
    """A few words (at most DESK_MAX characters) for what a job is about: the
    topic of the first factory/<topic> branch the task names, else the first
    words of its first non-empty line without a leading "Repo: x." label. One
    clean line: only printable characters, no markup, "" when nothing is left."""
    text = str(brief or "")
    topic = branch_topic(text)
    if topic:
        words = re.sub(r"[-_.]+", " ", topic).split()
    else:
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        first = "".join(ch if ch.isprintable() else " " for ch in first)
        first = TASK_LABEL.sub("", first.strip(), count=1)
        words = re.sub(r"[`*_#<>\[\]{}|\\]+", " ", first).split()
    label = ""
    for word in words:
        word = "".join(ch for ch in word if ch.isprintable())
        nxt = f"{label} {word}".strip()
        if len(nxt) > DESK_MAX:
            label = label or word[:DESK_MAX - 1] + "…"
            break
        label = nxt
    return label


def reviewed_lane(brief: str) -> str:
    """The developer lane a task points a reviewer at: the lane in the first
    factory-<lane>/factory/ ref (the form the bridge imports branches as), else ""."""
    m = DEV_REF.search(str(brief or ""))
    return m.group(1) if m else ""


def lane_branch(brief: str, branch_lines=()) -> str:
    """The factory/<topic> branch a developer lane is on: the one the task names,
    else the first branch line in the job's log (the bridge writes one per imported
    branch). At most BRANCH_MAX printable characters, "" when there is none."""
    topic = branch_topic(brief)
    for line in branch_lines:
        if topic:
            break
        topic = branch_topic(line)
    name = f"factory/{topic}" if topic else ""
    return name[:BRANCH_MAX]
