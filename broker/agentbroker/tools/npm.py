"""npm_fetch: the tarballs a lockfile pins, downloaded into an offline mirror."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core import config
from ..core.errors import Denied
from ..core.filters import sanitize
from ..core.net import NO_REDIRECT_OPENER, read_secret_file
from ..core.policy import check_package, host_allowed
from ..core.registry import register
from ..core.workspace import factory_dir, tool_lock, workspace_path

# --------------------------------------------------------------------------
# npm_fetch: the tarballs a lockfile pins, downloaded into an offline mirror in
# .factory/npm-mirror/. No package manager and nothing from the packages runs
# here: the lockfile is parsed as data, every URL must be a plain tarball URL
# on an allowed registry, and every download must match the lockfile's hash.
# Roles then install offline in their own sandbox with factory-test
# (templates/agent/factory-test), never into the project folder, which is
# shared with the host and its own platform's node_modules.

NPM_PUBLIC_REGISTRY = "https://registry.npmjs.org/"
# Yarn v1 lockfiles record registry.yarnpkg.com, which serves the npm
# registry's tarballs at the same paths; fetch those from the npm registry.
_NPM_REGISTRY_ALIASES = {"https://registry.yarnpkg.com/": NPM_PUBLIC_REGISTRY}
# What follows the registry prefix: [@scope/]name/-/[@scope/]file.tgz. No
# "%", "?", ".." or empty segments can get through.
_NPM_TARBALL_RE = re.compile(
    r"^(?:(@[a-z0-9][a-z0-9._~-]*)/)?([a-z0-9][a-z0-9._~-]*)/-/"
    r"(?:@[a-z0-9][a-z0-9._~-]*/)?([A-Za-z0-9][A-Za-z0-9._+-]*\.tgz)$", re.IGNORECASE)
_SRI_ALGOS = ("sha512", "sha384", "sha256", "sha1")  # strongest first
_NPM_MAX_TARBALL_BYTES = 300_000_000
_NPM_DOWNLOAD_WORKERS = 8
_NPM_MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class NpmFetchError(Exception):
    """A tarball that couldn't be mirrored for a reason other than policy; str() is the report."""


def npm_lock_entries(proj: Path) -> tuple[str, list[tuple[str, str | None, str | None]]]:
    """(lockfile name, [(label, resolved, integrity)]) from yarn.lock v1 or package-lock.json v2+."""
    yarn_lock, npm_lock = proj / "yarn.lock", proj / "package-lock.json"
    if yarn_lock.is_file():
        text = yarn_lock.read_text()
        if "# yarn lockfile v1" not in text[:1000]:
            raise Denied("only yarn v1 lockfiles are supported (this yarn.lock is from yarn 2 or later)")
        entries: list[list] = []
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[0].isspace():  # entry header: "a@^1", a@^2:
                entries.append([line.split(",")[0].strip().rstrip(":").strip('"'), None, None])
            elif entries and line.startswith("  ") and not line.startswith("   "):
                key, _, val = line.strip().partition(" ")
                if key == "resolved":
                    entries[-1][1] = val.strip().strip('"')
                elif key == "integrity":
                    entries[-1][2] = val.strip().strip('"')
        return "yarn.lock", [tuple(e) for e in entries]
    if npm_lock.is_file():
        lock = json.loads(npm_lock.read_text())
        if "packages" not in lock:
            raise Denied("package-lock.json lockfileVersion 1 is not supported; regenerate it with npm 7 or later")
        out = []
        for key, meta in lock["packages"].items():
            if "node_modules/" not in key or meta.get("inBundle"):
                continue  # the root project, workspace members, bundled deps
            if meta.get("link"):
                raise Denied(f"linked package {key} is not allowed")
            out.append((key.rsplit("node_modules/", 1)[-1], meta.get("resolved"), meta.get("integrity")))
        return "package-lock.json", out
    raise Denied("no yarn.lock or package-lock.json found (npm_fetch only downloads what a lockfile pins)")


def npm_expected_digest(label: str, integrity: str | None, fragment: str) -> tuple[str, str]:
    """(algorithm, hex digest) to check a download against, from SRI or a yarn '#<sha1>' URL fragment."""
    found = {}
    for part in (integrity or "").split():
        algo, _, b64 = part.partition("-")
        if algo in _SRI_ALGOS and b64:
            try:
                found.setdefault(algo, base64.b64decode(b64.split("?")[0], validate=True).hex())
            except ValueError:
                raise Denied(f"{label} has a malformed integrity hash") from None
    for algo in _SRI_ALGOS:
        if algo in found:
            return algo, found[algo]
    if re.fullmatch(r"[0-9a-f]{40}", fragment):
        return "sha1", fragment
    raise Denied(f"{label} has no integrity hash in the lockfile")


