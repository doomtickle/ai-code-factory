"""The dashboard's control unit (CU): the one place a page can act.

The main page shows agent-written text, so it stays a display: no endpoint it
can reach acts, and it holds no key. Every control lives in the control unit
instead, a page on the dashboard's second origin (dashboard_term.py serves
it at /cu, framed by the main page, like the terminal) with its own per-run
key. Its API is POST /cu/api/<name>, JSON in and out, and every request needs
the right Host and Origin, the dashboard cookie and the key in the
X-Factory-Control header (dashboard_term.control_refusal). This module is what
those requests reach:

  state    the held writes (with each card's fingerprint, and whether the
           gateway has staged it), the recent deferred writes the gateway made
           on approval (display only: made, failed, running), the bound
           project, the current service action, `first_shift` (whether the
           first /login or RESTART is still to do: RESTART blinks) and `config`, the effective
           configuration for the SETUP screen (dashboard_setup.py: read-only,
           from the factory's own parsers, no secrets, cached SETUP_TTL seconds)
  decide   approve or decline one held write: one press, a file write through
           approvals.write_decision(), the same record `factory approvals`
           writes. Exact id, and the card's fingerprint must still match.
           One decision per card: a card already decided (it stays pending
           until the gateway acts on it) is a 409 "decided" with the verdict that
           stands.
  prepare  the confirm step for RESTART, UPDATE or DIAG: what the action would
           do (jobs it cancels, held writes it drops; for UPDATE, the output
           of the read-only `factory update --plan`; DIAG stops nothing) and a
           single-use token, valid CONFIRM_TTL seconds, for that one action.
           Runs nothing else.
  run      start the action named by a prepared token. So no single request
           ever starts a host command.
  log      the action's output, from an offset.
  patch    the held push's patch, for INSPECT: read from the broker's state
           (approvals/patches/<sha256>.patch, named by the card's
           patch_sha256), checked against that digest, parsed and
           sanitised here (patchview.py), never by the page. Exact id and
           fingerprint, as decide. The main page never receives it.

An action is one fixed argv, never a shell: [factory, "restart"],
[factory, "update", "--yes"] or, for DIAG, [factory, "doctor"], with stdin from /dev/null (so a sudo or
confirm prompt fails rather than hangs), its output in
FACTORY_STATE/dashboard/actions/<time>-<action>.log (folder 0700, file 0600)
and its own session, so it survives the dashboard exiting. One at a time.
RESTART and UPDATE stop the Coordinator's sandbox, so before starting, the
Coordinator's terminal hangs up (Term.hang_up) and refuses POWER ON until
the action ends; if it was attached and the action succeeded, it attaches
again. DIAG (`factory doctor`) changes nothing and leaves the terminal alone;
it exits 1 when it finds problems.

After an update that succeeded, the dashboard's own files are compared with
a digest taken at start-up: changed server code needs the dashboard
restarted ("server"), a changed page only a reload ("page").

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import approvals
import dashboard_setup
import patchview

SCRIPTS = Path(__file__).resolve().parent
FACTORY = SCRIPTS.parent / "bin" / "factory"
ACTIONS = ("restart", "update", "doctor")
HANGS_UP = ("restart", "update")  # the actions that stop the Coordinator's sandbox
CONFIRM_TTL = 120      # seconds a prepared confirm token stays valid
SETUP_TTL = 5          # seconds the SETUP screen's data is reused: it stats and reads the config folder
PLAN_TIMEOUT = 90      # seconds `factory update --plan` may take
LOG_CHUNK = 64 * 1024  # most bytes /cu/api/log returns at once
MAX_REASON = 500       # characters in a decision's reason
MAX_OFFSET = 2 ** 53   # largest log offset accepted (a page's JSON number stays exact)
CARD_KEYS = ("id", "tool", "role", "lanes", "repo", "branch", "base", "title", "draft", "commits",
             "file_count", "files", "seconds_left", "fp", "patch", "patch_truncated", "decided", "decided_by",
             "decided_at", "queued")
PATCH_SHA = re.compile(r"[0-9a-f]{64}")  # a card's patch_sha256 (fullmatch), before it names a file
PATCH_MAX = 2 * 1024 * 1024              # largest patch file read (the broker caps them at 256 KiB by default)


class Refused(Exception):
    """A request the API answers with an error: (status, plain-text reason),
    and any extra fields for the page (fixed keys, set here, never a card's)."""

    def __init__(self, status: int, text: str, extra: dict | None = None):
        super().__init__(text)
        self.status = status
        self.extra = extra or {}


def files_digest(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(str(p).encode() + b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"(missing)")
        h.update(b"\0")
    return h.hexdigest()


def dashboard_digests(scripts: Path = SCRIPTS) -> tuple:
    """(server, page): digests of the dashboard's Python (dashboard*.py and the
    modules it imports: approvals.py, watch.py, patchview.py) and of its page
    files under dashboard-ui/ (vendored files left out: they change only with a pin bump)."""
    server = [p for p in scripts.glob("dashboard*.py")] + [scripts / n for n in ("approvals.py", "watch.py",
                                                                              "patchview.py")]
    ui = scripts / "dashboard-ui"
    page = [p for p in ui.rglob("*") if p.is_file() and "vendor" not in p.relative_to(ui).parts] if ui.is_dir() else []
    return files_digest(server), files_digest(page)


def clean_reason(text) -> str:
    """A decision's reason as the agent will read it: no control characters,
    trimmed. Longer than MAX_REASON is refused, not cut."""
    if not isinstance(text, str):
        raise Refused(400, "reason must be text")
    text = "".join(ch for ch in text if ch >= " " and not 0x7f <= ord(ch) <= 0x9f).strip()
    if len(text) > MAX_REASON:
        raise Refused(400, f"the reason is over {MAX_REASON} characters")
    return text


def utf8_cut(data: bytes) -> bytes:
    """data without a trailing, incomplete UTF-8 sequence (left for the next read)."""
    for back in range(1, min(4, len(data)) + 1):
        b = data[-back]
        if b < 0x80:
            return data  # ASCII: complete
        if b >= 0xC0:  # a lead byte: is its sequence complete?
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return data if back >= need else data[:-back]
    return data


def parse_plan(text: str) -> dict:
    """`factory update --plan` output (tab-separated: commit, recreate, maybe,
    note) as the lists prepare returns. Unknown lines are ignored."""
    out = {"commits": [], "recreate": [], "maybe": [], "notes": []}
    for line in text.splitlines():
        kind, _, rest = line.partition("\t")
        a, _, b = rest.partition("\t")
        if kind == "commit" and a:
            out["commits"].append({"sha": a, "subject": b})
        elif kind == "recreate" and a:
            out["recreate"].append({"sandbox": a, "loses": b})
        elif kind == "maybe" and a:
            out["maybe"].append({"sandbox": a, "why": b})
        elif kind == "note" and rest:
            out["notes"].append(rest)
    return out


class Controls:
    """The control unit's state and actions.

    board is the Dashboard (its lanes and cards), state the factory state
    folder, term the Coordinator's Term (or None). factory, clock, approver
    and scripts are injectable for tests: factory is the path run as
    [factory, "restart"] and so on (`factory dashboard --factory PATH`)."""

    def __init__(self, board, state: Path, term=None, factory: str | Path = FACTORY,
                 clock=time.time, approver: str | None = None, scripts: Path = SCRIPTS):
        self.board, self.state, self.term = board, Path(state), term
        self.factory, self.clock, self.scripts = str(factory), clock, scripts
        self.approver = approver or os.environ.get("USER") or getpass.getuser()
        self.lock = threading.Lock()
        self._setup: tuple = (0.0, None)  # (when, data) of the SETUP screen's config
        self.plan_lock = threading.Lock()  # one `factory update --plan` at a time
        self.confirm = None        # (token, action, expires) of the last prepare
        self.action = None         # name of the current/last action
        self.phase = "idle"        # idle, running, done, failed
        self.started = self.ended = self.exit = None
        self.reload = "none"
        self.log_path = None
        self.proc = None
        self.digests = dashboard_digests(scripts)

    # --- the API
    def api(self, name: str, body: dict) -> tuple:
        """(status, JSON object) for POST /cu/api/<name> with body."""
        handler = {"state": self.api_state, "decide": self.api_decide, "prepare": self.api_prepare,
                   "run": self.api_run, "log": self.api_log, "patch": self.api_patch}.get(name)
        if handler is None:
            return 404, {"error": "no such control"}
        try:
            return handler(body)
        except Refused as e:
            return e.status, dict(e.extra, error=str(e))
        except Exception as e:  # never the message: it could quote a card or a patch
            print(f"control unit: /cu/api/{name} failed: {type(e).__name__}", file=sys.stderr)
            return 500, {"error": "the control unit failed; see the dashboard's log"}

    def bound(self) -> str:
        """The project the factory is bound to ('' if none)."""
        try:
            return (self.state / "broker" / "project").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def gate(self, now: float) -> tuple:
        """(cards for the CU, raw cards by id, running lanes) as of now."""
        with self.board.lock:
            self.board.refresh()
            cards = self.board.cards(now, fp=True)
            running = [{"lane": lane.name, "task": str(lane.job.get("task") or "")}
                       for lane in self.board.watch.lanes if lane.job and lane.job.get("status") == "running"]
        raw = {str(c.get("id")): c for c in approvals.read_cards(self.state / "broker")}
        out = []
        for c in cards:
            # "patch": the page may offer INSPECT (a push held with its patch stored);
            # "patch_truncated": the stored patch was cut, so APPROVE asks again.
            card = raw.get(c["id"]) or {}
            patch = card.get("tool") == "git_push" and isinstance(card.get("patch_sha256"), str) \
                and PATCH_SHA.fullmatch(card["patch_sha256"]) is not None
            c = dict(c, patch=patch, patch_truncated=patch and card.get("patch_truncated") is True)
            out.append({k: c.get(k) for k in CARD_KEYS})
        return out, raw, running

    def action_json(self) -> dict:
        return {"name": self.action, "phase": self.phase, "started": self.started, "ended": self.ended,
                "exit": self.exit, "reload": self.reload}

    def api_state(self, body: dict) -> tuple:
        now = self.clock()
        cards, _, _ = self.gate(now)
        with self.board.lock:
            writes = self.board.writes(now)  # display only: deferred writes the gateway made (or failed)
        # These two read and hash files, so they run outside board.lock (each caches its own result).
        service = self.board.service(now)["due"]  # display only: which service key's LED to light
        first_shift = self.board.first_shift(now)  # display only: RESTART blinks until the first RESTART is done
        config = self.setup(now)
        project = self.bound()
        with self.lock:
            action = self.action_json()
        return 200, {"now": now, "project": project, "bound": bool(project), "cards": cards, "writes": writes,
                     "service": service, "first_shift": first_shift, "action": action, "config": config}

    def setup_source(self) -> tuple:
        """(factory.context.Paths, project name) the SETUP screen reads; paths None when there is none."""
        return self.board.paths, self.board.project

    def setup(self, now: float) -> dict | None:
        """The SETUP screen's data (dashboard_setup.py), rebuilt at most every SETUP_TTL seconds."""
        at, cached = self._setup
        if now - at >= SETUP_TTL or now < at:
            paths, project = self.setup_source()
            cached = dashboard_setup.setup(paths, project, self.board.look_problem)
            self._setup = (now, cached)
        return cached

    def held_card(self, cid: str, fp: str) -> dict:
        """The pending card with exactly this id, if its fingerprint is still fp."""
        # Exact id only: a prefix (as the CLI allows) could pick another card.
        matches = [c for c in approvals.read_cards(self.state / "broker") if c.get("id") == cid]
        if not matches:
            raise Refused(409, "gone")
        if len(matches) > 1:  # two pending files claim one id: act on neither from here
            raise Refused(409, "ambiguous")
        card = matches[0]
        if not hmac.compare_digest(approvals.card_fingerprint(card).encode(), fp.encode()):
            raise Refused(409, "changed")
        return card

    def api_decide(self, body: dict) -> tuple:
        cid, fp, verdict = body.get("id"), body.get("fp"), body.get("verdict")
        if not isinstance(cid, str) or not cid or not isinstance(fp, str):
            raise Refused(400, "id and fp are required")
        if verdict not in ("approve", "decline"):
            raise Refused(400, "verdict must be approve or decline")
        reason = clean_reason(body.get("reason", ""))
        if verdict == "decline" and not reason:
            raise Refused(400, "give the agent a reason for declining")
        card = self.held_card(cid, fp)
        # One decision per card: the first stands until the gateway acts on
        # it (checked again, race-free, by write_decision).
        self.refuse_if_decided(approvals.read_decision(self.state / "broker", card))
        try:
            expired = float(card.get("expires", 0)) < self.clock()
        except (TypeError, ValueError):
            expired = True
        if expired:
            raise Refused(409, "expired")
        try:
            approvals.write_decision(self.state / "broker", card, verdict, self.approver, reason)
        except approvals.AlreadyDecided as e:
            self.refuse_if_decided(e.decision)
            raise
        except ValueError:
            raise Refused(400, "this card's id can't be decided: use 'factory approvals'") from None
        except OSError:
            raise Refused(503, "the decision could not be written; try again") from None
        try:
            text = approvals.one_line(card)
        except (TypeError, ValueError):
            text = cid
        return 200, {"ok": True, "id": cid, "verdict": verdict, "text": text}

    @staticmethod
    def refuse_if_decided(decision) -> None:
        """409 "decided", with the verdict that stands, if there is one."""
        if decision is not None:
            raise Refused(409, "decided", approvals.decision_info(decision))

    def api_patch(self, body: dict) -> tuple:
        cid, fp = body.get("id"), body.get("fp")
        if not isinstance(cid, str) or not cid or not isinstance(fp, str):
            raise Refused(400, "id and fp are required")
        card = self.held_card(cid, fp)
        sha = card.get("patch_sha256")
        if sha is None:  # an older broker, or a PR card
            raise Refused(404, "no patch for this card")
        if not isinstance(sha, str) or not PATCH_SHA.fullmatch(sha):
            raise Refused(400, "the card's patch digest is not valid")
        data = self.read_patch(sha)
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest().encode(), sha.encode()):
            raise Refused(409, "patch does not match its card")
        return 200, patchview.view(data.decode("utf-8", "replace"), patch_bytes=len(data),
                                   files_total=card.get("file_count"), card_files=card.get("files"),
                                   card_truncated=card.get("patch_truncated") is True)

    def read_patch(self, sha: str) -> bytes:
        """The bytes of approvals/patches/<sha>.patch under the broker's state.
        The broker can write there, so no part of the path below the broker's
        state folder is followed if it is a symlink, only a regular file is read (a
        FIFO can't hang the open), and never more than PATCH_MAX bytes."""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        dflags = flags | getattr(os, "O_DIRECTORY", 0)
        fds = []
        try:
            try:
                # The state folder itself is the factory's own; below it, the broker's.
                fds.append(os.open(str(self.state / "broker"), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                                   | getattr(os, "O_DIRECTORY", 0)))
                for part in ("approvals", "patches"):
                    fds.append(os.open(part, dflags, dir_fd=fds[-1]))
                fds.append(os.open(sha + ".patch", flags | getattr(os, "O_NONBLOCK", 0), dir_fd=fds[-1]))
            except FileNotFoundError:
                raise Refused(404, "the patch file is missing (pruned, or never written)") from None
            except OSError:
                raise Refused(409, "the patch is not a regular file") from None
            fd = fds[-1]
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise Refused(409, "the patch is not a regular file")
            if st.st_size > PATCH_MAX:
                raise Refused(413, "the patch is too large to show")
            chunks, total = [], 0
            while total <= PATCH_MAX:
                chunk = os.read(fd, min(65536, PATCH_MAX + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > PATCH_MAX:  # it grew after the fstat
                raise Refused(413, "the patch is too large to show")
            return b"".join(chunks)
        finally:
            for fd in fds:
                os.close(fd)

    def api_prepare(self, body: dict) -> tuple:
        action = body.get("action")
        if action not in ACTIONS:
            raise Refused(400, "action must be restart, update or doctor")
        project = self.bound()
        with self.lock:
            if self.phase == "running":
                raise Refused(409, f"{self.action} is still running")
        if action == "restart" and not project:
            raise Refused(409, "the factory isn't bound to a project: run 'factory start' in one first")
        _, raw, running = self.gate(self.clock())
        if action not in HANGS_UP:
            raw, running = {}, []  # it stops nothing: no jobs or held writes to warn about
        held, queued = [], []  # queued cards are files the gateway keeps: a restart drops none of them
        for cid, card in raw.items():
            try:
                text = approvals.one_line(card)
            except (TypeError, ValueError):
                text = approvals.clean(cid, 64)
            (queued if approvals.is_queued(self.state / "broker", card) else held).append({"id": cid, "text": text})
        plan, plan_error = {"commits": [], "recreate": [], "maybe": [], "notes": []}, None
        if action == "update":
            with self.plan_lock:
                plan, plan_error = self.plan()
        token = secrets.token_urlsafe(24)
        with self.lock:
            if self.phase == "running":  # started while the plan ran
                raise Refused(409, f"{self.action} is still running")
            self.confirm = (token, action, self.clock() + CONFIRM_TTL)  # replaces any earlier token
        return 200, dict({"action": action, "confirm": token, "expires_in": CONFIRM_TTL, "project": project,
                          "jobs": running, "held": held, "queued": queued, "plan_error": plan_error}, **plan)

    def plan(self) -> tuple:
        """(parsed plan, error or None) from `<factory> update --plan` (fixed argv)."""
        empty = {"commits": [], "recreate": [], "maybe": [], "notes": []}
        try:
            # Its own process group, so a timeout kills everything it started
            # (a hanging `git fetch` included), not just the script.
            proc = subprocess.Popen([self.factory, "update", "--plan"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                    close_fds=True)
        except OSError as e:
            return empty, f"could not run 'factory update --plan': {e.strerror or e}"
        try:
            stdout, stderr = proc.communicate(timeout=PLAN_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.communicate()
            return empty, f"'factory update --plan' took over {PLAN_TIMEOUT} s"
        out = stdout.decode("utf-8", "replace")
        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip().splitlines()
            return parse_plan(out), (err[-1] if err else f"'factory update --plan' exited {proc.returncode}")[:500]
        return parse_plan(out), None

    def api_run(self, body: dict) -> tuple:
        action, given = body.get("action"), body.get("confirm")
        if action not in ACTIONS:
            raise Refused(400, "action must be restart, update or doctor")
        with self.lock:
            held = self.confirm
            if not isinstance(given, str) or held is None \
                    or not hmac.compare_digest(given.encode(), held[0].encode()):
                raise Refused(403, "no confirmation: press the key again")
            self.confirm = None  # single use, whatever happens next
            if held[1] != action:
                raise Refused(403, "that confirmation was for another action")
            if self.clock() > held[2]:
                raise Refused(403, "the confirmation expired: press the key again")
            if self.phase == "running":
                raise Refused(409, f"{self.action} is still running")
            # The new log first, so /cu/api/log never serves the last action's
            # output as this one's.
            try:
                path, fd = self.open_log(action)
            except OSError as e:
                raise Refused(500, f"could not open the action's log: {e.strerror or e}") from None
            self.log_path = path
            self.action, self.phase, self.exit, self.reload = action, "running", None, "none"
            self.started, self.ended = self.clock(), None
        threading.Thread(target=self._run, args=(action, fd), daemon=True).start()
        return 202, {"started": True}

    def api_log(self, body: dict) -> tuple:
        offset = body.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_OFFSET:
            raise Refused(400, "offset must be a whole number")
        with self.lock:
            path, action = self.log_path, self.action_json()
        text, nxt = "", 0
        if path is not None:
            try:
                with open(path, "rb") as f:
                    if offset > os.fstat(f.fileno()).st_size:
                        offset = 0  # a new, shorter log: start it from the top (the page resets)
                    f.seek(offset)
                    data = utf8_cut(f.read(LOG_CHUNK))
                text, nxt = data.decode("utf-8", "replace"), offset + len(data)
            except (OSError, OverflowError, ValueError):
                text, nxt = "", 0
        return 200, {"text": text, "offset": nxt, "phase": action["phase"], "exit": action["exit"],
                     "reload": action["reload"]}

    # --- running an action
    def argv(self, action: str) -> list:
        return {"restart": [self.factory, "restart"], "update": [self.factory, "update", "--yes"],
                "doctor": [self.factory, "doctor"]}[action]

    def open_log(self, action: str) -> tuple:
        folder = self.state / "dashboard" / "actions"
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        os.chmod(folder.parent, 0o700)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        for n in range(100):
            path = folder / (f"{stamp}-{action}.log" if n == 0 else f"{stamp}-{action}-{n}.log")
            try:
                return path, os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
        raise OSError("no free log file name")

    def _run(self, action: str, fd: int) -> None:
        """Run action with its output to fd (its log, opened by api_run; closed here)."""
        term = self.term if action in HANGS_UP else None
        attached, code = False, None
        try:
            if term is not None:
                attached = term.running()
                term.hold("service")       # first: no POWER ON between the hang-up and the start
                term.hang_up("service")
            try:
                proc = subprocess.Popen(self.argv(action), stdin=subprocess.DEVNULL, stdout=fd,
                                        stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
            except OSError as e:
                os.write(fd, f"could not start {self.argv(action)[0]}: {e.strerror or e}\n".encode())
                raise
            finally:
                os.close(fd)
            with self.lock:
                self.proc = proc
            code = proc.wait()
        except OSError:
            code = None
        finally:
            reload = "none"
            if action == "update" and code == 0:
                server, page = dashboard_digests(self.scripts)
                reload = "server" if server != self.digests[0] else "page" if page != self.digests[1] else "none"
                if reload == "page":
                    # Reported once: the reload loads these files, so a later
                    # update that changes nothing more reports "none".
                    self.digests = (self.digests[0], page)
            with self.lock:
                self.proc = None
                self.exit, self.ended, self.reload = code, self.clock(), reload
                self.phase = "done" if code == 0 else "failed"
            if term is not None:
                term.release()
                if attached and code == 0:
                    term.power_on()

    def wait(self, timeout: float = 30.0) -> bool:
        """Wait for the current action to end (tests)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if self.phase != "running":
                    return True
            time.sleep(0.02)
        return False

