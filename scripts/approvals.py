#!/usr/bin/env python3
"""factory approvals: decide what the gateway is holding for a person.

The gateway stages each held write (a push, pull or merge request, an
issue, a plugin tool's call) as a card under the broker state directory
(approvals/pending/<id>.json, with its job in approvals/queued/<id>.json)
and acts on the decision file (approvals/decisions/<id>.json) that this
console writes. It
runs on the host with the system Python (3.9 on macOS), standard library
only, and touches nothing but those files. The dashboard's control unit
(scripts/dashboard_controls.py) writes decisions through write_decision()
here, so both write the same record.

    factory approvals                      pending cards (decided ones marked), then recent deferred writes
    factory approvals show <id>            a pending card, or a finished deferred write
    factory approvals approve <id> [--as NAME] [--reason TEXT]
    factory approvals decline <id> [--as NAME] [--reason TEXT]
    factory approvals watch                print cards and deferred writes as they arrive and change
    factory approvals summary              one line for scripts: "pending oldest_min expired_unhandled approved running made failed"

A decision is final for its card: a second approve or decline of the same
card is refused, here and in the dashboard, rather than overwriting the
first. The agent's call returned as soon as the write was staged; once the
card is approved the gateway makes the write itself within seconds and
files the outcome under approvals/done/<id>.json (write: running, done,
failed). Lists mark staged cards and show those outcomes (deferred writes),
failures first. Every field read from these
files is untrusted text: clean() makes it one line with control and format
characters removed and a length cap.
"""

import argparse
import errno
import fcntl
import getpass
import hashlib
import json
import os
import re
import sys
import stat
import tempfile
import time
import unicodedata
from pathlib import Path


# What a card id may be before it names a file (decisions/<id>.json). Passdesk
# Pro's ids fit; anything else is refused rather than joined onto a path.
CARD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


LINE_MAX = 160        # longest field shown on one line (clean's default cap)
SUMMARY_MAX = 200     # longest deferred-write summary
FILE_MAX = 256 * 1024  # largest queued/ or done/ file read (the gateway caps results at 3,000 characters)
WRITES_LIMIT = 8      # deferred writes listed
WRITES_SCAN = 60      # newest done/ files looked at for them
MADE_WINDOW = 24 * 3600     # a write made is listed for a day
FAILED_WINDOW = 3 * 24 * 3600  # a failure for three days
WRITE_STATES = {"running": "running", "done": "made", "failed": "failed"}  # done/ `write` -> shown


def clean(value, cap=LINE_MAX):
    """Text from a state file as one safe line: whitespace (newlines, tabs,
    line and paragraph separators) becomes single spaces; control, format
    (bidi, zero-width), surrogate and private-use characters are dropped; cut
    at cap characters with an ellipsis. Anything that is not text is shown as
    its str(); None is empty."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    out = []
    for ch in text[:cap * 4 + 16]:
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"):
            out.append(ch)
    text = " ".join("".join(out).split())
    return text if len(text) <= cap else text[:max(0, cap - 1)] + "\u2026"


def default_state():
    xdg = os.environ.get("XDG_STATE_HOME", "")
    base = Path(xdg) if xdg.startswith("/") else Path.home() / ".local" / "state"
    return base / "ai-code-factory" / "broker"


def read_cards(state):
    cards = []
    for path in sorted((state / "approvals" / "pending").glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(card, dict) and isinstance(card.get("id"), str) and card["id"]:
            cards.append(card)
    return cards


def find(state, cid):
    for card in read_cards(state):
        if card["id"] == cid or card["id"].startswith(cid):
            return card
    return None


def read_json(path):
    """A state file as a dict, or None: opened without following a symlink or
    blocking on a FIFO (the gateway can write there), regular files of at
    most FILE_MAX bytes only, and only a JSON object."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(FILE_MAX + 1)
        if len(data) > FILE_MAX:
            return None
        rec = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    finally:
        os.close(fd)
    return rec if isinstance(rec, dict) else None


