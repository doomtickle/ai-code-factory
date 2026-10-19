#!/usr/bin/env python3
"""factory watch: a live terminal view of every lane's transcript.

One pane per active lane (a job running, or finished in the last ten minutes)
in a grid that fits the terminal; idle lanes take one status line each at the
bottom, and "a" shows every lane. Each pane's title shows the
lane, its current job's status, elapsed time, model and cost; the body shows the
transcript rendered for reading: task lines marked, tool calls condensed to one
line ("$ <command>", "read <path>", "broker <tool>(...)"), results dimmed,
errors red, branches and packets highlighted, text wrapped.

Keys: a digit focuses one lane full-screen, "a" the grid (again: every lane), "p" shows
the focused lane's latest review packet, j/k (or arrows) scroll, "q" quits.

It only reads files in the dispatch state folder: dispatch/logs/<lane>.log,
dispatch/audit.jsonl (job costs) and dispatch/packets/. No sbx, no herdr, no
network. Standard library only; runs on the macOS system Python (3.9).

  factory watch           the grid, live (curses)
  factory watch --once    one plain-text frame on stdout, for scripts and tests
"""

from __future__ import annotations

import argparse
import calendar
import collections
import json
import os
import re
import shutil
import sys
import textwrap
import time
from pathlib import Path

LINE = re.compile(r"^(\d\d:\d\d:\d\d)  (.*)$")
TOOL = re.compile(r"^🔧 (\S+): (.*)$")
START = re.compile(r"^▶ job (\S+)")
END = re.compile(r"^■ job (\S+) (\w+) after (\d+)s")
MODEL = re.compile(r"^   model: (.*)$")
BRANCH = re.compile(r"^⎇ (?:branch )?(.*)$")
PACKET = re.compile(r"^📄 review packet: factory packet (\S+)")
CONF_LANES = re.compile(r'^LANES="(\d*)"', re.M)
# A tool call's JSON arguments are cut at 140 characters in the transcript, so
# the JSON may not parse; these pull the one field worth showing out of the text.
FIELD = re.compile(r'"(command|file_path|notebook_path|query|description|pattern|prompt)": "(.*)')
MIN_PANE_WIDTH = 60
ACTIVE_SECONDS = 600  # a finished job keeps its pane this long

Entry = collections.namedtuple("Entry", "ts kind text")


# --------------------------------------------------------------------------
# Transcript lines

def short_path(path: str) -> str:
    """The last three components of a path, prefixed with "…" when it was longer."""
    path = str(path or "")
    parts = [p for p in path.split("/") if p]
    if len(parts) <= 3:
        return path
    return "…/" + "/".join(parts[-3:])


