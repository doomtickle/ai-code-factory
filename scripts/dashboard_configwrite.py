"""The control unit's one config write (doomtickle/passdesk-ops#185, #186): a role's tuning
override, FACTORY_CONFIG/roles/<role>.conf, and the bound project's notes,
FACTORY_CONFIG/projects/<project>.md and <project>.<role>.md. Both are data the factory parses
(the override) or appends to a role's prompt (the notes), never run.

Every target is named here from server-side facts only: FACTORY_CONFIG, the dashboard's
project folder name and a role list_roles returns; no path comes from the request. A role
override may set only roles.ROLE_OVERRIDE_KEYS (never WORKSPACE, TEMPLATE or BROKER_TOOLS),
and the new file is checked with roles.validate_role, what `factory start` runs, before
anything is written; a notes file is at most NOTES_LIMIT characters of strict UTF-8, and no
role's instructions plus notes may grow past the bridge's limit (dashboard_setup._notes
counts them, as the bridge will).

prepare_role / prepare_notes build a Plan (the exact new bytes, the SHA-256 of the current
file's bytes or "absent", and what the page shows: a diff, the display path); apply writes
it if the file still has that digest. Below FACTORY_CONFIG nothing is followed if it is a
symlink (roles/ or projects/ and the file itself are opened O_NOFOLLOW, the file
non-blocking and as a regular file only), and a write is a temp file in the same folder
(O_CREAT|O_EXCL|O_NOFOLLOW, 0600), fsynced, then os.replace'd over the target.

The bridge reads only the copies `factory start` snapshots into FACTORY_STATE/dispatch/
(factory/dispatch_start.py _snapshot_roles), so every change applies on the next RESTART.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import secrets
import shutil
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package

from factory import roles  # noqa: E402
from factory.conf import match_conf_line  # noqa: E402
from factory.context import FactoryError, Paths  # noqa: E402

import dashboard_setup  # noqa: E402
import patchview  # noqa: E402
from dashboard_service import clean  # noqa: E402

APPLIES = "RESTART"           # the bridge reads FACTORY_STATE's snapshot, never FACTORY_CONFIG
OVERRIDE_MAX = 64 * 1024      # bytes of an existing role override read
VALUE_MAX = 200               # characters of one override value
DIFF_LINES = 400              # diff entries returned at most
DIFF_CONTEXT = 3              # unchanged lines shown around a change
DIFF_LINE_MAX = 500           # characters of one diff line shown
_O = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR = getattr(os, "O_DIRECTORY", 0)


class WriteRefused(Exception):
    """(status, plain-text reason): the control unit answers it as a Refused."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


class Plan:
    """One prepared write: the target (FACTORY_CONFIG/<sub>/<name>), the bytes it held when
    prepared (as a digest) and the exact bytes to write; `view` is what prepare returns."""

    def __init__(self, config: str, sub: str, name: str, cap: int, digest: str, content: bytes, view: dict):
        self.config, self.sub, self.name, self.cap = config, sub, name, cap
        self.digest, self.content, self.view = digest, content, view


def display_path(path: str) -> str:
    """path with the home folder as ~, one clean line."""
    home = os.path.expanduser("~")
    if home not in ("", "/", "~") and (path == home or path.startswith(home.rstrip("/") + "/")):
        path = "~" + path[len(home.rstrip("/")):]
    return clean(path, 240)


def digest(data: bytes | None) -> str:
    return "absent" if data is None else hashlib.sha256(data).hexdigest()


