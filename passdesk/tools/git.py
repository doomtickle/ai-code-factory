"""git_fetch, git_push, github_create_pr, gitlab_create_mr, github_create_issue. Network git
runs only in the broker's private mirrors; code crosses into the workspace as git bundles.

Two forges. A [git].repos entry is a GitHub repo ("owner/name") or, when
[gitlab].url names a GitLab instance (self-hosted or gitlab.com), a project
on it, host first ("gitlab.example.com/group/sub/project"). A first segment
with a dot or a port can't be a GitHub owner, so the name alone says which
forge a repo is on (remote_of).

GitHub credential: with sbx the egress proxy attaches the token and the
gateway never sees it (credentials.get("github") returns None). Where the
deployment delivers a `github` credential instead (standalone), github_token()
returns it; git gets it only through GIT_CONFIG_* variables in the one
child's environment (an extraHeader scoped to GITHUB_GIT_URL, never argv or
disk), the API call sends it as a Bearer header, neither follows a redirect,
and the value and its encoded form are redacted from everything the gateway
returns, audits or shows on an approval card (filters.remember_secret).

GitLab credential: nothing proxy-injects it, so the deployment always
delivers it as the `gitlab` file credential (owner decision, docs/decisions.md).
It is handled exactly like a gateway-held GitHub token, scoped to the
[gitlab].url prefix instead: git gets it as an extraHeader for that prefix
only, gitlab_create_mr as a Bearer header through NO_REDIRECT_OPENER, and it
is redacted everywhere. Neither forge's token is ever sent to the other.

Approve later (doomtickle/passdesk-ops#91): git_push, github_create_pr and
gitlab_create_mr pass the approval point a job (the checked arguments of
their write) and register a completer (complete_*), so a write still
undecided when the hold runs out is queued and, on approval, made by the
gateway itself from that job: never from the agent's workspace, and only
after the job is checked again against the policy and the stored card (and,
for a push, the mirror's ref and a fresh secret scan of what it would push).
github_create_issue keeps the retry-the-call hold.
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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..core import approvals, config, credentials, filters
from ..core.approvals import require as require_approval
from ..core.approvals import with_held_note
from ..core.errors import Denied
from ..core.filters import block_outbound_secrets, find_secrets, sanitize, untrusted
from ..core.net import NO_REDIRECT_OPENER
from ..core.policy import host_allowed as policy_host_allowed
from ..core.registry import register
from ..core.workspace import (copy_workspace_file, factory_dir, install_file, private_tempdir, run, run_capped,
                              run_head, tool_lock)

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# A GitLab project, host first: the host (and port, if not 443) of [gitlab].url,
# then 2-22 path segments (group, up to 20 subgroups, project). GitLab's own path
# rule: letters, digits, "_", "-" and ".", not starting with "-" or ".".
_GITLAB_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}"
_GITLAB_REPO_RE = re.compile(rf"^[A-Za-z0-9.-]+(?::[0-9]{{1,5}})?(?:/{_GITLAB_SEGMENT}){{2,22}}$")


def check_branch_name(name: str) -> str:
    if (not _BRANCH_RE.match(name) or ".." in name or "//" in name
            or name.endswith((".lock", "/", ".")) or "@{" in name):
        raise Denied(f"invalid branch name: {name!r}")
    # Every ref name an agent supplies (fetch and push branch, base, PR head)
    # is sent to the forge (GitHub or GitLab), and a pushed branch or PR head is published under
    # that name, so a credential in a branch name is refused outright rather
    # than merely redacted on the approval card.
    block_outbound_secrets("branch name", name)
    return name


def is_gitlab_name(repo: str) -> bool:
    """True for a host-qualified repo name (a GitLab project). GitHub owners
    are letters, digits and dashes, so a first segment with a dot or a colon
    is a host."""
    first = repo.split("/", 1)[0]
    return ("." in first or ":" in first) and re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", first) is not None


def check_repo(repo: str, policy: dict) -> str:
    if not isinstance(repo, str):
        raise Denied("invalid repo")
    if is_gitlab_name(repo):
        if (not _GITLAB_REPO_RE.match(repo) or ".." in repo
                or repo.lower().endswith((".git", ".atom")) or "/." in repo or "/-" in repo):
            raise Denied(f"invalid repo: {repo!r} (expected owner/name, or <gitlab host>/<group>/<project>)")
    elif not _REPO_RE.match(repo) or ".." in repo:
        raise Denied(f"invalid repo: {repo!r} (expected owner/name)")
    allowed = [r.lower() for r in policy["git"].get("repos", []) if isinstance(r, str)]
    if repo.lower() not in allowed:
        raise Denied(f"repo {repo} is not in the broker allowlist (policy.toml [git].repos)")
    remote_of(repo, policy)  # a GitLab name must be on the configured instance
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

# The only URL prefix git sends the gateway's GitHub token to (GitHub
# mirrors' remotes are all under it; the GitLab token goes only to
# [gitlab].url), and the API endpoint the PR call uses.
GITHUB_GIT_URL = "https://github.com/"
GITHUB_API = "https://api.github.com"
# Every GitHub token format (ghp_, github_pat_, ghs_, gho_, ...) is letters,
# digits and "_". Anything else is refused before it reaches a header or a
# git config value, and the length floor keeps redaction meaningful.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{20,255}")
# GitLab tokens (glpat-..., and the newer forms with "." separators): letters,
# digits, "_", "-" and ".". Same reasons, same floor.
_GITLAB_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]{20,255}")
# A DNS name (or IPv4 address) label by label: no empty, leading-dash or
# trailing-dash label, and no trailing dot (which would slip past exact-host
# matches such as web_fetch's denylist).
_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Hosts [gitlab].url may never name: GitHub's own, whose token is a different credential.
_NOT_GITLAB = ("github.com", "*.github.com", "*.githubusercontent.com")


@dataclass(frozen=True)
class Remote:
    """Where a [git].repos entry lives: its forge, its path there ("owner/name"
    on GitHub, the namespace path on GitLab) and the URL prefix git may send
    that forge's token to."""
    forge: str  # "github" or "gitlab"
    path: str
    base: str

    @property
    def label(self) -> str:
        return "GitLab" if self.forge == "gitlab" else "GitHub"

    @property
    def git_url(self) -> str:
        return f"{self.base}{self.path}.git"



