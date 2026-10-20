"""git_fetch, git_push, github_create_pr, github_create_issue. Network git runs only in the
broker's private mirrors; code crosses into the workspace as git bundles.

GitHub credential: with sbx the egress proxy attaches the token and the
gateway never sees it (credentials.get("github") returns None). Where the
deployment delivers a `github` credential instead (standalone), github_token()
returns it; git gets it only through GIT_CONFIG_* variables in the one
child's environment (an extraHeader scoped to GITHUB_GIT_URL, never argv or
disk), the API call sends it as a Bearer header, neither follows a redirect,
and the value and its encoded form are redacted from everything the gateway
returns, audits or shows on an approval card (filters.remember_secret).
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from ..core import approvals, config, credentials, filters
from ..core.approvals import require as require_approval
from ..core.errors import Denied
from ..core.filters import block_outbound_secrets, find_secrets, sanitize, untrusted
from ..core.net import NO_REDIRECT_OPENER
from ..core.registry import register
from ..core.workspace import (copy_workspace_file, factory_dir, install_file, private_tempdir, run, run_capped,
                              run_head, tool_lock)

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
    prefix = git.get("push_branch_prefix", config.DEFAULT_PUSH_BRANCH_PREFIX)
    if prefix and not branch.startswith(prefix):
        raise Denied(f"agents may only push branches starting with {prefix!r}")
    return branch


_GIT_HARDENING = ("-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", "-c", "core.fsmonitor=false")

# The only URL prefix git sends the gateway's token to (ensure_mirror's
# remotes are all under it), and the API endpoint the PR call uses.
GITHUB_GIT_URL = "https://github.com/"
GITHUB_API = "https://api.github.com"
# Every GitHub token format (ghp_, github_pat_, ghs_, gho_, ...) is letters,
# digits and "_". Anything else is refused before it reaches a header or a
# git config value, and the length floor keeps redaction meaningful.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{20,255}")
# Variables that would make git (or curl under it) write request headers to
# stderr or a file, or add config we didn't choose. Removed from the child's
# environment when it carries the token.
_UNSAFE_ENV_PREFIXES = ("GIT_TRACE", "GIT_CURL_VERBOSE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_UNSAFE_ENV = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG")


def git_basic_auth(token: str) -> str:
    """The Authorization value git sends: Basic x-access-token:<token>, the
    form GitHub documents for every token type over HTTPS (and the one
    actions/checkout uses)."""
    return "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def github_token() -> str | None:
    """The gateway's own GitHub token, or None if it holds none (the egress
    proxy supplies it, or nothing does). Read fresh on every call; once read,
    the value and the header form git sends are redacted everywhere."""
    token = credentials.get("github")
    if token is None:
        return None
    if not _TOKEN_RE.fullmatch(token):
        raise Denied("the github credential is not a GitHub token (expected 20-255 letters, digits or "
                     "underscores); deliver a fine-grained token - see README, Configuring credentials")
    b64 = git_basic_auth(token).removeprefix("Basic ")
    # Remembered without its "=" padding: a prefix of the padded form, so
    # redaction catches an echo of either form (e.g. one with padding
    # stripped by a tool that doesn't preserve it).
    filters.remember_secret(token, b64.rstrip("="))
    return token


def git_auth_env() -> dict | None:
    """Environment changes that give one git child the token, or None (no
    change at all) when the gateway holds none.

    The token travels only as GIT_CONFIG_COUNT/KEY/VALUE in that child's
    environment: an http.extraHeader scoped to GITHUB_GIT_URL, so git sends
    it to no other host. Redirects are refused (git would otherwise follow
    the first one), no credential helper runs, and every trace or verbose
    switch that would echo headers is removed.
    """
    token = github_token()
    if token is None:
        return None
    env: dict = {k: None for k in os.environ if k.startswith(_UNSAFE_ENV_PREFIXES) or k in _UNSAFE_ENV}
    pairs = [(f"http.{GITHUB_GIT_URL}.extraHeader", f"Authorization: {git_basic_auth(token)}"),
             ("http.followRedirects", "false"),
             ("credential.helper", "")]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def git(*args: str, cwd: Path, timeout: int = 300, check: bool = True,
        auth: bool = False) -> subprocess.CompletedProcess:
    """Run git in a broker-owned repository. auth=True for a command that
    talks to GitHub: it carries the gateway's token if there is one."""
    cp = run(["git", *_GIT_HARDENING, *args], cwd=cwd, timeout=timeout,
             extra_env=git_auth_env() if auth else None)
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


