#!/usr/bin/env python3
"""factory approvals: decide what the gateway is holding for a person.

Passdesk Pro records each held push or pull request as a card under the
broker state directory (approvals/pending/<id>.json) and waits for a
decision file (approvals/decisions/<id>.json) that this console writes. It
runs on the host with the system Python (3.9 on macOS), standard library
only, and touches nothing but those files.

    factory approvals                      pending cards
    factory approvals show <id>
    factory approvals approve <id> [--as NAME] [--reason TEXT]
    factory approvals decline <id> [--as NAME] [--reason TEXT]
    factory approvals watch                print cards as they arrive
"""

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path


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
        if isinstance(card, dict) and card.get("id"):
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
    approver = args.as_name or os.environ.get("USER") or getpass.getuser()
    # The nonce ties the decision to this card: a later card with the same id
    # (the same push, re-requested after this one expired) needs a new decision.
    decision = {"id": card["id"], "nonce": card.get("nonce"), "decision": verdict, "approver": approver,
                "reason": args.reason or "", "ts": time.time()}
    path = state / "approvals" / "decisions" / "{}.json".format(card["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    print("{} {} as {}: {}".format("approved" if verdict == "approve" else "declined", card["id"], approver,
                                   one_line(card)))
    if verdict == "approve":
        print("The agent's call proceeds if it is still waiting; otherwise its retry will.")
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
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
