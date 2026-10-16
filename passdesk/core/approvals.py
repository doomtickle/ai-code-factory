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


def require(card: dict) -> None:
    """Hold here until the write described by `card` may proceed.

    Returns to allow it; raises Denied (from the hook) to refuse it. The
    card gains the calling principal before the hook sees it.
    """
    if _hook is None:
        return
    _hook({**card, "principal": PRINCIPAL.get()})
