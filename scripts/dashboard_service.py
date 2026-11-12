"""The dashboard's SERVICE DUE lamp: whether something changed that `factory update` (a sandbox
is out of date) or a restart (a setting read at start-up) has to apply. Display only.

It reads files and never runs a command, not even `sbx ls -q`:
  - UPDATE: factory/fingerprints.py's is_stale for each sandbox with a recorded fingerprint
    (FACTORY_STATE/sandboxes/), the same test `factory start` warns with, and stale_reason for why.
  - RESTART: a role override file (FACTORY_CONFIG/roles/<role>.conf) or the enabled-roles list that
    differs from the copy `factory start` snapshotted into FACTORY_STATE/dispatch/, or project
    notes (FACTORY_CONFIG/projects/<project>[.<role>].md) that differ from the copy the snapshot
    appended to the role's instructions (a deleted file counts; with no instructions snapshot to
    compare, a file modified after the snapshot does).
UPDATE wins when both apply (and then only its lines are shown). Every line is host-made and one clean line; the page draws it as text.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package, for fingerprints

from factory import conf, fingerprints, roles  # noqa: E402
from factory.context import Paths  # noqa: E402

READ_MAX = 1 << 16
UNSAFE = re.compile(r"[^\x20-\x7e]")


def clean(text: str, cap: int = 160) -> str:
    return UNSAFE.sub("?", str(text))[:cap]


def _read(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read(READ_MAX)
    except OSError:
        return None


def safe_read(path, cap: int = READ_MAX) -> bytes | None:
    """At most `cap` bytes of a regular file opened without following a symlink and without
    blocking (a FIFO or device reads as None), or None when it is absent or unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(cap)
    except OSError:
        return None
    finally:
        os.close(fd)


SNAPSHOT_MAX = 3 << 20      # bytes read of one role's snapshotted instructions
NOTES_MAX = 1 << 20         # bytes read of one notes file


def snapshot_made(paths: Paths) -> bool:
    """True once `factory start` has written its snapshot (enabled-roles is the last thing in it)."""
    return safe_read(Path(paths.dispatch_state) / "enabled-roles", 1) is not None


def snapshot_instructions(paths: Paths, role: str) -> bytes | None:
    """The instructions (with the project notes appended) the bridge hands `role`'s jobs, or None."""
    return safe_read(Path(paths.dispatch_state) / "instructions" / f"{role}.md", SNAPSHOT_MAX)


def snapshot_override(paths: Paths, role: str) -> dict | None:
    """The override of `role` that the bridge runs with ({} if it has none), or None when the
    snapshot or its read is missing. Parsed, never executed."""
    if not snapshot_made(paths):
        return None
    data = safe_read(Path(paths.dispatch_state) / "role-overrides" / f"{role}.conf", READ_MAX)
    return {} if data is None else conf.parse_conf(data.decode("utf-8", "replace"))


def notes_headers(project: str, role: str) -> tuple:
    """The two lines `factory start` writes before the project notes in a role's instructions."""
    name = os.fsencode(project)
    return (b"\n# This project (" + name + b")\n",
            b"\n# This project (" + name + b"), for the " + role.encode() + b"\n")


def notes_running(snapshot: bytes, project: str, role: str) -> tuple:
    """(shared, own): the bytes of the project notes and of the role's notes inside `snapshot`, an
    instructions snapshot, or None for a part that is not there."""
    shared_hdr, own_hdr = notes_headers(project, role)
    i, j = snapshot.find(shared_hdr), snapshot.find(own_hdr)
    own = snapshot[j + len(own_hdr):] if j >= 0 else None
    shared = None
    if i >= 0:
        shared = snapshot[i + len(shared_hdr):j if j > i else len(snapshot)]
    return shared, own


def notes_saved(paths: Paths, project: str, role: str) -> tuple:
    """(shared, own): the notes files as `factory start` would append them now (None for one that
    is absent or empty)."""
    folder = Path(paths.config) / "projects"
    return tuple(safe_read(folder / name, NOTES_MAX) or None
                 for name in (f"{project}.md", f"{project}.{role}.md"))


def is_link(path) -> bool:
    """True if `path` is a symlink. `factory start` follows one when it snapshots notes, and the safe
    readers here don't, so such a file can't be compared byte for byte."""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


