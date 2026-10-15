"""The tool registry. Each module in agentbroker/tools/ registers its tools on
import; importing agentbroker.tools registers them all.

An entry is name -> (handler, description, JSON-schema properties, required
argument names). A handler takes (args, policy) and returns the text sent back
to the agent, or raises Denied.
"""

from __future__ import annotations

from typing import Callable

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
