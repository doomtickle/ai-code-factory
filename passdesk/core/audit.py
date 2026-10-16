"""The append-only JSONL audit log in the state directory."""

from __future__ import annotations

import json
import os
import threading
import time

from . import config
from .filters import redact

_audit_lock = threading.Lock()


def audit(record: dict) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _audit_lock:
        with open(config.STATE / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def summarize_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        s = redact(v if isinstance(v, str) else json.dumps(v))
        out[k] = s if len(s) <= 500 else s[:500] + "...[truncated]"
    return out


# --------------------------------------------------------------------------
