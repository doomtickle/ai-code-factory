#!/usr/bin/env python3
"""factory dashboard: the whole factory on one screen, in the browser.

Serves one page (scripts/dashboard-ui/, the "FC-1984" design) and a JSON
state endpoint the page polls every two seconds: every lane's live transcript
tail, job status, model and cost; today's spend by hour and by role; an event
printer of broker calls and finished jobs; and the writes the gateway is
holding for approval.

The state view is read-only. To build it, it reads files under the
factory state folder and makes no network connection:
  dispatch/logs/<lane>.log, dispatch/audit.jsonl, dispatch/packets/, dispatch/pid
  broker/audit.jsonl, broker/approvals/pending/, broker/project
It reuses watch.py's transcript model and approvals.py's card reader rather
than parsing either command's output. The main page only displays: nothing
it can reach acts.

The MANUAL key opens the factory's own docs at /docs/: README.md,
docs/setup-wsl.md and docs/guides/*.md from this checkout, rendered once at
start-up by docs.py (build_served: separate CSS and JS, so the page runs under
the same no-inline CSP; no git, no file:// links) and held in memory. Like
the main page, it only displays and needs this run's cookie.

Every action lives in the control unit (dashboard_controls.py), a page on
the second origin the main page frames, with its own key. What it writes and
runs, and nothing else:
  - APPROVE / DECLINE: one decision file, broker/approvals/decisions/<id>.json,
    through approvals.write_decision() (what `factory approvals approve` writes).
  - RESTART / UPDATE, each only after a confirm step (a prepare request, then a
    run request with its single-use token): the fixed argv `<factory> restart`
    or `<factory> update --yes` (no shell; <factory> is --factory, by default
    this repo's bin/factory), its output in dashboard/actions/<time>-<action>.log.
    Preparing UPDATE runs `<factory> update --plan` (read-only but for a
    hook-free `git fetch` of the factory checkout) to show what it would do.
  - DIAG, after the same confirm step: the fixed argv `<factory> doctor`
    (read-only), its output logged the same way. Terminal 0 stays attached.

With --coordinator NAME it also serves Terminal 0, the Coordinator's live
terminal (dashboard_term.py): one fixed argv, `sbx run --name NAME`, in a PTY
(no shell), started only when a page asks (POWER ON), refused while the
Coordinator is attached anywhere else, and shared by every open tab. It lives
on a second port (its own origin) the main page frames; the terminal page is
served only to a same-site iframe load (Sec-Fetch-Dest/Sec-Fetch-Site), and a
WebSocket there needs the right Host and Origin, this run's cookie and the
terminal key that only the terminal page holds. Besides that, the checks before starting it run
`ps -axo pid=,ppid=,args=` and `sbx ls -q` (fixed argv).

Besides those and the control unit's commands above, the one process it
starts is the browser opener, once, at start-up (unless --no-open): on WSL `rundll32.exe url.dll,FileProtocolHandler <url>` (Windows'
URL protocol handler; WSL passes the argv through verbatim, no shell), else
`wslview <url>`, else Python's webbrowser module. Never explorer.exe, which
reads its argument as a folder and drops the query (and with it the token),
and never cmd.exe or `powershell -Command`, which would re-parse the URL. The
token therefore appears briefly in the opener's argv and once in the
browser's history; on a single-user machine that is acceptable, since the
token dies with this process.

The server binds 127.0.0.1 only. Every request needs the per-run token: the
URL printed at start-up carries it once, the server swaps it for an HttpOnly,
SameSite=Strict cookie named after the port (factory_dashboard_<port>, so two
dashboards don't overwrite each other's), and any request without exactly one
matching cookie gets 403. The Host header must name this server (a
DNS-rebinding page can't borrow the cookie), only GET is served on the main
port, and the page runs under a CSP that allows nothing but its own files. Agent-written text
(transcripts, packets, cards, audit arguments) is only ever inserted into the
page as text, never as HTML.

Limitation: cookies ignore ports, so any other server on 127.0.0.1 that this
browser visits receives the cookie too. That is tolerable for what the page
only shows; anything that lets a page act (the terminal; later approve and
decline) must require a key in a request header, not just the cookie, and
check `Origin`: the terminal's WebSocket and the control unit's POSTs do both.

  factory dashboard              serve and open the page
  factory dashboard --no-open    serve and print the URL only
  factory dashboard --coordinator factory-coordinator
                                 ... with the Coordinator's terminal
  factory dashboard --factory PATH
                                 RESTART and UPDATE run PATH instead of bin/factory
  factory dashboard --demo       made-up data from dashboard-demo/ in a temp folder, nothing acts
                                 (no --state, --coordinator, --project or --factory; see dashboard_demo.py)

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import argparse
import calendar
import collections
import hmac
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import approvals
import dashboard_controls as dc
import dashboard_demo as demo
import dashboard_term as dt
import docs
import watch as w

UI = Path(__file__).resolve().parent / "dashboard-ui"
DOCS_ROOT = Path(__file__).resolve().parent.parent  # the factory checkout
# The only files the server sends: path -> (file under UI, content type). No path
# from a request is ever joined onto the filesystem.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/theme.css": ("theme.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/floor.css": ("floor.css", "text/css; charset=utf-8"),
    "/floor.js": ("floor.js", "text/javascript; charset=utf-8"),
    "/sound.js": ("sound.js", "text/javascript; charset=utf-8"),
    "/fonts/vt323.woff2": ("fonts/vt323.woff2", "font/woff2"),
    "/fonts/barlow-condensed-600.woff2": ("fonts/barlow-condensed-600.woff2", "font/woff2"),
    "/fonts/barlow-condensed-700.woff2": ("fonts/barlow-condensed-700.woff2", "font/woff2"),
    "/fonts/dotgothic16.woff2": ("fonts/dotgothic16.woff2", "font/woff2"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def page_csp(term_port: int | None = None) -> str:
    """The main page's CSP; with a terminal, it may frame the terminal origin and nothing else."""
    if term_port is None:
        return CSP
    return CSP + f"; frame-src http://127.0.0.1:{term_port} http://localhost:{term_port}"