# What git_push stores for an approver to inspect: oldest commit first, each
# message indented four spaces (%w) so no message line can start at column 0
# and pass for a header. Renames stay detected (git's default); no -a, so a
# binary file is git's one "Binary files ... differ" line, never its bytes.
PATCH_FORMAT = "commit %H%nAuthor: %an <%ae>%nDate:   %aI%n%n%w(0,4,4)%B%n"
# --full-index: abbreviated blob names grow with the mirror's object count,
# which would change the digest of the same bundle over time.
_PATCH_ARGS = ("log", "-p", "--stat", "--cc", "--reverse", "--full-index", "--no-ext-diff", "--no-textconv",
               "--no-color", f"--format={PATCH_FORMAT}")
# Read past the cap, so redaction can't shrink what is kept below it.
_PATCH_SLACK = 65_536


def cap_patch(text: str, rest: int, limit: int) -> tuple[bytes, bool]:
    """The patch as stored: text redacted, as UTF-8, cut at the last newline
    at or before limit bytes with one trailer line saying so. rest is how
    many bytes of git's output were counted but not kept (past text)."""
    # Line by line: a pattern whose \s* spans "\n" (assigned-secret) would
    # otherwise merge two patch lines into one and shorten a hunk.
    data = "\n".join(filters.redact(line) for line in text.split("\n")).encode("utf-8")
    total = len(data) + rest
    if total <= limit:
        return data, False
    kept = data[:limit].rfind(b"\n") + 1
    return data[:kept] + f"[passdesk: patch truncated at {kept} of {total} bytes]\n".encode(), True


def build_patch(rng: str, cwd: Path, limit: int) -> tuple[bytes, bool]:
    """The commits in rng as the patch an approver inspects (cap_patch)."""
    # COLUMNS would change --stat's width, and with it the patch's digest.
    code, head, total, err = run_head(["git", *_GIT_HARDENING, *_PATCH_ARGS, rng], cwd=cwd,
                                      keep=limit + _PATCH_SLACK, timeout=300, extra_env={"COLUMNS": None})
    if code != 0:
        raise RuntimeError("git log failed:\n" + untrusted("git log", sanitize(err.strip(), 2000)))
    return cap_patch(head.decode("utf-8", "replace"), total - len(head), limit)


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
        git("fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=mirror,
            auth=True)
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
        git("fetch", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}", cwd=mirror,
            auth=True)
        verify = git("bundle", "verify", str(bundle), cwd=mirror, check=False)
        if verify.returncode != 0:
            raise Denied("bundle is invalid or its prerequisites are not on "
                         f"origin/{base}: {sanitize(verify.stderr.strip(), 1000)}")
        heads = git("bundle", "list-heads", str(bundle), cwd=mirror).stdout
        if not re.search(rf"\srefs/heads/{re.escape(branch)}$", heads, re.M):
            raise Denied(f"bundle does not contain refs/heads/{branch}")
        incoming = f"{config.REF_NAMESPACE}/{branch}"
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
        # With a hook installed, the person deciding can read the code: the
        # same mirror and range, written before the lock is released. The
        # card names the file by digest; it never carries the content.
        patch_fields = {}
        if approvals.hook_installed():
            patch, truncated = build_patch(rng, mirror, approvals.patch_max_bytes(policy))
            patch_fields = {"patch_sha256": approvals.store_patch(patch), "patch_bytes": len(patch),
                            "patch_truncated": truncated}
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
                      "bundle_sha256": bundle_digest, "secret_scan": "clean", **patch_fields})
    with tool_lock:
        mirror = ensure_mirror(repo)
        if git("rev-parse", incoming, cwd=mirror).stdout.strip() != sha:
            raise Denied(f"{branch} changed in the broker's mirror while awaiting approval; retry the push")
        push = git("push", "--porcelain", "origin", f"{incoming}:refs/heads/{branch}", cwd=mirror, check=False,
                   auth=True)
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
    headers = {"Accept": "application/vnd.github+json", "Content-Type": "application/json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": config.USER_AGENT}
    # Read after approval, so a long wait still uses the current token.
    token = github_token()
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{GITHUB_API}/repos/{repo}/pulls", data=payload, method="POST", headers=headers)
    # With no token the egress proxy authenticates the request, as before.
    open_url = NO_REDIRECT_OPENER.open if token is not None else urllib.request.urlopen
    try:
        with open_url(req, timeout=30) as resp:
            pr = json.load(resp)
    except urllib.error.HTTPError as e:
        with e:
            detail = sanitize(e.read().decode("utf-8", "replace"), 2000)
        if token is not None:
            fix = (f"the gateway's github credential must be a token with Contents and Pull requests "
                   f"read/write access to {repo}")
        else:
            sbx = " (with sbx: sbx secret set github --sandbox factory-broker)" if config.DEPLOYMENT == "sbx" else ""
            fix = f"the GitHub credential is supplied by the deployment{sbx}"
        hint = f"\nHint: {fix}" if e.code in (401, 403, 404) else ""
        return f"GitHub API returned HTTP {e.code}:\n" + untrusted(f"GitHub API ({repo})", detail) + hint
    return f"Opened PR #{pr['number']}: {pr['html_url']}"


