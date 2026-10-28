"""The dashboard's OUTBOX module: finished factory/* branches, with the reviewer's verdict,
the push state and the pull request, one row each (doomtickle/passdesk-ops#189). Display only.

It reads the bridge's job index (dispatch/jobs.json: which branches each job imported), the
reviewer packets (dispatch/packets/<job>.md: the verdict and the branches the task names), the
broker audit lines and deferred writes the dashboard already holds (push and PR state), and the
host project's git. The git it runs is fixed, argv-only and read-only, with hooks and fsmonitor
off and none of the dashboard's GIT_* environment, and never takes a branch name:
  git for-each-ref --format=%(objectname) %(refname) refs/remotes/factory-<lane>/factory/ ...
  git rev-list --count HEAD..<object id from the line above>
The result is cached for a few seconds, rows are capped and a deadline bounds the git time, so a
page polling every two seconds never queues git runs.

Branch names, verdicts, results and URLs are agent-influenced text. A ref is only compared with
what for-each-ref lists, never passed on; every string is cleaned to one printable line and the
page draws it as text. A link comes only from a PR or MR the gateway reports opening, and only if
it is https://github.com/<owner>/<repo>/pull/<n> (a pull request) or https://<the policy's
GitLab host>/<project>/-/merge_requests/<n> (a merge request); anything else is shown as text.

Standard library only; runs on the macOS system Python (3.9).
"""

from __future__ import annotations

import calendar
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.append(str(Path(__file__).resolve().parent.parent))  # the factory package

from factory import policy, repos  # noqa: E402

import approvals  # noqa: E402

TTL = 8                    # seconds between recomputations (git included)
MAX_ROWS = 20              # rows in the module
MAX_CANDIDATES = 40        # newest branches looked at
MAX_REVIEWS = 200          # newest reviewer jobs whose packets are read
RETIRE_SECONDS = 24 * 3600  # a MERGED or GONE row stays this long after its job ended
INDEX_MAX = 5_000_000      # bytes read of jobs.json (the bridge writes at most this)
PACKET_MAX = 200_000       # bytes read of one reviewer packet
GIT_TIMEOUT = 5            # seconds for one git run
GIT_BUDGET = 12            # seconds for all the git runs of one recomputation
MAX_REF_LINES = 5000       # lines of for-each-ref output looked at
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")

JOB_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")
LANE = re.compile(r"[a-z][a-z0-9-]{1,40}")
ROLE = re.compile(r"[a-z][a-z0-9-]{1,30}")
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
# The refs the bridge imports: refs/remotes/factory-<lane>/factory/<topic>. Anything else is shown
# but never checked.
REF = re.compile(r"factory-(" + LANE.pattern + r")/factory/[A-Za-z0-9][A-Za-z0-9._/-]{0,150}")
REF_IN_TEXT = re.compile(r"(?<![\w./-])(?:refs/remotes/)?(factory-" + LANE.pattern
                         + r"/factory/[A-Za-z0-9][A-Za-z0-9._/-]*)")