def instructions_changed(paths: Paths, role: str, snapshot: bytes, project: str) -> bool:
    """True if the role's committed instructions.md differs from the part of the running
    instructions snapshot that comes before the project notes."""
    shared_hdr, own_hdr = notes_headers(project, role)
    cut = [i for i in (snapshot.find(shared_hdr), snapshot.find(own_hdr)) if i >= 0]
    saved = safe_read(Path(paths.root) / "roles" / role / "instructions.md", SNAPSHOT_MAX)
    return saved is not None and saved != snapshot[:min(cut) if cut else len(snapshot)]


def _names(folder: Path, suffix: str) -> list:
    try:
        return sorted(n for n in os.listdir(folder) if n.endswith(suffix) and not n.startswith(".")
                      and os.path.isfile(folder / n))
    except OSError:
        return []


def stale_names(paths: Paths) -> list:
    """The sandboxes with a recorded fingerprint that is out of date."""
    return [name for name in fingerprints.recorded_sandboxes(paths) if fingerprints.is_stale(paths, name)]


def notes_changed(paths: Paths, project: str, since: float) -> list:
    """Names of the project's notes files that differ from what the bridge runs with, for the
    roles this machine runs. Where a role has an instructions snapshot the bytes are compared (so a
    deleted file counts); where it has none, or the file is a symlink, a file modified after
    `since` counts."""
    changed = []
    for role in roles.list_roles(paths):
        snap = snapshot_instructions(paths, role)
        files = (f"{project}.md", f"{project}.{role}.md")
        folder = Path(paths.config) / "projects"
        compared = {} if snap is None else dict(zip(files, zip(notes_saved(paths, project, role),
                                                               notes_running(snap, project, role))))
        for name in files:
            if name in compared and not is_link(folder / name):
                saved, running = compared[name]
                if saved != running:
                    changed.append(name)
                continue
            try:
                if os.stat(folder / name).st_mtime > since:
                    changed.append(name)
            except OSError:
                pass
    return list(dict.fromkeys(changed))


def restart_lines(paths: Paths, project: str) -> list:
    """What changed in FACTORY_CONFIG since `factory start` last snapshotted it. Nothing before the
    first start."""
    snap = Path(paths.dispatch_state)
    try:
        since = os.stat(snap / "enabled-roles").st_mtime  # the last thing the snapshot writes
    except OSError:
        return []
    config = Path(paths.config)
    lines = []
    # Only the roles this machine runs get an override copied (as `factory start` does), plus
    # whatever the snapshot already holds, so a removed override is still noticed.
    running = {r + ".conf" for r in roles.list_roles(paths)}
    for name in sorted((running & set(_names(config / "roles", ".conf"))) | set(_names(snap / "role-overrides", ".conf"))):
        if _read(config / "roles" / name) != _read(snap / "role-overrides" / name):
            lines.append(f"role override {clean(name)} changed")
    listed = _read(snap / "enabled-roles")
    if sorted(roles.enabled_roles_listed(paths)) != sorted(
            roles.parse_enabled_roles((listed or b"").decode("utf-8", "replace"))):
        lines.append("enabled-roles changed")
    usable = bool(project) and project != "(no project)" and "/" not in project and not project.startswith(".")
    if usable:
        for name in notes_changed(paths, project, since):
            lines.append(f"project notes {clean(name)} changed")
    for role in roles.list_roles(paths):
        snap = snapshot_instructions(paths, role)
        if snap is not None and instructions_changed(paths, role, snap, project if usable else ""):
            lines.append(f"role instructions {clean(role)} changed")
    return lines


def service(paths: Paths | None, project: str) -> dict:
    """{"due": "update" | "restart" | None, "lines": [...], "stale": [...]} for the header lamp.
    "stale" (the out-of-date sandbox names, for the FIRST SHIFT hints) stays inside the dashboard."""
    nothing: dict = {"due": None, "lines": [], "stale": []}
    if paths is None:
        return nothing
    try:
        names, restart = stale_names(paths), restart_lines(paths, project)
        update = [f"{clean(n)}: {clean(fingerprints.stale_reason(paths, n))}" for n in names]
    except Exception:  # a lamp must not take the state view down; a broken fixture reads as nothing due
        return nothing
    stale = [clean(n) for n in names]
    if update:  # UPDATE re-runs the start as well, so the restart lines would only repeat it
        return {"due": "update", "lines": update[:8], "stale": stale}
    return {"due": "restart" if restart else None, "lines": restart[:8], "stale": []}