# The docs (MANUAL): path -> (file name in the build_served() snapshot, content type).
DOCS = {
    "/docs/": ("index.html", "text/html; charset=utf-8"),
    "/docs/docs.css": ("docs.css", "text/css; charset=utf-8"),
    "/docs/docs-head.js": ("docs-head.js", "text/javascript; charset=utf-8"),
    "/docs/docs.js": ("docs.js", "text/javascript; charset=utf-8"),
}


def load_docs(root: Path = DOCS_ROOT):
    """The docs as served at /docs/ ({file name: bytes}), or why they couldn't be
    built (a string): the dashboard runs without them then."""
    try:
        files, _warnings = docs.build_served(root)
    except (docs.DocsError, OSError, UnicodeDecodeError) as e:
        return f"the docs could not be built: {e}"
    return {name: text.encode("utf-8") for name, text in files.items()}


COOKIE = "factory_dashboard"  # prefix: the cookie is COOKIE_<port> (see cookie_name)
TAIL = 40          # transcript lines per lane in /api/state (taller lane screens)
FULL = 1500        # transcript lines per lane in /api/lane
EVENTS = 80        # event printer lines
WRITES_TTL = 4     # seconds between reads of approvals/done/ for the deferred-write list
VERDICT = re.compile(r"^- Verdict: (\S+)", re.M)
DESK_MAX = 18      # characters of a lane's desk placard
TOPIC = re.compile(r"(?:(?<=\.\.)|(?<=\borigin/)|(?<![\w./-]))(?:factory-[A-Za-z0-9-]+/)?factory/([A-Za-z0-9][A-Za-z0-9._-]*)")
DEV_REF = re.compile(r"(?<![\w-])factory-(developer(?:-lane[0-9]+)?)/factory/")
TASK_LABEL = re.compile(r"^(?:(?:repo|repository|project)\s*:\s*[^\s;]*[^\s.;][.;]?\s*|(?:task|goal)\s*:\s*)", re.IGNORECASE)


# --------------------------------------------------------------------------
# Time