# The packet's sections after the task (dispatch/server.py write_packet): the task may hold headings of its own.
SECTION = re.compile(r"\n## (?:Branches|Tests|Broker calls by the |Report)")
VERDICT = re.compile(r"^- Verdict: (APPROVED|REVISE)\s*$", re.M)
OPENED = re.compile(r"Opened (PR #|MR !)([0-9]{1,9})(?:: (\S+))?")
GITHUB_PR = re.compile(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/([0-9]{1,9})")
GITLAB_MR = re.compile(r"/[A-Za-z0-9_./-]+/-/merge_requests/([0-9]{1,9})")
PUSH = "git_push"
OPENERS = ("github_create_pr", "gitlab_create_mr")
WRITERS = (PUSH,) + OPENERS


def read_regular(path: Path, cap: int) -> bytes | None:
    """At most cap bytes of a regular file opened without following a symlink or blocking on a
    FIFO, else None (missing, unreadable, or not a regular file)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
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


def valid_ref(ref) -> bool:
    return (isinstance(ref, str) and REF.fullmatch(ref) is not None and ".." not in ref and "//" not in ref
            and not ref.endswith((".", "/", ".lock")) and "/." not in ref)


def pr_url(url: str, tool: str, gitlab_host: str) -> str:
    """url if it is a pull or merge request page of the right forge, else "". Exact scheme, host
    (no userinfo, port or case tricks), and path; no query, fragment or other characters."""
    if not isinstance(url, str) or not url.isascii() or not url.isprintable():
        return ""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.query or parts.fragment or parts.username is not None or port is not None:
        return ""
    if parts.netloc != (parts.hostname or "\0"):  # a bare host: no "@", ":" or trailing dot games
        return ""
    if tool == "github_create_pr":
        ok = parts.netloc == "github.com" and GITHUB_PR.fullmatch(parts.path)
    elif tool == "gitlab_create_mr":
        ok = bool(gitlab_host) and parts.netloc == gitlab_host and GITLAB_MR.fullmatch(parts.path) \
            and ".." not in parts.path and "//" not in parts.path
    else:
        ok = False
    return url if ok else ""


def gitlab_host(paths) -> str:
    """The bare host of the policy's [gitlab].url (lower case), "" when none is configured."""
    if paths is None:
        return ""
    try:
        raw, _ = policy.policy_value_source(paths, "gitlab", "url")
        first = re.search(r'"([^"]*)"|\'([^\']*)\'', raw or "")
        parts = urlsplit((first.group(1) or first.group(2) or "") if first else "")
        if parts.scheme != "https" or parts.port is not None:
            return ""
        return (parts.hostname or "").lower()
    except (OSError, ValueError):
        return ""


class HostGit:
    """The host project's git, read-only. Every run is a fixed argv with no shell."""

    def __init__(self, state: Path):
        self.state = state
        self._deadline = 0.0
        self._project: Path | None = None

    def begin(self) -> None:
        """Start a recomputation: find the project and set the deadline for all its git runs."""
        self._deadline = time.monotonic() + GIT_BUDGET
        raw = read_regular(self.state / "dispatch" / "project", 4096)
        text = raw.decode("utf-8", "replace").strip() if raw else ""
        self._project = Path(text) if text and os.path.isabs(text) and "\0" not in text and os.path.isdir(text) else None

    def repos(self) -> list:
        return repos.project_repos(self._project) if self._project else []

    def _run(self, rel: str, *args: str):
        left = self._deadline - time.monotonic()
        if self._project is None or left <= 0:
            return None
        repo = self._project if rel == "." else self._project / rel
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            cp = subprocess.run(["git", *GIT_SAFE, "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, env=env, timeout=min(GIT_TIMEOUT, left))
        except (OSError, subprocess.SubprocessError):
            return None
        return cp.stdout.decode("utf-8", "replace") if cp.returncode == 0 else None

    def refs(self, rel: str, lanes: list) -> dict | None:
        """{full ref name: object id} of the imported refs of these lanes, None if git failed."""
        out = self._run(rel, "for-each-ref", "--format=%(objectname) %(refname)",
                        *[f"refs/remotes/factory-{lane}/factory/" for lane in lanes])
        if out is None:
            return None
        found = {}
        for line in out.splitlines()[:MAX_REF_LINES]:
            oid, _, name = line.partition(" ")
            if OID.fullmatch(oid) and name:
                found[name] = oid
        return found

    def ahead(self, rel: str, oid: str) -> int | None:
        """Commits of the object past the host's HEAD, None if git failed (no HEAD yet, timeout)."""
        if not OID.fullmatch(oid):
            return None
        out = self._run(rel, "rev-list", "--count", f"HEAD..{oid}")
        return int(out) if out is not None and out.strip().isdigit() else None


class FixtureGit:
    """Demo mode: git answers from dispatch/demo-refs.json in the throwaway folder, and nothing runs.
    {"<repo>": {"<full ref>": {"oid": "<hex>", "ahead": N}}}"""

    def __init__(self, state: Path):
        self.state = state
        self.data: dict = {}

    def begin(self) -> None:
        raw = read_regular(self.state / "dispatch" / "demo-refs.json", 100_000)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            data = {}
        self.data = data if isinstance(data, dict) else {}

    def repos(self) -> list:
        return [r for r in self.data if isinstance(r, str)]

    def refs(self, rel: str, lanes: list) -> dict | None:
        refs = self.data.get(rel)
        return {k: v["oid"] for k, v in refs.items() if isinstance(v, dict) and isinstance(v.get("oid"), str)} \
            if isinstance(refs, dict) else None

    def ahead(self, rel: str, oid: str) -> int | None:
        for v in (self.data.get(rel) or {}).values():
            if isinstance(v, dict) and v.get("oid") == oid and isinstance(v.get("ahead"), int):
                return v["ahead"]
        return None


def _utc(ts) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError, OverflowError):
        return None


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value - value == 0 else None


class Outbox:
    def __init__(self, state: Path, project: str, paths=None, git=None):
        self.state, self.project, self.paths = state, project, paths
        self.git = git or HostGit(state)
        self._lock = threading.Lock()
        self._cached: tuple = (0.0, [])
        self._packets: dict = {}  # reviewer job id -> (verdict, refs its task names)

    # ----- the job index and the reviewer packets

    def _jobs(self) -> list:
        raw = read_regular(self.state / "dispatch" / "jobs.json", INDEX_MAX + 1)
        if raw is None or len(raw) > INDEX_MAX:
            return []
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            return []
        records = data.get("jobs") if isinstance(data, dict) and data.get("version") == 1 else None
        out = []
        for rec in records if isinstance(records, list) else []:
            if not isinstance(rec, dict):
                continue
            job, role, lane = rec.get("id"), rec.get("role"), rec.get("lane")
            started = _finite(rec.get("started"))
            if (not isinstance(job, str) or not JOB_ID.fullmatch(job) or not isinstance(role, str)
                    or not ROLE.fullmatch(role) or not isinstance(lane, str) or not LANE.fullmatch(lane)
                    or started is None or rec.get("status") == "running"):
                continue
            finished = _finite(rec.get("finished"))
            out.append({"id": job, "role": role, "lane": lane, "started": started,
                        "ended": finished if finished is not None else started, "branches": rec.get("branches")})
        out.sort(key=lambda j: j["ended"], reverse=True)
        return out

    def _review(self, job: str):
        """(verdict, refs) from a reviewer packet, None while it isn't written. The verdict is only
        read from the packet's own header, the refs only from its Task section."""
        if job not in self._packets:
            raw = read_regular(self.state / "dispatch" / "packets" / f"{job}.md", PACKET_MAX)
            if raw is None:
                return None
            text = raw.decode("utf-8", "replace")
            head, _, rest = text.partition("\n## Task\n")
            m = VERDICT.search(head) if rest else None
            task = SECTION.split(rest, 1)[0]
            self._packets[job] = (m.group(1) if m else "", frozenset(ref.rstrip("./") for ref in REF_IN_TEXT.findall(task)))
        return self._packets[job]

    def _verdicts(self, jobs: list) -> dict:
        """{ref: (verdict, reviewer job started)} the newest reviewer verdict naming each ref."""
        reviewers = [j for j in jobs if j["role"] == "reviewer"][:MAX_REVIEWS]
        for gone in set(self._packets) - {j["id"] for j in reviewers}:
            del self._packets[gone]
        out: dict = {}
        for j in sorted(reviewers, key=lambda j: j["started"], reverse=True):
            got = self._review(j["id"])
            if not got or not got[0]:
                continue
            for ref in got[1]:
                out.setdefault(ref, (got[0], j["started"]))
        return out

    # ----- push state and pull request, from the broker audit and the deferred writes

    @staticmethod
    def _events(calls: list, writes: list) -> dict:
        """{branch: [event, ...]} oldest first; an event is (time, tool, kind, id, text)."""
        out: dict = {}

        def add(branch, when, tool, kind, cid, text):
            if isinstance(branch, str) and isinstance(tool, str) and when is not None:
                out.setdefault(branch, []).append((when, tool, kind, str(cid or ""), text if isinstance(text, str) else ""))

        for rec in calls:
            args = rec.get("args") if isinstance(rec.get("args"), dict) else {}
            branch = rec.get("branch") or args.get("branch") or args.get("head")
            if rec.get("event") is None:
                # A write no hold stopped (the default: [approvals].tools is empty) is an ordinary record.
                tool, decision = rec.get("tool"), rec.get("decision")
                kind = ("done" if decision == "allow" and tool in WRITERS
                        else "failed" if decision in ("deny", "error") and tool == PUSH else None)
            else:
                kind = {"approval-requested": "staged", "approval-queued": "staged", "approval-granted": "staged",
                        "approval-declined": "cleared", "approval-write-done": "done",
                        "approval-write-failed": "failed"}.get(rec.get("event"))
            if kind:
                text = rec.get("result") if kind == "done" else ""
                add(branch, _utc(rec.get("ts")), rec.get("tool"), kind, rec.get("id"), text)
        for w in writes:
            kind = {"made": "done", "failed": "failed"}.get(w.get("state"))
            if kind:
                add(w.get("target"), w.get("at"), w.get("tool"), kind, w.get("id"), w.get("summary"))
        for events in out.values():
            events.sort(key=lambda e: e[0])
        return out

    def _publication(self, events: list, pending: set, host: str):
        """(push state, pull request or None) from one branch's events."""
        push, pr, written = "", None, False
        for _when, tool, kind, cid, text in events:
            if tool == PUSH:
                if kind == "staged":
                    if cid in pending:  # a staged push nobody can approve any more (expired) shows nothing
                        push = "STAGED"
                elif kind == "cleared":
                    push = ""
                else:
                    push = "PUSHED" if kind == "done" else "FAILED"
            elif tool in OPENERS and kind == "done":
                written = True
                m = OPENED.search(text)
                if m:
                    number = int(m.group(2))
                    url = pr_url(m.group(3) or "", tool, host)
                    page = (GITHUB_PR if tool == "github_create_pr" else GITLAB_MR).fullmatch(urlsplit(url).path) if url else None
                    pr = {"label": ("PR #" if tool == "github_create_pr" else "MR !") + str(number),
                          "url": url if page and int(page.group(1)) == number else "", "number": number}
                else:
                    pr = {"label": "PR" if tool == "github_create_pr" else "MR", "url": "", "number": None}
        if not push and written:
            push = "WRITTEN"
        return push, pr

    # ----- the rows

    def rows(self, now: float, calls: list, pending: set, writes: list) -> list:
        """The module's rows, newest first, recomputed at most every TTL seconds. Never blocks on a
        recomputation another poll is making (that one's result, or the last, is returned)."""
        at, cached = self._cached
        if now - at < TTL and now >= at:
            return [dict(r) for r in cached]
        if not self._lock.acquire(blocking=False):
            return [dict(r) for r in cached]
        try:
            try:
                cached = self._compute(now, calls, pending, writes)
            except Exception:  # a module must not take the state view down
                cached = []
            self._cached = (now, cached)
        finally:
            self._lock.release()
        return [dict(r) for r in cached]

    def _compute(self, now: float, calls: list, pending: set, writes: list) -> list:
        jobs = self._jobs()
        # One candidate per (repo, ref): the newest job that imported it.
        seen: dict = {}
        for j in jobs:
            for b in j["branches"] if isinstance(j["branches"], list) else []:
                if not (isinstance(b, dict) and all(isinstance(b.get(k), str) for k in ("repo", "ref", "commit"))):
                    continue
                seen.setdefault((b["repo"], b["ref"]), (j, b["commit"]))
                if len(seen) >= MAX_CANDIDATES:
                    break
            if len(seen) >= MAX_CANDIDATES:
                break
        if not seen:
            return []
        self.git.begin()
        known = set(self.git.repos())
        listing: dict = {}
        for rel in {rel for rel, _ in seen}:
            lanes = sorted({REF.fullmatch(ref).group(1) for r, ref in seen if r == rel and valid_ref(ref)})
            if rel in known:
                listing[rel] = self.git.refs(rel, lanes) if lanes else {}
        verdicts = self._verdicts(jobs)
        events = self._events(calls, writes)
        host = gitlab_host(self.paths)
        rows = []
        for (rel, ref), (job, commit) in seen.items():
            row = self._row(rel, ref, commit, job, listing, verdicts, events, pending, host)
            if row["status"] in ("MERGED", "GONE") and now - job["ended"] > RETIRE_SECONDS:
                continue
            rows.append(row)
        rows.sort(key=lambda r: r["at"], reverse=True)
        return rows[:MAX_ROWS]

    def _row(self, rel, ref, commit, job, listing, verdicts, events, pending, host) -> dict:
        status, ahead, oid = "UNCHECKED", None, None
        refs = listing.get(rel)
        if valid_ref(ref) and refs is not None:
            oid = refs.get(f"refs/remotes/{ref}")
            if oid is None:
                status = "GONE"
            else:
                ahead = self.git.ahead(rel, oid)
                status = "UNCHECKED" if ahead is None else ("MERGED" if ahead == 0 else "OPEN")
        verdict, stale = "", False
        if ref in verdicts:
            verdict, reviewed = verdicts[ref]
            stale = reviewed < job["ended"]  # the review began before this job's last commit
        branch = ref.split("/factory/", 1)[1] if "/factory/" in ref else ""
        push, pr = self._publication(events.get(f"factory/{branch}", []) if branch else [], pending, host)
        return {
            "repo": approvals.clean(self.project if rel == "." else rel, 60),
            "branch": approvals.clean(ref, 160),
            "commit": approvals.clean(oid[:7] if oid else commit, 12),
            "ahead": ahead, "status": status, "verdict": verdict, "stale": stale, "push": push, "pr": pr,
            "job": job["id"], "lane": job["lane"], "at": job["ended"],
        }
