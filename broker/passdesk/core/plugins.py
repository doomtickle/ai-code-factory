"""Plugins: modules named in PASSDESK_PLUGINS, imported once at start-up.

A plugin module may declare tools (PASSDESK_TOOL_NAMESPACE and
PASSDESK_TOOLS; see core/registry.py and docs/plugin-tools.md), which load()
validates and registers before anything else, and may define `activate() ->
bool`, called once after that to register what else it provides (an
approval hook via core.approvals.set_hook, which replaces the core hold's
own hook if the policy enabled it, credential providers via
core.credentials.register_provider); importing alone has no side effects.
Nothing is loaded unless named, so a gateway without the variable behaves
exactly as the core alone. Modules are found on PYTHONPATH; the image puts its optional
`plugins` build context there.
"""

from __future__ import annotations

import importlib

from . import config, registry
from .audit import audit


# The commercial add-on whose hold moved into core (core/holds.py,
# docs/decisions.md 2026-10-21). An old copy still named here would import
# fine and put its own, licence-checking hook over core's, so it is refused
# by name rather than loaded.
RETIRED_PRO = "passdesk_pro"
RETIRED_PRO_HINT = ("passdesk_pro is no longer a plugin: its hold is part of the gateway. Remove it from "
                    "PASSDESK_PLUGINS (and the image's plugins directory) and list the tools to hold in "
                    "[approvals].tools instead")


def names() -> list[str]:
    return [n.strip() for n in config.PLUGINS.split(",") if n.strip()]


def load() -> list[str]:
    """Import every module named in config.PLUGINS. Returns the names loaded.

    A plugin that fails to import, or declares tools that don't validate,
    stops the server: a deployment that asked for one should not silently
    run without it.
    """
    loaded: list[str] = []
    active: list[str] = []
    tools: list[str] = []
    for name in names():
        if name == RETIRED_PRO:
            raise ImportError(RETIRED_PRO_HINT)
        module = importlib.import_module(name)
        loaded.append(name)
        declared = registry.plugin_tools(name, module)
        if declared is not None:
            registry.register_plugin_tools(name, *declared)
            tools.extend(declared[1])
        activate = getattr(module, "activate", None)
        if callable(activate) and activate():
            active.append(name)
    if loaded:
        audit({"event": "plugins", "loaded": loaded, "active": active, "tools": tools})
    return loaded


def declared_tools() -> tuple[dict[str, registry.PluginTool], list[tuple[str, str]]]:
    """The tools the plugins in PASSDESK_PLUGINS declare, without activating
    any of them (for the doctor and the principal CLI), and a list of
    (plugin, problem) for each plugin that can't be imported or whose
    declaration is invalid."""
    tools: dict[str, registry.PluginTool] = {}
    problems: list[tuple[str, str]] = []
    for name in names():
        if name == RETIRED_PRO:
            problems.append((name, RETIRED_PRO_HINT))
            continue
        try:
            module = importlib.import_module(name)
            declared = registry.plugin_tools(name, module)
        except ImportError as e:
            problems.append((name, f"cannot be imported: {e}"))
            continue
        except registry.PluginToolError as e:
            problems.append((name, str(e)))
            continue
        if declared is not None:
            clash = sorted(set(declared[1]) & set(tools))
            if clash:
                problems.append((name, f"declares tool(s) another plugin declares: {', '.join(clash)}"))
                continue
            tools.update(declared[1])
    return tools, problems
