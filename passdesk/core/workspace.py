"""The agent-controlled workspace: path containment, the exchange
directories (config.EXCHANGE_DIR, default ".factory"), and running
subprocesses with a clean git environment.

Everything under the workspace is agent-controlled and may change between
two system calls, so the broker never follows a symlink there and never
reuses a checked path. Directories are walked one component at a time from
the workspace root with O_NOFOLLOW (`_open_dir`), files are opened relative
to those directory descriptors, reads take regular files only, and writes go
to a fresh temp file that is renamed into place (a rename replaces a symlink
or hard link rather than writing through it). Subprocesses never get a
workspace path to write to: they write into broker-private temp space
(`private_tempdir`) and the result is copied in with `install_file`.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config
from .errors import Denied

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK = 1 << 20
_NO_SYMLINKS = "the broker never follows symlinks in the workspace; use a regular file or directory"


def _rel_parts(rel: str) -> list[str]:
    """Components of a workspace-relative path, normalized lexically (valid
    because no component may be a symlink)."""
    if "\0" in rel:
        raise Denied(f"path {rel!r} contains a NUL byte")
    norm = os.path.normpath(rel or ".")
    if os.path.isabs(norm) or norm == ".." or norm.startswith("../"):
        raise Denied(f"path {rel!r} is outside the workspace")
    return [] if norm == "." else norm.split("/")


def _root_fd() -> int:
    # The workspace root itself is the mount the deployment set up, not
    # something the agent can replace; only what is under it is untrusted.
    return os.open(config.WORKSPACE.resolve(), _DIR_FLAGS)


def _open_dir(parts: list[str], create: bool = False) -> int:
    """O_DIRECTORY fd for workspace/<parts>, refusing any symlinked component.
    With create, missing directories are made. The caller closes the fd."""
    fd = _root_fd()
    try:
        for i, name in enumerate(parts):
            if create:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                nfd = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise Denied(f"path {'/'.join(parts[:i + 1])!r} does not exist in the workspace") from None
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise Denied(f"{'/'.join(parts[:i + 1])!r} is a symlink or not a directory; "
                                 f"{_NO_SYMLINKS}") from None
                raise
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def workspace_path(rel: str, must_exist: bool = True) -> Path:
    """Absolute path of workspace/<rel>, for display and as a directory to
    open from. Refuses '..' escapes and symlinked components. Do not hand the
    result to anything that reads or writes: use the fd-based helpers below."""
    root = config.WORKSPACE.resolve()
    parts = _rel_parts(rel)
    p = root.joinpath(*parts)
    cur = root
    for i, name in enumerate(parts):
        cur = cur / name
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if must_exist:
                raise Denied(f"path {rel!r} does not exist in the workspace") from None
            break
        if stat.S_ISLNK(st.st_mode):
            raise Denied(f"{'/'.join(parts[:i + 1])!r} is a symlink; {_NO_SYMLINKS}")
    if p != root and root not in p.resolve().parents:
        raise Denied(f"path {rel!r} is outside the workspace")
    return p


def _open_file(dir_fd: int, name: str, label: str) -> int:
    """Read fd for a regular file `name` in dir_fd; refuses symlinks, FIFOs,
    devices and directories."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise Denied(f"{label} does not exist in the workspace") from None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise Denied(f"{label} is a symlink; {_NO_SYMLINKS}") from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Denied(f"{label} is not a regular file")
    os.set_blocking(fd, True)
    return fd


def _open_workspace_file(rel: str) -> int:
    parts = _rel_parts(rel)
    if not parts:
        raise Denied(f"path {rel!r} is not a regular file")
    dfd = _open_dir(parts[:-1])
    try:
        return _open_file(dfd, parts[-1], repr(rel))
    finally:
        os.close(dfd)


def _too_big(label: str, limit: int, hint: str) -> Denied:
    return Denied(f"{label} is larger than {limit} bytes{'; ' + hint if hint else ''}")


