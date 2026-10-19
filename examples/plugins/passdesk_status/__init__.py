"""Example Passdesk plugin: one read-only tool, `status_check`, that reports a
service's status from its public Statuspage page (docs/plugin-tools.md).

Install by putting this directory in the image's `plugins` build context and
naming it in PASSDESK_PLUGINS=passdesk_status. Then allow the pages in the
deployment's policy.local.toml:

    [plugins.status]
    pages = { github = "www.githubstatus.com", npm = "status.npmjs.org" }

and grant `status_check` to the principals that may use it. The tool can
only reach hosts listed in both HOSTS (what this code may ever contact) and
the policy (what this deployment allows). It needs no credential; a tool
that does would read one with passdesk.core.credentials.get(name).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from passdesk.core import config
from passdesk.core.errors import Denied
from passdesk.core.net import NO_REDIRECT_OPENER
from passdesk.core.registry import PluginTool

VERSION = "0.1.0"
HOSTS = ("www.githubstatus.com", "status.npmjs.org")
_MAX_BYTES = 65536
_open = NO_REDIRECT_OPENER.open  # tests replace this


def _pages(policy: dict) -> dict[str, str]:
    section = policy.get("plugins", {}).get("status", {}) if isinstance(policy.get("plugins"), dict) else {}
    pages = section.get("pages", {}) if isinstance(section, dict) else {}
    return {k: v for k, v in pages.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(pages, dict) else {}


def check(args: dict, policy: dict) -> str:
    pages = _pages(policy)
    page = args["page"]
    host = pages.get(page)
    if host is None:
        raise Denied(f"page {page!r} is not configured; allowed pages: {', '.join(sorted(pages)) or 'none'} "
                     "(policy [plugins.status].pages)")
    if host not in HOSTS:
        raise Denied(f"page {page!r} points at {host}, which this plugin is not built to contact")
    req = urllib.request.Request(f"https://{host}/api/v2/status.json",
                                 headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    try:
        with _open(req, timeout=10) as resp:
            data = json.loads(resp.read(_MAX_BYTES))
    except urllib.error.HTTPError as e:
        with e:
            raise Denied(f"{host} returned HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Denied(f"could not read the status of {page} from {host}: {getattr(e, 'reason', e)}") from None
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        raise Denied(f"{host} did not return a Statuspage status")
    return f"{page} ({host}): {status.get('indicator', '?')}: {status.get('description', '?')}"


PASSDESK_TOOL_NAMESPACE = "status"
PASSDESK_TOOLS = {
    "status_check": PluginTool(
        handler=check,
        description="Report a service's current status from its public status page. "
                    "`page` is one of the pages this deployment allows.",
        properties={"page": {"type": "string", "description": "a configured page name, e.g. github"}},
        required=("page",),
        hosts=HOSTS,
    ),
}