def open_sub(config: str, sub: str, create: bool) -> int | None:
    """An fd for FACTORY_CONFIG/<sub> (roles or projects), never followed if it is a symlink;
    None if it is missing and create is false, else made (0700, FACTORY_CONFIG too)."""
    where = display_path(os.path.join(config, sub))
    try:
        top = os.open(config, os.O_RDONLY | _O | _DIR)  # the config folder is the user's own: followed
    except FileNotFoundError:
        if not create:
            return None
        os.makedirs(config, mode=0o700, exist_ok=True)
        top = os.open(config, os.O_RDONLY | _O | _DIR)
    try:
        for attempt in (1, 2):
            try:
                fd = os.open(sub, os.O_RDONLY | _O | _DIR | _NOFOLLOW, dir_fd=top)
                break
            except FileNotFoundError:
                if not create or attempt == 2:
                    return None
                try:
                    os.mkdir(sub, 0o700, dir_fd=top)
                except FileExistsError:
                    pass
            except OSError:
                raise WriteRefused(409, f"{where} is a symlink or not a folder: fix it by hand") from None
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            raise WriteRefused(409, f"{where} is a symlink or not a folder: fix it by hand")
        return fd
    finally:
        os.close(top)


def read_target(sub_fd: int | None, name: str, cap: int, where: str, cut: bool = False) -> bytes | None:
    """The target's bytes (None if absent): a regular file only (opened O_NOFOLLOW and
    non-blocking, so a FIFO can't hang it), at most cap bytes: a larger file is refused, or
    with cut, read only to cap."""
    if sub_fd is None:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | _O | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=sub_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise WriteRefused(409, f"{where} is a symlink or not a regular file: fix it by hand") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise WriteRefused(409, f"{where} is a symlink or not a regular file: fix it by hand")
        if st.st_size > cap and not cut:
            raise WriteRefused(409, f"{where} is too large to edit here: edit it by hand")
        chunks, total = [], 0
        while total <= cap:
            chunk = os.read(fd, min(65536, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > cap and not cut:
            raise WriteRefused(409, f"{where} is too large to edit here: edit it by hand")
        return b"".join(chunks)[:cap]
    finally:
        os.close(fd)


def write_target(sub_fd: int, name: str, data: bytes) -> None:
    """data as sub_fd/name: a new 0600 temp file in the same folder, fsynced, then renamed over it."""
    tmp = f".{name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _O, 0o600, dir_fd=sub_fd)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=sub_fd, dst_dir_fd=sub_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=sub_fd)
        except OSError:
            pass
        raise
    try:
        os.fsync(sub_fd)
    except OSError:
        pass  # some file systems can't fsync a folder; the rename itself is done


def current(config: str, sub: str, name: str, cap: int, cut: bool = False) -> bytes | None:
    where = display_path(os.path.join(config, sub, name))
    fd = open_sub(config, sub, create=False)
    try:
        return read_target(fd, name, cap, where, cut)
    finally:
        if fd is not None:
            os.close(fd)


def apply(plan: Plan) -> bool:
    """Write plan's content if the target still holds what it held at prepare (else a 409
    "changed", nothing written). Call under the control unit's write lock. True if it wrote."""
    where = display_path(os.path.join(plan.config, plan.sub, plan.name))
    fd = open_sub(plan.config, plan.sub, create=plan.view["changed"])
    try:
        if digest(read_target(fd, plan.name, plan.cap, where)) != plan.digest:
            raise WriteRefused(409, "changed")
        if not plan.view["changed"]:
            return False
        if fd is None:
            raise WriteRefused(409, f"{where} could not be created")
        write_target(fd, plan.name, plan.content)
        return True
    finally:
        if fd is not None:
            os.close(fd)


def diff(old: str, new: str) -> tuple:
    """([{"op": " " | "+" | "-", "text"}], truncated): the changed lines with DIFF_CONTEXT
    unchanged ones around them; a run of unchanged lines left out is one " " entry with
    "gap": true. Every line is made safe to show (patchview.clean)."""
    a, b = old.split("\n"), new.split("\n")
    if a and a[-1] == "":
        a.pop()
    if b and b[-1] == "":
        b.pop()
    out = []

    def line(op, text):
        out.append({"op": op, "text": patchview.clean(text, set(), DIFF_LINE_MAX)})

    groups = list(difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(DIFF_CONTEXT))
    shown = 0
    for group in groups:
        start = group[0][1]
        if start > shown:
            out.append({"op": " ", "text": f"⋯ {start - shown} unchanged lines", "gap": True})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for t in a[i1:i2]:
                    line(" ", t)
            else:
                for t in a[i1:i2]:
                    line("-", t)
                for t in b[j1:j2]:
                    line("+", t)
        shown = group[-1][2]
    if groups and shown < len(a):
        out.append({"op": " ", "text": f"⋯ {len(a) - shown} unchanged lines", "gap": True})
    return out[:DIFF_LINES], len(out) > DIFF_LINES


def running_role(paths, role) -> str:
    """role if it is one this machine runs (list_roles), else a 400."""
    if not isinstance(role, str) or not roles.valid_role_name(role) or role not in roles.list_roles(paths):
        raise WriteRefused(400, "role must be one of the roles this machine runs")
    return role


def _bad_char(ch: str) -> bool:
    return ch < " " or 0x7F <= ord(ch) <= 0x9F or unicodedata.category(ch) in ("Zl", "Zp") or ch in '"\\'


def check_changes(set_, unset) -> tuple:
    """({key: value}, [key]) from the request, or a 400: keys only from ROLE_OVERRIDE_KEYS, each
    value one line of at most VALUE_MAX characters with no quote, backslash or control character."""
    if not isinstance(set_, dict) or not isinstance(unset, list):
        raise WriteRefused(400, "set must be an object and unset a list")
    allowed = " ".join(roles.ROLE_OVERRIDE_KEYS)
    for key in list(set_) + unset:
        if not isinstance(key, str) or key not in roles.ROLE_OVERRIDE_KEYS:
            shown = clean(key, 40) if isinstance(key, str) else "that key"
            raise WriteRefused(400, f"{shown} can't be set from the dashboard (allowed: {allowed}); "
                                    "WORKSPACE, TEMPLATE and BROKER_TOOLS stay in roles/<role>/role.conf")
    if len(set(unset)) != len(unset) or set(set_) & set(unset):
        raise WriteRefused(400, "a key is named twice")
    for key, value in set_.items():
        if not isinstance(value, str):
            raise WriteRefused(400, f"{key} must be text")
        if len(value) > VALUE_MAX or any(_bad_char(ch) for ch in value):
            raise WriteRefused(400, f"{key} must be one line of at most {VALUE_MAX} characters, "
                                    "with no quotes, backslashes or control characters")
    return set_, unset


def merge(old: str, set_: dict, unset: list) -> str:
    """old with each KEY="value" line of a key in set_ given its new value (in place), every
    line of a key in unset dropped, and keys set_ adds appended in ROLE_OVERRIDE_KEYS order.
    Comments, blank lines, other lines and the order are kept."""
    lines = old.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out, seen = [], set()
    for line in lines:
        pair = match_conf_line(line)
        if pair and pair[0] in unset:
            continue
        if pair and pair[0] in set_:
            out.append(f'{pair[0]}="{set_[pair[0]]}"')
            seen.add(pair[0])
            continue
        out.append(line)
    out += [f'{k}="{set_[k]}"' for k in roles.ROLE_OVERRIDE_KEYS if k in set_ and k not in seen]
    return "".join(line + "\n" for line in out)


def validate_override(paths, role: str, text: str, where: str) -> None:
    """roles.validate_role (what `factory start` runs) on the role with text as its override,
    in a throwaway config folder, so the real file is untouched until it passes."""
    shadow = tempfile.mkdtemp(prefix="factory-cu-")
    try:
        os.mkdir(os.path.join(shadow, "roles"), 0o700)
        with open(os.path.join(shadow, "roles", role + ".conf"), "wb") as f:
            f.write(text.encode("utf-8"))
        try:
            roles.validate_role(Paths(paths.root, shadow, paths.state), role)
        except FactoryError as e:
            message = str(e).replace(os.path.join(shadow, "roles", role + ".conf"), where)
            raise WriteRefused(400, clean(message.replace(shadow, display_path(paths.config)), 400)) from None
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def prepare_role(paths, role, set_, unset) -> Plan:
    role = running_role(paths, role)
    set_, unset = check_changes(set_, unset)
    path = roles.role_override_file(paths, role)
    where = display_path(path)
    data = current(paths.config, "roles", role + ".conf", OVERRIDE_MAX)
    try:
        old = "" if data is None else data.decode("utf-8")
    except UnicodeDecodeError:
        raise WriteRefused(409, f"{where} is not UTF-8 text: fix it by hand") from None
    new = merge(old, set_, unset)
    validate_override(paths, role, new, where)
    content = new.encode("utf-8")
    lines, truncated = diff(old, new)
    changed = content != (data or b"")
    return Plan(paths.config, "roles", role + ".conf", OVERRIDE_MAX, digest(data), content,
                {"path": where, "diff": lines, "diff_truncated": truncated, "changed": changed, "applies": APPLIES})


def notes_name(project: str, role: str) -> str:
    return f"{project}.md" if role == "" else f"{project}.{role}.md"


def notes_target(paths, project: str, role) -> tuple:
    """(role, file name) of a notes file of the bound project: role "" is the shared notes."""
    if not dashboard_setup.usable_project(project):
        raise WriteRefused(409, "the dashboard has no project to keep notes for")
    if role != "":
        role = running_role(paths, role)
    return role, notes_name(project, role)


def prepare_notes(paths, project: str, role, text) -> Plan:
    role, name = notes_target(paths, project, role)
    if not isinstance(text, str):
        raise WriteRefused(400, "text must be text")
    limit = dashboard_setup.NOTES_LIMIT
    if len(text) > limit:
        raise WriteRefused(400, f"the notes are over {limit:,} characters")
    try:
        content = text.encode("utf-8")
    except UnicodeEncodeError:
        raise WriteRefused(400, "the notes are not valid UTF-8 text") from None
    if "\0" in text:
        raise WriteRefused(400, "the notes hold a NUL character")
    path = os.path.join(paths.config, "projects", name)
    where = display_path(path)
    data = current(paths.config, "projects", name, dashboard_setup.NOTES_READ_MAX)
    running = roles.list_roles(paths)[:dashboard_setup.MAX_ROLES]
    _, before = dashboard_setup._notes(paths, project, running)

    def chars(p):
        return len(text) if p == path else dashboard_setup._read_chars(p)

    _, after = dashboard_setup._notes(paths, project, running, chars=chars)
    for r, total in sorted(after.items()):
        # Over the limit and growing: the bridge would refuse every task for it. A change that
        # shrinks a total already over the limit is let through (it is the way back under).
        if total > limit and total > before.get(r, 0):
            raise WriteRefused(409, f"the {clean(r, 40)}'s instructions and project notes would come to "
                                    f"{total:,} characters, over the {limit:,} limit")
    old = "" if data is None else data.decode("utf-8", "replace")
    lines, truncated = diff(old, text)
    changed = content != (data or b"")
    return Plan(paths.config, "projects", name, dashboard_setup.NOTES_READ_MAX, digest(data), content,
                {"path": where, "diff": lines, "diff_truncated": truncated, "changed": changed, "applies": APPLIES,
                 "totals": {clean(r, 40): n for r, n in after.items()}, "limit": limit})


def read_notes(paths, project: str, role) -> dict:
    """{"text", "chars", "exists"} of a notes file of the bound project ("" and false if absent),
    read to NOTES_READ_MAX bytes at most."""
    role, name = notes_target(paths, project, role)
    data = current(paths.config, "projects", name, dashboard_setup.NOTES_READ_MAX, cut=True)
    text = "" if data is None else data.decode("utf-8", "replace")
    return {"text": text, "chars": len(text), "exists": data is not None}