_ISSUE_TITLE_MAX = 256
_ISSUE_BODY_MAX = 65536  # GitHub's own limit
_ISSUE_LABELS_MAX = 10
_LABEL_PAGES_MAX = 10
_TITLE_BAD_RE = re.compile(r"[\x00-\x1f\x7f]")


def check_issue_repo(repo: str, policy: dict) -> str:
    """Issues may be filed only in repos listed in [github].issue_repos. It is
    its own list, never [git].repos: those usually include public repos, and
    an issue in a public repo is a way to publish whatever an agent writes."""
    if not _REPO_RE.match(repo) or ".." in repo:
        raise Denied(f"invalid repo: {repo!r} (expected owner/name)")
    gh = policy.get("github") if isinstance(policy.get("github"), dict) else {}
    listed = gh.get("issue_repos", [])
    allowed = [r.lower() for r in listed if isinstance(r, str)] if isinstance(listed, list) else []
    if repo.lower() not in allowed:
        raise Denied(f"repo {repo} is not in the broker's issue allowlist (policy.toml [github].issue_repos)")
    return repo


def _github_api(method: str, path: str, token: str | None, payload: dict | None = None):
    """One GitHub REST call; returns the parsed JSON. Raises HTTPError. With a
    token the gateway holds, it goes as a Bearer header and redirects are
    refused; with none, the deployment's egress proxy authenticates it."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": config.USER_AGENT}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{GITHUB_API}{path}", data=data, method=method, headers=headers)
    open_url = NO_REDIRECT_OPENER.open if token is not None else urllib.request.urlopen
    with open_url(req, timeout=30) as resp:
        return json.load(resp)


def _github_api_failure(e: urllib.error.HTTPError, repo: str, held: bool, needs: str) -> str:
    with e:
        detail = sanitize(e.read().decode("utf-8", "replace"), 2000)
    if held:
        fix = f"the gateway's github credential must be a token with {needs} access to {repo}"
    else:
        sbx = " (with sbx: sbx secret set github --sandbox factory-broker)" if config.DEPLOYMENT == "sbx" else ""
        fix = f"the GitHub credential is supplied by the deployment{sbx}; it needs {needs} access to {repo}"
    hint = f"\nHint: {fix}" if e.code in (401, 403, 404) else ""
    return f"GitHub API returned HTTP {e.code}:\n" + untrusted(f"GitHub API ({repo})", detail) + hint


def _repo_labels(repo: str, token: str | None) -> dict[str, str]:
    """The repo's labels, lower-cased name -> the name as GitHub has it."""
    names: dict[str, str] = {}
    for page in range(1, _LABEL_PAGES_MAX + 1):
        data = _github_api("GET", f"/repos/{repo}/labels?per_page=100&page={page}", token)
        if not isinstance(data, list):
            raise Denied(f"GitHub returned an unexpected label list for {repo}")
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names[item["name"].lower()] = item["name"]
        if len(data) < 100:
            break
    return names


