"""git_fetch, git_push, github_create_pr. Network git runs only in the
broker's private mirrors; code crosses into the workspace as git bundles."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from ..core import config
from ..core.errors import Denied
from ..core.filters import block_outbound_secrets, sanitize
from ..core.registry import register
from ..core.workspace import factory_dir, run, tool_lock, workspace_path

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def check_branch_name(name: str) -> str:
    if (not _BRANCH_RE.match(name) or ".." in name or "//" in name
            or name.endswith((".lock", "/", ".")) or "@{" in name):
        raise Denied(f"invalid branch name: {name!r}")
    return name


def check_repo(repo: str, policy: dict) -> str:
    if not _REPO_RE.match(repo) or ".." in repo:
        raise Denied(f"invalid repo: {repo!r} (expected owner/name)")
    allowed = [r.lower() for r in policy["git"].get("repos", [])]
    if repo.lower() not in allowed:
        raise Denied(f"repo {repo} is not in the broker allowlist (policy.toml [git].repos)")
    return repo


def check_push_branch(branch: str, policy: dict) -> str:
    check_branch_name(branch)
    git = policy["git"]
    for pat in git.get("protected_branches", []):
        if fnmatch.fnmatchcase(branch, pat):
            raise Denied(f"branch {branch} is protected ({pat})")
    prefix = git.get("push_branch_prefix", "")
    if prefix and not branch.startswith(prefix):
        raise Denied(f"agents may only push branches starting with {prefix!r}")
    return branch


def git(*args: str, cwd: Path, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
           "-c", "core.fsmonitor=false", *args]
    cp = run(cmd, cwd=cwd, timeout=timeout)
    if check and cp.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {sanitize(cp.stderr.strip(), 2000)}")
    return cp


def ensure_mirror(repo: str) -> Path:
    path = config.MIRRORS / (repo.replace("/", "__") + ".git")
    if not path.exists():
        path.mkdir(parents=True)
        git("init", "--bare", "-q", cwd=path)
        git("remote", "add", "origin", f"https://github.com/{repo}.git", cwd=path)
    return path


def tool_git_fetch(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    branch = check_branch_name(args["branch"])
    with tool_lock:
        mirror = ensure_mirror(repo)
        git("fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=mirror)
        name = f"{repo.replace('/', '__')}__{branch.replace('/', '__')}.bundle"
        bundle = factory_dir("incoming") / name
        git("bundle", "create", "-q", str(bundle), f"refs/remotes/origin/{branch}", cwd=mirror)
        sha = git("rev-parse", f"refs/remotes/origin/{branch}", cwd=mirror).stdout.strip()
    rel = bundle.relative_to(config.WORKSPACE)
    return (f"Fetched {repo}@{branch} ({sha}) into {rel}.\n"
            f"Import it in your sandbox with:\n"
            f"  git fetch {rel} refs/remotes/origin/{branch}:refs/remotes/origin/{branch}")


def tool_git_push(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    branch = check_push_branch(args["branch"], policy)
    base = check_branch_name(args.get("base") or policy["git"].get("default_base", "main"))
    fname = args["bundle"]
    if "/" in fname or not fname.endswith(".bundle"):
        raise Denied("bundle must be a file name ending in .bundle inside .factory/outgoing/")
    bundle = workspace_path(f".factory/outgoing/{fname}")
    limits = policy.get("filters", {})

    with tool_lock:
        mirror = ensure_mirror(repo)
        git("fetch", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}", cwd=mirror)
        verify = git("bundle", "verify", str(bundle), cwd=mirror, check=False)
        if verify.returncode != 0:
            raise Denied("bundle is invalid or its prerequisites are not on "
                         f"origin/{base}: {sanitize(verify.stderr.strip(), 1000)}")
        heads = git("bundle", "list-heads", str(bundle), cwd=mirror).stdout
        if not re.search(rf"\srefs/heads/{re.escape(branch)}$", heads, re.M):
            raise Denied(f"bundle does not contain refs/heads/{branch}")
        incoming = f"refs/factory/{branch}"
        git("fetch", "--no-tags", str(bundle), f"+refs/heads/{branch}:{incoming}", cwd=mirror)

        rng = f"refs/remotes/origin/{base}..{incoming}"
        count = int(git("rev-list", "--count", rng, cwd=mirror).stdout.strip())
        if count == 0:
            raise Denied(f"{branch} has no commits beyond origin/{base}")
        if count > int(limits.get("max_commits", 200)):
            raise Denied(f"{count} commits exceeds the per-push limit")
        diff = git("diff", "--no-ext-diff", "--no-textconv", "--unified=0",
                   f"refs/remotes/origin/{base}...{incoming}", cwd=mirror).stdout
        if len(diff) > int(limits.get("max_diff_bytes", 2_000_000)):
            raise Denied("diff exceeds the size limit")
        added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        block_outbound_secrets("the diff", added)
        block_outbound_secrets("commit messages", git("log", "--format=%B", rng, cwd=mirror).stdout)

        push = git("push", "--porcelain", "origin", f"{incoming}:refs/heads/{branch}", cwd=mirror, check=False)
        sha = git("rev-parse", incoming, cwd=mirror).stdout.strip()
    if push.returncode != 0:
        return f"Push rejected by GitHub:\n{sanitize(push.stderr + push.stdout, 3000)}"
    return f"Pushed {count} commit(s) to {repo}:{branch} ({sha}).\n{sanitize(push.stdout, 2000)}"


def tool_github_create_pr(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    head = check_push_branch(args["head"], policy)
    base = check_branch_name(args.get("base") or policy["git"].get("default_base", "main"))
    title, body = args["title"], args.get("body", "")
    block_outbound_secrets("PR title", title)
    block_outbound_secrets("PR body", body)
    payload = json.dumps({"title": title, "body": body, "head": head, "base": base,
                          "draft": bool(args.get("draft", True))}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls", data=payload, method="POST",
        headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            pr = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = sanitize(e.read().decode("utf-8", "replace"), 2000)
        hint = ("\nHint: a GitHub token must be stored for the broker sandbox: "
                "sbx secret set github --sandbox factory-broker") if e.code in (401, 403, 404) else ""
        return f"GitHub API returned HTTP {e.code}: {detail}{hint}"
    return f"Opened PR #{pr['number']}: {pr['html_url']}"


register({
    "git_fetch": (tool_git_fetch, "Fetch a branch of an allowlisted GitHub repo. Writes a git bundle to "
                  ".factory/incoming/ and returns the command to import it.",
                  {"repo": {"type": "string", "description": "owner/name"},
                   "branch": {"type": "string"}}, ["repo", "branch"]),
    "git_push": (tool_git_push, "Push a branch to an allowlisted GitHub repo. First run "
                 "`git bundle create .factory/outgoing/<name>.bundle origin/<base>..<branch>` "
                 "(or the whole branch). Diff and commit messages are secret-scanned; protected branches "
                 "are refused and force-pushes are impossible.",
                 {"repo": {"type": "string", "description": "owner/name"},
                  "branch": {"type": "string", "description": "branch to push; must use the allowed prefix"},
                  "bundle": {"type": "string", "description": "file name inside .factory/outgoing/"},
                  "base": {"type": "string", "description": "branch the work is based on (default from policy)"}},
                 ["repo", "branch", "bundle"]),
    "github_create_pr": (tool_github_create_pr, "Open a pull request on an allowlisted GitHub repo. "
                         "Title and body are secret-scanned. Draft by default.",
                         {"repo": {"type": "string"}, "head": {"type": "string"}, "base": {"type": "string"},
                          "title": {"type": "string"}, "body": {"type": "string"},
                          "draft": {"type": "boolean"}}, ["repo", "head", "title"]),
})