def gitlab_base(policy: dict) -> str | None:
    """[gitlab].url, checked: an https:// URL with a host and no credentials,
    query or fragment, ending in "/" (a sub-path such as https://acme.net/gitlab/
    is fine). None when it is unset or "". Raises Denied when it is malformed."""
    gl = policy.get("gitlab")
    url = gl.get("url", "") if isinstance(gl, dict) else ""
    if url == "":
        return None
    if not isinstance(url, str):
        raise Denied("[gitlab].url must be a string")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise Denied(f"[gitlab].url is not a URL: {url!r}") from None
    host = parts.hostname or ""
    if (parts.scheme != "https" or not _HOST_RE.fullmatch(host) or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or not url.endswith("/")
            or any(c.isspace() for c in url) or port == 0
            or any(bad in parts.path for bad in ("//", "/./", "/../", "@", "%", "\\"))):
        raise Denied(f"[gitlab].url must be https://<host>[:port]/[path/], ending in \"/\" (got {url!r})")
    if any(policy_host_allowed(host, [p]) for p in _NOT_GITLAB):
        raise Denied(f"[gitlab].url names {host}, a GitHub host")
    return f"https://{host}{'' if port in (None, 443) else f':{port}'}{parts.path}"


def gitlab_host(policy: dict) -> str | None:
    """The host (and port, if not 443) GitLab repo names start with, or None."""
    base = gitlab_base(policy)
    return urllib.parse.urlsplit(base).netloc if base else None


