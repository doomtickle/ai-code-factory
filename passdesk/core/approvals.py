"""The approval point: where a deployment can hold an external write for a
person (docs/pro-approvals.md).

A tool that is about to write outside the broker (push, open a pull request)
calls `require(card)` after every check of its own has passed, with a card
describing exactly what is about to happen. By default `require` returns at
once. A plugin may install a hook with `set_hook`; the hook returns to allow
the write or raises Denied to refuse it, and may take its time deciding.

Cards hold only what the broker itself verified (never a credential, never
content from the network) so they can be shown to a person and written to
the record as they are.
"""

from __future__ import annotations

import contextvars
from typing import Callable

from . import filters

Hook = Callable[[dict], None]

# The principal whose call is running, set by the protocol layer per call so
# a card can say who asked.
PRINCIPAL: contextvars.ContextVar[str] = contextvars.ContextVar("passdesk_principal", default="")

_hook: Hook | None = None


def set_hook(fn: Hook | None) -> None:
    """Install (or, with None, remove) the hook every external write goes through."""
    global _hook
    _hook = fn


def hook_installed() -> bool:
    return _hook is not None


def _redacted(value):
    """Recursively redact known secret patterns in every string value.

    A credential-looking string (a leaked token pasted into a PR title, say)
    must not reach a notification webhook, the pending card file or the
    console verbatim, however it got into the card. Non-string values (ints,
    bools, the digest in `bundle_sha256`) pass through untouched.
    """
    if isinstance(value, str):
        return filters.redact(value)
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    return value


def require(card: dict) -> None:
    """Hold here until the write described by `card` may proceed.

    Returns to allow it; raises Denied (from the hook) to refuse it. The
    card gains the calling principal before the hook sees it, and every
    string value in the copy the hook sees has gone through
    `filters.redact` - so a leaked credential cannot reach a notification,
    the pending card file or the console through a card field (the title,
    say). The real tool call already ran with the unredacted arguments;
    only what gets shown to an approver is scrubbed here.
    """
    if _hook is None:
        return
    _hook(_redacted({**card, "principal": PRINCIPAL.get()}))
