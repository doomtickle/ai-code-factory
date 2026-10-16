"""pip_download: binary wheels for a fully pinned requirements file."""

from __future__ import annotations

import re
from typing import NamedTuple

from ..core import config
from ..core.errors import Denied
from ..core.filters import sanitize
from ..core.policy import check_package
from ..core.registry import register
from ..core.workspace import (factory_dir, install_file, private_tempdir, read_workspace_file, run, tool_lock,
                              workspace_path)

_MAX_REQUIREMENTS_BYTES = 1_000_000
# name==version, optional extras, one or more sha256 hashes, optional marker.
# The marker charset has no "-", so nothing in it can read as a pip option.
_REQ_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9,._ -]+\])?==(?P<ver>[0-9][A-Za-z0-9.+!_-]*)"
    r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})*)\s*(?:;(?P<marker>[A-Za-z0-9_.<>=!~'\" ()]*))?$")
_REQ_FORMAT = ("'name==version --hash=sha256:<64 hex>' (generate one with "
               "`uv pip compile --generate-hashes` or `pip-compile --generate-hashes`)")


class Pin(NamedTuple):
    name: str
    version: str
    hashes: tuple[str, ...]
    marker: str


def parse_requirements(text: str) -> list[Pin]:
    """Accept only `name==version --hash=sha256:...` lines; every option line
    (-r, -c, --index-url, --no-binary, ...) is refused."""
    joined = re.sub(r"\\\r?\n", " ", text)
    pins = []
    for n, raw in enumerate(joined.splitlines(), 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            raise Denied(f"requirements line {n} is a pip option, which pip_download refuses (only "
                         f"{_REQ_FORMAT} lines are allowed): {line[:120]!r}")
        m = _REQ_RE.match(line)
        if not m:
            raise Denied(f"requirements line {n} is not a pinned {_REQ_FORMAT} requirement: {line[:120]!r}")
        hashes = tuple(re.findall(r"sha256:([0-9a-f]{64})", m["hashes"]))
        if not hashes:
            raise Denied(f"requirements line {n} has no --hash; pip_download needs {_REQ_FORMAT}: {line[:120]!r}")
        pins.append(Pin(m["name"], m["ver"], hashes, (m["marker"] or "").strip()))
    if not pins:
        raise Denied("requirements file has no packages")
    return pins


def render_requirements(pins: list[Pin]) -> str:
    """The broker's own requirements file, rebuilt from parsed pins only."""
    lines = []
    for p in pins:
        hashes = " ".join(f"--hash=sha256:{h}" for h in p.hashes)
        lines.append(f"{p.name}=={p.version}{'; ' + p.marker if p.marker else ''} {hashes}")
    return "\n".join(lines) + "\n"


def tool_pip_download(args: dict, policy: dict) -> str:
    rel = args.get("requirements", "requirements.txt")
    # Read the agent's file exactly once; pip only ever sees the broker's copy.
    raw = read_workspace_file(rel, _MAX_REQUIREMENTS_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Denied(f"requirements file {rel!r} is not UTF-8 text") from None
    pins = parse_requirements(text)
    for pin in pins:
        check_package("pypi", pin.name, policy)
    rel_req = workspace_path(rel).relative_to(config.WORKSPACE.resolve())
    with tool_lock, private_tempdir() as tmp:
        req = tmp / "requirements.txt"
        req.write_text(render_requirements(pins))
        wheels = tmp / "wheels"
        wheels.mkdir()
        cp = run(["python3", "-m", "pip", "download", "--isolated", "--no-input", "--disable-pip-version-check",
                  "--only-binary=:all:", "--no-deps", "--require-hashes", "--index-url", "https://pypi.org/simple",
                  "-d", str(wheels), "-r", str(req)], cwd=tmp, timeout=900)
        out = sanitize((cp.stdout + cp.stderr).strip(), 4000)
        if cp.returncode != 0:
            return f"pip download failed (exit {cp.returncode}):\n{out}"
        with factory_dir("wheels") as dest:
            for whl in sorted(wheels.iterdir()):
                if whl.is_file() and not whl.is_symlink() and whl.name.endswith(".whl"):
                    install_file(dest, whl.name, whl)
    return (f"Downloaded {len(pins)} wheel(s) to .factory/wheels (binary wheels only, hash-checked, "
            f"no code executed).\n"
            f"Install them offline in your sandbox with:\n"
            f"  uv venv && uv pip install --offline --no-index --find-links .factory/wheels -r {rel_req}")


register({
    "pip_download": (tool_pip_download, "Download binary wheels for a fully pinned requirements file into "
                     ".factory/wheels/ (no code runs). Returns the offline install command.",
                     {"requirements": {"type": "string", "description": "path (default requirements.txt)"}}, []),
})