def remote_of(repo: str, policy: dict) -> Remote:
    """The forge a (format-checked) repo name is on. A host-qualified name must
    name [gitlab].url's host, or it is refused."""
    if not is_gitlab_name(repo):
        return Remote("github", repo, GITHUB_GIT_URL)
    base = gitlab_base(policy)
    if base is None:
        raise Denied(f"repo {repo} names a GitLab host, but policy.toml [gitlab].url is not set")
    host, path = repo.split("/", 1)
    if host.lower() != urllib.parse.urlsplit(base).netloc:
        raise Denied(f"repo {repo} is not on the configured GitLab ({urllib.parse.urlsplit(base).netloc})")
    return Remote("gitlab", path, base)
# Variables that would make git (or curl under it) write request headers to
# stderr or a file, or add config we didn't choose. Removed from the child's
# environment when it carries the token.
_UNSAFE_ENV_PREFIXES = ("GIT_TRACE", "GIT_CURL_VERBOSE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_UNSAFE_ENV = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG")


def git_basic_auth(token: str, user: str = "x-access-token") -> str:
    """The Authorization value git sends: Basic <user>:<token>. GitHub documents
    x-access-token for every token type over HTTPS (the form actions/checkout
    uses); GitLab takes any user name with an access token, oauth2 by custom."""
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


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


def gitlab_token() -> str | None:
    """The gateway's GitLab token (the `gitlab` file credential), or None.
    Read fresh on every call; once read, it and git's header form are redacted."""
    token = credentials.get("gitlab")
    if token is None:
        return None
    if not _GITLAB_TOKEN_RE.fullmatch(token):
        raise Denied("the gitlab credential is not a GitLab token (expected 20-255 letters, digits, '_', '-' "
                     "or '.'); deliver a project access token - see README, Configuring credentials")
    filters.remember_secret(token, git_basic_auth(token, "oauth2").removeprefix("Basic ").rstrip("="))
    return token


def git_auth_env(remote: Remote | None = None) -> dict | None:
    """Environment changes that give one git child the token for remote's
    forge (GitHub when None), or None (no change at all) when the gateway
    holds none.

    The token travels only as GIT_CONFIG_COUNT/KEY/VALUE in that child's
    environment: an http.extraHeader scoped to that forge's URL prefix
    (GITHUB_GIT_URL, or [gitlab].url), so git sends it to no other host.
    Redirects are refused (git would otherwise follow the first one), no
    credential helper runs, and every trace or verbose switch that would
    echo headers is removed.
    """
    if remote is not None and remote.forge == "gitlab":
        token, prefix, user = gitlab_token(), remote.base, "oauth2"
    else:
        token, prefix, user = github_token(), GITHUB_GIT_URL, "x-access-token"
    if token is None:
        return None
    env: dict = {k: None for k in os.environ if k.startswith(_UNSAFE_ENV_PREFIXES) or k in _UNSAFE_ENV}
    pairs = [(f"http.{prefix}.extraHeader", f"Authorization: {git_basic_auth(token, user)}"),
             ("http.followRedirects", "false"),
             ("credential.helper", "")]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def git(*args: str, cwd: Path, timeout: int = 300, check: bool = True,
        auth: bool | Remote = False) -> subprocess.CompletedProcess:
    """Run git in a broker-owned repository. auth for a command that talks to
    the forge: the repo's Remote (True means GitHub); it carries the gateway's
    token for that forge if there is one."""
    remote = auth if isinstance(auth, Remote) else None
    cp = run(["git", *_GIT_HARDENING, *args], cwd=cwd, timeout=timeout,
             extra_env=git_auth_env(remote) if auth else None)
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