def npm_tarball(label: str, resolved: str | None, integrity: str | None, policy: dict) -> dict:
    """Check one lockfile entry against policy; return what to download and where to put it."""
    if not resolved:
        raise Denied(f"{label} has no resolved registry URL (file:, link: and workspace deps are not allowed)")
    url, _, fragment = resolved.partition("#")
    for alias, target in _NPM_REGISTRY_ALIASES.items():
        if url.startswith(alias):
            url = target + url[len(alias):]
    private = policy.get("packages", {}).get("npm_private_registry", "")
    if private and not private.endswith("/"):
        raise Denied("policy.toml [packages].npm_private_registry must end with '/'")
    for prefix in filter(None, (NPM_PUBLIC_REGISTRY, private)):
        if url.startswith(prefix):
            m = _NPM_TARBALL_RE.match(url[len(prefix):])
            break
    else:
        raise Denied(f"{label} resolves outside the allowed npm registries: {sanitize(resolved, 200)}")
    if not m:
        raise Denied(f"{label} is not a plain registry tarball URL: {sanitize(resolved, 200)}")
    scope, name, base = m.groups()
    check_package("npm", f"{scope}/{name}" if scope else name, policy)
    algo, digest = npm_expected_digest(label, integrity, fragment)
    # Yarn v1's offline-mirror file name (TarballFetcher.getTarballMirrorPath).
    return {"label": label, "url": url, "private": prefix == private, "algo": algo, "digest": digest,
            "file": f"{scope}-{base}" if scope else base}


def npm_registry_auth() -> str | None:
    """Authorization header value for npm_private_registry, or None if no credential was delivered."""
    value = read_secret_file(config.NPM_REGISTRY_SECRET_PATH)
    if not value:
        return None
    if value.startswith(("Bearer ", "Basic ")):
        return value
    if ":" in value:  # user:token
        return "Basic " + base64.b64encode(value.encode()).decode()
    return "Bearer " + value


def file_digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def redact_url(url: str) -> str:
    """scheme://host/path for error reports. Signed storage URLs carry their
    credential in the query string, so that is never shown."""
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.hostname or ''}{u.path}" + ("?<query redacted>" if u.query else "")


def npm_open(item: dict, auth: str | None, policy: dict):
    """Open a tarball URL, following up to _NPM_MAX_REDIRECTS redirects.

    Registries such as JFrog Cloud answer an authorized download with a 302 to
    signed cloud storage. The credential is attached only while the URL is
    under npm_private_registry: a signed URL carries its own auth (S3 and GCS
    reject a request that also has a bearer header) and the credential must not
    reach another host. A redirect may leave the registry it started from only
    for an https host in [packages].npm_redirect_hosts (it must also be in the
    broker's egress kit). Returns (response, final URL, whether it carried the credential).
    """
    pk = policy.get("packages", {})
    registry = pk.get("npm_private_registry", "") if item["private"] else NPM_PUBLIC_REGISTRY
    url = item["url"]
    for hops in range(_NPM_MAX_REDIRECTS + 1):
        with_auth = bool(item["private"] and auth and url.startswith(registry))
        headers = {"User-Agent": config.USER_AGENT}
        if with_auth:
            headers["Authorization"] = auth
        cred = "credential attached" if with_auth else "no credential attached"
        try:
            return NO_REDIRECT_OPENER.open(urllib.request.Request(url, headers=headers), timeout=120), url, with_auth
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location") if e.headers else None
            e.close()
            if e.code not in _REDIRECT_CODES:
                why = {401: "the credential was rejected" if with_auth else "authentication required",
                       403: "the credential was rejected" if with_auth else "access denied",
                       404: "not found"}.get(e.code, "")
                hint = (" - check the \"factory-npm-registry\" keychain entry (README, Enabling a private npm "
                        "registry)" if with_auth and e.code in (401, 403) else "")
                raise NpmFetchError(f"HTTP {e.code} from {redact_url(url)} ({cred})"
                                    f"{': ' + why if why else ''}{hint}") from None
            if not location:
                raise NpmFetchError(f"HTTP {e.code} redirect from {redact_url(url)} has no Location header") from None
            target = urllib.parse.urljoin(url, location)
            t = urllib.parse.urlsplit(target)
            if t.scheme != "https" or t.username or t.password or not t.hostname:
                raise Denied(f"HTTP {e.code} redirect from {redact_url(url)} to a non-https or "
                             f"malformed URL ({redact_url(target)})") from None
            if not target.startswith(registry) and not host_allowed(t.hostname, pk.get("npm_redirect_hosts", [])):
                raise Denied(f"HTTP {e.code} redirect from {redact_url(url)} to {redact_url(target)}: "
                             f"host {t.hostname} is not in [packages].npm_redirect_hosts; add it there and to "
                             "the broker kit (README, Enabling a private npm registry)") from None
            url = target
        except urllib.error.URLError as e:
            raise NpmFetchError(f"could not reach {urllib.parse.urlsplit(url).hostname} ({cred}): "
                                f"{sanitize(str(e.reason), 200)}; if the broker's network policy blocks this "
                                "host, add it to the broker kit") from None
    raise NpmFetchError(f"more than {_NPM_MAX_REDIRECTS} redirects, the last to {redact_url(url)}")


