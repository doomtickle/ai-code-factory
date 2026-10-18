"""git_fetch, git_push, github_create_pr. Network git runs only in the
broker's private mirrors; code crosses into the workspace as git bundles."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from ..core import config
from ..core.approvals import require as require_approval
from ..core.errors import Denied
from ..core.filters import block_outbound_secrets, find_secrets, sanitize, untrusted
from ..core.registry import register
from ..core.workspace import copy_workspace_file, factory_dir, install_file, private_tempdir, run, run_capped, tool_lock

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def check_branch_name(name: str) -> str:
    if (not _BRANCH_RE.match(name) or ".." in name or "//" in name
            or name.endswith((".lock", "/", ".")) or "@{" in name):
        raise Denied(f"invalid branch name: {name!r}")
    # Every ref name an agent supplies (fetch and push branch, base, PR head)
    # is sent to GitHub, and a pushed branch or PR head is published under
    # that name, so a credential in a branch name is refused outright rather
    # than merely redacted on the approval card.
    block_outbound_secrets("branch name", name)
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


_GIT_HARDENING = ("-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", "-c", "core.fsmonitor=false")


def git(*args: str, cwd: Path, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    cp = run(["git", *_GIT_HARDENING, *args], cwd=cwd, timeout=timeout)
    if check and cp.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed:\n" +
                           untrusted(f"git {args[0]}", sanitize(cp.stderr.strip(), 2000)))
    return cp


def git_capped(*args: str, cwd: Path, limit: int) -> str | None:
    """stdout of a read-only git command as text, or None if it exceeds limit bytes."""
    code, out, err = run_capped(["git", *_GIT_HARDENING, *args], cwd=cwd, limit=limit, timeout=300)
    if out is None:
        return None
    if code != 0:
        raise RuntimeError(f"git {args[0]} failed:\n" +
                           untrusted(f"git {args[0]}", sanitize(err.strip(), 2000)))
    # Binary content is scanned too; the secret patterns are ASCII.
    return out.decode("utf-8", "replace")


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
        with private_tempdir() as tmp:
            bundle = tmp / name
            git("bundle", "create", "-q", str(bundle), f"refs/remotes/origin/{branch}", cwd=mirror)
            with factory_dir("incoming") as incoming:
                install_file(incoming, name, bundle)
        sha = git("rev-parse", f"refs/remotes/origin/{branch}", cwd=mirror).stdout.strip()
    rel = f"{config.EXCHANGE_DIR}/incoming/{name}"
    return (f"Fetched {repo}@{branch} ({sha}) into {rel}.\n"
            f"Import it in your sandbox with:\n"
            f"  git fetch {rel} refs/remotes/origin/{branch}:refs/remotes/origin/{branch}")


def check_commit_patches(patches: str, base: str) -> None:
    """Refuse if any commit's added lines, file names or binary content look like secrets."""
    commits: dict[str, list[str]] = {}
    lines: list[str] = []
    # str.splitlines() also breaks on \r, \x0b, \x0c, \x1c-\x1e, U+0085 and
    # U+2028, not just "\n". git's own patch output only ever uses "\n", so
    # an added line that *contains* one of those characters (e.g. "ok\r-ghp_...")
    # must stay one line here -- splitlines() would cut it into a piece
    # starting with "-", which is then skipped as a removed line, hiding a
    # secret from the scan below.
    for line in patches.split("\n"):
        if line.startswith("commit ") and len(line) >= 47:
            lines = commits.setdefault(line[7:].strip(), [])
        elif not line.startswith("-"):  # removed lines are already upstream or in an earlier commit
            lines.append(line)
    for sha, added in commits.items():
        found = find_secrets("\n".join(added))
        if found:
            raise Denied(f"commit {sha[:12]} appears to contain secrets ({', '.join(found)}). Every commit is "
                         "pushed, so a later commit that deletes them does not help: remove them from the "
                         f"branch history (e.g. git rebase -i origin/{base}), re-create the bundle and retry")