def utc_ts(ts) -> float | None:
    """An audit log timestamp (2026-10-19T06:15:55Z) as epoch seconds."""
    try:
        return float(calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def local_midnight(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def clock(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def num(value) -> float | None:
    """A finite number from a JSON record, or None for anything else (a string,
    null, a bool, NaN): one malformed record must not break every /api/state."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def desk_label(brief: str) -> str:
    """A few words (at most DESK_MAX characters) for what a job is about: the
    topic of the first factory/<topic> branch the task names, else the first
    words of its first non-empty line without a leading "Repo: x." label. One
    clean line: only printable characters, no markup, "" when nothing is left."""
    text = str(brief or "")
    m = TOPIC.search(text)
    if m:
        words = re.sub(r"[-_.]+", " ", m.group(1)).split()
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


# --------------------------------------------------------------------------
# Broker audit log -> event printer lines

class BrokerAudit(w.Tail):
    """Tool calls from broker/audit.jsonl, newest last."""

    def __init__(self, path: Path):
        super().__init__(path)
        self.calls: collections.deque = collections.deque(maxlen=500)

    def reset(self) -> None:
        self.calls.clear()

    def feed(self) -> None:
        for line in self.lines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            # Keep only records the printer prints: "intent" records would crowd
            # real calls out of the 500 on a busy day.
            if isinstance(rec, dict) and rec.get("tool") and broker_event(rec) is not None:
                self.calls.append(rec)


def call_target(rec: dict) -> str:
    """The one argument worth printing for a broker call."""
    args = rec.get("args")
    if not isinstance(args, dict):
        args = {}
    repo = rec.get("repo") or args.get("repo")
    branch = rec.get("branch") or args.get("branch") or args.get("head")
    if repo:
        return f"{repo} {branch}" if branch else str(repo)
    for key in ("url", "query", "path", "requirements", "lockfile"):
        if args.get(key):
            return str(args[key])
    return ""


def broker_event(rec: dict) -> dict | None:
    """A broker audit record as a printer line, or None for records not worth one."""
    event, decision = rec.get("event"), rec.get("decision")
    kind = {"approval-requested": "HELD", "approval-granted": "OK", "approval-declined": "NO",
            "approval-queued": "QUEUED", "approval-write-done": "WRITTEN", "approval-write-failed": "FAILED"}.get(event)
    if kind is None and decision in ("allow", "deny"):
        kind = "ALLOW" if decision == "allow" else "DENY"
    if kind is None:
        # An "intent" record (its allow/deny record follows), or the call that
        # was queued (decision "queued"; its approval-queued event is the line).
        return None
    who = rec.get("role") or rec.get("principal") or ""
    bits = [str(rec["tool"]), str(who), call_target(rec)]
    if kind == "DENY" and rec.get("reason"):
        bits.append(f"· {rec['reason']}")
    if kind in ("OK", "NO") and rec.get("approver"):
        bits.append(f"· by {rec['approver']}")
    if kind == "FAILED" and rec.get("reason"):
        bits.append("· " + approvals.clean(rec["reason"]))
    if kind == "WRITTEN" and rec.get("result"):
        bits.append("· " + approvals.clean(next((ln for ln in str(rec["result"]).splitlines() if ln.strip()), "")))
    return {"t": utc_ts(rec.get("ts")), "kind": kind, "red": kind in ("DENY", "HELD", "NO", "QUEUED", "FAILED"),
            "text": " ".join(b for b in bits if b)}


# --------------------------------------------------------------------------
# State

class Dashboard:
    def __init__(self, state: Path, roles: Path, project: str, demo: bool = False):
        self.state, self.project, self.demo = state, project, demo
        self.watch = w.Watch(state / "dispatch", roles, project)
        self.broker = BrokerAudit(state / "broker" / "audit.jsonl")
        self.verdicts: dict = {}
        self.lock = threading.Lock()
        self._writes: tuple = (0.0, [])

    def refresh(self) -> None:
        self.watch.refresh()
        self.broker.feed()

    def verdict(self, job_id: str) -> str | None:
        """APPROVED / REVISE from a finished job's review packet (reviewer jobs only)."""
        if job_id not in self.verdicts:
            try:
                text = (self.state / "dispatch" / "packets" / f"{job_id}.md").read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None  # not written yet; try again next poll
            m = VERDICT.search(text)
            self.verdicts[job_id] = m.group(1).upper() if m else None
        return self.verdicts[job_id]

    def running(self) -> dict:
        """Role -> the names of its lanes with a job running."""
        out: dict = collections.defaultdict(list)
        for lane in self.watch.lanes:
            if lane.job and lane.job["status"] == "running":
                out[lane.role].append(lane.name)
        return out

    def writes(self, now: float) -> list:
        """Recent deferred writes (approvals.read_writes: made, failed and running,
        failures first), every text field already one clean line. The folder is
        read at most every WRITES_TTL seconds: the page polls every 2 s, the
        gateway files an outcome every few seconds at most."""
        at, cached = self._writes
        if now - at >= WRITES_TTL or now < at:
            cached = approvals.read_writes(self.state / "broker", now)
            self._writes = (now, cached)
        return [dict(w) for w in cached]

    def cards(self, now: float, fp: bool = False) -> list:
        """The pending approval cards. A card's principal is the broker token's
        name, which is the role (developer), never the lane (developer-lane2),
        so "lanes" lists every running lane of that role: any of them may be
        the one waiting. With fp (the control unit only, never the main
        page's /api/state), each card also carries approvals.card_fingerprint.
        A card someone has decided stays pending until the agent retries the
        call: "decided" is then "approve" or "decline", with "decided_by"
        and "decided_at" (else all three are None). "queued" is true for a
        card the gateway queued (undecided past its hold): the agent has moved
        on and the gateway makes the write once it is approved."""
        running = self.running()
        out = []
        for card in approvals.read_cards(self.state / "broker"):
            files = card.get("files") if isinstance(card.get("files"), list) else []
            expires = num(card.get("expires"))
            role = str(card.get("principal") or "")
            commits, file_count = num(card.get("commits")), num(card.get("file_count"))
            out.append({
                "id": str(card.get("id")), "tool": str(card.get("tool") or ""),
                "principal": role, "role": role, "lanes": list(running.get(role, [])),
                "repo": str(card.get("repo") or ""), "branch": str(card.get("branch") or card.get("head") or ""),
                "base": str(card.get("base") or ""), "title": str(card.get("title") or ""),
                # A PR or MR card is a draft unless it says otherwise (as `factory approvals` reads it).
                "draft": card.get("tool") in ("github_create_pr", "gitlab_create_mr")
                and card.get("draft", True) is not False,
                "commits": None if commits is None else int(commits),
                "file_count": None if file_count is None else int(file_count),
                "files": [str(f) for f in files[:12]],
                "queued": approvals.is_queued(self.state / "broker", card),
                "seconds_left": None if expires is None else int(expires - now),
                **approvals.decided(self.state / "broker", card),
            })
            if fp:
                out[-1]["fp"] = approvals.card_fingerprint(card)
        return out

    @staticmethod
    def held(cards: list) -> dict:
        """Lane name -> note for each running lane whose role has a write held.
        With one running lane the hold is surely its own (note ""); with several
        the card can't say which, so each is marked held and the note says so.
        A decided, expired or queued card holds nothing: the broker's hook waits
        only hold_seconds, so by then the agent's call has returned."""
        out: dict = {}
        for card in cards:
            if card["decided"] or card.get("queued") or (card["seconds_left"] is not None and card["seconds_left"] < 0):
                continue
            lanes = card["lanes"]
            note = "" if len(lanes) == 1 else (
                f"one of {len(lanes)} running {card['role']} lanes is held; the broker names the role, not the lane")
            for name in lanes:
                out[name] = note
        return out

    def lane_json(self, lane: w.Lane, now: float, held: dict, tail: int) -> dict:
        job = lane.job
        rec = (self.watch.audit.jobs.get(job["id"]) if job else None) or {}
        state = "idle"
        if job:
            if job["status"] == "running":
                state = "held" if lane.name in held else "running"
            elif lane.active(now):
                state = job["status"]
                verdict = self.verdict(job["id"]) if state == "done" else None
                if verdict in ("REVISE", "APPROVED"):
                    state = verdict.lower()
        model = str(rec.get("model") or (job or {}).get("model") or "")
        brief = str((job or {}).get("brief") or (job or {}).get("task") or "") if state != "idle" else ""
        return {
            "name": lane.name, "role": lane.role, "state": state,
            "held_note": held.get(lane.name, "") if state == "held" else "",
            "job": (job or {}).get("id"), "task": (job or {}).get("task", ""),
            "desk": desk_label(brief), "reviews": reviewed_lane(brief) if lane.role == "reviewer" else "",
            "model": model.replace("claude-", ""), "effort": rec.get("effort"),
            "elapsed": lane.elapsed(now), "ended": (job or {}).get("ended"),
            "cost": num(rec.get("cost_usd")), "context_tokens": num(rec.get("context_tokens")),
            "tail": [{"ts": e.ts, "kind": e.kind, "text": e.text} for e in list(lane.entries)[-tail:]],
        }

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            self.refresh()
            cards = self.cards(now)
            held = self.held(cards)
            lanes = [self.lane_json(lane, now, held, TAIL) for lane in self.watch.lanes]

            midnight = local_midnight(now)
            jobs = []
            for rec in self.watch.audit.jobs.values():
                ts = utc_ts(rec.get("ts"))
                if ts is not None and ts >= midnight:
                    jobs.append((ts, rec))
            jobs.sort(key=lambda j: j[0])
            hours = collections.OrderedDict((h, 0.0) for h in range(24))
            roles: dict = collections.defaultdict(float)
            for ts, rec in jobs:
                cost = num(rec.get("cost_usd")) or 0.0
                hours[time.localtime(ts).tm_hour] += cost
                roles[str(rec.get("role") or "?")] += cost

            events = []
            for ts, rec in jobs:
                status = str(rec.get("status") or "?")
                cost = num(rec.get("cost_usd"))
                text = f"{rec.get('lane') or rec.get('role')} {status} {w.fmt_secs(num(rec.get('elapsed_seconds')) or 0)}"
                if cost is not None:
                    text += f" ${cost:.2f}"
                events.append({"t": ts, "kind": "JOB", "red": status in ("failed", "cancelled", "interrupted"), "text": text})
            for rec in self.broker.calls:
                ev = self.broker_today(rec, midnight)
                if ev:
                    events.append(ev)
            events.sort(key=lambda e: e["t"])
            events = [dict(e, t=clock(e["t"])) for e in events[-EVENTS:]]

        total = sum(num(rec.get("cost_usd")) or 0.0 for _, rec in jobs)
        return {
            "now": now, "clock": clock(now), "project": self.project, "demo": self.demo,
            "bridge": self.watch.bridge_alive(),
            "totals": {"cost": round(total, 4), "jobs": len(jobs),
                       "running": sum(1 for lane in lanes if lane["state"] in ("running", "held"))},
            "lanes": lanes,
            "spend": {"hours": [{"hour": h, "cost": round(c, 4)} for h, c in hours.items()],
                      "roles": [{"role": r, "cost": round(c, 4)} for r, c in sorted(roles.items(), key=lambda x: -x[1])]},
            "events": events,
            "gate": cards,
            "writes": self.writes(now),
        }

    @staticmethod
    def broker_today(rec: dict, midnight: float) -> dict | None:
        ts = utc_ts(rec.get("ts"))
        if ts is None or ts < midnight:
            return None
        return broker_event(rec)

    def lane_detail(self, name: str, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self.lock:
            self.refresh()
            lane = next((x for x in self.watch.lanes if x.name == name), None)
            if lane is None:
                return None
            out = self.lane_json(lane, now, self.held(self.cards(now)), FULL)
            out["packet"] = self.watch.packet_text(lane)
        return out


# --------------------------------------------------------------------------
# HTTP

def cookie_name(port: int) -> str:
    """Cookies ignore ports: naming it after the port keeps two dashboards (or a
    restart on another port) from overwriting each other's."""
    return f"{COOKIE}_{port}"


def make_handler(board: Dashboard, token: str, port: int, term: dt.Term | None = None,
                 term_port: int | None = None, manual=None):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    cookie = cookie_name(port)
    csp = page_csp(term_port)

    class Handler(BaseHTTPRequestHandler):
        server_version = "factory-dashboard"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet: the page polls every 2 s
            pass

        def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
                 page_policy: str | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", page_policy or csp)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def refuse(self, code: int, text: str) -> None:
            self.send(code, (text + "\n").encode(), "text/plain; charset=utf-8")

        def authorized(self) -> bool:
            """Exactly one cookie of this port's name, holding the token."""
            return dt.cookie_matches(self.headers, cookie, token)

        def do_GET(self) -> None:
            if self.headers.get("Host", "") not in hosts:
                self.refuse(403, "wrong Host header")
                return
            url = urlsplit(self.path)
            given = parse_qs(url.query).get("token", [""])[0]
            if url.path == "/" and given:
                if not hmac.compare_digest(given.encode(), token.encode()):
                    self.refuse(403, "wrong token: open the link 'factory dashboard' printed")
                    return
                # Swap the token in the URL for a cookie, and drop it from the address bar.
                self.send(303, b"", "text/plain", {
                    "Location": "/",
                    "Set-Cookie": f"{cookie}={token}; HttpOnly; SameSite=Strict; Path=/"})
                return
            if not self.authorized():
                self.refuse(403, "no token: open the link 'factory dashboard' printed")
                return
            if url.path == "/api/state":
                snap = board.snapshot()
                # Where the terminal origin is, and whether the Coordinator is
                # attached (for the lamp). Never the terminal key.
                snap["term"] = None if term is None else {
                    "port": term_port, "running": term.running(), "reason": term.reason}
                # Where the control unit is (the same second origin). Never its key.
                snap["cu"] = None if term_port is None else {"port": term_port}
                self.json(snap)
            elif url.path == "/api/lane":
                detail = board.lane_detail(parse_qs(url.query).get("name", [""])[0])
                if detail is None:
                    self.refuse(404, "no such lane")
                else:
                    self.json(detail)
            elif url.path == "/favicon.ico":
                self.send(204, b"", "text/plain")  # no icon; don't fill the console with 404s
            elif url.path == "/docs":
                self.send(303, b"", "text/plain", {"Location": "/docs/"})
            elif url.path in DOCS:
                self.manual(url.path)
            elif url.path in STATIC:
                name, ctype = STATIC[url.path]
                try:
                    body = (UI / name).read_bytes()
                except OSError:
                    self.refuse(500, f"missing {name}")
                    return
                self.send(200, body, ctype)
            else:
                self.refuse(404, "not found")

        do_HEAD = do_GET

        def manual(self, path: str) -> None:
            """The docs snapshot, under the main page's CSP minus frame-src (it frames nothing)."""
            if manual is None:
                self.refuse(404, "this dashboard doesn't serve the docs: run factory docs")
            elif isinstance(manual, str):
                self.refuse(503, f"{manual}; run factory docs")
            else:
                name, ctype = DOCS[path]
                self.send(200, manual[name], ctype, page_policy=CSP)

        def json(self, obj) -> None:
            self.send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def refuse_method(self) -> None:
            self.refuse(405, "GET only: the controls are in the control unit")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse_method

    return Handler


def serve(board: Dashboard, port: int = 0, token: str | None = None, term: dt.Term | None = None,
          term_port: int = 0, controls: dc.Controls | None = None, manual=None) -> tuple:
    """Start the server on 127.0.0.1 in a background thread: (server, url with token),
    and the second origin on another port (server.term_server): the control
    unit (with controls) and, with a Term, the Coordinator's terminal. manual is
    load_docs()'s result, served at /docs/ (None: no docs)."""
    token = token or secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    server.daemon_threads = True
    real_port = server.server_address[1]
    try:
        server.term_server, _ = dt.serve(term, token, cookie_name(real_port), real_port, term_port,
                                         controls=controls)
    except OSError:
        server.server_close()
        raise
    real_term_port = server.term_server.server_address[1]
    server.RequestHandlerClass = make_handler(board, token, real_port, term, real_term_port, manual)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{real_port}/?token={token}"


def stop(server, term: dt.Term | None = None) -> None:
    """Hang up on the Coordinator (if attached) and stop both servers, even if
    a second Ctrl-C lands during the hang-up (Term.shutdown still kills and
    reaps the child before the KeyboardInterrupt propagates)."""
    try:
        if term is not None:
            term.shutdown()
    finally:
        for s in (getattr(server, "term_server", None), server):
            if s is not None:
                s.shutdown()
                s.server_close()


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in platform.uname().release.lower()


def open_browser(url: str) -> str | None:
    """Ask the default browser to open url: the name of the opener that started,
    or None. Starting is not proof a window opened, so the caller always prints
    the URL as well.

    On WSL, Windows' browser (it reaches 127.0.0.1 through WSL's localhost
    forwarding): rundll32's URL protocol handler, then wslview (wslu). Each gets
    the URL as one argv item, never through cmd.exe or powershell, which would
    re-parse it; never explorer.exe, which opens a folder instead and loses the
    query. Elsewhere, or if neither starts, whatever webbrowser finds."""
    if is_wsl():
        for argv in (["rundll32.exe", "url.dll,FileProtocolHandler", url], ["wslview", url]):
            if not shutil.which(argv[0]):
                continue
            try:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return argv[0]
            except OSError:
                continue
    try:
        return "webbrowser" if webbrowser.open(url) else None
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory dashboard", description=__doc__.splitlines()[0])
    ap.add_argument("--state", type=Path, help="the factory state folder (holds dispatch/ and broker/)")
    ap.add_argument("--roles", type=Path, help="the factory's roles/ folder")
    ap.add_argument("--project", default="", help="the project the factory runs on (for the header)")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    ap.add_argument("--no-open", action="store_true", help="print the URL without opening a browser")
    ap.add_argument("--coordinator", default="", metavar="NAME",
                    help="the Coordinator's sandbox: serve its live terminal (sbx run --name NAME)")
    ap.add_argument("--factory", type=Path, default=None, metavar="PATH",
                    help="the factory command the RESTART and UPDATE keys run (default: this repo's bin/factory)")
    ap.add_argument("--demo", action="store_true",
                    help="show made-up data from scripts/dashboard-demo/, with no factory running; nothing acts")
    args = ap.parse_args(argv)
    if args.demo:
        given = [f for f, v in (("--state", args.state), ("--project", args.project),
                                ("--coordinator", args.coordinator), ("--factory", args.factory)) if v]
        if given:
            ap.error(f"--demo shows built-in sample data and has no state folder, project, Coordinator terminal "
                     f"or commands: drop {', '.join(given)}")
        args.roles = args.roles or DOCS_ROOT / "roles"
    elif args.state is None or args.roles is None:
        ap.error("--state and --roles are required (or --demo)")
    term = None
    if args.coordinator:
        try:
            term = dt.Term(dt.coordinator_argv(args.coordinator), args.coordinator)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    scratch = None
    if args.demo:
        scratch = tempfile.mkdtemp(prefix="factory-demo-")  # 0700, outside the checkout and the real state
        args.state, args.project = Path(scratch), demo.PROJECT
        try:
            demo.build(args.state)
        except (OSError, UnicodeDecodeError) as e:
            shutil.rmtree(scratch, ignore_errors=True)
            print(f"could not build the demo data: {e}", file=sys.stderr)
            return 1
    try:
        return _serve(args, term)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _serve(args, term) -> int:
    board = Dashboard(args.state, args.roles, args.project or "(no project)", demo=args.demo)
    if not board.watch.lanes:
        print("no roles or transcripts found", file=sys.stderr)
        return 1
    # SIGTERM and SIGHUP (the terminal closing) stop it as Ctrl-C does, so the
    # Coordinator's sbx run is never left behind. Installed before anything
    # listens or the browser opener starts.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _interrupt)
    if args.demo:
        controls = demo.DemoControls(board, args.state, None)
    else:
        controls = dc.Controls(board, args.state, term, args.factory or dc.FACTORY)
    manual = load_docs()
    if isinstance(manual, str):
        print(f"warning: MANUAL is off: {manual}", file=sys.stderr)
    server, ticker = None, None
    try:
        if args.demo:
            ticker = demo.start_ticker(args.state)
        server, url = serve(board, args.port, term=term, controls=controls, manual=manual)
        return _run(args, server, url, term)
    except OSError as e:
        if server is not None:
            raise
        print(f"could not listen on 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if ticker is not None:
            ticker.set()
        if server is not None:
            try:
                stop(server, term)
            except KeyboardInterrupt:  # a second Ctrl-C: the child is already reaped
                pass


def _run(args, server, url: str, term) -> int:
    """Print the link, open the browser and serve until Ctrl-C (or SIGTERM/SIGHUP)."""
    print(f"Factory dashboard: {url}")
    print("The link carries this run's access token; it stops working when this command exits.")
    if args.no_open:
        print("Open it in a browser on this machine.")
    elif open_browser(url):
        print("Trying to open it in your browser. If nothing opens, open the link above in a browser on this machine.")
    else:
        print("Could not start a browser: open the link above in a browser on this machine.")
    if args.demo:
        print("DEMO MODE: made-up data from scripts/dashboard-demo/, no factory needed. The keys that act are "
              "switched off and there is no Coordinator terminal.")
    if term is not None:
        print(f"Terminal 0 attaches to {args.coordinator} when you press POWER ON on the page "
              "(unless it is open elsewhere).")
    if args.demo:
        print("Ctrl-C stops the dashboard and deletes the demo data.", flush=True)
    else:
        print("Ctrl-C stops the dashboard" + (" and hangs up on the Coordinator." if term else ".")
              + " A RESTART or UPDATE started from it keeps running.", flush=True)
    while True:
        time.sleep(3600)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    sys.exit(main())
