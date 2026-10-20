"""passdesk doctor: check a Passdesk deployment and say how to fix each problem.

    python3 -m passdesk.doctor [--live] [--kit spec.yaml ...] [--json]

It reads the policy (with its local overlay), the state directory (tokens,
grants, audit log), the workspace's exchange directory, the credential files,
and, with --live, asks the running broker, GitHub and (for projects on
[gitlab].url) GitLab. It changes nothing: every check is a read, and the only
network traffic is the --live probes.

Designed to run wherever the broker itself runs (its container, where the
credentials and the baked-in policy are) but works on a host against a
checkout too; the environment variables config.py reads say where things
are. A deployment's own doctor (e.g. the AI Code Factory's `factory doctor`)
covers the platform: sbx, Docker, KVM. This one covers the gateway.

With BROKER_DEPLOYMENT=standalone, the sbx kit comparison (--kit, which
checks the policy's egress hosts against an sbx kit's network allowlist) is
replaced by check_standalone: how many hosts the policy can reach (no kit to
compare against, so --kit itself is flagged as ignored), a GitHub credential
present through core/credentials.py, TLS (check_tls: the files load, or a
bind off loopback without them is refused unless BROKER_INSECURE_BIND=1),
and the exchange directory's writability.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import ssl
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import egress_guard
from . import tools  # noqa: F401 - registers every tool
from .core import approvals, auth, config, credentials, plugins, registry
from .core import policy as policy_mod
from .core.errors import Denied
from .core.net import NO_REDIRECT_OPENER, RefuseRedirects
from .core.registry import TOOLS
from .tools import git as git_tool
from .tools.git import check_branch_name
from .tools.web import credentialed_host_tool

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"

_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# email:token, as README "Configuring credentials" says to store it.
_JIRA_SECRET_RE = re.compile(r"^[^\s:@]+@[^\s:@]+\.[^\s:@]+:\S+$")

# Hosts each tool contacts, beyond [web].allow_hosts. A deployment's egress
# allowlist (e.g. an sbx kit) must include every host the policy can reach.
_TOOL_HOSTS = {
    "git_fetch": ["github.com"],
    "git_push": ["github.com"],
    "github_create_pr": ["api.github.com"],
    "github_create_issue": ["api.github.com"],
    "gitlab_create_mr": [],  # [gitlab].url's host, added by egress_hosts
    "npm_fetch": ["registry.npmjs.org"],
    "pip_download": ["pypi.org", "files.pythonhosted.org"],
    "gemini_flash_query": ["generativelanguage.googleapis.com"],
    "jira_my_issues": ["api.atlassian.com"],
    "jira_get_issue": ["api.atlassian.com"],
}
_SECRET_TOOLS = {"jira": {"jira_my_issues", "jira_get_issue"}, "gemini": {"gemini_flash_query"},
                 "npm-registry": {"npm_fetch"}, "gitlab": {"git_fetch", "git_push", "gitlab_create_mr"}}
# Tools that reach [gitlab].url's host when it is set.
_GITLAB_TOOLS = {"git_fetch", "git_push", "gitlab_create_mr"}
_EXCHANGE_DIRS = ("incoming", "outgoing", "npm-mirror", "wheels")
# Tools that need the "github" credential. In sbx mode this is proxy-injected
# (never read by the gateway); in standalone mode check_standalone checks it
# through credentials.get(), the only place that distinction matters.
_GITHUB_TOOLS = {"git_fetch", "git_push", "github_create_pr", "github_create_issue"}


def _proxy_blocks_other_providers(order: list[str]) -> bool:
    """True if "proxy" is checked before some other provider in `order`. A
    PROXY_INJECTED name (today just "github") then never reaches that other
    provider: `credentials.get` stops at `proxy`'s INJECTED regardless of
    what the other provider holds."""
    if "proxy" not in order:
        return False
    idx = order.index("proxy")
    return any(order.index(p) > idx for p in order if p != "proxy")


def _github_token_hint(action: str) -> str:
    """`action`, with the sbx re-attach command appended only for
    BROKER_DEPLOYMENT=sbx (the default); a standalone deployment gets the
    same text with no sbx-specific command."""
    if config.DEPLOYMENT == "sbx":
        return f"{action} (with sbx: sbx secret set github --sandbox factory-broker)"
    return action


class Report:
    """The findings, in order, for printing or asserting on."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, list[str]]] = []  # (section, level, text, fix lines)
        self._section = ""

    def section(self, name: str) -> None:
        self._section = name

    def add(self, level: str, text: str, *fix: str) -> None:
        self.items.append((self._section, level, text, [f for f in fix if f]))

    def ok(self, text: str) -> None:
        self.add(OK, text)

    def info(self, text: str, *fix: str) -> None:
        self.add(INFO, text, *fix)

    def warn(self, text: str, *fix: str) -> None:
        self.add(WARN, text, *fix)

    def fail(self, text: str, *fix: str) -> None:
        self.add(FAIL, text, *fix)

    def count(self, level: str) -> int:
        return sum(1 for _, lvl, _, _ in self.items if lvl == level)

    @property
    def problems(self) -> int:
        return self.count(FAIL)

    def messages(self, level: str | None = None) -> list[str]:
        return [text for _, lvl, text, _ in self.items if level is None or lvl == level]

    def to_dict(self) -> dict:
        """The same findings as `render`, as a JSON-ready object."""
        return {
            "problems": self.problems,
            "warnings": self.count(WARN),
            "items": [{"section": sec, "level": level, "text": text, "fix": fix}
                      for sec, level, text, fix in self.items],
        }


# --- policy -------------------------------------------------------------------------