def mirror_name(remote: Remote) -> str:
    """The mirror's directory name. GitHub's is the readable owner__name (an
    owner has no "_", so two repos can't share it). A GitLab path may hold
    "_" anywhere, so its mirror is named by a digest of host and path, and
    two projects never share one (and so never push to each other)."""
    if remote.forge == "github":
        return remote.path.replace("/", "__") + ".git"
    host = urllib.parse.urlsplit(remote.base).netloc.replace(":", "_")
    digest = hashlib.sha256(f"{remote.base}\0{remote.path.lower()}".encode()).hexdigest()[:32]
    return f"gitlab__{host}__{digest}.git"


def bundle_stem(repo: str, remote: Remote) -> str:
    """The start of git_fetch's bundle name: owner__name on GitHub; on GitLab
    the readable path plus a digest, since "__" can occur inside a GitLab path
    (a__b/c and a/b__c would otherwise share a bundle)."""
    if remote.forge == "github":
        return repo.replace("/", "__")
    digest = hashlib.sha256(f"{remote.base}\0{remote.path.lower()}".encode()).hexdigest()[:12]
    return f"{repo.replace('/', '__').replace(':', '_')}__{digest}"


def ensure_mirror(repo: str, remote: Remote | None = None) -> Path:
    remote = remote or Remote("github", repo, GITHUB_GIT_URL)
    path = config.MIRRORS / mirror_name(remote)
    if not path.exists():
        path.mkdir(parents=True)
        git("init", "--bare", "-q", cwd=path)
        git("remote", "add", "origin", remote.git_url, cwd=path)
    return path


