"""passdesk CLI.

    python3 -m passdesk principal add <name> --tools TOOL [TOOL ...]
    python3 -m passdesk principal list
    python3 -m passdesk principal revoke <name>
    python3 -m passdesk egress-guard [--bind HOST] [--port PORT] [--max-connections N]

`principal` mints and manages the principals a running broker
authenticates: tokens in BROKER_STATE/tokens/<name>.sha256 and grants in
BROKER_STATE/grants.json - uses the same files and formats
scripts/start-broker.sh writes (in a factory deployment, start-broker.sh
owns grants.json and rebuilds it on every start). See passdesk/principal.py
for the on-disk formats.

`egress-guard` runs the CONNECT allowlist proxy an agent container's model
traffic goes through in a standalone deployment (passdesk/egress_guard.py).
"""

from __future__ import annotations

import argparse
import sys

from . import egress_guard, principal


def _add(args: argparse.Namespace) -> int:
    try:
        token = principal.add(args.name, args.tools)
    except principal.PrincipalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"principal {args.name!r} created with tools: {', '.join(sorted(args.tools))}", file=sys.stderr)
    print("token (shown once; it is not stored anywhere - save it now):", file=sys.stderr)
    print(token)
    return 0


def _list(args: argparse.Namespace) -> int:
    people = principal.list_principals()
    if not people:
        print("no principals", file=sys.stderr)
        return 0
    for p in people:
        tools_text = ", ".join(p.tools) if p.tools else "(no tools granted)"
        flag = "" if p.has_token else "  [no token: cannot authenticate]"
        print(f"{p.name}: {tools_text}{flag}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    try:
        principal.revoke(args.name)
    except principal.PrincipalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"principal {args.name!r} revoked", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m passdesk", description="Manage a Passdesk deployment.")
    sub = parser.add_subparsers(dest="command", required=True)

    principal_parser = sub.add_parser("principal", help="manage principals (tokens and tool grants)")
    principal_sub = principal_parser.add_subparsers(dest="principal_command", required=True)

    add_parser = principal_sub.add_parser("add", help="create a principal and mint its token")
    add_parser.add_argument("name", help="principal name (lowercase letters, digits, '_' or '-')")
    add_parser.add_argument("--tools", nargs="+", required=True, metavar="TOOL",
                            help="tool name(s) to grant, e.g. --tools git_fetch npm_fetch")
    add_parser.set_defaults(func=_add)

    list_parser = principal_sub.add_parser("list", help="list principals and their granted tools")
    list_parser.set_defaults(func=_list)

    revoke_parser = principal_sub.add_parser("revoke", help="remove a principal's token and grant")
    revoke_parser.add_argument("name", help="principal name")
    revoke_parser.set_defaults(func=_revoke)

    egress_guard.add_parser(sub)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
