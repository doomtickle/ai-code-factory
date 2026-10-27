"""The dashboard's SERVICE DUE lamp: whether something changed that `factory update` (a sandbox
is out of date) or a restart (a setting read at start-up) has to apply. Display only.

It reads files and never runs a command, not even `sbx ls -q`:
  - UPDATE: factory/fingerprints.py's is_stale for each sandbox with a recorded fingerprint
    (FACTORY_STATE/sandboxes/), the same test `factory start` warns with, and stale_reason for why.
  - RESTART: a role override file (FACTORY_CONFIG/roles/<role>.conf) or the enabled-roles list that
    differs from the copy `factory start` snapshotted into FACTORY_STATE/dispatch/, or project
    notes (FACTORY_CONFIG/projects/<project>[.<role>].md) modified after that snapshot.
UPDATE wins when both apply (and then only its lines are shown). Every line is host-made and one clean line; the page draws it as text.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package, for fingerprints

from factory import fingerprints, roles  # noqa: E402
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


def _names(folder: Path, suffix: str) -> list:
    try:
        return sorted(n for n in os.listdir(folder) if n.endswith(suffix) and not n.startswith(".")
                      and os.path.isfile(folder / n))
    except OSError:
        return []


def stale_lines(paths: Paths) -> list:
    """'factory-broker: kit changed' for each out-of-date sandbox with a recorded fingerprint."""
    return [f"{clean(name)}: {clean(fingerprints.stale_reason(paths, name))}"
            for name in fingerprints.recorded_sandboxes(paths) if fingerprints.is_stale(paths, name)]


def restart_lines(paths: Paths, project: str) -> list:
    """What changed in FACTORY_CONFIG since `factory start` last snapshotted it. Nothing before the
    first start. Deleted project notes are not noticed (they leave no file to compare)."""
    snap = Path(paths.dispatch_state)
    try:
        since = os.stat(snap / "enabled-roles").st_mtime  # the last thing the snapshot writes
    except OSError:
        return []
    config = Path(paths.config)
    lines = []
    for name in sorted(set(_names(config / "roles", ".conf")) | set(_names(snap / "role-overrides", ".conf"))):
        if _read(config / "roles" / name) != _read(snap / "role-overrides" / name):
            lines.append(f"role override {clean(name)} changed")
    listed = _read(snap / "enabled-roles")
    if sorted(roles.enabled_roles_listed(paths)) != sorted(
            roles.parse_enabled_roles((listed or b"").decode("utf-8", "replace"))):
        lines.append("enabled-roles changed")
    if project and project != "(no project)":
        notes = [project + ".md"] + [f"{project}.{r}.md" for r in roles.list_all_roles(paths)]
        for name in notes:
            try:
                if os.stat(config / "projects" / name).st_mtime > since:
                    lines.append(f"project notes {clean(name)} changed")
            except OSError:
                pass
    return lines


def service(paths: Paths | None, project: str) -> dict:
    """{"due": "update" | "restart" | None, "lines": [...]} for the header lamp."""
    if paths is None:
        return {"due": None, "lines": []}
    try:
        update, restart = stale_lines(paths), restart_lines(paths, project)
    except Exception:  # a lamp must not take the state view down; a broken fixture reads as nothing due
        return {"due": None, "lines": []}
    if update:  # UPDATE re-runs the start as well, so the restart lines would only repeat it
        return {"due": "update", "lines": update[:8]}
    return {"due": "restart" if restart else None, "lines": restart[:8]}
