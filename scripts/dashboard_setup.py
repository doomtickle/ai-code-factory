"""The dashboard's SETUP screen data: the factory's effective configuration, read-only.

It reads files with the factory's own parsers (factory/roles.py, factory/conf.py,
factory/policy.py), never runs a command, and shows no secret: the only things taken from
policy.toml are [approvals].tools and expire_seconds, [git].repos and [gitlab].url (with any
user:password@ part cut), and a credential is never read or named. Every string is cleaned to
printable ASCII and capped here, because the control unit's page draws it as text only.

Each line carries the command that applies a change to it (`applies`: START or UPDATE), the
table in docs/guides/04-customize.md:
  START   roles' MODEL, MODELS, EFFORT, LANES, MAX_SESSION_TOKENS, MAX_TURNS, MAX_JOB_SECONDS;
          a role's BROKER_TOOLS (once it had some); enabled-roles; project notes
  UPDATE  a role's TEMPLATE and WORKSPACE; the local policy.toml; the Coordinator's kit

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package

from factory import policy, roles  # noqa: E402
from factory.conf import read_conf  # noqa: E402

from dashboard_service import clean  # noqa: E402

NOTES_LIMIT = 20_000        # characters a role's instructions plus the project notes may total (dispatch/server.py)
NOTES_READ_MAX = 1 << 20    # bytes read of one notes file; past this it is over the limit whatever it holds
MAX_ROLES = 32
MAX_ITEMS = 64
FIXED_KEYS = (("WORKSPACE", "UPDATE", ""), ("TEMPLATE", "UPDATE", ""),
              ("BROKER_TOOLS", "START", "UPDATE if the role had none, or has none now"))
NO_PROJECT = "(no project)"


def _items(raw) -> list:
    """The quoted strings of a TOML array's raw text (as policy.github_repos_listed reads them)."""
    return [clean(a or b, 120) for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', raw or "")][:MAX_ITEMS]


def _words(value: str) -> list:
    return [clean(w, 60) for w in value.split()][:MAX_ITEMS]


def _site(url: str) -> str:
    """A URL as scheme://host[:port]/path with no user:password@, no query and no fragment."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return clean(url.split("@")[-1].split("?")[0].split("#")[0], 160)
    if not parts.scheme or not host:
        return clean(url.split("@")[-1].split("?")[0].split("#")[0], 160)
    return clean(f"{parts.scheme}://{host}{':%d' % port if port else ''}{parts.path}", 160)


def _read_chars(path: str) -> int | None:
    """Characters in a file (None if it can't be read as a regular file)."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return len(f.read(NOTES_READ_MAX).decode("utf-8", "replace"))
    except OSError:
        return None


def _role(paths, name: str, on: bool) -> dict:
    repo = read_conf(roles.role_conf_path(paths, name))
    override_path = roles.role_override_file(paths, name)
    override = read_conf(override_path) if os.path.isfile(override_path) else {}
    keys = []
    for key in roles.ROLE_OVERRIDE_KEYS:
        row = {"key": key, "value": clean(roles.role_get(paths, name, key)), "applies": "START",
               "source": "LOCAL" if key in override else "REPO"}
        if key in override:
            row["repo"] = clean(repo.get(key, ""))
        keys.append(row)
    fixed = []
    for key, applies, note in FIXED_KEYS:
        value = roles.role_get(paths, name, key)
        row = {"key": key, "value": clean(value, 400), "applies": applies}
        if note:
            row["note"] = note
        fixed.append(row)
    return {"name": clean(name, 40), "optional": roles.role_optional(paths, name), "on": on,
            "override": os.path.isfile(override_path),
            "ignored": [clean(k, 40) for k in sorted(override) if k not in roles.ROLE_OVERRIDE_KEYS][:MAX_ITEMS],
            "keys": keys, "fixed": fixed}


def usable_project(project: str) -> bool:
    """True if `project` can name a notes file: a folder name, not '(no project)', no '/', NUL or leading '.'."""
    return bool(project) and project != NO_PROJECT and "/" not in project and "\0" not in project \
        and not project.startswith(".")


def _notes(paths, project: str, running: list) -> tuple:
    """(the notes block, {role: characters its instructions plus the notes come to}) for the bound project."""
    block = {"project": clean(project), "limit": NOTES_LIMIT, "applies": "START", "files": []}
    totals: dict = {}
    if not usable_project(project):
        return block, totals
    folder = os.path.join(paths.config, "projects")
    shared = _read_chars(os.path.join(folder, project + ".md"))
    if shared is not None:
        block["files"].append({"name": clean(project + ".md"), "role": None, "chars": shared})
    for name in roles.list_all_roles(paths)[:MAX_ROLES]:
        own = _read_chars(os.path.join(folder, f"{project}.{name}.md"))
        if own is not None:
            block["files"].append({"name": clean(f"{project}.{name}.md"), "role": clean(name, 40), "chars": own,
                                   "read": name in running})
    # What the bridge counts for a role: its instructions, then each non-empty notes file under a heading.
    for name in running[:MAX_ROLES]:
        base = _read_chars(os.path.join(paths.root, "roles", name, "instructions.md"))
        if base is None:
            continue
        total = base
        if shared:
            total += len(f"\n# This project ({project})\n") + shared
        own = _read_chars(os.path.join(folder, f"{project}.{name}.md"))
        if own:
            total += len(f"\n# This project ({project}), for the {name}\n") + own
        totals[name] = total
    for f in block["files"]:
        f["over"] = f["chars"] > NOTES_LIMIT
    return block, totals


def build(paths, project: str) -> dict:
    """The SETUP screen's data. Missing files read as nothing set, never as an error."""
    listed = roles.enabled_roles_listed(paths)
    every = roles.list_all_roles(paths)[:MAX_ROLES]
    running = set(roles.list_roles(paths))
    notes, totals = _notes(paths, project, [r for r in every if r in running])
    out_roles = []
    for name in every:
        row = _role(paths, name, name in running)
        if name in totals:
            row["instructions_chars"] = totals[name]
            row["over"] = totals[name] > NOTES_LIMIT
        out_roles.append(row)

    def policy_row(section, key):
        raw, source = policy.policy_value_source(paths, section, key)
        return raw, (source or "").upper()

    tools_raw, tools_src = policy_row("approvals", "tools")
    expire_raw, expire_src = policy_row("approvals", "expire_seconds")
    repos_raw, repos_src = policy_row("git", "repos")
    url_raw, url_src = policy_row("gitlab", "url")
    url = _items(url_raw)
    try:
        coordinator = roles.coordinator_broker_tools(paths)
    except OSError:
        coordinator = ""
    return {
        "project": notes["project"],
        "roles": out_roles,
        "enabled": {"file": os.path.isfile(roles.enabled_roles_file(paths)), "applies": "START",
                    "listed": [clean(n, 40) for n in listed][:MAX_ITEMS],
                    "unknown": [clean(n, 40) for n in listed if n not in every][:MAX_ITEMS]},
        "approvals": {"tools": _items(tools_raw), "source": tools_src, "applies": "UPDATE",
                      "expire_seconds": clean(re.sub(r"\s*#.*", "", expire_raw or "").strip(), 20),
                      "expire_source": expire_src},
        "repos": {"list": _items(repos_raw), "source": repos_src, "applies": "UPDATE"},
        "gitlab": {"url": _site(url[0]) if url else "", "source": url_src, "applies": "UPDATE"},
        "coordinator": {"broker_tools": _words(coordinator), "applies": "UPDATE"},
        "local_policy": os.path.isfile(os.path.join(paths.config, "policy.toml")),
        "notes": notes,
    }


PROMPTS = (
    "Add unit tests for the slugify helper in src/utils/slug.ts: empty strings, unicode input and repeated "
    "dashes. Don't change the helper itself. Get the tests reviewed, then tell me which branch to merge.",
    "Read the README and the top-level folders, then tell me in five lines what this project does and how to "
    "run its tests. Don't change any files.",
    "Find one small function that has no tests, write tests for it on a new branch, get them reviewed, and tell "
    "me which branch to merge.",
)
MAX_BRIEF_REPOS = 12


def example_prompt(now: float) -> str:
    """One of PROMPTS, the same all day: the first (the first-session guide's) on day 0 of each cycle."""
    return PROMPTS[int(now // 86400) % len(PROMPTS)]


def briefing(config: dict | None, paths, repos: list | None, now: float) -> dict | None:
    """The STANDBY plate's shift briefing, the subset of `config` (build()) that the main page may show, plus the
    project's repos. Facts the host derived only: sizes and names of notes files, never their contents; no
    credential, no [git].repos, no GitLab URL. `repos` is None when the project folder is unknown."""
    if config is None:
        return None
    project = config["project"]
    usable = usable_project(project)
    notes = config["notes"]
    out_roles = []
    for r in config["roles"]:
        raw = next((k["value"] for k in r["keys"] if k["key"] == "LANES"), "")
        try:
            lanes = min(max(int(raw or "1"), 1), 8)
        except ValueError:
            lanes = 1
        out_roles.append({"name": r["name"], "lanes": lanes, "optional": r["optional"], "on": r["on"]})
    tools = list(config["approvals"]["tools"])
    return {
        "project": project, "usable": usable,
        "repos": [clean(n, 80) for n in (repos or [])[:MAX_BRIEF_REPOS]],
        "repos_total": len(repos or []), "repos_known": repos is not None,
        "notes": {"usable": usable, "limit": NOTES_LIMIT,
                  "path": clean(os.path.join(paths.config, "projects", project + ".md"), 240) if usable else "",
                  "files": [{"name": f["name"], "role": f["role"], "chars": f["chars"], "over": f["over"]}
                            for f in notes["files"]]},
        "roles": out_roles,
        "holds": {"on": bool(tools), "tools": tools},
        "prompt": example_prompt(now),
    }


def setup(paths, project: str, look_problem: str = "") -> dict | None:
    """build() plus the dashboard.toml row (`look`: is there a file, and why it was refused, if it was),
    or None when there is no config to read (no paths) or reading it failed."""
    if paths is None:
        return None
    try:
        out = build(paths, project)
        out["look"] = {"file": os.path.isfile(os.path.join(paths.config, "dashboard.toml")),
                       "problem": clean(look_problem, 400)}
        return out
    except Exception:  # a screen must not take the state view down; a broken fixture reads as unavailable
        return None
