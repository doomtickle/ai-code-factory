#!/usr/bin/env python3
"""factory approvals: decide what the gateway is holding for a person.

The gateway records each held push or pull request as a card under the
broker state directory (approvals/pending/<id>.json) and waits for a
decision file (approvals/decisions/<id>.json) that this console writes. It
runs on the host with the system Python (3.9 on macOS), standard library
only, and touches nothing but those files. The dashboard's control unit
(scripts/dashboard_controls.py) writes decisions through write_decision()
here, so both write the same record.

    factory approvals                      pending cards (decided ones marked)
    factory approvals show <id>
    factory approvals approve <id> [--as NAME] [--reason TEXT]
    factory approvals decline <id> [--as NAME] [--reason TEXT]
    factory approvals watch                print cards as they arrive
    factory approvals summary              one line for scripts: "pending oldest_min expired_unhandled decided"

A decision is final for its card: the gateway keeps the card in pending/
until the agent retries the held call (the retry picks the decision up), and
a second approve or decline of the same card is refused, here and in the
dashboard, rather than overwriting the first.
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
from pathlib import Path


# What a card id may be before it names a file (decisions/<id>.json). Passdesk
# Pro's ids fit; anything else is refused rather than joined onto a path.
CARD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


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


def minutes_left(card, now=None):
    now = time.time() if now is None else now
    return int((float(card.get("expires", 0)) - now) // 60)


def one_line(card):
    tool = card.get("tool")
    who = card.get("principal") or "?"
    if tool == "git_push":
        what = "push {} -> {} ({} commit(s), {} file(s))".format(
            card.get("branch"), card.get("repo"), card.get("commits"), card.get("file_count"))
    elif tool in ("github_create_pr", "gitlab_create_mr"):
        what = "{}{} on {}: {} -> {} \"{}\"".format(
            "draft " if card.get("draft", True) else "", "MR" if tool == "gitlab_create_mr" else "PR",
            card.get("repo"), card.get("head"), card.get("base"), (card.get("title") or "")[:60])
    else:
        what = tool
    return "{}  {:<10} {}  ({} min left)".format(card["id"], who, what, minutes_left(card))


def status_note(state, card, now=None):
    """What the card is waiting for, if not for a decision: "" while it is."""
    info = decided(state, card)
    if info["decided"]:
        return info, decided_text(info)
    if minutes_left(card, now) < 0:
        return info, "expired; the agent's retry will make a fresh card"
    return info, ""


def cmd_list(state, _args):
    cards = read_cards(state)
    if not cards:
        print("No approvals pending.")
        return 0
    for card in cards:
        _, note = status_note(state, card)
        print(one_line(card) + ("\n    [{}]".format(note) if note else ""))
    print("\nfactory approvals show <id> | approve <id> | decline <id> --reason ...")
    return 0


def cmd_show(state, args):
    card = find(state, args.id)
    if not card:
        print("no pending approval matches {}".format(args.id), file=sys.stderr)
        return 1
    skip = {"created", "expires", "nonce"}
    info, note = status_note(state, card)
    print("approval {}  ({} min left)".format(card["id"], minutes_left(card)))
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
            print("  {:<14}".format(key))
            for item in val:
                print("    {}".format(item))
        else:
            print("  {:<14} {}".format(key, val))
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


def decided_text(info):
    """ "approved by X at T; waiting for the agent to retry" (or declined)."""
    return "{} by {} at {}; waiting for the agent to retry".format(
        "approved" if info["decided"] == "approve" else "declined", info["decided_by"], when(info["decided_at"]))


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
    info = decided(state, card)
    if info["decided"]:
        print("approval {} is already {}".format(card["id"], decided_text(info)), file=sys.stderr)
        return 1
    if minutes_left(card) < 0:
        print("approval {} has expired; the agent's retry will make a fresh one".format(card["id"]), file=sys.stderr)
        return 1
    if verdict == "decline" and not args.reason:
        print("give the agent a reason: --reason \"...\"", file=sys.stderr)
        return 1
    approver = args.as_name or default_approver()
    try:
        write_decision(state, card, verdict, approver, args.reason)
    except AlreadyDecided as e:  # someone else decided it a moment ago
        info = decision_info(e.decision)
        print("approval {} is already {}".format(card["id"], decided_text(info)), file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print("cannot decide {}: {}".format(card["id"], e), file=sys.stderr)
        return 1
    print("{} {} as {}: {}".format("approved" if verdict == "approve" else "declined", card["id"], approver,
                                   one_line(card)))
    if verdict == "approve":
        print("The agent's call proceeds if it is still waiting; otherwise its retry will.")
    return 0


def is_expired_unhandled(state, card, now=None):
    """A card past its expiry with no decision that still matches it.

    The gateway sweeps expired cards out of pending/ on its own, but only
    while the broker is actively handling hook() calls; one left behind
    (broker idle, agent gone) is still expired and still unhandled. A
    decision file whose nonce doesn't match the card's nonce belongs to an
    earlier card with the same id (a retry after expiry mints a fresh
    nonce), so it doesn't count as having handled this one.
    """
    if minutes_left(card, now) >= 0:
        return False
    return read_decision(state, card) is None


def summarize(state, now=None):
    """pending: cards still waiting for a person (no decision of their own);
    oldest_minutes: the oldest of those, in minutes (None if none);
    expired_unhandled: expired cards with no decision; decided: cards decided
    and waiting for the agent's retry (they need no one)."""
    cards = read_cards(state)
    undecided = [c for c in cards if read_decision(state, c) is None]
    oldest_minutes = None
    if undecided:
        now_ts = time.time() if now is None else now
        oldest_minutes = int((now_ts - min(float(c.get("created", 0)) for c in undecided)) // 60)
    expired_unhandled = sum(1 for c in undecided if minutes_left(c, now) < 0)
    return {"pending": len(undecided), "oldest_minutes": oldest_minutes, "expired_unhandled": expired_unhandled,
            "decided": len(cards) - len(undecided)}


def cmd_summary(state, _args):
    # One line, four whitespace-separated fields, for a caller (bin/factory)
    # to read with `set --` rather than parsing JSON: undecided pending count,
    # the oldest undecided card's age in minutes (-1 if none), expired-unhandled
    # count, and decided cards waiting for the agent's retry.
    s = summarize(state)
    oldest = -1 if s["oldest_minutes"] is None else s["oldest_minutes"]
    print("{} {} {} {}".format(s["pending"], oldest, s["expired_unhandled"], s["decided"]))
    return 0


def cmd_watch(state, _args):
    seen = set()
    print("Watching {} (Ctrl-C to stop)".format(state / "approvals" / "pending"))
    try:
        while True:
            for card in read_cards(state):
                if card["id"] not in seen:
                    seen.add(card["id"])
                    print(time.strftime("%H:%M:%S"), one_line(card))
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
