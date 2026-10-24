"""The tool registry. Each module in passdesk/tools/ registers its tools on
import; importing passdesk.tools registers them all.

An entry is name -> (handler, description, JSON-schema properties, required
argument names). A handler takes (args, policy) and returns the text sent back
to the agent, or raises Denied.

Plugins add tools by declaring them, never by calling register() themselves:
a plugin module named in PASSDESK_PLUGINS sets PASSDESK_TOOL_NAMESPACE and
PASSDESK_TOOLS (a dict of PluginTool), core.plugins validates the declaration
with plugin_tools() and registers it with register_plugin_tools(). A
registered plugin tool runs behind guard(): every string argument is
secret-scanned before the handler sees it, a tool that writes outside the
gateway goes through the approval point first, and whatever the handler
returns is sanitized and wrapped as untrusted. Grants, argument checks and
audit records are the protocol layer's, the same as for a core tool.
docs/plugin-tools.md is the guide for plugin authors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import approvals, filters
from .errors import Denied

Handler = Callable[[dict, dict], str]
TOOLS: dict[str, tuple[Handler, str, dict, list[str]]] = {}


def register(tools: dict) -> None:
    for name, entry in tools.items():
        if name in TOOLS:
            raise RuntimeError(f"broker tool {name!r} is registered twice")
        TOOLS[name] = entry


def tool_list() -> list[dict]:
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req}}
            for name, (_, desc, props, req) in TOOLS.items()]


# --- plugin tools ---------------------------------------------------------------------

@dataclass(frozen=True)
class PluginTool:
    """One tool a plugin declares in PASSDESK_TOOLS.

    handler(args, policy) returns text for the agent or raises Denied.
    properties maps each argument name to {"type": "string" | "boolean"} with
    an optional "description"; the protocol layer enforces exactly these
    types, so nothing else is accepted. writes_externally marks a tool whose
    effect lands outside the gateway: it gets an "intent" audit record before
    it runs and goes through the approval point (`[approvals].tools` can hold it).
    hosts lists the hosts the handler contacts, for the doctor's egress
    check; declaring them doesn't open the network.
    """

    handler: Handler
    description: str
    properties: dict = field(default_factory=dict)
    required: tuple[str, ...] = ()
    writes_externally: bool = False
    hosts: tuple[str, ...] = ()


class PluginToolError(Exception):
    """A plugin's tool declaration is invalid."""


_NAMESPACE_RE = re.compile(r"[a-z][a-z0-9]{1,15}")
_SUFFIX_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_ARG_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
_MAX_NAME = 64
_MAX_TOOLS = 32
_MAX_DESCRIPTION = 1000
_ARG_TYPES = {"string", "boolean"}
# Card fields the gateway sets on an approval card; an argument can't take their place.
_RESERVED_ARGS = {"tool", "principal"}
# Namespaces a plugin may not use: the prefixes of core tools (and any core
# tool registered later), plus names that read as the gateway itself.
_RESERVED = {"git", "github", "web", "gemini", "jira", "npm", "pip",
             "passdesk", "broker", "core", "mcp", "factory", "approvals", "plugin", "plugins"}

PLUGIN_TOOLS: dict[str, str] = {}  # tool name -> the plugin module that declared it
WRITES_EXTERNALLY: set[str] = set()  # plugin tools declared writes_externally
_namespaces: dict[str, str] = {}  # namespace -> plugin module


def _reserved() -> set[str]:
    core = {n.split("_", 1)[0] for n in TOOLS if n not in PLUGIN_TOOLS}
    return _RESERVED | core


def plugin_tools(module_name: str, module: object) -> tuple[str, dict[str, PluginTool]] | None:
    """The (namespace, tools) a plugin module declares, validated; None if it
    declares none. Raises PluginToolError naming the first problem. Reads
    module attributes only, so the doctor and the principal CLI can call it
    without activating the plugin."""
    tools = getattr(module, "PASSDESK_TOOLS", None)
    namespace = getattr(module, "PASSDESK_TOOL_NAMESPACE", None)
    if tools is None and namespace is None:
        return None
    where = f"plugin {module_name}"
    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        raise PluginToolError(f"{where}: PASSDESK_TOOL_NAMESPACE must be 2-16 lowercase letters or digits, "
                              f"starting with a letter (got {namespace!r})")
    if namespace in _reserved():
        raise PluginToolError(f"{where}: namespace {namespace!r} is reserved for the gateway's own tools")
    if not isinstance(tools, dict) or not tools:
        raise PluginToolError(f"{where}: PASSDESK_TOOLS must be a non-empty dict of name -> PluginTool")
    if len(tools) > _MAX_TOOLS:
        raise PluginToolError(f"{where}: declares {len(tools)} tools; at most {_MAX_TOOLS}")
    for name, spec in tools.items():
        _check_tool(where, namespace, name, spec)
    return namespace, dict(tools)


