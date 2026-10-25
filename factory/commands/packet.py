"""factory packet [<job-id> | latest | <lane> | --list]: print a job's review packet, which the
dispatch bridge writes after every job to dispatch/packets/<job-id>.md in the state folder.
Job ids start with the job's timestamp, so reverse name order is newest first."""

import os
import re

from factory.context import FactoryError

_ID = re.compile(r"[A-Za-z0-9-]{1,80}")
_USAGE = "usage: factory packet [<job-id> | latest | <lane> | --list]"


def _emit(ctx, data):
    """Write bytes as they are when stdout has a byte stream (a packet is printed exactly)."""
    buffer = getattr(ctx.stdout, "buffer", None)
    if buffer is not None:
        ctx.stdout.flush()
        buffer.write(data)
        buffer.flush()
    else:
        ctx.stdout.write(data.decode("utf-8", "replace"))


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise FactoryError("cannot read %s: %s" % (path, e.strerror))


def _packets(entries):
    """The packet ids (file names without .md), newest first."""
    return sorted((n[:-3] for n in entries if n.endswith(".md") and not n.startswith(".")), reverse=True)


def _title(folder, packet_id):
    first = _read(os.path.join(folder, packet_id + ".md")).split(b"\n", 1)[0]
    return first[2:] if first.startswith(b"# ") else b""


def run(ctx, args):
    folder = os.path.join(ctx.paths.dispatch_state, "packets")
    which = args[0] if args and args[0] else "latest"
    if len(args) > 1:
        raise FactoryError(_USAGE)
    try:
        entries = os.listdir(folder)
    except OSError:
        entries = []
    if not entries:
        raise FactoryError("no review packets yet (the dispatch bridge writes one per job to %s)" % folder)
    packets = _packets(entries)
    if which in ("--list", "-l", "list"):
        for packet_id in packets[:20]:
            _emit(ctx, packet_id.encode("utf-8", "surrogateescape") + b"  " + _title(folder, packet_id) + b"\n")
        return 0
    if which == "latest":
        if not packets:
            raise FactoryError("no review packets yet (the dispatch bridge writes one per job to %s)" % folder)
        chosen = packets[0]
    elif not _ID.fullmatch(which):
        raise FactoryError("not a job id or lane: %s" % which)
    elif os.path.isfile(os.path.join(folder, which + ".md")):
        chosen = which
    else:
        # A lane name: its newest packet.
        marker = ("- Lane: %s" % which).encode()
        chosen = next((i for i in packets if marker in _read(os.path.join(folder, i + ".md")).split(b"\n")), None)
        if chosen is None:
            raise FactoryError("no packet for job or lane '%s' (see: factory packet --list)" % which)
    _emit(ctx, _read(os.path.join(folder, chosen + ".md")))
    return 0
