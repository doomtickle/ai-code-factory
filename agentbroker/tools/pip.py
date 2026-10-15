"""pip_download: binary wheels for a fully pinned requirements file."""

from __future__ import annotations

import re

from ..core import config
from ..core.errors import Denied
from ..core.filters import sanitize
from ..core.policy import check_package
from ..core.registry import register
from ..core.workspace import factory_dir, run, tool_lock, workspace_path

_REQ_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9,._ -]+\])?==(?P<ver>[A-Za-z0-9.+!_-]+)"
    r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})*)\s*(?:;.*)?$")


def parse_requirements(text: str) -> list[tuple[str, str]]:
    """Accept only fully pinned `name==version [--hash=...]` lines."""
    joined = re.sub(r"\\\n", " ", text)
    pins = []
    for n, raw in enumerate(joined.splitlines(), 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        m = _REQ_RE.match(line)
        if not m:
            raise Denied(f"requirements line {n} is not a pinned 'name==version' requirement: {line[:120]!r}")
        pins.append((m["name"], m["ver"]))
    if not pins:
        raise Denied("requirements file has no packages")
    return pins


def tool_pip_download(args: dict, policy: dict) -> str:
    req = workspace_path(args.get("requirements", "requirements.txt"))
    pins = parse_requirements(req.read_text())
    for name, _ in pins:
        check_package("pypi", name, policy)
    dest = factory_dir("wheels")
    with tool_lock:
        cp = run(["python3", "-m", "pip", "download", "--isolated", "--no-input", "--disable-pip-version-check",
                  "--only-binary=:all:", "--no-deps", "--index-url", "https://pypi.org/simple",
                  "-d", str(dest), "-r", str(req)], timeout=900)
    out = sanitize((cp.stdout + cp.stderr).strip(), 4000)
    if cp.returncode != 0:
        return f"pip download failed (exit {cp.returncode}):\n{out}"
    rel_req = req.relative_to(config.WORKSPACE.resolve())
    return (f"Downloaded {len(pins)} wheel(s) to .factory/wheels (binary wheels only, no code executed).\n"
            f"Install them offline in your sandbox with:\n"
            f"  uv venv && uv pip install --offline --no-index --find-links .factory/wheels -r {rel_req}")


register({
    "pip_download": (tool_pip_download, "Download binary wheels for a fully pinned requirements file into "
                     ".factory/wheels/ (no code runs). Returns the offline install command.",
                     {"requirements": {"type": "string", "description": "path (default requirements.txt)"}}, []),
})