def tool_git_fetch(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    remote = remote_of(repo, policy)
    branch = check_branch_name(args["branch"])
    with tool_lock:
        mirror = ensure_mirror(repo, remote)
        git("fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=mirror,
            auth=remote)
        name = f"{bundle_stem(repo, remote)}__{branch.replace('/', '__')}.bundle"
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


def check_range(rng: str, mirror: Path, branch: str, base: str, limits: dict) -> int:
    """The number of commits in rng (base..branch in the mirror), after
    refusing an empty or oversized range and any commit whose patch,
    message or author/committer names and emails look like secrets."""
    max_diff = int(limits.get("max_diff_bytes", 2_000_000))
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
    return count


def _mirror_ref(mirror: Path, ref: str) -> str:
    """The commit ref names in the mirror, or "" if there is none."""
    return git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", cwd=mirror, check=False).stdout.strip()


def _push_failure(push: subprocess.CompletedProcess, remote: Remote, repo: str, branch: str) -> str:
    body = sanitize(push.stderr + push.stdout, 3000)
    return f"Push rejected by {remote.label}:\n" + untrusted(f"git push {repo}:{branch}", body)


def tool_git_push(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    remote = remote_of(repo, policy)
    branch = check_push_branch(args["branch"], policy)
    base = check_branch_name(args.get("base") or policy["git"].get("default_base", "main"))
    fname = args["bundle"]
    outgoing = f"{config.EXCHANGE_DIR}/outgoing"
    if "/" in fname or not fname.endswith(".bundle"):
        raise Denied(f"bundle must be a file name ending in .bundle inside {outgoing}/")
    limits = policy.get("filters", {})
    max_bundle = int(limits.get("max_bundle_bytes", 100_000_000))
    max_diff = int(limits.get("max_diff_bytes", 2_000_000))  # the file list below

    with tool_lock, private_tempdir() as tmp:
        # One read of the agent's file, as a regular file within the cap; every
        # git command below runs on this private copy, so it can't be swapped.
        bundle = tmp / "push.bundle"
        copy_workspace_file(f"{outgoing}/{fname}", bundle, max_bundle,
                            hint=f"bundle only the new commits (git bundle create {outgoing}/{fname} "
                                 f"origin/{base}..{branch}) or push the work in smaller pieces")
        mirror = ensure_mirror(repo, remote)
        git("fetch", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}", cwd=mirror,
            auth=remote)
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
        count = check_range(rng, mirror, branch, base, limits)
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
    # The job is what the gateway pushes itself if the write is queued
    # (complete_git_push): the verified ref, pinned by head and bundle digest.
    held = require_approval({"tool": "git_push", "repo": repo, "branch": branch, "base": base, "head": sha,
                             "commits": count, "shortstat": shortstat, "file_count": len(files),
                             "files": files[:50], "bundle_sha256": bundle_digest, "secret_scan": "clean",
                             **patch_fields},
                            job={"repo": repo, "branch": branch, "base": base, "head": sha,
                                 "bundle_sha256": bundle_digest, "commits": count})
    with tool_lock:
        mirror = ensure_mirror(repo, remote)
        if _mirror_ref(mirror, incoming) != sha:
            raise Denied(f"{branch} changed in the broker's mirror while awaiting approval; retry the push")
        push = git("push", "--porcelain", "origin", f"{incoming}:refs/heads/{branch}", cwd=mirror, check=False,
                   auth=remote)
    if push.returncode != 0:
        return with_held_note(_push_failure(push, remote, repo, branch), held)
    return with_held_note(f"Pushed {count} commit(s) to {repo}:{branch} ({sha}).\n{sanitize(push.stdout, 2000)}",
                          held)


_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _job_str(job: dict, key: str) -> str:
    value = job.get(key)
    if not isinstance(value, str):
        raise Denied(f"the queued write's {key} is missing or not text; nothing was written")
    return value


def _job_matches(job: dict, card: dict, keys: tuple[str, ...]) -> None:
    """The job a queued write runs from must say what its card says."""
    for key in keys:
        if job.get(key) != card.get(key):
            raise Denied(f"the queued write's {key} does not match its approval card; nothing was written")


def complete_git_push(job: dict, card: dict, policy: dict) -> str:
    """Make a queued push the person approved (core/holds.py runs it): the
    ref git_push verified, still in the broker's mirror, with nothing read
    from the workspace. Every argument is checked again against the policy
    and must match the card; the mirror must still exist and its ref still
    point at the card's head; that range is secret-scanned again (so even a
    hand-made job can push only what passes the scan); then the push runs
    exactly as git_push's own. Raises Denied, with nothing pushed, if any
    check fails or the forge rejects it."""
    repo = check_repo(_job_str(job, "repo"), policy)
    remote = remote_of(repo, policy)
    branch = check_push_branch(_job_str(job, "branch"), policy)
    base = check_branch_name(_job_str(job, "base"))
    sha = _job_str(job, "head")
    if not _SHA_RE.fullmatch(sha):
        raise Denied("the queued push's head is not a commit id; nothing was pushed")
    _job_matches(job, card, ("repo", "branch", "base", "head", "bundle_sha256"))
    if not (config.MIRRORS / mirror_name(remote)).is_dir():
        raise Denied(f"the broker's mirror of {repo} is gone; nothing was pushed")
    if remote.forge == "gitlab" and gitlab_token() is None:
        raise Denied("the gateway holds no gitlab credential; nothing was pushed")
    incoming = f"{config.REF_NAMESPACE}/{branch}"
    with tool_lock:
        mirror = ensure_mirror(repo, remote)
        if _mirror_ref(mirror, incoming) != sha:
            raise Denied(f"{branch} changed in the broker's mirror after the push was queued (another push to the "
                         "same branch?); nothing was pushed")
        count = check_range(f"refs/remotes/origin/{base}..{incoming}", mirror, branch, base,
                            policy.get("filters", {}))
        push = git("push", "--porcelain", "origin", f"{incoming}:refs/heads/{branch}", cwd=mirror, check=False,
                   auth=remote)
    if push.returncode != 0:
        raise Denied(_push_failure(push, remote, repo, branch))
    return f"Pushed {count} commit(s) to {repo}:{branch} ({sha}).\n{sanitize(push.stdout, 2000)}"


def tool_github_create_pr(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    if remote_of(repo, policy).forge != "github":
        raise Denied(f"{repo} is a GitLab project: open a merge request with gitlab_create_mr")
    head = check_push_branch(args["head"], policy)
    base = check_branch_name(args.get("base") or policy["git"].get("default_base", "main"))
    title, body = args["title"], args.get("body", "")
    block_outbound_secrets("PR title", title)
    block_outbound_secrets("PR body", body)
    draft = bool(args.get("draft", True))
    held = require_approval({"tool": "github_create_pr", "repo": repo, "head": head, "base": base,
                             "title": title, "body": body[:2000], "draft": draft},
                            job={"repo": repo, "head": head, "base": base, "title": title, "body": body,
                                 "draft": draft})
    _, text = _open_pr(repo, head, base, title, body, draft)
    return with_held_note(text, held)


def _open_pr(repo: str, head: str, base: str, title: str, body: str, draft: bool) -> tuple[bool, str]:
    """POST the pull request: (True, "Opened PR ...") or, for an HTTP error,
    (False, the error with a hint). Anything else raises."""
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
        return False, f"GitHub API returned HTTP {e.code}:\n" + untrusted(f"GitHub API ({repo})", detail) + hint
    return True, f"Opened PR #{pr['number']}: {pr['html_url']}"


def _queued_pr_args(job: dict, card: dict, policy: dict, forge: str) -> tuple[str, str, str, str, str, bool]:
    """A queued PR/MR job's (repo, head, base, title, body, draft), checked
    again as the tool checked them and against the card."""
    repo = check_repo(_job_str(job, "repo"), policy)
    if remote_of(repo, policy).forge != forge:
        raise Denied(f"{repo} is not on the forge this queued write is for; nothing was written")
    head = check_push_branch(_job_str(job, "head"), policy)
    base = check_branch_name(_job_str(job, "base"))
    title, body, draft = _job_str(job, "title"), _job_str(job, "body"), job.get("draft")
    if not isinstance(draft, bool):
        raise Denied("the queued write's draft flag is not true or false; nothing was written")
    block_outbound_secrets("title", title)
    block_outbound_secrets("body", body)
    _job_matches(job, card, ("repo", "head", "base", "title", "draft"))
    if card.get("body") != body[:2000]:
        raise Denied("the queued write's body does not match its approval card; nothing was written")
    return repo, head, base, title, body, draft


def complete_github_create_pr(job: dict, card: dict, policy: dict) -> str:
    """Open a queued pull request the person approved (core/holds.py runs
    it) from the arguments github_create_pr checked and stored. Raises
    Denied if a check fails or GitHub refuses it."""
    ok, text = _open_pr(*_queued_pr_args(job, card, policy, "github"))
    if not ok:
        raise Denied(text)
    return text


_MR_TITLE_MAX = 255  # GitLab's limit
_MR_BODY_MAX = 65536  # GitHub's PR body limit; GitLab's own (1 MiB) is far more than an approver can read
# Title prefixes GitLab documents as marking a merge request a draft.
_DRAFT_PREFIXES = ("draft:", "[draft]", "(draft)")


def _gitlab_failure(e: urllib.error.HTTPError, repo: str) -> str:
    with e:
        detail = sanitize(e.read().decode("utf-8", "replace"), 2000)
    hint = ""
    if e.code in (401, 403, 404):
        hint = (f"\nHint: the gateway's gitlab credential must be a project (or group) access token with the "
                f"Developer role and the api and write_repository scopes on {repo}")
    return f"GitLab API returned HTTP {e.code}:\n" + untrusted(f"GitLab API ({repo})", detail) + hint


def tool_gitlab_create_mr(args: dict, policy: dict) -> str:
    repo = check_repo(args["repo"], policy)
    remote = remote_of(repo, policy)
    if remote.forge != "gitlab":
        raise Denied(f"{repo} is a GitHub repo: open a pull request with github_create_pr")
    head = check_push_branch(args["head"], policy)
    base = check_branch_name(args.get("base") or policy["git"].get("default_base", "main"))
    title, body = args["title"], args.get("body", "")
    if not isinstance(title, str) or not title.strip() or _TITLE_BAD_RE.search(title):
        raise Denied("merge request title must be a single line with no control characters")
    if not isinstance(body, str) or len(body) > _MR_BODY_MAX:
        raise Denied(f"merge request body must be a string of at most {_MR_BODY_MAX} characters")
    block_outbound_secrets("MR title", title)
    block_outbound_secrets("MR body", body)
    draft = bool(args.get("draft", True))
    if draft and not title.strip().lower().startswith(_DRAFT_PREFIXES):
        title = f"Draft: {title}"
    if len(title) > _MR_TITLE_MAX:
        raise Denied(f"merge request title is over {_MR_TITLE_MAX} characters")
    # Checked before the hold, so nobody approves a call that can't be made.
    if gitlab_token() is None:
        raise Denied("the gateway holds no gitlab credential; the deployment must deliver it "
                     "(README, Configuring credentials)")
    # The card shows the body's start, and carries its digest so two merge
    # requests whose bodies differ only past the excerpt never share a card.
    held = require_approval({"tool": "gitlab_create_mr", "repo": repo, "head": head, "base": base,
                             "title": title, "body": body[:2000],
                             "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "body_chars": len(body),
                             "draft": draft},
                            job={"repo": repo, "head": head, "base": base, "title": title, "body": body,
                                 "draft": draft})
    _, text = _open_mr(repo, remote, head, base, title, body)
    return with_held_note(text, held)


def _open_mr(repo: str, remote: Remote, head: str, base: str, title: str, body: str) -> tuple[bool, str]:
    """POST the merge request: (True, "Opened MR ...") or, for an HTTP
    error, (False, the error with a hint). Raises Denied with no token or
    an unexpected answer, and anything else it meets."""
    # Read after approval, so a long wait still uses the current token.
    token = gitlab_token()
    if token is None:
        raise Denied("the gateway's gitlab credential was removed while the call awaited approval")
    payload = json.dumps({"source_branch": head, "target_branch": base, "title": title,
                          "description": body}).encode()
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": config.USER_AGENT, "Authorization": f"Bearer {token}"}
    project = urllib.parse.quote(remote.path, safe="")
    req = urllib.request.Request(f"{remote.base}api/v4/projects/{project}/merge_requests", data=payload,
                                 method="POST", headers=headers)
    try:
        with NO_REDIRECT_OPENER.open(req, timeout=30) as resp:
            mr = json.load(resp)
    except urllib.error.HTTPError as e:
        return False, _gitlab_failure(e, repo)
    if not isinstance(mr, dict) or not isinstance(mr.get("iid"), int):
        raise Denied(f"GitLab returned an unexpected response for the new merge request in {repo}")
    return True, f"Opened MR !{mr['iid']}: {sanitize(str(mr.get('web_url', '')), 300)}"


def complete_gitlab_create_mr(job: dict, card: dict, policy: dict) -> str:
    """Open a queued merge request the person approved (core/holds.py runs
    it) from the arguments gitlab_create_mr checked and stored (its title
    already as sent, "Draft: " included). Raises Denied if a check fails, the
    gitlab credential is gone or GitLab refuses it."""
    repo, head, base, title, body, _ = _queued_pr_args(job, card, policy, "gitlab")
    if card.get("body_sha256") != hashlib.sha256(body.encode()).hexdigest():
        raise Denied("the queued write's body does not match its approval card; nothing was written")
    if _TITLE_BAD_RE.search(title) or len(title) > _MR_TITLE_MAX or len(body) > _MR_BODY_MAX:
        raise Denied("the queued merge request's title or body is not one GitLab takes; nothing was written")
    ok, text = _open_mr(repo, remote_of(repo, policy), head, base, title, body)
    if not ok:
        raise Denied(text)
    return text


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
    held = require_approval({"tool": "github_create_issue", "repo": repo, "title": title, "body": body[:2000],
                             "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "labels": wanted})
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    # Read after approval, so a long wait still uses the current token.
    token = github_token()
    try:
        issue = _github_api("POST", f"/repos/{repo}/issues", token, payload)
    except urllib.error.HTTPError as e:
        return with_held_note(_github_api_failure(e, repo, token is not None, "Issues read/write"), held)
    if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
        raise Denied(f"GitHub returned an unexpected response for the new issue in {repo}")
    return with_held_note(f"Opened issue #{issue['number']} in {repo}: "
                          f"{sanitize(str(issue.get('html_url', '')), 300)}", held)


_REPO_DESCRIPTION = "owner/name on GitHub, or <gitlab host>/<group>/<project> on the configured GitLab"
_QUEUED_NOTE = (" If the deployment holds it for approval, the result may say it is queued: the gateway makes "
                "the write itself once approved, so report it as queued, not done. A result whose first line ends "
                "with \"held for approval ... approved by\" and the approver's name is complete, not queued: the "
                "write was made after the approval.")

approvals.register_deferred("git_push", complete_git_push)
approvals.register_deferred("github_create_pr", complete_github_create_pr)
approvals.register_deferred("gitlab_create_mr", complete_gitlab_create_mr)

register({
    "git_fetch": (tool_git_fetch, f"Fetch a branch of an allowlisted GitHub repo or GitLab project. Writes a "
                  f"git bundle to {config.EXCHANGE_DIR}/incoming/ and returns the command to import it.",
                  {"repo": {"type": "string", "description": _REPO_DESCRIPTION},
                   "branch": {"type": "string"}}, ["repo", "branch"]),
    "git_push": (tool_git_push, "Push a branch to an allowlisted GitHub repo or GitLab project. First run "
                 f"`git bundle create {config.EXCHANGE_DIR}/outgoing/<name>.bundle origin/<base>..<branch>` "
                 "(or the whole branch). Diff, commit messages and the branch name are secret-scanned; "
                 "protected branches are refused and force-pushes are impossible." + _QUEUED_NOTE,
                 {"repo": {"type": "string", "description": _REPO_DESCRIPTION},
                  "branch": {"type": "string", "description": "branch to push; must use the allowed prefix"},
                  "bundle": {"type": "string", "description": f"file name inside {config.EXCHANGE_DIR}/outgoing/"},
                  "base": {"type": "string", "description": "branch the work is based on (default from policy)"}},
                 ["repo", "branch", "bundle"]),
    "github_create_pr": (tool_github_create_pr, "Open a pull request on an allowlisted GitHub repo. "
                         "Title, body and branch names are secret-scanned. Draft by default." + _QUEUED_NOTE,
                         {"repo": {"type": "string"}, "head": {"type": "string"}, "base": {"type": "string"},
                          "title": {"type": "string"}, "body": {"type": "string"},
                          "draft": {"type": "boolean"}}, ["repo", "head", "title"]),
    "gitlab_create_mr": (tool_gitlab_create_mr, "Open a merge request on an allowlisted GitLab project "
                         "(<gitlab host>/<group>/<project>). Title, body and branch names are secret-scanned. "
                         "Draft by default (the title gets GitLab's \"Draft: \" prefix)." + _QUEUED_NOTE,
                         {"repo": {"type": "string", "description": "<gitlab host>/<group>/<project>"},
                          "head": {"type": "string", "description": "source branch; must use the allowed prefix"},
                          "base": {"type": "string", "description": "target branch (default from policy)"},
                          "title": {"type": "string"}, "body": {"type": "string", "description": "Markdown"},
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
