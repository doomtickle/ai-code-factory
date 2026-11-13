"""FACTORY_CONFIG/dashboard.toml: the dashboard's cosmetic settings (doomtickle/passdesk-ops#196).

Read once when `factory dashboard` starts and served to the page in /api/state as `look`. Display
only: nothing here is executed, reaches a command or moves a security boundary.

    plant = "LOIS WORKS"       # the nameplate

The keys of the retired classic view (shifts, crt, sound, ui, and the [roles.colors] table) are still
accepted, so an old file starts the dashboard, but their values are not read and do nothing; load()
names them in a one-line notice.

The reader is strict about the rest: an unknown key or table, a bad `plant`, a repeated key, a
symlink, a file that is not a regular one, one over MAX_BYTES, or text that is not UTF-8 refuses the
whole file, and the dashboard then shows today's defaults and says why (its log, and the SETUP
screen). Strings are one line of printable ASCII, "basic" without escapes or 'literal', read by
factory.policy.toml_string.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package

from factory import policy  # noqa: E402

FILE = "dashboard.toml"
MAX_BYTES = 16 * 1024
MAX_PLANT = 24
RETIRED_KEYS = ("shifts", "crt", "sound", "ui")   # read by the classic view only: accepted, ignored
TOP_KEYS = ("plant",) + RETIRED_KEYS
RETIRED_TABLE = "roles.colors"                    # ditto, and everything under it

_PRINTABLE = re.compile(r"[\x20-\x7e]*")
_HEADER = re.compile(r"\[([^\]]*)\](.*)")
_KEY_LINE = re.compile(r"([A-Za-z0-9_-]+)[ \t]*=[ \t]*(.*)")


class Refused(ValueError):
    """The file can't be used; the message names the file, the line and the key."""


def defaults() -> dict:
    """What the page shows when the file is absent or refused: nothing set, so its own defaults."""
    return {"plant": None}


def _text(raw: str, key: str, cap: int) -> str:
    try:
        value = policy.toml_string(raw)
    except ValueError:
        raise Refused(f'{key} must be a one-line string in "double" or \'single\' quotes (no escapes)') from None
    if not value.strip() or len(value) > cap or not _PRINTABLE.fullmatch(value):
        raise Refused(f"{key} must be 1 to {cap} printable ASCII characters, not only spaces")
    return value


def parse(text: str) -> tuple:
    """(the settings in dashboard.toml's TEXT as defaults() fills them, the retired keys it set);
    Refused (with the line number and the key) for anything the file may not say."""
    out, table, seen, retired = defaults(), "", set(), []
    for number, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith("#"):
            continue

        def refuse(why: str, number=number):
            raise Refused(f"line {number}: {why}")

        if stripped.startswith("["):
            m = _HEADER.fullmatch(stripped)
            if m and m.group(2).strip(" \t") and not m.group(2).lstrip(" \t").startswith("#"):
                m = None
            name = m.group(1).strip(" \t") if m else ""
            if name != RETIRED_TABLE:
                refuse(f"unknown table {('[' + name + ']') if m else stripped[:20]} (no tables are read any more)")
            if table:
                refuse(f"[{RETIRED_TABLE}] appears twice")
            table = name
            retired.append(f"[{name}]")
            continue
        m = _KEY_LINE.fullmatch(stripped)
        if m is None:
            refuse("expected key = value")
        key, raw = m.group(1), m.group(2).strip(" \t")
        where = (table, key)
        if where in seen:
            refuse(f"{key} is set twice")
        seen.add(where)
        if table == RETIRED_TABLE:
            continue
        if key == "plant":
            try:
                out["plant"] = _text(raw, key, MAX_PLANT)
            except Refused as e:
                refuse(str(e))
        elif key in RETIRED_KEYS:
            retired.append(key)
        else:
            refuse(f'unknown key "{key}" (known: {", ".join(TOP_KEYS)})')
    return out, retired


def _read(path: str) -> str | None:
    """The file's text, None when it is absent; Refused for a symlink, a non-regular file, one over
    MAX_BYTES or text that isn't UTF-8."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Refused(f"can't be read ({e.strerror or type(e).__name__})") from None
    if not stat.S_ISREG(before.st_mode):
        raise Refused("is a symlink or not a regular file, which is refused")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as e:
        raise Refused(f"can't be opened ({e.strerror or type(e).__name__})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused("is not a regular file")
        data = b""
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as e:
        raise Refused(f"can't be read ({e.strerror or type(e).__name__})") from None
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise Refused(f"is over {MAX_BYTES // 1024} KiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("is not UTF-8 text") from None


def load(config_dir) -> tuple:
    """(settings, problem, notice) from CONFIG_DIR/dashboard.toml. An absent file, or no folder (None), is
    today's defaults and no problem; a refused file is the defaults and a one-line problem naming it.
    The notice is one line naming any retired keys the file still sets (they do nothing), else ''."""
    if not config_dir:
        return defaults(), "", ""
    path = os.path.join(str(config_dir), FILE)
    try:
        text = _read(path)
        if text is None:
            return defaults(), "", ""
        out, retired = parse(text)
    except Refused as e:
        return defaults(), f"{path}: {e}", ""
    notice = (f"{path}: {', '.join(retired)} no longer do anything (the Thoughtfield is the only view); "
              "you can delete them") if retired else ""
    return out, "", notice
