#!/usr/bin/env python3
"""factory usage: your Claude plan's session (5-hour) and weekly (7-day) usage,
for the dashboard.

Claude Code hands its status line command a JSON object on stdin; for Pro and
Max subscribers it includes `rate_limits.five_hour` and `rate_limits.seven_day`
(`used_percentage`, `resets_at` in epoch seconds). That is the only supported
place these numbers appear (https://code.claude.com/docs/en/statusline). This
script is that status line command: it copies those four numbers, and nothing
else, to <state>/usage.json for `factory dashboard`, then prints a short status
line. It never reads or sends a credential and makes no network connection.

If you already had a status line, `enable` saves it and this script runs it
after recording the numbers, with the same stdin, and prints its output instead
of its own. `disable` puts it back.

The numbers refresh only while a Claude Code session runs on this machine (the
host): sessions inside the factory's sandboxes have no way to write here, by
design.

  usage-statusline.py --state DIR [--config DIR]          status line mode (stdin JSON)
  usage-statusline.py enable --settings FILE --state DIR --config DIR --python EXE
  usage-statusline.py disable --settings FILE --config DIR
  usage-statusline.py show --state DIR

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

MARK = "usage-statusline.py"   # how `enable` recognizes its own status line
PREVIOUS = "statusline-previous.json"
WINDOWS = ("five_hour", "seven_day")


def window(raw) -> dict | None:
    """{used_percentage, resets_at} with finite numbers, or None."""
    if not isinstance(raw, dict):
        return None
    pct, resets = raw.get("used_percentage"), raw.get("resets_at")
    ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (pct, resets))
    return {"used_percentage": float(pct), "resets_at": float(resets)} if ok else None


def extract(payload) -> dict | None:
    """The usage record to store from a status line payload, or None when it has none."""
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    out = {name: window(limits.get(name)) for name in WINDOWS}
    if not any(out.values()):
        return None
    out["updated"] = time.time()
    return out


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def summary(record: dict | None) -> str:
    if not record:
        return ""
    bits = []
    for name, label in (("five_hour", "session"), ("seven_day", "week")):
        w = record.get(name)
        if w:
            bits.append(f"{label} {w['used_percentage']:.0f}%")
    return " · ".join(bits)


def statusline(state: Path, config: Path | None) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    record = extract(payload)
    if record:
        try:
            write_atomic(state / "usage.json", json.dumps(record) + "\n")
        except OSError:
            pass  # a status line must never fail because the state folder is missing
    previous = load_previous(config) if config else None
    if previous and previous.get("type") == "command" and previous.get("command"):
        # Your own earlier status line, run the way Claude Code runs one: through
        # the shell, with the same JSON on stdin.
        try:
            done = subprocess.run(previous["command"], shell=True, input=raw, capture_output=True,
                                  text=True, timeout=10)
            sys.stdout.write(done.stdout)
            return 0
        except (OSError, subprocess.SubprocessError):
            pass
    print(summary(record))
    return 0


# --------------------------------------------------------------------------
# enable / disable: edit Claude Code's user settings file

def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def load_previous(config: Path):
    try:
        data = json.loads((config / PREVIOUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("statusLine") if isinstance(data, dict) else None


def is_ours(status) -> bool:
    return isinstance(status, dict) and MARK in str(status.get("command", ""))


def save_settings(path: Path, data: dict) -> None:
    if path.exists():
        backup = path.with_name(path.name + ".factory-backup")
        backup.write_bytes(path.read_bytes())
    write_atomic(path, json.dumps(data, indent=2) + "\n")


def enable(settings: Path, state: Path, config: Path, python: str) -> int:
    try:
        data = load_settings(settings)
    except ValueError as e:
        print(f"can't read {settings}: {e}; fix it and run this again", file=sys.stderr)
        return 1
    current = data.get("statusLine")
    script = Path(__file__).resolve()
    command = " ".join(shlex.quote(str(x)) for x in (python, script, "--state", state, "--config", config))
    if not is_ours(current):
        # Keep whatever was there (possibly nothing) so disable can restore it and
        # the status line keeps showing it meanwhile.
        write_atomic(config / PREVIOUS, json.dumps({"statusLine": current}, indent=2) + "\n")
    new = {"type": "command", "command": command}
    if isinstance(current, dict) and not is_ours(current) and "padding" in current:
        new["padding"] = current["padding"]
    if current == new:
        print(f"Already enabled in {settings}.")
        return 0
    data["statusLine"] = new
    save_settings(settings, data)
    chained = isinstance(current, dict) and not is_ours(current)
    print(f"Enabled in {settings}" + (" (your previous status line still shows; it runs after this one)." if chained else "."))
    print("Your plan usage appears on the dashboard after the next reply in a Claude Code session on this machine.")
    return 0


def disable(settings: Path, config: Path) -> int:
    try:
        data = load_settings(settings)
    except ValueError as e:
        print(f"can't read {settings}: {e}; fix it and run this again", file=sys.stderr)
        return 1
    if not is_ours(data.get("statusLine")):
        print("Not enabled: the status line in {} isn't the factory's; nothing changed.".format(settings))
        return 0
    previous = load_previous(config)
    if previous is None:
        data.pop("statusLine", None)
    else:
        data["statusLine"] = previous
    save_settings(settings, data)
    try:
        (config / PREVIOUS).unlink()
    except OSError:
        pass
    print(f"Disabled in {settings}" + ("; your previous status line is back." if previous is not None else "."))
    return 0


def show(state: Path) -> int:
    try:
        record = json.loads((state / "usage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("No usage recorded yet. Run 'factory usage enable', then use Claude Code on this machine.")
        return 0
    now = time.time()
    for name, label in (("five_hour", "Session (5 h)"), ("seven_day", "Week (7 d)")):
        w = window(record.get(name))
        if not w or w["resets_at"] <= now:
            print(f"{label:<14} unknown")
            continue
        left = int(w["resets_at"] - now)
        print(f"{label:<14} {w['used_percentage']:5.1f}%  resets in {left // 3600}h{(left % 3600) // 60:02d}m")
    updated = record.get("updated")
    if isinstance(updated, (int, float)):
        print(f"as of {time.strftime('%H:%M', time.localtime(updated))}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("enable", "disable", "show"):
        ap = argparse.ArgumentParser(prog=f"factory usage {argv[0]}")
        if argv[0] in ("enable", "disable"):
            ap.add_argument("--settings", required=True, type=Path)
            ap.add_argument("--config", required=True, type=Path)
        if argv[0] in ("enable", "show"):
            ap.add_argument("--state", required=True, type=Path)
        if argv[0] == "enable":
            ap.add_argument("--python", required=True)
        args = ap.parse_args(argv[1:])
        if argv[0] == "enable":
            return enable(args.settings, args.state, args.config, args.python)
        if argv[0] == "disable":
            return disable(args.settings, args.config)
        return show(args.state)
    ap = argparse.ArgumentParser(description="Claude Code status line command: records plan usage for the dashboard.")
    ap.add_argument("--state", required=True, type=Path, help="the factory state folder")
    ap.add_argument("--config", type=Path, default=None, help="the factory config folder (for a chained status line)")
    args = ap.parse_args(argv)
    return statusline(args.state, args.config)


if __name__ == "__main__":
    sys.exit(main())
