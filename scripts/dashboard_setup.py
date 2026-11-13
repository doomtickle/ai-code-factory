"""The dashboard's SETUP screen data: the factory's effective configuration, read-only.

It reads files with the factory's own parsers (factory/roles.py, factory/conf.py,
factory/policy.py), never runs a command, and shows no secret: the only things taken from
policy.toml are [approvals].tools and expire_seconds, [git].repos and [gitlab].url (with any
user:password@ part cut), and a credential is never read or named. Every string is cleaned to
printable ASCII and capped here, because the control unit's page draws it as text only.

Each line carries the command that applies a change to it (`applies`: START or UPDATE), the
table in docs/guides/04-customize.md:
  START   roles' MODEL, MODELS, EFFORT, LANES, MAX_SESSION_TOKENS, MAX_TURNS, MAX_JOB_SECONDS;
          enabled-roles; project notes
  UPDATE  a role's TEMPLATE, WORKSPACE and BROKER_TOOLS; the local policy.toml; the Coordinator's kit

What is saved is not always what runs: the bridge reads only the copy `factory start` made in
FACTORY_STATE/dispatch/ (role-overrides/, instructions/, enabled-roles). Where that copy exists
(`snapshot`), each tuning key carries the `running` value, each notes file the `running_chars` and
each of them a `pending` flag when the two differ, and `pending` counts them. Notes are compared
by their bytes, which are never sent.

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

from dashboard_service import (clean, instructions_changed, is_link, notes_running,  # noqa: E402
                               notes_saved, safe_read, snapshot_instructions, snapshot_made, snapshot_override)

NOTES_LIMIT = 20_000        # characters a role's instructions plus the project notes may total (dispatch/server.py)
NOTES_READ_MAX = 1 << 20    # bytes read of one notes file; past this it is over the limit whatever it holds
MAX_ROLES = 32
MAX_ITEMS = 64
FIXED_KEYS = (("WORKSPACE", "UPDATE", ""), ("TEMPLATE", "UPDATE", ""),
              ("BROKER_TOOLS", "UPDATE", "Restart is enough only if the role had tools and keeps some"))
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


def _role(paths, name: str, on: bool, runs: bool | None = None) -> dict:
    repo = read_conf(roles.role_conf_path(paths, name))
    override_path = roles.role_override_file(paths, name)
    override = read_conf(override_path) if os.path.isfile(override_path) else {}
    # What the bridge runs: the snapshotted override over the committed role.conf (only for a role it runs,
    # `runs`: on now and in the start's enabled-roles). The snapshot holds the override only; the bridge reads
    # role.conf live, so a key the snapshot override doesn't set runs at the file's current value.
    running = snapshot_override(paths, name) if (on if runs is None else runs) else None
    keys = []
    for key in roles.ROLE_OVERRIDE_KEYS:
        row = {"key": key, "value": clean(roles.role_get(paths, name, key)), "applies": "START",
               "source": "LOCAL" if key in override else "REPO"}
        if key in override:
            row["repo"] = clean(repo.get(key, ""))
        row["pending"] = False
        if running is not None:
            row["running"] = clean(running.get(key, repo.get(key, "")))
            row["pending"] = row["running"] != row["value"]
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


def _notes(paths, project: str, running: list, chars=_read_chars) -> tuple:
    """(the notes block, {role: characters its instructions plus the notes come to}) for the bound project.
    chars(path) counts a file (the config write passes one that counts a proposed notes file's new text)."""
    block = {"project": clean(project), "limit": NOTES_LIMIT, "applies": "START", "files": []}
    totals: dict = {}
    if not usable_project(project):
        return block, totals
    folder = os.path.join(paths.config, "projects")
    shared = chars(os.path.join(folder, project + ".md"))
    if shared is not None:
        block["files"].append({"name": clean(project + ".md"), "role": None, "chars": shared})
    for name in roles.list_all_roles(paths)[:MAX_ROLES]:
        own = chars(os.path.join(folder, f"{project}.{name}.md"))
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
        own = chars(os.path.join(folder, f"{project}.{name}.md"))
        if own:
            total += len(f"\n# This project ({project}), for the {name}\n") + own
        totals[name] = total
    for f in block["files"]:
        f["over"] = f["chars"] > NOTES_LIMIT
    return block, totals


def _text_len(data: bytes) -> int:
    return len(data.decode("utf-8", "replace"))


def _mark_notes(paths, project: str, running: list, notes: dict, out_roles: dict) -> int:
    """Compare the notes files, and each running role's instructions, with the copy the bridge runs
    on (dispatch/instructions/<role>.md). Adds `running_chars` and `pending` to each notes file,
    `gone` entries for a file that was deleted since the start, and `instructions_running` and
    `instructions_pending` (the committed instructions.md itself changed) to the roles. Returns the
    number of pending notes files plus pending instructions."""
    pending = 0
    files = {f["name"]: f for f in notes["files"]}  # keyed by the cleaned name
    for f in notes["files"]:
        f["pending"], f["running_chars"] = False, None
    gone: dict = {}

    def note(name: str, role, saved, run):
        nonlocal pending
        if is_link(os.path.join(paths.config, "projects", name)):
            return   # followed by `factory start`, never by us: not compared, so never pending or gone
        f = files.get(clean(name))
        if f is None and run is not None:    # deleted (or emptied) since the start
            f = gone.setdefault(name, {"name": clean(name), "role": clean(role, 40) if role else None, "chars": 0,
                                       "over": False, "gone": True, "read": True, "pending": False,
                                       "running_chars": _text_len(run)})
        if f is None:
            return
        if f["running_chars"] is None and run is not None:
            f["running_chars"] = _text_len(run)
        if saved != run and not f["pending"]:
            f["pending"] = True
            pending += 1

    for role in running[:MAX_ROLES]:
        snap = snapshot_instructions(paths, role)
        if snap is None:
            continue
        shared_run, own_run = notes_running(snap, project, role)
        shared_saved, own_saved = notes_saved(paths, project, role)
        note(project + ".md", None, shared_saved, shared_run)
        note(f"{project}.{role}.md", role, own_saved, own_run)
        row = out_roles.get(role)
        if row is not None:
            row["instructions_running"] = _text_len(snap)
            row["instructions_pending"] = instructions_changed(paths, role, snap, project)
            pending += row["instructions_pending"]
    notes["files"].extend(gone.values())
    for f in notes["files"]:
        f.setdefault("pending", False)
    return pending


def build(paths, project: str) -> dict:
    """The SETUP screen's data. Missing files read as nothing set, never as an error."""
    listed = roles.enabled_roles_listed(paths)
    every = roles.list_all_roles(paths)[:MAX_ROLES]
    running = set(roles.list_roles(paths))
    notes, totals = _notes(paths, project, [r for r in every if r in running])
    out_roles = []
    snapshot = snapshot_made(paths)
    listed_running = None
    if snapshot:
        data = safe_read(os.path.join(paths.dispatch_state, "enabled-roles"), 1 << 16) or b""
        listed_running = set(roles.parse_enabled_roles(data.decode("utf-8", "replace")))
    for name in every:
        optional = roles.role_optional(paths, name)
        row = _role(paths, name, name in running,
                    name in running and (not optional or (listed_running is not None and name in listed_running)))
        if name in totals:
            row["instructions_chars"] = totals[name]
            row["over"] = totals[name] > NOTES_LIMIT
        if listed_running is not None and row["optional"]:
            row["running_on"] = name in listed_running
            row["on_pending"] = row["running_on"] != row["on"]
        out_roles.append(row)
    pending = sum(k["pending"] for r in out_roles for k in r["keys"]) + sum(r.get("on_pending", False) for r in out_roles)
    notes["snapshot"] = snapshot
    if snapshot and usable_project(project):
        pending += _mark_notes(paths, project, [r for r in every if r in running and (not roles.role_optional(paths, r) or r in listed_running)],
                               notes, {r["name"]: r for r in out_roles})

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
        "snapshot": snapshot, "pending": pending,
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


def example_prompt(now: float) -> str:
    """One of PROMPTS, the same all day: the first (the first-session guide's) on day 0 of each cycle."""
    return PROMPTS[int(now // 86400) % len(PROMPTS)]


def look_values(look) -> dict:
    """What the dashboard is running with from dashboard.toml (dashboard_config.load's result), as plain
    values for the Look tab: the nameplate cleaned to one line, or None."""
    look = look if isinstance(look, dict) else {}
    return {"plant": clean(look["plant"], 40) if isinstance(look.get("plant"), str) else None}


def setup(paths, project: str, look_problem: str = "", look: dict | None = None) -> dict | None:
    """build() plus the dashboard.toml row (`look`: is there a file, why it was refused if it was, and
    the `values` the dashboard started with), or None when there is no config to read (no paths) or
    reading it failed."""
    if paths is None:
        return None
    try:
        out = build(paths, project)
        out["look"] = {"file": os.path.isfile(os.path.join(paths.config, "dashboard.toml")),
                       "problem": clean(look_problem, 400), "values": look_values(look)}
        return out
    except Exception:  # a screen must not take the state view down; a broken fixture reads as unavailable
        return None
