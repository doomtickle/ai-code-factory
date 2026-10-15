"""The agent-controlled workspace: path containment, the .factory/ exchange
directories, and running subprocesses with a clean git environment."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from . import config
from .errors import Denied


def factory_dir(sub: str) -> Path:
    d = config.WORKSPACE / ".factory" / sub
    d.mkdir(parents=True, exist_ok=True)
    gi = config.WORKSPACE / ".factory" / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    return d


def workspace_path(rel: str, must_exist: bool = True) -> Path:
    root = config.WORKSPACE.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise Denied(f"path {rel!r} is outside the workspace")
    if must_exist and not p.exists():
        raise Denied(f"path {rel!r} does not exist in the workspace")
    return p


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600,
        extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(Path.home())})
    env.update(extra_env or {})
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


tool_lock = threading.Lock()  # serialize mutating operations on mirrors/workspace
