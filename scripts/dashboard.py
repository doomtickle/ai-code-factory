#!/usr/bin/env python3
"""factory dashboard: the whole factory on one screen, in the browser.

Serves one page (scripts/dashboard-ui/, the "FC-1984" design) and a JSON
state endpoint the page polls every two seconds: every lane's live transcript
tail, job status, model and cost; today's spend by hour and by role; an event
printer of broker calls and finished jobs; and the writes Passdesk Pro is
holding for approval.

This version is read-only. It reads files under the factory state folder and
never writes there, runs no sbx, git or shell command, and makes no network
connection:
  dispatch/logs/<lane>.log, dispatch/audit.jsonl, dispatch/packets/, dispatch/pid
  broker/audit.jsonl, broker/approvals/pending/
It reuses watch.py's transcript model and approvals.py's card reader rather
than parsing either command's output.

The server binds 127.0.0.1 only. Every request needs the per-run token: the
URL printed at start-up carries it once, the server swaps it for an HttpOnly,
SameSite=Strict cookie, and any request without it gets 403. The Host header
must name this server (a DNS-rebinding page can't borrow the cookie), only GET
is served, and the page runs under a CSP that allows nothing but its own files.
Agent-written text (transcripts, packets, cards, audit arguments) is only ever
inserted into the page as text, never as HTML.

  factory dashboard              serve and open the page
  factory dashboard --no-open    serve and print the URL only

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import argparse
import calendar
import collections
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import approvals
import watch as w

UI = Path(__file__).resolve().parent / "dashboard-ui"
# The only files the server sends: path -> (file under UI, content type). No path
# from a request is ever joined onto the filesystem.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/fonts/vt323.woff2": ("fonts/vt323.woff2", "font/woff2"),
    "/fonts/barlow-condensed-600.woff2": ("fonts/barlow-condensed-600.woff2", "font/woff2"),
    "/fonts/barlow-condensed-700.woff2": ("fonts/barlow-condensed-700.woff2", "font/woff2"),
    "/fonts/dotgothic16.woff2": ("fonts/dotgothic16.woff2", "font/woff2"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
COOKIE = "factory_dashboard"
TAIL = 10          # transcript lines per lane in /api/state
FULL = 1500        # transcript lines per lane in /api/lane
EVENTS = 80        # event printer lines
VERDICT = re.compile(r"^- Verdict: (\S+)", re.M)


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
            if isinstance(rec, dict) and rec.get("tool"):
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
    kind = {"approval-requested": "HELD", "approval-granted": "OK", "approval-declined": "NO"}.get(event)
    if kind is None and decision in ("allow", "deny"):
        kind = "ALLOW" if decision == "allow" else "DENY"
    if kind is None:
        return None  # an "intent" record; its allow/deny record follows
    who = rec.get("role") or rec.get("principal") or ""
    bits = [str(rec["tool"]), str(who), call_target(rec)]
    if kind == "DENY" and rec.get("reason"):
        bits.append(f"· {rec['reason']}")
    if kind in ("OK", "NO") and rec.get("approver"):
        bits.append(f"· by {rec['approver']}")
    return {"t": utc_ts(rec.get("ts")), "kind": kind, "red": kind in ("DENY", "HELD", "NO"),
            "text": " ".join(b for b in bits if b)}


# --------------------------------------------------------------------------
# State

class Dashboard:
    def __init__(self, state: Path, roles: Path, project: str):
        self.state, self.project = state, project
        self.watch = w.Watch(state / "dispatch", roles, project)
        self.broker = BrokerAudit(state / "broker" / "audit.jsonl")
        self.verdicts: dict = {}
        self.lock = threading.Lock()

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

    def cards(self, now: float) -> list:
        out = []
        for card in approvals.read_cards(self.state / "broker"):
            files = card.get("files") if isinstance(card.get("files"), list) else []
            out.append({
                "id": str(card.get("id")), "tool": str(card.get("tool") or ""),
                "principal": str(card.get("principal") or ""),
                "repo": str(card.get("repo") or ""), "branch": str(card.get("branch") or card.get("head") or ""),
                "base": str(card.get("base") or ""), "title": str(card.get("title") or ""),
                "commits": card.get("commits"), "file_count": card.get("file_count"),
                "files": [str(f) for f in files[:12]],
                "seconds_left": int(float(card.get("expires", 0) or 0) - now),
            })
        return out

    def lane_json(self, lane: w.Lane, now: float, held: set, tail: int) -> dict:
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
        model = rec.get("model") or (job or {}).get("model") or ""
        return {
            "name": lane.name, "role": lane.role, "state": state,
            "job": (job or {}).get("id"), "task": (job or {}).get("task", ""),
            "model": model.replace("claude-", ""), "effort": rec.get("effort"),
            "elapsed": lane.elapsed(now), "ended": (job or {}).get("ended"),
            "cost": rec.get("cost_usd"), "context_tokens": rec.get("context_tokens"),
            "tail": [{"ts": e.ts, "kind": e.kind, "text": e.text} for e in list(lane.entries)[-tail:]],
        }

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            self.refresh()
            cards = self.cards(now)
            held = {c["principal"] for c in cards}
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
                cost = rec.get("cost_usd") or 0.0
                hours[time.localtime(ts).tm_hour] = hours.get(time.localtime(ts).tm_hour, 0.0) + cost
                roles[str(rec.get("role") or "?")] += cost

            events = []
            for ts, rec in jobs:
                status = str(rec.get("status") or "?")
                cost = rec.get("cost_usd")
                text = f"{rec.get('lane') or rec.get('role')} {status} {w.fmt_secs(rec.get('elapsed_seconds') or 0)}"
                if cost is not None:
                    text += f" ${cost:.2f}"
                events.append({"t": ts, "kind": "JOB", "red": status in ("failed", "cancelled"), "text": text})
            for rec in self.broker.calls:
                ev = self.broker_today(rec, midnight)
                if ev:
                    events.append(ev)
            events.sort(key=lambda e: e["t"])
            events = [dict(e, t=clock(e["t"])) for e in events[-EVENTS:]]

        total = sum(rec.get("cost_usd") or 0.0 for _, rec in jobs)
        return {
            "now": now, "clock": clock(now), "project": self.project,
            "bridge": self.watch.bridge_alive(),
            "totals": {"cost": round(total, 4), "jobs": len(jobs),
                       "running": sum(1 for lane in lanes if lane["state"] in ("running", "held"))},
            "lanes": lanes,
            "spend": {"hours": [{"hour": h, "cost": round(c, 4)} for h, c in hours.items()],
                      "roles": [{"role": r, "cost": round(c, 4)} for r, c in sorted(roles.items(), key=lambda x: -x[1])]},
            "events": events,
            "gate": cards,
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
            held = {c["principal"] for c in self.cards(now)}
            out = self.lane_json(lane, now, held, FULL)
            out["packet"] = self.watch.packet_text(lane)
        return out


# --------------------------------------------------------------------------
# HTTP

def make_handler(board: Dashboard, token: str, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "factory-dashboard"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet: the page polls every 2 s
            pass

        def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
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
                self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def refuse(self, code: int, text: str) -> None:
            self.send(code, (text + "\n").encode(), "text/plain; charset=utf-8")

        def authorized(self) -> bool:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE and hmac.compare_digest(value.encode(), token.encode()):
                    return True
            return False

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
                    "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"})
                return
            if not self.authorized():
                self.refuse(403, "no token: open the link 'factory dashboard' printed")
                return
            if url.path == "/api/state":
                self.json(board.snapshot())
            elif url.path == "/api/lane":
                detail = board.lane_detail(parse_qs(url.query).get("name", [""])[0])
                if detail is None:
                    self.refuse(404, "no such lane")
                else:
                    self.json(detail)
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

        def json(self, obj) -> None:
            self.send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def refuse_method(self) -> None:
            self.refuse(405, "this dashboard is read-only")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse_method

    return Handler


def serve(board: Dashboard, port: int = 0, token: str | None = None) -> tuple:
    """Start the server on 127.0.0.1 in a background thread: (server, url with token)."""
    token = token or secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    server.daemon_threads = True
    real_port = server.server_address[1]
    server.RequestHandlerClass = make_handler(board, token, real_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{real_port}/?token={token}"


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in platform.uname().release.lower()


def open_browser(url: str) -> bool:
    """The default browser: Windows' on WSL (it reaches 127.0.0.1 through WSL's
    localhost forwarding), otherwise whatever webbrowser finds."""
    if is_wsl() and shutil.which("explorer.exe"):
        try:
            subprocess.Popen(["explorer.exe", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory dashboard", description=__doc__.splitlines()[0])
    ap.add_argument("--state", required=True, type=Path, help="the factory state folder (holds dispatch/ and broker/)")
    ap.add_argument("--roles", required=True, type=Path, help="the factory's roles/ folder")
    ap.add_argument("--project", default="", help="the project the factory runs on (for the header)")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    ap.add_argument("--no-open", action="store_true", help="print the URL without opening a browser")
    args = ap.parse_args(argv)
    board = Dashboard(args.state, args.roles, args.project or "(no project)")
    if not board.watch.lanes:
        print("no roles or transcripts found", file=sys.stderr)
        return 1
    try:
        server, url = serve(board, args.port)
    except OSError as e:
        print(f"could not listen on 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        return 1
    print(f"Factory dashboard: {url}")
    print("The link carries this run's access token; it stops working when this command exits.")
    if not args.no_open and open_browser(url):
        print("Opening it in your browser. Ctrl-C stops the dashboard.", flush=True)
    else:
        print("Open it in a browser on this machine. Ctrl-C stops the dashboard.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