def tool_git_push(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    branch = check_push_branch(args["branch"], policy)
    base = check_branch_name(args.get("base") or policy["git"].get("default_base", "main"))
    fname = args["bundle"]
    outgoing = f"{config.EXCHANGE_DIR}/outgoing"
    if "/" in fname or not fname.endswith(".bundle"):
        raise Denied(f"bundle must be a file name ending in .bundle inside {outgoing}/")
    limits = policy.get("filters", {})
    max_bundle = int(limits.get("max_bundle_bytes", 100_000_000))
    max_diff = int(limits.get("max_diff_bytes", 2_000_000))

    with tool_lock, private_tempdir() as tmp:
        # One read of the agent's file, as a regular file within the cap; every
        # git command below runs on this private copy, so it can't be swapped.
        bundle = tmp / "push.bundle"
        copy_workspace_file(f"{outgoing}/{fname}", bundle, max_bundle,
                            hint=f"bundle only the new commits (git bundle create {outgoing}/{fname} "
                                 f"origin/{base}..{branch}) or push the work in smaller pieces")
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
        # Every commit is pushed, so every commit's patch is scanned (not the
        # net diff, where a secret added and later deleted would not show).
        # -a: binary files are scanned and counted as text; --cc: merge
        # commits show what they change beyond their parents.
        patches = git_capped("log", "-p", "--cc", "-a", "--no-ext-diff", "--no-textconv", "--no-color",
                             "--unified=0", "--format=commit %H", rng, cwd=mirror, limit=max_diff)
        if patches is None:
            raise Denied(f"the pushed commits' patches exceed [filters].max_diff_bytes ({max_diff} bytes, "
                         "counted over every commit, binary files included); push fewer or smaller commits")
        check_commit_patches(patches, base)
        meta = git_capped("log", "--format=%an <%ae>%n%cn <%ce>%n%B", rng, cwd=mirror, limit=max_diff)
        if meta is None:
            raise Denied(f"the commit messages exceed [filters].max_diff_bytes ({max_diff} bytes)")
        block_outbound_secrets("commit messages or author/committer names and emails", meta)
        sha = git("rev-parse", incoming, cwd=mirror).stdout.strip()
        # What a person sees if the deployment holds pushes for approval:
        # only what was verified above, never the content itself.
        shortstat = git("diff", "--shortstat", rng, cwd=mirror).stdout.strip()
        names = git_capped("diff", "--name-only", rng, cwd=mirror, limit=max_diff) or ""
        files = [f for f in names.split("\n") if f]
        with open(bundle, "rb") as fh:
            bundle_digest = hashlib.file_digest(fh, "sha256").hexdigest()
    # Outside the tool lock: a pending approval must not block other git calls.
    require_approval({"tool": "git_push", "repo": repo, "branch": branch, "base": base, "head": sha,
                      "commits": count, "shortstat": shortstat, "file_count": len(files), "files": files[:50],
                      "bundle_sha256": bundle_digest, "secret_scan": "clean"})
    with tool_lock:
        mirror = ensure_mirror(repo)
        if git("rev-parse", incoming, cwd=mirror).stdout.strip() != sha:
            raise Denied(f"{branch} changed in the broker's mirror while awaiting approval; retry the push")
        push = git("push", "--porcelain", "origin", f"{incoming}:refs/heads/{branch}", cwd=mirror, check=False)
    if push.returncode != 0:
        body = sanitize(push.stderr + push.stdout, 3000)
        return "Push rejected by GitHub:\n" + untrusted(f"git push {repo}:{branch}", body)
    return f"Pushed {count} commit(s) to {repo}:{branch} ({sha}).\n{sanitize(push.stdout, 2000)}"


def tool_github_create_pr(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    head = check_push_branch(args["head"], policy)
    base = check_branch_name(args.get("base") or policy["git"].get("default_base", "main"))
    title, body = args["title"], args.get("body", "")
    block_outbound_secrets("PR title", title)
    block_outbound_secrets("PR body", body)
    draft = bool(args.get("draft", True))
    require_approval({"tool": "github_create_pr", "repo": repo, "head": head, "base": base,
                      "title": title, "body": body[:2000], "draft": draft})
    payload = json.dumps({"title": title, "body": body, "head": head, "base": base, "draft": draft}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls", data=payload, method="POST",
        headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            pr = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = sanitize(e.read().decode("utf-8", "replace"), 2000)
        hint = ("\nHint: the GitHub credential is supplied by the deployment (with sbx: "
                "sbx secret set github --sandbox factory-broker)") if e.code in (401, 403, 404) else ""
        return f"GitHub API returned HTTP {e.code}:\n" + untrusted(f"GitHub API ({repo})", detail) + hint
    return f"Opened PR #{pr['number']}: {pr['html_url']}"


register({
    "git_fetch": (tool_git_fetch, f"Fetch a branch of an allowlisted GitHub repo. Writes a git bundle to "
                  f"{config.EXCHANGE_DIR}/incoming/ and returns the command to import it.",
                  {"repo": {"type": "string", "description": "owner/name"},
                   "branch": {"type": "string"}}, ["repo", "branch"]),
    "git_push": (tool_git_push, "Push a branch to an allowlisted GitHub repo. First run "
                 f"`git bundle create {config.EXCHANGE_DIR}/outgoing/<name>.bundle origin/<base>..<branch>` "
                 "(or the whole branch). Diff, commit messages and the branch name are secret-scanned; "
                 "protected branches are refused and force-pushes are impossible.",
                 {"repo": {"type": "string", "description": "owner/name"},
                  "branch": {"type": "string", "description": "branch to push; must use the allowed prefix"},
                  "bundle": {"type": "string", "description": f"file name inside {config.EXCHANGE_DIR}/outgoing/"},
                  "base": {"type": "string", "description": "branch the work is based on (default from policy)"}},
                 ["repo", "branch", "bundle"]),
    "github_create_pr": (tool_github_create_pr, "Open a pull request on an allowlisted GitHub repo. "
                         "Title, body and branch names are secret-scanned. Draft by default.",
                         {"repo": {"type": "string"}, "head": {"type": "string"}, "base": {"type": "string"},
                          "title": {"type": "string"}, "body": {"type": "string"},
                          "draft": {"type": "boolean"}}, ["repo", "head", "title"]),
})