def tool_github_create_issue(args: dict, policy: dict) -> str:
    repo = check_issue_repo(args["repo"], policy)
    title = args["title"].strip()
    if not title or len(title) > _ISSUE_TITLE_MAX:
        raise Denied(f"issue title must be 1-{_ISSUE_TITLE_MAX} characters")
    if _TITLE_BAD_RE.search(title):
        raise Denied("issue title must be a single line with no control characters")
    body = args.get("body", "")
    if len(body) > _ISSUE_BODY_MAX:
        raise Denied(f"issue body is over {_ISSUE_BODY_MAX} characters")
    block_outbound_secrets("issue title", title)
    block_outbound_secrets("issue body", body)
    wanted: list[str] = []
    for name in (n.strip() for n in args.get("labels", "").split(",")):
        if name and name.lower() not in (w.lower() for w in wanted):
            wanted.append(name)
    if len(wanted) > _ISSUE_LABELS_MAX:
        raise Denied(f"at most {_ISSUE_LABELS_MAX} labels")
    for name in wanted:
        block_outbound_secrets("label", name)
    labels: list[str] = []
    if wanted:
        # Labels must already exist: GitHub would otherwise create any label
        # named in the request, and this tool only files issues.
        token = github_token()
        try:
            existing = _repo_labels(repo, token)
        except urllib.error.HTTPError as e:
            return _github_api_failure(e, repo, token is not None, "Issues read/write")
        missing = [n for n in wanted if n.lower() not in existing]
        if missing:
            known = sanitize(", ".join(sorted(existing.values())[:60]), 2000)
            raise Denied(f"label(s) not defined in {repo}: {', '.join(missing)}; this tool never creates labels "
                         f"(existing: {known or 'none'})")
        labels = [existing[n.lower()] for n in wanted]
    # The card carries the body's digest, so two issues whose bodies differ
    # only past the excerpt never share a card, and the labels as the agent
    # named them (scanned above), not as the label lookup returned them:
    # nothing on a card comes from the network.
    require_approval({"tool": "github_create_issue", "repo": repo, "title": title, "body": body[:2000],
                      "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "labels": wanted})
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    # Read after approval, so a long wait still uses the current token.
    token = github_token()
    try:
        issue = _github_api("POST", f"/repos/{repo}/issues", token, payload)
    except urllib.error.HTTPError as e:
        return _github_api_failure(e, repo, token is not None, "Issues read/write")
    if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
        raise Denied(f"GitHub returned an unexpected response for the new issue in {repo}")
    return f"Opened issue #{issue['number']} in {repo}: {sanitize(str(issue.get('html_url', '')), 300)}"


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
    "github_create_issue": (tool_github_create_issue, "File an issue in a GitHub repo on the broker's issue "
                            "allowlist. Title and body are secret-scanned; labels must already exist in "
                            "the repo. No assignees; issues can't be edited or closed with this tool.",
                            {"repo": {"type": "string", "description": "owner/name"},
                             "title": {"type": "string", "description": "one line, at most 256 characters"},
                             "body": {"type": "string", "description": "Markdown"},
                             "labels": {"type": "string",
                                        "description": "comma-separated names of existing labels"}},
                            ["repo", "title"]),
})