def npm_download(item: dict, dest: Path, auth: str | None, policy: dict) -> int:
    """Download one tarball into dest, verified against its lockfile hash. Returns bytes written."""
    tmp = dest / f".{item['file']}.{secrets.token_hex(4)}.part"
    h, size = hashlib.new(item["algo"]), 0
    try:
        resp, url, _ = npm_open(item, auth, policy)
        with resp, open(tmp, "wb") as f:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                size += len(chunk)
                if size > _NPM_MAX_TARBALL_BYTES:
                    raise NpmFetchError(f"larger than {_NPM_MAX_TARBALL_BYTES} bytes ({redact_url(url)})")
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != item["digest"]:
            raise NpmFetchError(f"the download from {redact_url(url)} does not match the lockfile's "
                                f"{item['algo']} hash")
        os.replace(tmp, dest / item["file"])
        return size
    finally:
        tmp.unlink(missing_ok=True)


def tool_npm_fetch(args: dict, policy: dict) -> str:
    proj = workspace_path(args.get("path", "."))
    lock_name, entries = npm_lock_entries(proj)
    items: dict[str, dict] = {}
    for label, resolved, integrity in entries:
        item = npm_tarball(label, resolved, integrity, policy)
        other = items.setdefault(item["file"], item)
        if (other["url"], other["digest"]) != (item["url"], item["digest"]):
            raise Denied(f"{other['label']} and {label} would share the mirror file {item['file']}")
    with tool_lock:
        dest = factory_dir("npm-mirror")
        missing = [i for i in items.values()
                   if not (dest / i["file"]).is_file() or file_digest(dest / i["file"], i["algo"]) != i["digest"]]
        failures, total = [], 0
        auth = npm_registry_auth()
        if not auth:
            failures = [f"{i['label']}: no credential for the private npm registry; store one in the host "
                        "keychain (service \"factory-npm-registry\") and run scripts/start-broker.sh again - "
                        "see README, Enabling a private npm registry" for i in missing if i["private"]]
        with ThreadPoolExecutor(max_workers=_NPM_DOWNLOAD_WORKERS) as pool:
            futures = {pool.submit(npm_download, i, dest, auth, policy): i
                       for i in missing if auth or not i["private"]}
            for fut in futures:
                label = futures[fut]["label"]
                try:
                    total += fut.result()
                except Denied as e:
                    failures.append(f"{label}: DENIED by broker policy: {sanitize(str(e), 500)}")
                except (NpmFetchError, OSError) as e:
                    failures.append(f"{label}: {sanitize(str(e), 500)}")
    rel = proj.relative_to(config.WORKSPACE.resolve())
    summary = (f"{len(items)} package(s) from {rel / lock_name}: {len(missing) - len(failures)} downloaded "
               f"({total / 1e6:.1f} MB), {len(items) - len(missing)} already in .factory/npm-mirror.")
    if failures:
        shown = "\n".join(f"  {f}" for f in failures[:20])
        more = f"\n  ... and {len(failures) - 20} more" if len(failures) > 20 else ""
        return f"npm_fetch incomplete: {len(failures)} download(s) failed.\n{shown}{more}\n{summary}"
    return (f"{summary}\nEvery tarball matches its lockfile hash; nothing was executed.\n"
            f"Install and run tests in your sandbox (a local copy; the project folder is left alone), e.g.\n"
            f"  factory-test{'' if str(rel) == '.' else f' -C {rel}'} -q -- npx vitest run")


register({
    "npm_fetch": (tool_npm_fetch, "Download every tarball a yarn.lock (v1) or package-lock.json pins into "
                  ".factory/npm-mirror/, each checked against policy and its lockfile hash (no code runs). "
                  "Then install and run tests in your sandbox with `factory-test -- <command>`.",
                  {"path": {"type": "string", "description": "directory with the lockfile (default .)"}}, []),
})