def _check_tool(where: str, namespace: str, name: object, spec: object) -> None:
    prefix = namespace + "_"
    if not isinstance(name, str) or not name.startswith(prefix) or len(name) > _MAX_NAME \
            or not _SUFFIX_RE.fullmatch(name[len(prefix):]):
        raise PluginToolError(f"{where}: tool name {name!r} must be {prefix}<lowercase words joined by _>, "
                              f"at most {_MAX_NAME} characters")
    where = f"{where}, tool {name}"
    if not isinstance(spec, PluginTool):
        raise PluginToolError(f"{where}: must be a passdesk.core.registry.PluginTool")
    if not callable(spec.handler):
        raise PluginToolError(f"{where}: handler is not callable")
    if not isinstance(spec.description, str) or not spec.description.strip() \
            or len(spec.description) > _MAX_DESCRIPTION:
        raise PluginToolError(f"{where}: description must be 1-{_MAX_DESCRIPTION} characters")
    if not isinstance(spec.properties, dict):
        raise PluginToolError(f"{where}: properties must be a dict")
    for arg, schema in spec.properties.items():
        if not isinstance(arg, str) or not _ARG_RE.fullmatch(arg):
            raise PluginToolError(f"{where}: argument name {arg!r} must be lowercase letters, digits and _")
        if arg in _RESERVED_ARGS:
            raise PluginToolError(f"{where}: argument name {arg!r} is reserved (the approval card sets it)")
        if not isinstance(schema, dict) or schema.get("type") not in _ARG_TYPES \
                or set(schema) - {"type", "description"} \
                or not isinstance(schema.get("description", ""), str):
            raise PluginToolError(f"{where}: argument {arg!r} must be {{\"type\": \"string\" or \"boolean\"}} "
                                  "with an optional \"description\" string, nothing else")
    required = spec.required
    if not isinstance(required, (tuple, list)) or not all(isinstance(r, str) for r in required) \
            or len(set(required)) != len(required) or not set(required) <= set(spec.properties):
        raise PluginToolError(f"{where}: required must list distinct argument names from properties")
    if not isinstance(spec.writes_externally, bool):
        raise PluginToolError(f"{where}: writes_externally must be True or False")
    if not isinstance(spec.hosts, (tuple, list)) or \
            not all(isinstance(h, str) and _HOST_RE.fullmatch(h) for h in spec.hosts):
        raise PluginToolError(f"{where}: hosts must list lowercase host names, e.g. (\"status.example.com\",)")


def register_plugin_tools(module_name: str, namespace: str, tools: dict[str, PluginTool]) -> None:
    """Register a validated declaration (plugin_tools()). Refuses a namespace
    another plugin already uses and, through register(), any name that is
    already registered: a plugin can never replace a core tool."""
    if namespace in _reserved():
        raise PluginToolError(f"plugin {module_name}: namespace {namespace!r} is reserved for the gateway's own tools")
    owner = _namespaces.get(namespace)
    if owner is not None:
        raise PluginToolError(f"plugin {module_name}: namespace {namespace!r} is already used by plugin {owner}")
    entries = {name: (guard(name, spec), spec.description,
                      {arg: dict(schema) for arg, schema in spec.properties.items()}, list(spec.required))
               for name, spec in tools.items()}
    register(entries)  # names carry the namespace; register() still refuses any duplicate
    _namespaces[namespace] = module_name
    for name, spec in tools.items():
        PLUGIN_TOOLS[name] = module_name
        if spec.writes_externally:
            WRITES_EXTERNALLY.add(name)


def guard(name: str, spec: PluginTool) -> Handler:
    """The handler registered for a plugin tool: the gateway's own checks
    around the plugin's handler, which a plugin cannot skip."""

    def run(args: dict, policy: dict) -> str:
        for key, val in args.items():
            if isinstance(val, str):
                filters.block_outbound_secrets(f"argument {key!r}", val)
        if spec.writes_externally:
            # The gateway's own fields last, so no argument can stand in for them.
            approvals.require({**args, "tool": name})
        try:
            out = spec.handler(dict(args), policy)
        except Denied as e:
            # A plugin's refusal may quote what a remote service said: clean
            # it like any other text that reaches the agent.
            raise Denied(filters.sanitize(str(e), 1000)) from None
        if not isinstance(out, str):
            raise TypeError(f"plugin tool {name} returned {type(out).__name__}, not text")
        return filters.untrusted(f"plugin tool {name}", filters.sanitize(out))

    return run
