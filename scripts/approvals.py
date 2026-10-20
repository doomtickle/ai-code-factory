#!/usr/bin/env python3
"""factory approvals: decide what the gateway is holding for a person.

Passdesk Pro records each held push or pull request as a card under the
broker state directory (approvals/pending/<id>.json) and waits for a
decision file (approvals/decisions/<id>.json) that this console writes. It
runs on the host with the system Python (3.9 on macOS), standard library
only, and touches nothing but those files. The dashboard's control unit
(scripts/dashboard_controls.py) writes decisions through write_decision()
here, so both write the same record.

    factory approvals                      pending cards
    factory approvals show <id>
    factory approvals approve <id> [--as NAME] [--reason TEXT]
    factory approvals decline <id> [--as NAME] [--reason TEXT]
    factory approvals watch                print cards as they arrive
    factory approvals summary              one line for scripts: "pending oldest_min expired_unhandled"
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
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
    elif tool == "github_create_pr":
        what = "{}PR on {}: {} -> {} \"{}\"".format(
            "draft " if card.get("draft", True) else "", card.get("repo"), card.get("head"), card.get("base"),
            (card.get("title") or "")[:60])
    else:
        what = tool
    return "{}  {:<10} {}  ({} min left)".format(card["id"], who, what, minutes_left(card))


def cmd_list(state, _args):
    cards = read_cards(state)
    if not cards:
        print("No approvals pending.")
        return 0
    for card in cards:
        print(one_line(card))
    print("\nfactory approvals show <id> | approve <id> | decline <id> --reason ...")
    return 0


def cmd_show(state, args):
    card = find(state, args.id)
    if not card:
        print("no pending approval matches {}".format(args.id), file=sys.stderr)
        return 1
    skip = {"created", "expires", "nonce"}
    print("approval {}  ({} min left)".format(card["id"], minutes_left(card)))
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


def card_fingerprint(card):
    """A short public stand-in for the card's nonce: the first 16 hex digits of
    sha256(id NUL nonce). The dashboard's control unit sends it back with a
    decision, so a decision meant for one card never lands on a newer card
    with the same id; the nonce itself never leaves this machine's files."""
    raw = "{}\0{}".format(card.get("id") or "", card.get("nonce") or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def write_decision(state, card, verdict, approver, reason=""):
    """Write the decision file Passdesk Pro waits for (approvals/decisions/<id>.json),
    atomically (a temporary file, then os.replace). Used by this console and
    by the dashboard's control unit, so both write exactly the same record.
    Returns the decision."""
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
    # one), in the same folder so os.replace is atomic; removed on failure.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".{}.".format(card["id"]), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(decision, indent=2) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return decision


def default_approver():
    return os.environ.get("USER") or getpass.getuser()


def decide(state, args, verdict):
    card = find(state, args.id)
    if not card:
        print("no pending approval matches {}".format(args.id), file=sys.stderr)
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
    except ValueError as e:
        print("cannot decide {}: {}".format(card["id"], e), file=sys.stderr)
        return 1
    print("{} {} as {}: {}".format("approved" if verdict == "approve" else "declined", card["id"], approver,
                                   one_line(card)))
    if verdict == "approve":
        print("The agent's call proceeds if it is still waiting; otherwise its retry will.")
    return 0


def is_expired_unhandled(state, card, now=None):
    """A card past its expiry with no decision that still matches it.

    passdesk-pro sweeps expired cards out of pending/ on its own, but only
    while the broker is actively handling hook() calls; one left behind
    (broker idle, agent gone) is still expired and still unhandled. A
    decision file whose nonce doesn't match the card's nonce belongs to an
    earlier card with the same id (a retry after expiry mints a fresh
    nonce), so it doesn't count as having handled this one.
    """
    if minutes_left(card, now) >= 0:
        return False
    path = state / "approvals" / "decisions" / "{}.json".format(card["id"])
    try:
        decision = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return decision.get("nonce") != card.get("nonce")


def summarize(state, now=None):
    """pending count, the oldest card's age in minutes (None if none), expired-unhandled count."""
    cards = read_cards(state)
    oldest_minutes = None
    if cards:
        now_ts = time.time() if now is None else now
        oldest_minutes = int((now_ts - min(float(c.get("created", 0)) for c in cards)) // 60)
    expired_unhandled = sum(1 for c in cards if is_expired_unhandled(state, c, now))
    return {"pending": len(cards), "oldest_minutes": oldest_minutes, "expired_unhandled": expired_unhandled}


def cmd_summary(state, _args):
    # One line, three whitespace-separated fields, for a caller (bin/factory)
    # to read with `set --` rather than parsing JSON: pending count, the
    # oldest card's age in minutes (-1 if none pending), expired-unhandled count.
    s = summarize(state)
    oldest = -1 if s["oldest_minutes"] is None else s["oldest_minutes"]
    print("{} {} {}".format(s["pending"], oldest, s["expired_unhandled"]))
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