def is_queued(state, card):
    """True if the gateway staged this very card (approvals/queued/<id>.json
    with the card's id and nonce): the agent has moved on, and the gateway
    makes the write itself once it is approved. Every card the gateway makes
    is; one that isn't was left by an older gateway."""
    cid, nonce = card.get("id"), card.get("nonce")
    if not isinstance(cid, str) or not CARD_ID.fullmatch(cid) or not isinstance(nonce, str) or not nonce:
        return False
    rec = read_json(state / "approvals" / "queued" / "{}.json".format(cid))
    return rec is not None and rec.get("id") == cid and rec.get("nonce") == nonce


def finite(value):
    """value as a float if it is a finite number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value - value == 0 else None


def write_record(rec, cid):
    """A done/ record of a deferred write as a display dict, or None if it
    is not one (not queued, declined or expired: those wrote nothing, or
    another state). Every text field is clean()ed."""
    if rec.get("queued") is not True or rec.get("id") != cid:
        return None
    shown = WRITE_STATES.get(rec.get("write")) if isinstance(rec.get("write"), str) else None
    if shown is None:
        return None
    result = rec.get("result")
    result = result if isinstance(result, str) else ""
    if shown == "failed":
        summary = clean(result, SUMMARY_MAX)  # the reason, whole
    else:  # the first line, as the webhook shows it
        summary = clean(next((ln for ln in result.splitlines() if ln.strip()), ""), SUMMARY_MAX)
    at = finite(rec.get("finished"))
    if at is None:
        at = finite(rec.get("started"))
    return {"id": cid, "tool": clean(rec.get("tool"), 40), "role": clean(rec.get("principal"), 40),
            "repo": clean(rec.get("repo"), 100), "target": clean(rec.get("branch") or rec.get("head"), 100),
            "title": clean(rec.get("title"), 80),
            "state": shown, "summary": summary, "at": at, "approver": clean(rec.get("approver"), 60)}


def read_writes(state, now=None, limit=WRITES_LIMIT):
    """Deferred writes the gateway made on approval (approvals/done/<id>.json
    with queued: true), newest first with failures ahead: running ones, those
    made in the last day and those that failed in the last three. At most
    `limit`, from the WRITES_SCAN newest files. Dicts as write_record()."""
    now = time.time() if now is None else now
    folder = state / "approvals" / "done"
    try:
        with os.scandir(str(folder)) as it:
            files = []
            for entry in it:
                stem = entry.name[:-5]
                if not entry.name.endswith(".json") or not CARD_ID.fullmatch(stem):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        files.append((entry.stat(follow_symlinks=False).st_mtime, stem))
                except OSError:
                    continue
    except OSError:
        return []
    files.sort(reverse=True)
    out = []
    for _, cid in files[:WRITES_SCAN]:
        rec = read_json(folder / "{}.json".format(cid))
        item = write_record(rec, cid) if rec else None
        if item is None:
            continue
        age = None if item["at"] is None else now - item["at"]
        window = {"made": MADE_WINDOW, "failed": FAILED_WINDOW}.get(item["state"])
        if window is not None and (age is None or age > window):
            continue
        out.append(item)
    rank = {"failed": 0, "running": 1, "made": 2}
    out.sort(key=lambda w: (rank[w["state"]], -(w["at"] or 0)))
    return out[:limit]


def find_write(state, cid):
    """The deferred write whose id is cid or starts with it (no age limit)."""
    folder = state / "approvals" / "done"
    try:
        names = sorted(n[:-5] for n in os.listdir(str(folder)) if n.endswith(".json") and CARD_ID.fullmatch(n[:-5]))
    except OSError:
        return None
    for name in names:
        if name == cid or name.startswith(cid):
            rec = read_json(folder / "{}.json".format(name))
            item = write_record(rec, name) if rec else None
            if item:
                return item
    return None


def write_line(item):
    """One display line for a deferred write."""
    label = {"failed": "!! FAILED", "running": "running", "made": "made"}[item["state"]]
    what = item["tool"] + (" " + item["target"] if item["target"] else "") + (" -> " + item["repo"] if item["repo"] else "")
    if item["tool"] in ("github_create_pr", "gitlab_create_mr") and item["title"]:
        what += " \"{}\"".format(item["title"])
    text = "{}  {:<9} {}".format(item["id"], label, what)
    if item["state"] != "running" and item["summary"]:
        text += "\n    " + item["summary"]
    elif item["state"] == "running":
        text += "  (the gateway is making it now)"
    return text


def minutes_left(card, now=None):
    now = time.time() if now is None else now
    return int((float(card.get("expires", 0)) - now) // 60)


def one_line(card):
    tool = clean(card.get("tool"), 40)
    who = clean(card.get("principal"), 40) or "?"
    if tool == "git_push":
        what = "push {} -> {} ({} commit(s), {} file(s))".format(
            clean(card.get("branch"), 100), clean(card.get("repo"), 100), clean(card.get("commits"), 12),
            clean(card.get("file_count"), 12))
    elif tool in ("github_create_pr", "gitlab_create_mr"):
        what = "{}{} on {}: {} -> {} \"{}\"".format(
            "draft " if card.get("draft", True) else "", "MR" if tool == "gitlab_create_mr" else "PR",
            clean(card.get("repo"), 100), clean(card.get("head"), 100), clean(card.get("base"), 100),
            clean(card.get("title"), 60))
    else:
        what = tool
    return "{}  {:<10} {}  ({} min left)".format(clean(card["id"], 64), who, what, minutes_left(card))


QUEUED_NOTE = "staged: the agent has moved on; the gateway makes the write when it is approved"
UNSTAGED_NOTE = "not staged (an older gateway's card): approving it makes nothing; the agent's next call stages it"
EXPIRED_NOTE = "expired; nothing is written (the agent's next call of it stages a fresh card)"


def status_note(state, card, now=None):
    """What the card is waiting for: a decision (staged), the gateway (once
    decided), or nothing (expired, or not staged)."""
    queued = is_queued(state, card)
    info = decided(state, card)
    if info["decided"]:
        return info, decided_text(info, queued)
    if minutes_left(card, now) < 0:
        return info, EXPIRED_NOTE
    return info, QUEUED_NOTE if queued else UNSTAGED_NOTE


def print_writes(writes):
    print("Deferred writes (the gateway made these after approval):")
    for item in writes:
        print(write_line(item))


def cmd_list(state, _args):
    cards = read_cards(state)
    writes = read_writes(state)
    if not cards:
        print("No approvals pending.")
    for card in cards:
        _, note = status_note(state, card)
        print(one_line(card) + ("\n    [{}]".format(note) if note else ""))
    if writes:
        print()
        print_writes(writes)
    if cards:
        print("\nfactory approvals show <id> | approve <id> | decline <id> --reason ...")
    return 0


def show_write(item):
    print("deferred write {}  ({})".format(item["id"], item["state"]))
    for key, label in (("tool", "tool"), ("role", "role"), ("repo", "repo"), ("target", "branch"), ("title", "title"),
                       ("approver", "approved_by")):
        if item[key]:
            print("  {:<14} {}".format(label, item[key]))
    print("  {:<14} {}".format("finished", when(item["at"])))
    print("  {:<14} {}".format("result" if item["state"] != "failed" else "reason", item["summary"] or "-"))


def cmd_show(state, args):
    card = find(state, args.id)
    if not card:
        item = find_write(state, args.id)
        if item:
            show_write(item)
            return 0
        print("no pending approval matches {}".format(args.id), file=sys.stderr)
        return 1
    skip = {"created", "expires", "nonce"}
    info, note = status_note(state, card)
    print("approval {}  ({} min left)".format(clean(card["id"], 64), minutes_left(card)))
    print("  {:<14} {}".format("decided", info["decided"] or "no"))
    if info["decided"]:
        print("  {:<14} {}".format("decided_by", info["decided_by"]))
        print("  {:<14} {}".format("decided_at", when(info["decided_at"])))
    if note:
        print("  {:<14} {}".format("status", note))
    for key in sorted(card):
        if key in skip or key == "id":
            continue
        val = card[key]
        if isinstance(val, list):
            print("  {:<14}".format(clean(key, 40)))
            for item in val:
                print("    {}".format(clean(item)))
        else:
            print("  {:<14} {}".format(clean(key, 40), clean(val, 500)))
    return 0


class AlreadyDecided(Exception):
    """write_decision() found a decision for this very card (same id and nonce)
    already written: the first one stands. .decision is that decision."""

    def __init__(self, decision):
        super().__init__("already decided")
        self.decision = decision


def read_decision(state, card):
    """The decision written for this card (same id and nonce), or None. A
    decision file with another nonce belongs to an earlier card with the same
    id and is not this card's."""
    cid = card.get("id")
    if not isinstance(cid, str) or not CARD_ID.fullmatch(cid):
        return None
    try:
        decision = json.loads((state / "approvals" / "decisions" / "{}.json".format(cid)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(decision, dict) or decision.get("nonce") != card.get("nonce"):
        return None
    if decision.get("decision") not in ("approve", "decline"):
        return None
    return decision


def decision_info(decision):
    """{"decided": "approve"|"decline"|None, "decided_by": str|None,
    "decided_at": float|None}: a decision (or None) as lists show it."""
    if decision is None:
        return {"decided": None, "decided_by": None, "decided_at": None}
    ts = decision.get("ts")
    return {"decided": decision.get("decision"), "decided_by": str(decision.get("approver") or "unknown"),
            "decided_at": float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None}


def decided(state, card):
    """decision_info() of this card's own decision."""
    return decision_info(read_decision(state, card))


def when(ts):
    """A decision's time for people: local "YYYY-MM-DD HH:MM", or "?"."""
    if ts is None:
        return "?"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def decided_text(info, queued=True):
    """ "approved by X at T; the gateway makes the write within seconds" (or
    declined: it files it and writes nothing). A card that isn't staged has
    nothing for the gateway to make."""
    if not queued:
        wait = "not staged, so nothing is made from it"
    elif info["decided"] == "approve":
        wait = "the gateway makes the write within seconds"
    else:
        wait = "the gateway files it; nothing is written"
    return "{} by {} at {}; {}".format(
        "approved" if info["decided"] == "approve" else "declined", clean(info["decided_by"], 60),
        when(info["decided_at"]), wait)


def card_fingerprint(card):
    """A short public stand-in for the card's nonce: the first 16 hex digits of
    sha256(id NUL nonce). The dashboard's control unit sends it back with a
    decision, so a decision meant for one card never lands on a newer card
    with the same id; the nonce itself never leaves this machine's files."""
    raw = "{}\0{}".format(card.get("id") or "", card.get("nonce") or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


LOCK_WAIT = 5.0  # seconds write_decision waits for another writer's lock


class _DecisionLock:
    """An exclusive flock on approvals/decisions/.write.lock, so two writers
    can't both set aside a stale decision and both write their own. The
    broker can write in its state folder, so the lock file is opened without
    following a symlink and without blocking on a FIFO, and must be a regular
    file. Waits up to LOCK_WAIT seconds, then raises OSError(EAGAIN)."""

    def __init__(self, folder):
        self.folder = folder
        self.fd = None

    def __enter__(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) \
            | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(self.folder / ".write.lock"), flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "the decisions lock is not a regular file")
            end = time.monotonic() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= end:
                        raise OSError(errno.EAGAIN, "another decision is being written; try again") from None
                    time.sleep(0.05)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *_exc):
        os.close(self.fd)  # releases the flock
        return False


def write_decision(state, card, verdict, approver, reason=""):
    """Write the decision file the gateway waits for (approvals/decisions/<id>.json).
    Used by this console and by the dashboard's control unit, so both write
    exactly the same record. Returns the decision.

    One decision per card: if a decision for this card (same id and nonce)
    is already there, nothing is written and AlreadyDecided is raised with
    it. The file is written in full under a temporary name and then hard-
    linked onto the final name, which fails if that name exists, so of two
    people deciding at once exactly one wins. A decision file with another
    nonce (an earlier card that had the same id) is removed first, under a
    lock (_DecisionLock), so it never blocks the new card. Raises OSError
    if the lock can't be had."""
    if verdict not in ("approve", "decline"):
        raise ValueError("verdict must be approve or decline")
    if not isinstance(card.get("id"), str) or not CARD_ID.fullmatch(card["id"]):
        raise ValueError("not a card id: {!r}".format(card.get("id")))
    # The nonce ties the decision to this card: a later card with the same id
    # (the same push, re-requested after this one expired) needs a new decision.
    decision = {"id": card["id"], "nonce": card.get("nonce"), "decision": verdict, "approver": approver,
                "reason": reason or "", "ts": time.time()}
    path = state / "approvals" / "decisions" / "{}.json".format(card["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # A temporary file of its own (two people deciding at once must not share
    # one), in the same folder so it can be linked onto the final name; it is
    # always removed, as the final name (if linked) keeps the data.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".{}.".format(card["id"]), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(decision, indent=2) + "\n")
        os.chmod(tmp, 0o644)
        with _DecisionLock(path.parent):
            for _ in range(3):
                try:
                    os.link(tmp, str(path))  # never replaces: EEXIST if a decision is there
                    return decision
                except FileExistsError:
                    pass
                existing = read_decision(state, card)
                if existing is not None:
                    raise AlreadyDecided(existing)
                # Another card's decision (another nonce), or unreadable: not
                # this card's, so it must not block it.
                try:
                    os.unlink(str(path))
                except FileNotFoundError:
                    pass
            raise OSError(errno.EAGAIN, "the decision file keeps reappearing; try again")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def default_approver():
    return os.environ.get("USER") or getpass.getuser()


def decide(state, args, verdict):
    card = find(state, args.id)
    if not card:
        print("no pending approval matches {}".format(args.id), file=sys.stderr)
        return 1
    queued = is_queued(state, card)
    info = decided(state, card)
    if info["decided"]:
        print("approval {} is already {}".format(clean(card["id"], 64), decided_text(info, queued)), file=sys.stderr)
        return 1
    if minutes_left(card) < 0:
        print("approval {} has expired; nothing will be written (the agent's next call of it stages a fresh "
              "one)".format(clean(card["id"], 64)), file=sys.stderr)
        return 1
    if verdict == "decline" and not args.reason:
        print("give the agent a reason: --reason \"...\"", file=sys.stderr)
        return 1
    approver = args.as_name or default_approver()
    try:
        write_decision(state, card, verdict, approver, args.reason)
    except AlreadyDecided as e:  # someone else decided it a moment ago
        info = decision_info(e.decision)
        print("approval {} is already {}".format(clean(card["id"], 64), decided_text(info, queued)), file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print("cannot decide {}: {}".format(clean(card["id"], 64), clean(e, 200)), file=sys.stderr)
        return 1
    print("{} {} as {}: {}".format("approved" if verdict == "approve" else "declined", card["id"], approver,
                                   one_line(card)))
    if not queued:
        print("This card was not staged (an older gateway's card), so nothing is made from it; the agent's next "
              "call stages it afresh.")
    elif verdict == "approve":
        print("This write is staged: the gateway will make it within seconds (it checks every 5 s) and record the "
              "outcome. See it with: factory approvals")
    else:
        print("This write is staged: the gateway files the decline and nothing is written.")
    return 0


def is_expired_unhandled(state, card, now=None):
    """A card past its expiry with no decision that still matches it.

    The gateway sweeps expired cards out of pending/ on its own, but only
    while the broker is actively handling hook() calls; one left behind
    (broker idle, agent gone) is still expired and still unhandled. A
    decision file whose nonce doesn't match the card's nonce belongs to an
    earlier card with the same id (staging the same write again after
    expiry mints a fresh nonce), so it doesn't count as having handled this
    one.
    """
    if minutes_left(card, now) >= 0:
        return False
    return read_decision(state, card) is None


def summarize(state, now=None):
    """pending: staged cards still waiting for a person (no decision of their
    own); oldest_minutes: the oldest of those, in minutes (None if none);
    expired_unhandled: expired cards with no decision; approved: cards
    approved and waiting for the gateway to make the write (they need no
    one; a declined card is just filed, so it isn't counted); running, made,
    failed: deferred writes as `factory approvals` lists them (being made,
    made in the last day, failed in the last three)."""
    cards = read_cards(state)
    decisions = [read_decision(state, c) for c in cards]
    undecided = [c for c, d in zip(cards, decisions) if d is None]
    oldest_minutes = None
    if undecided:
        now_ts = time.time() if now is None else now
        oldest_minutes = int((now_ts - min(float(c.get("created", 0)) for c in undecided)) // 60)
    expired_unhandled = sum(1 for c in undecided if minutes_left(c, now) < 0)
    states = [w["state"] for w in read_writes(state, now, limit=WRITES_SCAN)]
    return {"pending": len(undecided), "oldest_minutes": oldest_minutes, "expired_unhandled": expired_unhandled,
            "approved": sum(1 for d in decisions if d is not None and d.get("decision") == "approve"),
            "running": states.count("running"),
            "made": states.count("made"), "failed": states.count("failed")}


def cmd_summary(state, _args):
    # One line, seven whitespace-separated fields, for a caller (bin/factory)
    # to read with `set --` rather than parsing JSON: undecided pending count,
    # the oldest undecided card's age in minutes (-1 if none), expired-unhandled
    # count, approved cards whose write the gateway has yet to start, then deferred writes
    # being made, made (last day) and failed (last three days).
    s = summarize(state)
    oldest = -1 if s["oldest_minutes"] is None else s["oldest_minutes"]
    print("{} {} {} {} {} {} {}".format(s["pending"], oldest, s["expired_unhandled"], s["approved"], s["running"],
                                        s["made"], s["failed"]))
    return 0


def watch_once(state, seen, queued, writes, out=print):
    """One pass of watch: print each new card, each card that has become
    staged and each deferred write that is new or has changed state. seen,
    queued and writes (id -> state) carry what was printed between passes."""
    stamp = time.strftime("%H:%M:%S")
    for card in read_cards(state):
        if card["id"] not in seen:
            seen.add(card["id"])
            out("{} {}".format(stamp, one_line(card)))
        if card["id"] not in queued and is_queued(state, card):
            queued.add(card["id"])
            out("{} {}  [{}]".format(stamp, clean(card["id"], 64), QUEUED_NOTE))
    for item in read_writes(state):
        if writes.get(item["id"]) != item["state"]:
            writes[item["id"]] = item["state"]
            out("{} {}".format(stamp, write_line(item)))


def cmd_watch(state, _args):
    seen, queued, writes = set(), set(), {}
    print("Watching {} (Ctrl-C to stop)".format(state / "approvals" / "pending"))
    try:
        while True:
            watch_once(state, seen, queued, writes)
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="factory approvals", description=__doc__.splitlines()[0])
    p.add_argument("--state", type=Path, default=None, help="broker state directory")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("id")
    for name in ("approve", "decline"):
        s = sub.add_parser(name)
        s.add_argument("id")
        s.add_argument("--as", dest="as_name", default=None, help="approver name to record (default: your user name)")
        s.add_argument("--reason", default="", help="shown to the agent and kept on the record")
    sub.add_parser("watch")
    sub.add_parser("summary")
    args = p.parse_args(argv)
    state = args.state or default_state()
    if not state.is_dir():
        print("broker state directory not found: {} (has the factory been started?)".format(state), file=sys.stderr)
        return 1
    command = args.command or "list"
    if command == "list":
        return cmd_list(state, args)
    if command == "show":
        return cmd_show(state, args)
    if command in ("approve", "decline"):
        return decide(state, args, command)
    if command == "watch":
        return cmd_watch(state, args)
    if command == "summary":
        return cmd_summary(state, args)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