def summarize(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= 60 else text[:59] + "…"


def condense_tool(name: str, payload: str) -> str:
    """One readable line for a transcript tool call."""
    args: dict = {}
    try:
        value = json.loads(payload)
        if isinstance(value, dict):
            args = value
    except ValueError:
        m = FIELD.search(payload)
        if m:
            # Cut at the next key ('", "description": ...') and undo JSON escapes.
            value = re.split(r'",\s*"\w+":', m.group(2))[0].rstrip('"}')
            args = {m.group(1): value.replace('\\"', '"').replace("\\n", " ")}
        else:
            args = dict(re.findall(r'"(\w+)": "([^"]*)', payload))  # whatever pairs survived the cut
    if name == "Bash":
        return "$ " + " ".join(str(args.get("command", payload)).split())
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = args.get("file_path") or args.get("notebook_path") or payload
        return f"{name.lower()} {short_path(path)}"
    if name in ("Glob", "Grep"):
        where = short_path(args.get("path", ""))
        return f"{name.lower()} {args.get('pattern', payload)}" + (f" in {where}" if where else "")
    if name.startswith("mcp__"):
        tool = name.rsplit("__", 1)[-1]
        inner = ", ".join(f"{k}={summarize(v)}" for k, v in args.items()) if args else summarize(payload)
        return f"broker {tool}({inner})"
    if name == "ToolSearch":
        return f"tool search {args.get('query', payload)}"
    if name == "Agent":
        return f"subagent: {args.get('description') or summarize(args.get('prompt', payload))}"
    if name in ("WebSearch", "WebFetch"):
        return f"{name.lower()} {args.get('query') or args.get('url') or payload}"
    return f"{name} {summarize(payload)}"


def parse_line(line: str):
    """A transcript line as an Entry (ts, kind, text), or None for separators."""
    m = LINE.match(line.rstrip("\n"))
    if not m:
        return None
    ts, body = m.groups()
    if body.startswith("─"):
        return None
    m2 = START.match(body)
    if m2:
        return Entry(ts, "start", m2.group(1))
    m2 = END.match(body)
    if m2:
        return Entry(ts, "end", f"{m2.group(2)} {m2.group(3)}")
    m2 = TOOL.match(body)
    if m2:
        return Entry(ts, "tool", condense_tool(m2.group(1), m2.group(2)))
    m2 = BRANCH.match(body)
    if m2:
        return Entry(ts, "branch", m2.group(1))
    m2 = PACKET.match(body)
    if m2:
        return Entry(ts, "packet", m2.group(1))
    m2 = MODEL.match(body)
    if m2:
        return Entry(ts, "model", m2.group(1).strip())
    if body.startswith("✗ "):
        return Entry(ts, "error", body[2:])
    if body.startswith("   > ") or body == "   >":
        return Entry(ts, "task", body[5:])
    if body.startswith("   ↳ ") or body == "   ↳":
        return Entry(ts, "result", body[5:])
    if body.startswith("   ✗ ") or body == "   ✗":
        return Entry(ts, "error", body[5:])
    if body.startswith("   "):
        return Entry(ts, "text", body[3:])
    return Entry(ts, "text", body)


def seconds_of_day(ts: str) -> int:
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def since_today(ts: str, now: float | None = None) -> int:
    """Seconds from a transcript time of day (HH:MM:SS, local) to now, modulo a day."""
    now = time.time() if now is None else now
    t = time.localtime(now)
    return ((t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) - seconds_of_day(ts)) % 86400


def fmt_secs(n: int) -> str:
    n = max(0, int(n))
    return f"{n // 60}:{n % 60:02d}" if n < 3600 else f"{n // 3600}h{(n % 3600) // 60:02d}"


# --------------------------------------------------------------------------
# State

class Tail:
    """Incremental reader of a growing text file; starts over if it shrinks."""

    def __init__(self, path: Path):
        self.path, self.pos, self.buf = path, 0, b""

    def lines(self) -> list:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.pos:
            self.pos, self.buf = 0, b""
            self.reset()
        if size == self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            self.buf += f.read(size - self.pos)
            self.pos = size
        parts = self.buf.split(b"\n")
        self.buf = parts.pop()
        return [p.decode("utf-8", "replace") for p in parts]

    def reset(self) -> None:
        pass


class Lane(Tail):
    def __init__(self, name: str, role: str, path: Path):
        super().__init__(path)
        self.name, self.role = name, role
        self.entries: collections.deque = collections.deque(maxlen=3000)
        self.job: dict | None = None
        self.last_packet: str | None = None

    def reset(self) -> None:
        self.entries.clear()
        self.job = None

    def feed(self) -> None:
        for line in self.lines():
            self.add(line)

    def add(self, line: str) -> None:
        e = parse_line(line)
        if e is None:
            return
        if e.kind == "start":
            self.job = {"id": e.text, "started": e.ts, "status": "running", "model": None,
                        "seconds": None, "task": "", "ended": None}
        elif e.kind == "end" and self.job is not None:
            status, secs = e.text.split()
            self.job.update(status=status, seconds=int(secs), ended=e.ts)
        elif e.kind == "model":
            if self.job is not None:
                self.job["model"] = e.text
            return  # shown in the title, not the body
        elif e.kind == "task" and self.job is not None and not self.job["task"] and e.text.strip():
            self.job["task"] = e.text.strip()
        elif e.kind == "packet":
            self.last_packet = e.text
        if e.kind in ("text", "result") and not e.text.strip():
            return
        if e.kind == "text" and e.text.strip().startswith("```"):
            return  # a code fence in a report: noise on screen
        self.entries.append(e)

    def active(self, now: float | None = None) -> bool:
        """A job running, or one that ended less than ACTIVE_SECONDS ago."""
        if not self.job:
            return False
        if self.job["status"] == "running":
            return True
        if not self.job.get("ended"):
            return False
        return since_today(self.job["ended"], now) < ACTIVE_SECONDS

    def elapsed(self, now: float | None = None) -> int | None:
        if not self.job:
            return None
        if self.job["seconds"] is not None:
            return self.job["seconds"]
        return since_today(self.job["started"], now)


class Audit(Tail):
    """Job costs from dispatch/audit.jsonl's job-end records."""

    def __init__(self, path: Path):
        super().__init__(path)
        self.jobs: dict = {}

    def reset(self) -> None:
        self.jobs.clear()

    def feed(self) -> None:
        for line in self.lines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("event") == "job-end" and rec.get("job_id"):
                self.jobs[rec["job_id"]] = rec

    def totals(self, since: float) -> tuple:
        jobs = cost = 0
        for rec in self.jobs.values():
            try:
                ts = calendar.timegm(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ"))
            except (KeyError, ValueError, TypeError):
                continue
            if ts >= since:
                jobs += 1
                cost += rec.get("cost_usd") or 0
        return jobs, cost


def discover_lanes(roles_dir: Path, logs_dir: Path) -> list:
    """Every lane of every role in roles/ (in LANES order), then any other
    transcript in the logs folder (a removed role's, say)."""
    lanes = []
    for conf in sorted(roles_dir.glob("*/role.conf")):
        role = conf.parent.name
        m = CONF_LANES.search(conf.read_text(errors="replace"))
        n = int(m.group(1)) if m and m.group(1) else 1
        for k in range(1, max(1, min(n, 8)) + 1):
            name = role if k == 1 else f"{role}-lane{k}"
            lanes.append(Lane(name, role, logs_dir / f"{name}.log"))
    known = {lane.name for lane in lanes}
    for path in sorted(logs_dir.glob("*.log")):
        if path.stem not in known:
            lanes.append(Lane(path.stem, path.stem.split("-lane")[0], path))
    return lanes


class Watch:
    def __init__(self, state: Path, roles: Path, project: str):
        self.state, self.project = state, project
        self.lanes = discover_lanes(roles, state / "logs")
        self.audit = Audit(state / "audit.jsonl")
        self.focus: int | None = None  # index into lanes
        self.all = False  # grid of every lane, not only the active ones
        self.packet = False
        self.scroll = 0

    def refresh(self) -> None:
        for lane in self.lanes:
            lane.feed()
        self.audit.feed()

    def bridge_alive(self) -> bool:
        try:
            pid = int((self.state / "pid").read_text().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def packet_text(self, lane: Lane) -> str:
        job_id = lane.last_packet or (lane.job or {}).get("id")
        if not job_id:
            return "(no job on this lane yet)"
        path = self.state / "packets" / f"{job_id}.md"
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"(no packet for job {job_id} yet: it is written when the job ends)"


# --------------------------------------------------------------------------
# Rendering: rows of (text, style) segments, independent of curses

def layout(n: int, width: int, height: int) -> list:
    """(y, x, h, w) for n panes below a header line and above a footer line."""
    if n == 0:
        return []
    cols = max(1, min(n, width // MIN_PANE_WIDTH))
    rows = (n + cols - 1) // cols
    body_h = max(rows, height - 2)
    boxes = []
    for i in range(n):
        r, c = divmod(i, cols)
        y = 1 + r * body_h // rows
        y_end = 1 + (r + 1) * body_h // rows
        x = c * width // cols
        x_end = (c + 1) * width // cols
        boxes.append((y, x, max(1, y_end - y), max(1, x_end - x - (1 if c < cols - 1 else 0))))
    return boxes


def format_entry(e: Entry, width: int, compact: bool) -> list:
    """Rows of segments for one transcript entry, wrapped to width."""
    stamp = (e.ts[:5] + " ", "dim")
    indent = ("      ", "dim")
    body_w = max(10, width - 6)
    if e.kind == "start":
        return [[stamp, (f"▶ job {e.text}", "bold")]]
    if e.kind == "end":
        status, secs = e.text.split()
        return [[stamp, (f"■ {status} after {fmt_secs(int(secs))}", "ok" if status == "done" else "error")]]
    if e.kind == "packet":
        return [[stamp, (f"📄 factory packet {e.text}", "task")]]
    prefix, style, max_rows = {
        "task": ("› ", "task", 6 if compact else 40),
        "tool": ("", "tool", 2),
        "result": ("↳ ", "dim", 1),
        "error": ("✗ ", "error", 3),
        "branch": ("⎇ ", "ok", 2),
    }.get(e.kind, ("", "text", 8 if compact else 60))
    text = prefix + " ".join(e.text.split()) if e.kind != "task" else prefix + e.text
    lines = textwrap.wrap(text, width=body_w, subsequent_indent="  ") or [prefix.strip()]
    if len(lines) > max_rows:
        lines = lines[:max_rows]
        lines[-1] = lines[-1][: body_w - 1] + "…"
    rows = [[stamp, (lines[0], style)]]
    rows += [[indent, (line, style)] for line in lines[1:]]
    return rows


def job_info(lane: Lane, audit: Audit, now: float | None = None) -> tuple:
    """(glyph, style, text) summarizing the lane's current job."""
    job = lane.job
    if not job:
        return "·", "dim", "idle"
    status = job["status"]
    glyph, style = {"running": ("●", "warn"), "done": ("✓", "ok"), "failed": ("✗", "error"),
                    "cancelled": ("■", "dim")}.get(status, ("?", "dim"))
    bits = [status, fmt_secs(lane.elapsed(now) or 0)]
    if job.get("model"):
        bits.append(job["model"].replace("claude-", ""))
    rec = audit.jobs.get(job["id"]) or {}
    if rec.get("cost_usd") is not None:
        bits.append(f"${rec['cost_usd']:.2f}")
    return glyph, style, " · ".join(bits)


def status_row(lane: Lane, audit: Audit, width: int, now: float | None = None) -> list:
    """One line for a lane that has no pane: its name and its last job."""
    glyph, style, info = job_info(lane, audit, now)
    if lane.job and lane.job.get("ended"):
        info += f" · at {lane.job['ended'][:5]}"
    return [(f" {lane.name} ", "dim"), (f"{glyph} {info}"[: max(0, width - len(lane.name) - 2)], style)]


def pane_title(lane: Lane, audit: Audit, width: int, now: float | None = None) -> list:
    glyph, style, info = job_info(lane, audit, now)
    head = f" {lane.name} "
    tail = f" {glyph} {info} "
    fill = max(0, width - len(head) - len(tail))
    if fill == 0 and len(head) + len(tail) > width:
        tail = tail[: max(0, width - len(head) - 1)] + " "
        fill = max(0, width - len(head) - len(tail))
    return [(head, "title"), ("─" * fill, "dim"), (tail, style)]


def pane_rows(lane: Lane, audit: Audit, width: int, height: int, scroll: int = 0,
              compact: bool = True, now: float | None = None) -> list:
    rows = [pane_title(lane, audit, width, now)]
    body: list = []
    for e in reversed(lane.entries):
        body = format_entry(e, width, compact) + body
        if len(body) >= height - 1 + scroll:
            break
    body = body[: len(body) - scroll] if scroll else body
    body = body[-(height - 1):] if height > 1 else []
    while len(body) < height - 1:
        body.append([("", "text")])
    return rows + body


def header_row(watch: Watch, width: int, now: float | None = None) -> list:
    now = time.time() if now is None else now
    running = sum(1 for lane in watch.lanes if lane.job and lane.job["status"] == "running")
    jobs, cost = watch.audit.totals(now - 86400)
    bridge = "bridge up" if watch.bridge_alive() else "bridge down"
    text = (f" factory watch  {watch.project}  ·  {running} running  ·  last 24h: {jobs} jobs ${cost:.2f}"
            f"  ·  {bridge}  ·  {time.strftime('%H:%M:%S', time.localtime(now))}")
    return [(text[:width].ljust(width), "hdr")]


def footer_row(watch: Watch, width: int) -> list:
    if watch.packet:
        keys = " packet view   j/k scroll   p or a back   q quit"
    elif watch.focus is not None:
        keys = " focus   j/k scroll   p packet   a all lanes   q quit"
    else:
        keys = " 1-9 focus a lane   a " + ("active lanes only" if watch.all else "every lane") + "   p packet   q quit"
        keys += "   " + "  ".join(f"{i + 1}={lane.name}" for i, lane in enumerate(watch.lanes[:9]))
    return [(keys[:width].ljust(width), "hdr")]


def render(watch: Watch, width: int, height: int, now: float | None = None) -> list:
    """The whole screen as height rows of segments."""
    rows = [header_row(watch, width, now)]
    inner_h = max(1, height - 2)
    if watch.packet and watch.focus is not None:
        lane = watch.lanes[watch.focus]
        rows.append([(f" review packet · {lane.name} ".ljust(width, "─"), "title")])
        lines: list = []
        for raw in watch.packet_text(lane).splitlines():
            lines += textwrap.wrap(raw, width=max(10, width - 1)) or [""]
        start = max(0, len(lines) - (inner_h - 1) - watch.scroll)
        shown = lines[start: start + inner_h - 1]
        rows += [[(line, "bold" if line.startswith("#") else "text")] for line in shown]
        while len(rows) < height - 1:
            rows.append([("", "text")])
    elif watch.focus is not None:
        lane = watch.lanes[watch.focus]
        rows += pane_rows(lane, watch.audit, width, inner_h, watch.scroll, compact=False, now=now)
    else:
        shown = list(watch.lanes) if watch.all else [lane for lane in watch.lanes if lane.active(now)]
        if not shown and watch.lanes:
            # Nothing active: the lane with the most recent job gets the whole area.
            with_job = [lane for lane in watch.lanes if lane.job]
            shown = [max(with_job, key=lambda lane: lane.job["id"])] if with_job else [watch.lanes[0]]
        hidden = [lane for lane in watch.lanes if lane not in shown]
        strip = [status_row(lane, watch.audit, width, now) for lane in hidden]
        grid_h = max(1, inner_h - len(strip))
        grid = [[("", "text")] for _ in range(grid_h)]
        cells: dict = {}
        for lane, (y, x, h, w) in zip(shown, layout(len(shown), width, grid_h + 2)):
            for dy, row in enumerate(pane_rows(lane, watch.audit, w, h, now=now)):
                cells.setdefault(y - 1 + dy, []).append((x, w, row))
        for i in range(grid_h):
            if i in cells:
                grid[i] = [("__cells__", cells[i])]  # placed by the painter
        rows += grid + strip[: max(0, inner_h - grid_h)]
    rows.append(footer_row(watch, width))
    return rows[:height]


def plain(rows: list, width: int) -> str:
    """Rows as plain text (styles dropped), for --once and tests."""
    out = []
    for row in rows:
        if row and row[0][0] == "__cells__":
            line = [" "] * width
            for x, w, segs in row[0][1]:
                text = "".join(t for t, _ in segs)[:w]
                line[x: x + len(text)] = list(text)
            out.append("".join(line).rstrip())
        else:
            out.append("".join(t for t, _ in row)[:width].rstrip())
    return "\n".join(out)


# --------------------------------------------------------------------------
# curses

def run_curses(watch: Watch) -> None:
    import curses

    def main(stdscr):
        curses.curs_set(0)
        stdscr.timeout(500)
        attrs = {"text": curses.A_NORMAL, "dim": curses.A_DIM, "bold": curses.A_BOLD,
                 "title": curses.A_BOLD, "hdr": curses.A_REVERSE, "task": curses.A_BOLD,
                 "tool": curses.A_NORMAL, "ok": curses.A_BOLD, "warn": curses.A_BOLD, "error": curses.A_BOLD}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for i, color in enumerate((curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                                       curses.COLOR_RED, curses.COLOR_MAGENTA, curses.COLOR_BLUE), start=1):
                curses.init_pair(i, color, -1)
            attrs.update(task=curses.color_pair(1), tool=curses.color_pair(2), ok=curses.color_pair(3),
                         warn=curses.color_pair(2) | curses.A_BOLD, error=curses.color_pair(4) | curses.A_BOLD,
                         title=curses.color_pair(6) | curses.A_BOLD)

        def put(y, x, text, style, limit):
            text = text[: max(0, limit)]
            if not text:
                return
            try:
                stdscr.addstr(y, x, text, attrs.get(style, curses.A_NORMAL))
            except curses.error:
                pass  # the bottom-right cell, or a cut-off wide character

        while True:
            watch.refresh()
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            for y, row in enumerate(render(watch, width, height)):
                if row and row[0][0] == "__cells__":
                    for x, w, segs in row[0][1]:
                        cx = x
                        for text, style in segs:
                            put(y, cx, text, style, x + w - cx)
                            cx += len(text)
                else:
                    cx = 0
                    for text, style in row:
                        put(y, cx, text, style, width - cx)
                        cx += len(text)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), 27) and not watch.packet:
                return
            if key in (ord("q"), 27, ord("p"), ord("a")) and watch.packet:
                watch.packet, watch.scroll = False, 0
            elif ord("1") <= key <= ord("9") and key - ord("1") < len(watch.lanes):
                watch.focus, watch.packet, watch.scroll = key - ord("1"), False, 0
            elif key == ord("a"):
                if watch.focus is None:
                    watch.all = not watch.all  # already on the grid: toggle every lane / active only
                watch.focus, watch.scroll = None, 0
            elif key == ord("p"):
                if watch.focus is None and watch.lanes:
                    watch.focus = 0
                watch.packet, watch.scroll = True, 0
            elif key in (ord("k"), curses.KEY_UP):
                watch.scroll += 3
            elif key in (ord("j"), curses.KEY_DOWN):
                watch.scroll = max(0, watch.scroll - 3)
            elif key == curses.KEY_RESIZE:
                pass

    curses.wrapper(main)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live terminal view of every lane's transcript.")
    ap.add_argument("--state", required=True, help="the dispatch state folder")
    ap.add_argument("--roles", required=True, help="the factory's roles/ folder")
    ap.add_argument("--project", default="", help="the project the factory runs on (for the header)")
    ap.add_argument("--once", action="store_true", help="print one plain-text frame and exit")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    args = ap.parse_args(argv)
    watch = Watch(Path(args.state), Path(args.roles), args.project or "(no project)")
    if not watch.lanes:
        print("no roles or transcripts found", file=sys.stderr)
        return 1
    if args.once or not sys.stdout.isatty():
        size = shutil.get_terminal_size((120, 40))
        watch.refresh()
        print(plain(render(watch, args.width or size.columns, args.height or size.lines),
                    args.width or size.columns))
        return 0
    run_curses(watch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
