"""FACTORY_CONFIG/dashboard.toml: the dashboard's cosmetic settings (doomtickle/passdesk-ops#196).

Read once when `factory dashboard` starts and served to the page in /api/state as `look`. Display
only: nothing here is executed, reaches a command or moves a security boundary.

    plant = "LOIS WORKS"       # the nameplate and the floor's plate
    shifts = ["day", "swing", "night"]
    crt = 3                    # the CRT knob's default, 0-10, until the browser remembers its own
    sound = false              # the SOUND key's default, until the browser remembers its own
    [roles.colors]
    designer = "#d08cff"       # a role's hue on the floor and the modules

The reader is strict: an unknown key or table, a bad value, a repeated key, a symlink, a file that
is not a regular one, one over MAX_BYTES, or text that is not UTF-8 refuses the whole file, and the
dashboard then shows today's defaults and says why (its log, and the SETUP screen). Strings are one
line of printable ASCII, "basic" without escapes or 'literal', read by factory.policy.toml_string.
Values are one line each (the shifts array too). A colour is `#rrggbb` and is passed on in lower case.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package

from factory import policy, roles  # noqa: E402

FILE = "dashboard.toml"
MAX_BYTES = 16 * 1024
MAX_PLANT = 24
MAX_SHIFT = 16
SHIFTS = 3
MAX_COLORS = 32
CRT_RANGE = (0, 10)
TOP_KEYS = ("plant", "shifts", "crt", "sound")
COLORS_TABLE = "roles.colors"

_PRINTABLE = re.compile(r"[\x20-\x7e]*")
_HEADER = re.compile(r"\[[ \t]*([^\]]*?)[ \t]*\][ \t]*(#.*)?")
_KEY_LINE = re.compile(r"([A-Za-z0-9_-]+)[ \t]*=[ \t]*(.*)")
_INT = re.compile(r"(0|[1-9][0-9]*)[ \t]*(#.*)?")
_BOOL = re.compile(r"(true|false)[ \t]*(#.*)?")
_STRING_ITEM = re.compile(r"(\"[^\"\\\\]*\"|'[^']*')")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


class Refused(ValueError):
    """The file can't be used; the message names the file, the line and the key."""


def defaults() -> dict:
    """What the page shows when the file is absent or refused: nothing set, so its own defaults."""
    return {"plant": None, "shifts": None, "crt": None, "sound": None, "colors": {}}


def _text(raw: str, key: str, cap: int) -> str:
    try:
        value = policy.toml_string(raw)
    except ValueError:
        raise Refused(f'{key} must be a one-line string in "double" or \'single\' quotes (no escapes)') from None
    if not value.strip() or len(value) > cap or not _PRINTABLE.fullmatch(value):
        raise Refused(f"{key} must be 1 to {cap} printable ASCII characters, not only spaces")
    return value


def _shifts(raw: str) -> list:
    shape = 'shifts must be a one-line array of three strings, like ["day", "swing", "night"]'
    if raw[:1] != "[":
        raise Refused(shape)
    rest, items = raw[1:], []
    while True:
        rest = rest.lstrip(" \t")
        if rest[:1] == "]":
            rest = rest[1:]
            break
        item = _STRING_ITEM.match(rest)
        if item is None:
            raise Refused(shape)
        items.append(_text(item.group(1), "shifts", MAX_SHIFT))
        rest = rest[item.end():].lstrip(" \t")
        if rest[:1] == ",":
            rest = rest[1:]
        elif rest[:1] != "]":
            raise Refused(shape)
    if rest.strip(" \t") and not rest.lstrip(" \t").startswith("#"):
        raise Refused(shape)
    if len(items) != SHIFTS:
        raise Refused(f"shifts must name exactly {SHIFTS} shifts (day, swing, night), not {len(items)}")
    return items


def _crt(raw: str) -> int:
    m = _INT.fullmatch(raw)
    if m is None or not CRT_RANGE[0] <= int(m.group(1)) <= CRT_RANGE[1]:
        raise Refused(f"crt must be a whole number from {CRT_RANGE[0]} to {CRT_RANGE[1]}")
    return int(m.group(1))


def _sound(raw: str) -> bool:
    m = _BOOL.fullmatch(raw)
    if m is None:
        raise Refused("sound must be true or false")
    return m.group(1) == "true"


def _color(raw: str, role: str) -> str:
    try:
        value = policy.toml_string(raw)
    except ValueError:
        raise Refused(f'the colour of {role} must be a quoted "#rrggbb" string') from None
    if _COLOR.fullmatch(value) is None:
        raise Refused(f'the colour of {role} must be "#rrggbb" (three hex pairs), not "{value[:20]}"')
    return value.lower()


def parse(text: str) -> dict:
    """The settings in dashboard.toml's TEXT as defaults() fills them; Refused (with the line number
    and the key) for anything the file may not say."""
    out, table, seen = defaults(), "", set()
    for number, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith("#"):
            continue

        def refuse(why: str, number=number):
            raise Refused(f"line {number}: {why}")

        if stripped.startswith("["):
            m = _HEADER.fullmatch(stripped)
            name = m.group(1) if m else ""
            if name != COLORS_TABLE:
                refuse(f"unknown table {('[' + name + ']') if m else stripped[:20]} (the only table is [{COLORS_TABLE}])")
            if table:
                refuse(f"[{COLORS_TABLE}] appears twice")
            table = name
            continue
        m = _KEY_LINE.fullmatch(stripped)
        if m is None:
            refuse("expected key = value")
        key, raw = m.group(1), m.group(2).strip(" \t")
        where = (table, key)
        if where in seen:
            refuse(f"{key} is set twice")
        seen.add(where)
        try:
            if table == COLORS_TABLE:
                if not roles.valid_role_name(key):
                    refuse(f'"{key}" under [{COLORS_TABLE}] is not a role name '
                           "(lowercase letters, digits and dashes, 2 to 31 characters, starting with a letter)")
                if len(out["colors"]) >= MAX_COLORS:
                    refuse(f"more than {MAX_COLORS} role colours")
                out["colors"][key] = _color(raw, key)
            elif key == "plant":
                out["plant"] = _text(raw, key, MAX_PLANT)
            elif key == "shifts":
                out["shifts"] = _shifts(raw)
            elif key == "crt":
                out["crt"] = _crt(raw)
            elif key == "sound":
                out["sound"] = _sound(raw)
            else:
                refuse(f'unknown key "{key}" (known: {", ".join(TOP_KEYS)}, and [{COLORS_TABLE}])')
        except Refused as e:
            if str(e).startswith("line "):
                raise
            refuse(str(e))
    return out


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
    """(settings, problem) from CONFIG_DIR/dashboard.toml. An absent file, or no folder (None), is
    today's defaults and no problem; a refused file is the defaults and a one-line problem naming it."""
    if not config_dir:
        return defaults(), ""
    path = os.path.join(str(config_dir), FILE)
    try:
        text = _read(path)
        return (defaults() if text is None else parse(text)), ""
    except Refused as e:
        return defaults(), f"{path}: {e}"