def _is_pos_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _is_str_list(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _check_hosts(report: Report, where: str, hosts: object) -> None:
    if not _is_str_list(hosts):
        report.fail(f"{where} must be a list of host names")
        return
    for h in hosts:
        if not _HOST_RE.match(h.lower()):
            report.fail(f"{where}: {h!r} is not a host name",
                        'use a bare host like "docs.python.org", or "*.example.com" for its subdomains '
                        "(no scheme, path or port)")


def _check_policy_shape(report: Report, pol: dict) -> None:
    """Every table and key the tools read, with the shape each expects."""
    known = {
        "web": {"allow_hosts", "max_bytes"},
        "git": {"repos", "protected_branches", "push_branch_prefix", "default_base"},
        "github": {"issue_repos"},
        "gitlab": {"url"},
        "packages": {"npm_deny", "npm_allow", "pypi_deny", "pypi_allow", "npm_private_registry",
                     "npm_redirect_hosts"},
        "filters": {"max_commits", "max_diff_bytes", "max_bundle_bytes"},
        "gemini": {"model", "max_prompt_chars"},
        "jira": {"site", "cloud_id", "projects", "max_results", "max_comments"},
        "egress_guard": {"allow_hosts"},  # read by `passdesk egress-guard`, not a tool
        # patch_max_bytes is core's (git_push's inspectable patch); the rest
        # are read by an approval plugin (Passdesk Pro).
        "approvals": {"patch_max_bytes", "tools", "hold_seconds", "expire_seconds"},
    }
    for table, val in pol.items():
        if table not in known:
            report.warn(f"[{table}] is not a table any tool reads; a typo?",
                        f"known tables: {', '.join(known)}")
            continue
        if not isinstance(val, dict):
            report.fail(f"[{table}] must be a table")
            continue
        for key in val:
            if key not in known[table]:
                report.warn(f"[{table}].{key} is not a setting any tool reads; a typo?",
                            f"known keys: {', '.join(sorted(known[table]))}")
    appr = pol.get("approvals")
    if isinstance(appr, dict) and "patch_max_bytes" in appr:
        value = appr["patch_max_bytes"]
        lo, hi = approvals.PATCH_MAX_BYTES_MIN, approvals.PATCH_MAX_BYTES_MAX
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            report.warn(f"[approvals].patch_max_bytes = {value!r} is not a whole number from {lo} to {hi}",
                        f"git_push uses {approvals.patch_max_bytes(pol)} instead")
    for table in ("web", "git"):
        if not isinstance(pol.get(table), dict):
            report.fail(f"[{table}] is missing; web_fetch and the git tools need it even if its lists are empty")
    try:
        egress_guard.load_allowlist(pol)
    except ValueError as e:
        report.fail(str(e), "the egress guard refuses to start on this policy; fix the entry in policy.toml "
                    "(or the overlay) and rebuild the image")

    web = pol.get("web") if isinstance(pol.get("web"), dict) else {}
    _check_hosts(report, "[web].allow_hosts", web.get("allow_hosts", []))
    if "max_bytes" in web and not _is_pos_int(web["max_bytes"]):
        report.fail("[web].max_bytes must be a positive integer")
    for h in web.get("allow_hosts", []) if _is_str_list(web.get("allow_hosts", [])) else []:
        tool = credentialed_host_tool(h.lstrip("*."), pol)
        if tool:
            report.warn(f"[web].allow_hosts lists {h}, which web_fetch refuses anyway ({tool} owns that host)",
                        "remove it; the hard-coded credentialed-host denylist wins over this list")

    git = pol.get("git") if isinstance(pol.get("git"), dict) else {}
    repos = git.get("repos", [])
    if not _is_str_list(repos):
        report.fail("[git].repos must be a list of \"owner/name\" (or \"<gitlab host>/<group>/<project>\") strings")
    else:
        for r in repos:
            if git_tool.is_gitlab_name(r):
                continue  # a GitLab project: check_policy checks it against [gitlab].url
            if not _REPO_RE.match(r) or ".." in r:
                report.fail(f"[git].repos: {r!r} is not owner/name",
                            'e.g. "acme/widgets"; no URL, no .git suffix')
    prot = git.get("protected_branches", [])
    if not _is_str_list(prot):
        report.fail("[git].protected_branches must be a list of branch globs")
    elif not prot:
        report.warn("[git].protected_branches is empty: agents could push to main",
                    'the default is ["main", "master", "release/*"]')
    prefix_key_present = "push_branch_prefix" in git
    prefix = git.get("push_branch_prefix", config.DEFAULT_PUSH_BRANCH_PREFIX)
    if not isinstance(prefix, str):
        report.fail("[git].push_branch_prefix must be a string")
    elif not prefix_key_present:
        report.warn(f"[git].push_branch_prefix is not set: falling back to BROKER_PUSH_PREFIX "
                    f"(currently {config.DEFAULT_PUSH_BRANCH_PREFIX!r})",
                    "set [git].push_branch_prefix explicitly in policy.toml, "
                    "or set BROKER_PUSH_PREFIX to the fallback this deployment wants")
    elif prefix == "":
        report.warn("[git].push_branch_prefix is empty: agents may push any unprotected branch",
                    f'set a prefix such as {config.DEFAULT_PUSH_BRANCH_PREFIX!r} so agent branches are recognizable')
    elif re.search(r"\s", prefix):
        report.fail("[git].push_branch_prefix contains whitespace")
    base = git.get("default_base", "main")
    if not isinstance(base, str):
        report.fail("[git].default_base must be a string")
    else:
        try:
            check_branch_name(base)
        except Denied as e:
            report.fail(f"[git].default_base: {e}")

    pk = pol.get("packages") if isinstance(pol.get("packages"), dict) else {}
    for key in ("npm_deny", "npm_allow", "pypi_deny", "pypi_allow"):
        val = pk.get(key, [])
        if not _is_str_list(val):
            report.fail(f"[packages].{key} must be a list of package names")
        elif any(x != x.lower() for x in val):
            report.warn(f"[packages].{key} has upper-case names; names are compared lower-cased, so write them that way")
    private = pk.get("npm_private_registry", "")
    if not isinstance(private, str):
        report.fail("[packages].npm_private_registry must be a string")
    elif private:
        parts = urllib.parse.urlsplit(private)
        if parts.scheme != "https" or not parts.hostname or not private.endswith("/"):
            report.fail("[packages].npm_private_registry must be an https:// URL prefix ending in \"/\"",
                        'e.g. "https://acme.jfrog.io/acme/api/npm/npm-local/"')
    _check_hosts(report, "[packages].npm_redirect_hosts", pk.get("npm_redirect_hosts", []))

    filters = pol.get("filters") if isinstance(pol.get("filters"), dict) else {}
    for key in ("max_commits", "max_diff_bytes", "max_bundle_bytes"):
        if key in filters and not _is_pos_int(filters[key]):
            report.fail(f"[filters].{key} must be a positive integer")
    if _is_pos_int(filters.get("max_bundle_bytes", 1)) and _is_pos_int(filters.get("max_diff_bytes", 1)) \
            and filters.get("max_bundle_bytes", 100_000_000) < filters.get("max_diff_bytes", 2_000_000):
        report.warn("[filters].max_bundle_bytes is smaller than max_diff_bytes: a push under the diff cap "
                    "could still be refused for its bundle size")

    gem = pol.get("gemini") if isinstance(pol.get("gemini"), dict) else {}
    if "model" in gem and (not isinstance(gem["model"], str) or not gem["model"]):
        report.fail("[gemini].model must be a non-empty string")
    if "max_prompt_chars" in gem and not _is_pos_int(gem["max_prompt_chars"]):
        report.fail("[gemini].max_prompt_chars must be a positive integer")

    jira = pol.get("jira") if isinstance(pol.get("jira"), dict) else {}
    site, cloud_id = jira.get("site", ""), jira.get("cloud_id", "")
    if not isinstance(site, str) or not isinstance(cloud_id, str):
        report.fail("[jira].site and [jira].cloud_id must be strings")
    else:
        if site and not _HOST_RE.match(site.lower()):
            report.fail(f"[jira].site {site!r} is not a host name", 'e.g. "acme.atlassian.net" (no https://)')
        if cloud_id and not _UUID_RE.match(cloud_id.lower()):
            report.fail("[jira].cloud_id is not a UUID",
                        "copy cloudId from https://<site>/_edge/tenant_info")
        if bool(site) != bool(cloud_id):
            report.fail("[jira].site and [jira].cloud_id must be set together (or both left empty)")
    projects = jira.get("projects", [])
    if not _is_str_list(projects):
        report.fail("[jira].projects must be a list of project keys")
    else:
        for p in projects:
            if not _PROJECT_KEY_RE.match(p):
                report.fail(f"[jira].projects: {p!r} is not a Jira project key (e.g. \"PROJ\")")
    for key in ("max_results", "max_comments"):
        if key in jira and not _is_pos_int(jira[key]):
            report.fail(f"[jira].{key} must be a positive integer")


def check_policy(report: Report, path: Path | None = None) -> dict | None:
    report.section("Policy")
    path = path or config.POLICY_PATH
    local = path.with_name("policy.local.toml")
    try:
        pol = policy_mod.load_policy(path)
    except FileNotFoundError as e:
        report.fail(f"policy file not found: {e.filename}",
                    "set BROKER_POLICY to the policy.toml to check, or run this from where the broker runs")
        return None
    except Exception as e:  # noqa: BLE001 - tomllib errors vary; the message is what matters
        report.fail(f"policy does not parse: {e}", f"fix {path}" + (f" or {local}" if local.is_file() else ""))
        return None
    report.ok(f"policy parses: {path}" + (f" + {local.name}" if local.is_file() else " (no local overlay)"))
    _check_policy_shape(report, pol)
    git = pol.get("git") if isinstance(pol.get("git"), dict) else {}
    if _is_str_list(git.get("repos", [])) and not git.get("repos"):
        report.info("[git].repos is empty: git_fetch, git_push, github_create_pr and gitlab_create_mr return DENIED",
                    "add the repos agents may use, e.g. repos = [\"acme/widgets\"], then rebuild the image")
    gitlab_ok = True
    try:
        gitlab = git_tool.gitlab_base(pol)
    except Denied as e:
        gitlab, gitlab_ok = None, False
        report.fail(str(e), "fix [gitlab].url in the local policy.toml, then rebuild the image")
    if gitlab_ok and _is_str_list(git.get("repos", [])):
        on_gitlab = 0
        for repo in git["repos"]:
            try:
                git_tool.check_repo(repo, {**pol, "git": {**git, "repos": [repo]}})
            except Denied as e:
                report.fail(f"[git].repos entry {repo!r}: {e}",
                            "GitHub repos are owner/name; GitLab projects are <host of [gitlab].url>/<group>/<project>")
                continue
            on_gitlab += git_tool.is_gitlab_name(repo)
        if gitlab and not on_gitlab:
            report.info(f"[gitlab].url is set ({gitlab}) but no [git].repos entry is on it: "
                        "gitlab_create_mr returns DENIED",
                        f"add projects as \"{git_tool.gitlab_host(pol)}/<group>/<project>\"")
    gh = pol.get("github") if isinstance(pol.get("github"), dict) else {}
    issue_repos = gh.get("issue_repos", [])
    if not _is_str_list(issue_repos) or any(not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", r)
                                            or ".." in r for r in issue_repos):
        report.fail("[github].issue_repos must be a list of \"owner/name\" strings")
    elif not issue_repos:
        report.info("[github].issue_repos is empty: github_create_issue returns DENIED",
                    "list the repos agents may file issues in (keep it to private trackers), then rebuild the image")
    return pol


# --- egress hosts -------------------------------------------------------------------

def egress_hosts(pol: dict, granted: set[str] | None = None) -> list[str]:
    """Every host the policy lets some tool reach. With `granted`, only tools
    in that set count; otherwise every tool does."""
    hosts: set[str] = set()
    for tool, tool_hosts in _TOOL_HOSTS.items():
        if granted is None or tool in granted:
            hosts.update(tool_hosts)
    for tool, spec in plugins.declared_tools()[0].items():
        if granted is None or tool in granted:
            hosts.update(spec.hosts)
    if granted is None or "web_fetch" in granted:
        web = pol.get("web") if isinstance(pol.get("web"), dict) else {}
        for h in web.get("allow_hosts", []) if _is_str_list(web.get("allow_hosts", [])) else []:
            if not credentialed_host_tool(h.lstrip("*."), pol):
                hosts.add(h.lower())
    if granted is None or _GITLAB_TOOLS & granted:
        try:
            base = git_tool.gitlab_base(pol)
        except Denied:
            base = None  # check_policy already failed it
        if base:
            hosts.add(urllib.parse.urlsplit(base).hostname)
    if granted is None or "npm_fetch" in granted:
        pk = pol.get("packages") if isinstance(pol.get("packages"), dict) else {}
        private = pk.get("npm_private_registry", "")
        if isinstance(private, str) and private:
            host = urllib.parse.urlsplit(private).hostname
            if host:
                hosts.add(host.lower())
        for h in pk.get("npm_redirect_hosts", []) if _is_str_list(pk.get("npm_redirect_hosts", [])) else []:
            hosts.add(h.lower())
    return sorted(hosts)


def kit_allowed_hosts(path: Path) -> list[str]:
    """The `allow:` list under `network:` in an sbx kit spec.yaml.

    A deliberately small reader for the one list the kit format puts there
    (a YAML sequence of plain host names, comments allowed), not a YAML
    parser: anything fancier than that in a kit is a fine reason to run a
    real one.
    """
    hosts: list[str] = []
    in_allow, allow_indent = False, 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if in_allow:
            m = re.match(r"^\s*-\s*['\"]?([^'\"\s]+)['\"]?\s*$", line)
            if m and indent > allow_indent:
                hosts.append(m.group(1).lower())
                continue
            in_allow = False
        if re.match(r"^\s*allow:\s*$", line):
            in_allow, allow_indent = True, indent
    return hosts


def check_egress(report: Report, pol: dict, kits: list[Path]) -> None:
    report.section("Egress")
    needed = egress_hosts(pol)
    if not kits:
        report.info(f"{len(needed)} host(s) reachable under this policy; pass --kit <spec.yaml> to check "
                    "them against an sbx kit's network allowlist",
                    "list them with --egress-hosts")
        return
    allowed: list[str] = []
    for kit in kits:
        try:
            found = kit_allowed_hosts(kit)
        except OSError as e:
            report.fail(f"cannot read kit {kit}: {e}")
            return
        if not found:
            report.warn(f"{kit}: no network allow list found (or none this reader understands)")
        allowed.extend(found)
    missing = [h for h in needed if h not in allowed]
    wildcards = [h for h in missing if h.startswith("*.")]
    exact = [h for h in missing if not h.startswith("*.")]
    if exact:
        report.fail(f"{len(exact)} host(s) the policy can reach are not in the kit allowlist: {', '.join(exact)}",
                    "add each to the kit's permissions.network.allow (the kit is the network-level backstop);",
                    "then recreate the broker sandbox so the kit applies")
    if wildcards:
        report.warn(f"policy wildcard(s) {', '.join(wildcards)} need every subdomain listed in the kit by name",
                    "sbx kits allow exact hosts; list the subdomains you use, or drop the wildcard")
    if not missing:
        report.ok(f"every host the policy can reach ({len(needed)}) is in the kit allowlist")


# --- state --------------------------------------------------------------------------

def _mode_too_open(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def check_state(report: Report) -> set[str]:
    """Tokens, grants and the audit log. Returns every tool granted to anyone."""
    report.section("State")
    state = config.STATE
    granted: set[str] = set()
    if not state.is_dir():
        report.fail(f"state directory does not exist: {state}",
                    "set BROKER_STATE, or run this from where the deployment mounted it")
        return granted
    report.ok(f"state directory: {state}")
    if _mode_too_open(state):
        report.warn("the state directory is readable by other users; it holds token hashes and the audit log",
                    f"chmod 700 {state}")

    tokens_dir = state / "tokens"
    principals_with_token: set[str] = set()
    if not tokens_dir.is_dir():
        report.fail("no tokens/ directory: no caller can authenticate",
                    "the deployment's start script mints one token per principal into tokens/<name>.sha256")
    else:
        for f in sorted(tokens_dir.glob("*.sha256")):
            try:
                digest = f.read_text().strip()
            except OSError as e:
                report.fail(f"cannot read {f.name}: {e}")
                continue
            if not _SHA256_RE.match(digest):
                report.fail(f"tokens/{f.name} does not hold a SHA-256 hex digest; that principal cannot authenticate",
                            "delete the file and re-run the deployment's start script to mint a new token")
            else:
                principals_with_token.add(f.stem)
        if principals_with_token:
            report.ok(f"{len(principals_with_token)} principal token(s): {', '.join(sorted(principals_with_token))}")
        else:
            report.fail("tokens/ holds no valid token hash: no caller can authenticate",
                        "run the deployment's start script (it mints tokens/<principal>.sha256)")

    grants_path = auth.grants_path()
    grants: dict = {}
    if not grants_path.is_file():
        report.fail(f"{grants_path.name} is missing: every call is denied for lack of a grant",
                    "the deployment's start script writes it from each principal's tool list")
    else:
        try:
            grants = json.loads(grants_path.read_text())
            if not isinstance(grants, dict) or not all(auth.is_valid_grant(v) for v in grants.values()):
                raise ValueError('expected {"principal": ["tool", ...]} or '
                                  '{"principal": {"tools": ["tool", ...], "source": "..."}}')
        except (OSError, ValueError) as e:
            report.fail(f"{grants_path.name} is invalid: {e}")
            grants = {}
        known = [*TOOLS, *(t for t in plugins.declared_tools()[0] if t not in TOOLS)]
        for principal, grant in grants.items():
            tool_list = auth.grant_tools(grant)
            unknown = [t for t in tool_list if t not in known]
            if unknown:
                report.fail(f"{grants_path.name} grants {principal} unknown tool(s): {', '.join(unknown)}",
                            f"known tools: {', '.join(known)}")
            granted.update(t for t in tool_list if t in known)
            if principal not in principals_with_token:
                report.warn(f"{principal} has a grant but no token in tokens/: its calls fail with 401",
                            "re-run the deployment's start script")
        for principal in sorted(principals_with_token - set(grants)):
            report.warn(f"{principal} has a token but no grant in {grants_path.name}: every call it makes is denied")
        if grants:
            report.ok("grants: " + "; ".join(
                f"{p}: {', '.join(auth.grant_tools(g)) or '(none)'}" for p, g in sorted(grants.items())))

    audit = state / "audit.jsonl"
    if not os.access(state, os.W_OK):
        report.fail("the state directory is not writable: pushes and PRs are refused because their intent "
                    "cannot be recorded", f"make {state} writable by the user the broker runs as")
    elif audit.exists():
        try:
            with open(audit, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", "replace").strip().splitlines()
            last = tail[-1] if tail else ""
            if last:
                json.loads(last)
            report.ok(f"audit log: {audit} ({size} bytes)")
        except (OSError, ValueError):
            report.warn("the audit log's last line is not valid JSON (a crash mid-write?); readers skip it",
                        "nothing to fix unless it keeps happening; check broker.log")
    else:
        report.info("no audit.jsonl yet; it appears at the broker's first start")

    log = state / "broker.log"
    try:
        if log.is_file() and log.stat().st_size:
            lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            report.warn(f"broker.log is not empty ({len(lines)} line(s)); the server has crashed or logged errors",
                        f"last line: {lines[-1][:200]}")
    except OSError:
        pass
    return granted


# --- workspace ----------------------------------------------------------------------

def check_workspace(report: Report) -> None:
    report.section("Workspace")
    ws = config.WORKSPACE
    if not ws.is_dir():
        report.fail(f"workspace does not exist or is not a directory: {ws}",
                    "set BROKER_WORKSPACE to the project folder the deployment shares with the broker")
        return
    report.ok(f"workspace: {ws}")
    name = config.EXCHANGE_DIR
    exchange = ws / name
    try:
        st = os.lstat(exchange)
    except FileNotFoundError:
        report.info(f"{name}/ does not exist yet; the first fetch or mirror creates it")
        return
    if stat.S_ISLNK(st.st_mode):
        report.fail(f"{name} is a symlink; every tool that uses the exchange directory refuses it",
                    f"remove the link and let the broker create a real directory: rm {exchange}")
        return
    if not stat.S_ISDIR(st.st_mode):
        report.fail(f"{name} exists but is not a directory", f"move it aside: {exchange}")
        return
    for sub in _EXCHANGE_DIRS:
        p = exchange / sub
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            report.fail(f"{name}/{sub} is a symlink; the broker refuses it", f"rm {p}")
        elif not stat.S_ISDIR(st.st_mode):
            report.fail(f"{name}/{sub} is not a directory", f"move it aside: {p}")
    report.ok(f"{name}/ exchange directory is a real directory with no symlinked entries")


# --- TLS ---------------------------------------------------------------------------

def check_tls(report: Report) -> None:
    """BROKER_TLS_CERT/BROKER_TLS_KEY (README, "TLS"): both or neither, regular
    files that OpenSSL loads as a matching pair, the key not readable by
    others. Without TLS, a bind off loopback is a problem in standalone mode
    (the server refuses to start) unless BROKER_INSECURE_BIND=1 says the
    network is private, in which case it is a warning. Quiet on a loopback
    bind with no TLS: nothing to check."""
    cert, key = config.TLS_CERT, config.TLS_KEY
    off_loopback = not config.is_loopback(config.BIND_HOST)
    if not cert and not key:
        if not off_loopback:
            return
        if config.INSECURE_BIND:
            report.warn(f"bound to {config.BIND_HOST} (off loopback) without TLS; BROKER_INSECURE_BIND=1 "
                        "acknowledges it: every principal's bearer token crosses the network in clear text",
                        "only safe when nothing outside a private network (e.g. a compose stack's internal "
                        "network) can reach the port; otherwise set BROKER_TLS_CERT and BROKER_TLS_KEY")
        elif config.DEPLOYMENT == "standalone":
            report.fail(f"bound to {config.BIND_HOST} (off loopback) without TLS: the server refuses to start",
                        "set BROKER_TLS_CERT and BROKER_TLS_KEY (README, \"TLS\" has an openssl recipe), bind "
                        "127.0.0.1, or set BROKER_INSECURE_BIND=1 if nothing outside a private network can "
                        "reach the port")
        return
    if not cert or not key:
        report.fail("BROKER_TLS_CERT and BROKER_TLS_KEY must be set together (the server refuses to start)",
                    "set both to the PEM certificate and its private key, or neither")
        return
    usable = True
    for what, path in (("BROKER_TLS_CERT", cert), ("BROKER_TLS_KEY", key)):
        try:
            st = os.stat(path)
        except OSError as e:
            report.fail(f"{what}={path}: {e.strerror or e}", f"put the PEM file at {path}, readable by the "
                        "user the broker runs as")
            usable = False
            continue
        if not stat.S_ISREG(st.st_mode):
            report.fail(f"{what}={path} is not a regular file")
            usable = False
        elif what == "BROKER_TLS_KEY" and st.st_mode & 0o077:
            report.warn(f"{path} is readable by others (mode {stat.S_IMODE(st.st_mode):04o})", f"chmod 0600 {path}")
    if not usable:
        return
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    except (ssl.SSLError, OSError) as e:
        report.fail(f"TLS certificate and key do not load: {e}",
                    "check that BROKER_TLS_CERT is a PEM certificate (chain) and BROKER_TLS_KEY its unencrypted "
                    "private key (README, \"TLS\")")
        return
    report.ok(f"TLS on: {cert} and its key load; the MCP URL is https://")


# --- standalone ---------------------------------------------------------------------

def check_standalone(report: Report, granted: set[str], pol: dict | None = None,
                      kits: list[Path] | None = None, order_error: credentials.CredentialError | None = None) -> None:
    """What replaces the sbx kit checks (check_egress) when
    BROKER_DEPLOYMENT=standalone: there is no sbx kit, no egress proxy to
    inject GitHub's token, and no sbx-managed exchange directory permissions.
    Not run in sbx mode (see run()).

    `order_error`, when given, is a CredentialError `run()` already hit
    computing `credentials.provider_order()` for every credential this run
    checks; reported once there (Credentials section) instead of again here.
    """
    report.section("Standalone")

    needed = egress_hosts(pol or {})
    report.info(f"{len(needed)} host(s) reachable under this policy; list them with --egress-hosts")
    if isinstance((pol or {}).get("egress_guard"), dict):  # absent: this deployment doesn't run the guard
        try:
            allow = egress_guard.load_allowlist(pol)
        except ValueError:
            pass  # check_policy already failed it
        else:
            if allow:
                report.info(f"egress guard forwards CONNECT to port 443 on {len(allow)} host(s): "
                            f"{', '.join(sorted(allow))}")
            else:
                report.warn("[egress_guard].allow_hosts is empty: `passdesk egress-guard` refuses to start, so an "
                            "agent container behind it has no model API",
                            "list the model API hosts the agent needs (README, \"Egress guard\")")
    if kits:
        report.warn("--kit is ignored in standalone mode: there is no sbx kit to compare the policy's "
                     "egress hosts against",
                     "drop --kit; standalone has no kit-level network allowlist to check against")

    wanted = bool(_GITHUB_TOOLS & granted)
    if order_error is not None:
        pass  # already reported once, in the Credentials section
    else:
        reasons: list[str] = []
        try:
            token = credentials.get("github", on_unusable=lambda name, provider, reason: reasons.append(reason))
        except credentials.CredentialError as e:
            report.fail(str(e))
        else:
            for reason in reasons:
                report.warn(reason)
            order = credentials.provider_order()
            order_text = ", ".join(order) or "(none configured)"
            if token is not None:
                report.ok(f"github credential present (checked providers: {order_text})")
            elif wanted:
                if _proxy_blocks_other_providers(order):
                    report.warn(f"no github credential (checked providers: {order_text}); \"proxy\" runs "
                                "before the other provider(s), so a token delivered through file or "
                                "env-file is never read -- github is treated as proxy-injected",
                                "set PASSDESK_CREDENTIAL_PROVIDERS to an order without \"proxy\" "
                                "(README, Configuring credentials) so a file- or env-file-delivered "
                                "token is used")
                else:
                    report.warn(f"no github credential (checked providers: {order_text}); "
                                f"{', '.join(sorted(_GITHUB_TOOLS & granted))} return DENIED until it is delivered",
                                "deliver a fine-grained GitHub token through one of those providers "
                                "(README, Configuring credentials)")
            else:
                report.info(f"no github credential (not needed: no granted tool uses it; checked providers: {order_text})")

    check_tls(report)

    ws = config.WORKSPACE
    if ws.is_dir():
        exchange = ws / config.EXCHANGE_DIR
        try:
            st = os.lstat(exchange)
            is_symlink, is_dir = stat.S_ISLNK(st.st_mode), stat.S_ISDIR(st.st_mode)
        except FileNotFoundError:
            is_symlink, is_dir = False, False
        if is_symlink:
            pass  # check_workspace already failed this; nothing more to check here
        else:
            target = exchange if is_dir else ws
            if not os.access(target, os.W_OK):
                report.fail(f"{target} is not writable: git_fetch, npm_fetch and pip_download cannot hand "
                            "files to the agent sandbox",
                            f"make {target} writable by the user the broker runs as")
            else:
                report.ok(f"{target} is writable")
            if is_dir:
                for sub in ("incoming", "outgoing"):
                    p = exchange / sub
                    try:
                        sub_st = os.lstat(p)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(sub_st.st_mode) or not stat.S_ISDIR(sub_st.st_mode):
                        continue  # check_workspace already failed this
                    if not os.access(p, os.W_OK):
                        report.fail(f"{p} is not writable: git_fetch, npm_fetch and pip_download cannot "
                                    "hand files to the agent sandbox",
                                    f"make {p} writable by the user the broker runs as")
                    else:
                        report.ok(f"{p} is writable")
    # else: check_workspace already reported the missing/invalid workspace.


# --- credentials --------------------------------------------------------------------

def check_secrets(report: Report, pol: dict | None, granted: set[str],
                   order_error: credentials.CredentialError | None = None) -> None:
    report.section("Credentials")
    if order_error is not None:
        report.fail(str(order_error))
    paths = {"jira": config.JIRA_SECRET_PATH, "gemini": config.GEMINI_SECRET_PATH,
             "npm-registry": config.NPM_REGISTRY_SECRET_PATH, "gitlab": credentials.secrets_dir() / "gitlab"}
    pk = (pol or {}).get("packages") if isinstance((pol or {}).get("packages"), dict) else {}
    private_registry = bool(pk.get("npm_private_registry")) if isinstance(pk.get("npm_private_registry", ""), str) else False
    try:
        gitlab_set = bool(git_tool.gitlab_base(pol or {}))
    except Denied:
        gitlab_set = True  # malformed but meant: check_policy failed it; still check the credential
    # A credential only some deployments need counts as wanted only when its feature is configured.
    configured = {"npm-registry": private_registry, "gitlab": gitlab_set}
    if not any(p.parent.is_dir() for p in paths.values()):
        # Running on a host: the credential files exist only where the
        # broker itself runs, so their absence here says nothing.
        report.info(f"credential files live alongside the running broker ({config.JIRA_SECRET_PATH.parent}); "
                    "not visible from here", "run the doctor from where the broker runs to check them")
        return
    for name, path in paths.items():
        tools_using = _SECRET_TOOLS[name]
        wanted = bool(tools_using & granted) and configured.get(name, True)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            # Nothing at the configured path, but credentials.get() also checks
            # the Docker secret and PASSDESK_SECRET_<NAME>_FILE paths, either of
            # which can hold the only copy; only report absence once none of
            # them do.
            if order_error is not None:
                continue  # already reported once, above
            reasons: list[str] = []
            try:
                value = credentials.get(name, on_unusable=lambda n, p, reason, r=reasons: r.append(reason))
            except credentials.CredentialError as e:
                report.fail(str(e))
                continue
            for reason in reasons:
                report.warn(reason)
            if value is not None and name == "jira" and not _JIRA_SECRET_RE.match(value):
                report.fail("the jira credential is not in the form you@example.com:TOKEN",
                            "have the deployment re-deliver it in that form")
                continue
            if value is not None and name == "gitlab" and not git_tool._GITLAB_TOKEN_RE.fullmatch(value):
                report.fail("the gitlab credential is not a GitLab token (20-255 letters, digits, '_', '-' or '.')",
                            "have the deployment re-deliver a project access token")
                continue
            if value is not None:
                extra = "" if wanted else " (no granted tool uses it)"
                report.ok(f"{name} credential present{extra}")
            elif wanted:
                report.warn(f"no {name} credential at {path}; {', '.join(sorted(tools_using & granted))} "
                            f"return DENIED until it is delivered",
                            "have the deployment deliver it to that path again (README, Configuring credentials)")
            else:
                report.info(f"no {name} credential (not needed: no granted tool uses it)")
            continue
        if not stat.S_ISREG(st.st_mode):
            report.fail(f"{path} is not a regular file", "delete it and re-deliver the credential")
            continue
        if st.st_mode & 0o077:
            report.warn(f"{path} is readable by other users", f"chmod 600 {path}")
        try:
            value = path.read_text().strip()
        except OSError as e:
            report.fail(f"cannot read {path}: {e}")
            continue
        if not value:
            report.fail(f"{path} is empty; {', '.join(sorted(tools_using))} fail",
                        "re-deliver the credential, or delete the file so the tools report DENIED with setup hints")
            continue
        if name == "jira" and not _JIRA_SECRET_RE.match(value):
            report.fail("the jira credential is not in the form you@example.com:TOKEN",
                        "have the deployment re-deliver it in that form")
            continue
        if name == "gitlab" and not git_tool._GITLAB_TOKEN_RE.fullmatch(value):
            report.fail("the gitlab credential is not a GitLab token (20-255 letters, digits, '_', '-' or '.')",
                        "have the deployment re-deliver a project access token")
            continue
        extra = "" if wanted else " (no granted tool uses it)"
        report.ok(f"{name} credential present{extra}")


# --- plugins ------------------------------------------------------------------------

def check_plugins(report: Report) -> None:
    """What PASSDESK_PLUGINS names, whether it imports, and (for Passdesk
    Pro) whether its license is usable. Importing a plugin here has no side
    effects: plugins register only through activate(), which the server
    calls and the doctor does not."""
    report.section("Plugins")
    names = [n.strip() for n in config.PLUGINS.split(",") if n.strip()]
    if not names:
        report.info("no plugins named in PASSDESK_PLUGINS (approvals are off; the free core is running)")
        return
    for name in names:
        try:
            module = importlib.import_module(name)
        except ImportError as e:
            report.fail(f"plugin {name} cannot be imported: {e}",
                        "is it under the image's plugins directory (the deployment's plugins folder at build time)?")
            continue
        version = getattr(module, "VERSION", "?")
        try:
            declared = registry.plugin_tools(name, module)
        except registry.PluginToolError as e:
            report.fail(f"{e}: the gateway refuses to start with this plugin",
                        "fix the plugin's PASSDESK_TOOLS declaration (docs/plugin-tools.md)")
            continue
        if declared is not None:
            report.ok(f"plugin {name} {version} declares tool(s): {', '.join(declared[1])}")
        lic = getattr(module, "license", None)
        if lic is None or not hasattr(lic, "load"):
            if declared is None:
                report.ok(f"plugin {name} {version} imports")
            continue
        try:
            claims = lic.load()
        except Exception as e:  # noqa: BLE001 - the plugin's own error type; its message is the finding
            report.fail(f"plugin {name} {version} is installed but inactive: {e}",
                        "deliver a valid license key (README of the plugin); the gateway runs as the free core until then")
            continue
        left = claims.days_left()
        text = (f"plugin {name} {version}: licensed to {claims.sub}, {claims.tier}, "
                f"{claims.approvers} approver(s), {left} day(s) left")
        if left <= 14:
            report.warn(text + "; renew soon: when it expires, held writes are refused, not released")
        else:
            report.ok(text)


# --- live ---------------------------------------------------------------------------

def _http(url: str, headers: dict | None = None, data: bytes | None = None, timeout: float = 10,
          cafile: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"User-Agent": config.USER_AGENT, **(headers or {})})
    # `cafile` (the broker's own BROKER_TLS_CERT, for the --live probe of a
    # self-signed listener) trusts that certificate alone; every other call
    # (GitHub) keeps the system trust store.
    opener = NO_REDIRECT_OPENER if cafile is None else urllib.request.build_opener(
        RefuseRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(65536)
    except urllib.error.HTTPError as e:
        body = e.read(65536) if e.fp else b""
        return e.code, body


def check_live(report: Report, pol: dict | None, url: str, http=_http) -> None:
    report.section("Live")
    probe = {"cafile": config.TLS_CERT} if url.startswith("https://") and config.TLS_CERT else {}
    try:
        code, _ = http(url, data=b"{}", timeout=5, **probe)
    except (urllib.error.URLError, OSError, ValueError) as e:
        report.fail(f"the broker does not answer at {url}: {getattr(e, 'reason', e)}",
                    "is it running? the deployment's start script launches it; check broker.log in the state dir")
    else:
        if code == 401:
            report.ok(f"broker answers at {url} and demands a token (401 without one)")
        else:
            report.fail(f"something answers at {url} with HTTP {code}, not the 401 an unauthenticated call should get",
                        "is another service on that port, or an old broker build?")

    git = (pol or {}).get("git") if isinstance((pol or {}).get("git"), dict) else {}
    repos = git.get("repos", []) if _is_str_list(git.get("repos", [])) else []
    if not repos:
        return
    gitlab_repos = [r for r in repos if git_tool.is_gitlab_name(r)]
    if gitlab_repos:
        check_live_gitlab(report, pol or {}, gitlab_repos, http)
    for repo in repos:
        if git_tool.is_gitlab_name(repo):
            continue
        api = f"https://api.github.com/repos/{repo}"
        try:
            code, body = http(api, headers={"Accept": "application/vnd.github+json"})
        except (urllib.error.URLError, OSError, ValueError) as e:
            report.fail(f"cannot reach api.github.com to check {repo}: {getattr(e, 'reason', e)}",
                        "is api.github.com in the deployment's egress allowlist? run this from where the "
                        "broker runs")
            return
        if code == 200:
            try:
                perms = json.loads(body).get("permissions") or {}
            except ValueError:
                perms = {}
            if perms.get("push"):
                report.ok(f"GitHub token can read and push {repo}")
            elif perms:
                report.fail(f"GitHub token can read {repo} but not push to it",
                            "the fine-grained token needs Contents: read/write and Pull requests: read/write on this repo")
            else:
                report.warn(f"{repo} is reachable but no token was attached (public repo, unauthenticated): "
                            "pushes and pull requests will fail",
                            _github_token_hint("the deployment's egress proxy must attach a fine-grained token"),
                            "(from a host, outside that deployment, this warning is expected: only its "
                            "proxy attaches the token)")
        elif code == 404:
            report.fail(f"GitHub returns 404 for {repo}: it does not exist, or the token has no access to it",
                        "check the owner/name; outside the deployment where the broker runs no token is "
                        "attached, so private repos show 404")
        elif code in (401, 403):
            report.fail(f"GitHub rejects the token for {repo} (HTTP {code})",
                        _github_token_hint("the deployment must re-attach the token"))
        else:
            report.warn(f"GitHub answered HTTP {code} for {repo}")


# GitLab's access levels: 30 is Developer, the least that may push a branch.
_GITLAB_DEVELOPER = 30


def check_live_gitlab(report: Report, pol: dict, repos: list[str], http=_http) -> None:
    """Can the gateway's gitlab credential read and push each GitLab project?
    The token goes only to [gitlab].url's API, never redirected (_http); it
    is never printed."""
    try:
        base = git_tool.gitlab_base(pol)
    except Denied:
        return  # check_policy already failed it
    if not base:
        return  # check_policy already failed each of these entries
    reasons: list[str] = []
    try:
        token = credentials.get("gitlab", on_unusable=lambda name, provider, reason: reasons.append(reason))
    except credentials.CredentialError as e:
        report.fail(str(e))
        return
    for reason in reasons:
        report.warn(reason)
    if token is None or not git_tool._GITLAB_TOKEN_RE.fullmatch(token):
        report.warn(f"no usable gitlab credential: skipped the access check for {len(repos)} GitLab project(s)",
                    "deliver a project access token as the gitlab credential (README, Configuring credentials)")
        return
    host = urllib.parse.urlsplit(base).hostname
    for repo in repos:
        try:
            remote = git_tool.remote_of(repo, pol)
        except Denied:
            continue  # check_policy already failed it
        api = f"{base}api/v4/projects/{urllib.parse.quote(remote.path, safe='')}"
        try:
            code, body = http(api, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
        except (urllib.error.URLError, OSError, ValueError) as e:
            report.fail(f"cannot reach {host} to check {repo}: {getattr(e, 'reason', e)}",
                        f"is {host} in the deployment's egress allowlist? run this from where the broker runs")
            return
        if code == 200:
            try:
                perms = json.loads(body).get("permissions") or {}
            except (ValueError, AttributeError):
                perms = {}
            levels = [(p or {}).get("access_level") for p in (perms.get("project_access"), perms.get("group_access"))
                      if isinstance(p, dict)]
            level = max((lv for lv in levels if isinstance(lv, int)), default=None)
            if level is not None and level >= _GITLAB_DEVELOPER:
                report.ok(f"GitLab token can read and push {repo}")
            elif level is not None:
                report.fail(f"GitLab token can read {repo} but not push to it (access level {level})",
                            "the project access token needs the Developer role (and the api and "
                            "write_repository scopes)")
            else:
                report.warn(f"GitLab token can read {repo}; its push access could not be confirmed",
                            "the project access token needs the Developer role and the api and write_repository scopes")
        elif code == 404:
            report.fail(f"GitLab returns 404 for {repo}: it does not exist, or the token has no access to it",
                        "check the group/project path, and that the token belongs to this project (or its group)")
        elif code in (401, 403):
            report.fail(f"GitLab rejects the token for {repo} (HTTP {code})",
                        "the token may have expired or been revoked: create a new one and deliver it again")
        else:
            report.warn(f"GitLab answered HTTP {code} for {repo}")


# --- CLI ----------------------------------------------------------------------------

def run(policy_path: Path | None = None, kits: list[Path] | None = None, live: bool = False,
        url: str = "", http=_http) -> Report:
    report = Report()
    report.section("Config")
    try:
        config.validate()
    except ValueError as e:
        report.fail(str(e))
    pol = check_policy(report, policy_path)
    if pol is not None and config.DEPLOYMENT != "standalone":
        check_egress(report, pol, kits or [])
    granted = check_state(report)
    check_workspace(report)
    # Every credential check below shares one provider list; look it up once
    # so a bad PASSDESK_CREDENTIAL_PROVIDERS is reported once, not per credential.
    try:
        credentials.provider_order()
        order_error = None
    except credentials.CredentialError as e:
        order_error = e
    if config.DEPLOYMENT == "standalone":
        check_standalone(report, granted, pol, kits or [], order_error)
    elif config.TLS_CERT or config.TLS_KEY:
        report.section("TLS")  # sbx mode: only when a deployment turned it on, so the usual report is unchanged
        check_tls(report)
    check_secrets(report, pol, granted, order_error)
    check_plugins(report)
    if live:
        scheme = "https" if config.TLS_CERT else "http"
        check_live(report, pol, url or f"{scheme}://127.0.0.1:{config.PORT}/mcp", http)
    return report


def render(report: Report, color: bool) -> str:
    bold, dim, green, yellow, red, reset = ("\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m") \
        if color else ("",) * 6
    marks = {OK: f"{green}✓{reset}", INFO: f"{dim}·{reset}", WARN: f"{yellow}!{reset}", FAIL: f"{red}✗{reset}"}
    out: list[str] = []
    section = None
    for sec, level, text, fix in report.items:
        if sec != section:
            section = sec
            out.append(f"\n{bold}==> {sec}{reset}")
        out.append(f"  {marks[level]} {text}")
        out.extend(f"      {line}" for line in fix)
    out.append("")
    problems, warnings = report.problems, report.count(WARN)
    if problems:
        out.append(f"{red}{problems} problem(s) found.{reset} Fix them, then run the doctor again.")
    elif warnings:
        out.append(f"{green}No problems found.{reset} {warnings} warning(s) above are worth a look.")
    else:
        out.append(f"{green}No problems found.{reset}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m passdesk.doctor",
                                     description="Check a Passdesk deployment; print a fix for each problem. Changes nothing.")
    parser.add_argument("--policy", type=Path, default=None,
                        help="policy.toml to check (default: BROKER_POLICY, or the one beside the package)")
    parser.add_argument("--kit", type=Path, action="append", default=[],
                        help="an sbx kit spec.yaml whose network allowlist must cover the policy's hosts (repeatable)")
    parser.add_argument("--live", action="store_true",
                        help="also probe the running broker and GitHub or GitLab access for each [git].repos entry")
    parser.add_argument("--url", default="", help=f"broker MCP URL for --live (default: http://127.0.0.1:{config.PORT}/mcp)")
    parser.add_argument("--egress-hosts", action="store_true",
                        help="print every host the policy can reach, one per line, and exit")
    parser.add_argument("--json", action="store_true",
                        help="print the report as one JSON object instead of the human-readable text")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)

    if args.egress_hosts:
        try:
            pol = policy_mod.load_policy(args.policy)
        except Exception as e:  # noqa: BLE001
            print(f"error: cannot load policy: {e}", file=sys.stderr)
            return 2
        print("\n".join(egress_hosts(pol)))
        return 0

    report = run(args.policy, args.kit, args.live, args.url)
    if args.json:
        print(json.dumps(report.to_dict()))
    else:
        print(render(report, color=sys.stdout.isatty() and not args.no_color))
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