def _read_capped(fd: int, label: str, limit: int, hint: str) -> bytes:
    with os.fdopen(fd, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise _too_big(label, limit, hint)
    return data


def read_workspace_file(rel: str, limit: int, hint: str = "") -> bytes:
    """The contents of regular file workspace/<rel>, read once, at most limit bytes."""
    return _read_capped(_open_workspace_file(rel), repr(rel), limit, hint)


@contextmanager
def open_workspace_dir(rel: str) -> Iterator[int]:
    """O_DIRECTORY fd for existing directory workspace/<rel>, symlinks refused. Closed on exit."""
    fd = _open_dir(_rel_parts(rel))
    try:
        yield fd
    finally:
        os.close(fd)


def read_in_dir(dir_fd: int, name: str, label: str, limit: int, hint: str = "") -> bytes | None:
    """Contents of regular file `name` in dir_fd (at most limit bytes), or None
    if it does not exist. Symlinks and other non-regular files are refused."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _read_capped(_open_file(dir_fd, name, label), label, limit, hint)


def copy_workspace_file(rel: str, dest: Path, limit: int, hint: str = "") -> int:
    """Copy regular file workspace/<rel> once into broker-private dest (at most
    limit bytes) so later reads can't see a swapped file. Returns its size."""
    fd = _open_workspace_file(rel)
    size = 0
    with os.fdopen(fd, "rb") as src, open(dest, "xb") as out:
        for chunk in iter(lambda: src.read(_CHUNK), b""):
            size += len(chunk)
            if size > limit:
                raise _too_big(repr(rel), limit, hint)
            out.write(chunk)
    return size


def file_in_dir(dir_fd: int, name: str) -> int | None:
    """Read fd for regular file `name` in dir_fd, or None if it is missing or
    anything other than a regular file."""
    try:
        return _open_file(dir_fd, name, repr(name))
    except Denied:
        return None


def _check_name(name: str) -> None:
    if not name or "/" in name or "\0" in name or name in (".", ".."):
        raise ValueError(f"not a plain file name: {name!r}")


@contextmanager
def atomic_write(dir_fd: int, name: str):
    """Binary file object whose contents replace `name` in dir_fd on success."""
    _check_name(name)
    tmp = f".{name}.{secrets.token_hex(4)}.part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise


def install_file(dir_fd: int, name: str, src: Path) -> None:
    """Copy broker-private file src into dir_fd as `name`."""
    with open(src, "rb") as s, atomic_write(dir_fd, name) as d:
        shutil.copyfileobj(s, d, _CHUNK)


@contextmanager
def factory_dir(sub: str) -> Iterator[int]:
    """O_DIRECTORY fd for <exchange>/<sub> (created if missing, symlinks
    refused), with <exchange>/.gitignore ensured. Closed on exit. The
    exchange directory's name is config.EXCHANGE_DIR (default ".factory")."""
    _check_name(sub)
    root = config.EXCHANGE_DIR
    ffd = _open_dir([root], create=True)
    try:
        try:
            st = os.stat(".gitignore", dir_fd=ffd, follow_symlinks=False)
            ok = stat.S_ISREG(st.st_mode)
        except FileNotFoundError:
            ok = False
        if not ok:
            with atomic_write(ffd, ".gitignore") as f:
                f.write(b"*\n")
        try:
            os.mkdir(sub, 0o755, dir_fd=ffd)
        except FileExistsError:
            pass
        try:
            fd = os.open(sub, _DIR_FLAGS, dir_fd=ffd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Denied(f"'{root}/{sub}' is a symlink or not a directory; {_NO_SYMLINKS}") from None
            raise
    finally:
        os.close(ffd)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def private_tempdir() -> Iterator[Path]:
    """A fresh mode-0700 directory in the broker's own state, never in the workspace."""
    base = config.STATE / "tmp"
    base.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="call-", dir=base) as d:
        yield Path(d)


def _env(extra_env: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(Path.home())})
    env.update(extra_env or {})
    return env


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600,
        extra_env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=_env(extra_env), capture_output=True, text=True, timeout=timeout)


def run_capped(cmd: list[str], cwd: Path, limit: int, timeout: int = 600) -> tuple[int, bytes | None, str]:
    """(exit code, stdout, stderr) without ever holding more than limit bytes
    of stdout; stdout is None (and the process killed) if it would exceed it."""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, cwd=cwd, env=_env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=err)
        timed_out = threading.Event()
        timer = threading.Timer(timeout, lambda: (timed_out.set(), p.kill()))
        timer.start()
        try:
            out = p.stdout.read(limit + 1)
            if len(out) > limit:
                p.kill()
                out = None
            p.stdout.close()
            code = p.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(cmd, timeout)
        err.seek(0)
        return code, out, err.read(64_000).decode("utf-8", "replace")


tool_lock = threading.Lock()  # serialize mutating operations on mirrors/workspace
