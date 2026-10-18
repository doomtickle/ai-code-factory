"""Plugins: modules named in PASSDESK_PLUGINS, imported once at start-up.

A plugin module may define `activate() -> bool`, called once after import
to register what it provides (an approval hook via
core.approvals.set_hook, credential providers via
core.credentials.register_provider); importing alone has no side effects.
Nothing is loaded unless named, so a gateway without the variable behaves
exactly as the core alone. Modules are found on PYTHONPATH; the image puts its optional
`plugins` build context there.
"""

from __future__ import annotations

import importlib

from . import config
from .audit import audit


def load() -> list[str]:
    """Import every module named in config.PLUGINS. Returns the names loaded.

    A plugin that fails to import stops the server: a deployment that asked
    for one should not silently run without it.
    """
    loaded: list[str] = []
    active: list[str] = []
    for name in [n.strip() for n in config.PLUGINS.split(",") if n.strip()]:
        module = importlib.import_module(name)
        loaded.append(name)
        activate = getattr(module, "activate", None)
        if callable(activate) and activate():
            active.append(name)
    if loaded:
        audit({"event": "plugins", "loaded": loaded, "active": active})
    return loaded
