"""Install the exact sbx version pinned in scripts/lib/common.sh, verifying its SHA-256 checksum,
and skip it if sbx is already at the pinned version. Port of scripts/install-prereqs.sh, now a shim
that runs this as `factory _install-prereqs`; `factory setup` still calls the script.

  macOS 14+ (Apple silicon): under $PREFIX (default ~/.local).
  Ubuntu 24.04+ x86_64, including WSL2: sbx from Docker's .deb via apt (asks for sudo; it installs
  the AppArmor profile sbx needs).

This needs Python 3.9+ before sbx is installed, which `factory setup` has already checked (its
first step is `factory doctor`) and the README lists as a prerequisite.
"""

import copy
import hashlib
import os
import shutil
import tempfile

from factory import pins, preflight, sbx
from factory.context import FactoryError
from factory.preflight import SBX_VERSION


class Stop(Exception):
    """Stop with this exit status; what went wrong has been printed (bash's set -e)."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def _check(status):
    if status != 0:
        raise Stop(status)


def _require(ctx, name):
    if not preflight.which(ctx, name):
        raise FactoryError("'%s' not found in PATH (see README.md)" % name)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch(ctx, url, sha256, dest):
    if ctx.run(["curl", "-fsSL", "--retry", "3", "-o", dest, url]) != 0:
        raise FactoryError("download failed: %s" % url)
    if _sha256(dest) != sha256:
        raise FactoryError("checksum mismatch for %s" % url)


def _platform(ctx):
    system, machine = ctx.uname
    if (system, machine) == ("Darwin", "arm64"):
        return "macos"
    if (system, machine) == ("Linux", "x86_64"):
        return "linux"
    raise FactoryError("this installer supports macOS (Apple silicon) or Ubuntu Linux (x86_64, including WSL2) only; "
                       "found %s/%s" % (system, machine))


def _install(ctx, tmp):
    environ = ctx.environ
    prefix = environ.get("PREFIX") or os.path.join(environ.get("HOME", ""), ".local")
    platform = _platform(ctx)

    if platform == "macos":
        got = ctx.capture(["sw_vers", "-productVersion"])
        major = got.out.strip().split(".")[0]
        if got.status != 0 or not major.isdigit() or int(major) < 14:
            raise FactoryError("macOS 14 (Sonoma) or later required")
        mac = pins.read(ctx.paths.root, "SBX_URL", "SBX_SHA256")
    else:
        try:
            wsl = preflight.is_wsl(ctx.host_files["proc_version"])
            preflight.kvm_check(ctx.host_files["kvm"], wsl)
            deb_url, deb_sha256 = preflight.sbx_deb_pin(ctx.host_files["os_release"], wsl)
        except preflight.Problem as e:
            ctx._flush()
            ctx.stderr.write("%s\n" % e)
            raise Stop(1)
        _require(ctx, "apt-get")
        _require(ctx, "sudo")
    _require(ctx, "curl")

    os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)

    installed = preflight.which(ctx, "sbx")
    if installed and sbx.installed_version(ctx) == SBX_VERSION:
        ctx.say("sbx %s already installed: %s" % (SBX_VERSION, installed))
    elif platform == "macos":
        ctx.say("Installing sbx %s..." % SBX_VERSION)
        archive = os.path.join(tmp, "sbx.tar.gz")
        _fetch(ctx, mac["SBX_URL"], mac["SBX_SHA256"], archive)
        dest = os.path.join(prefix, "opt", "sbx", SBX_VERSION)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        _check(ctx.run(["tar", "-xzf", archive, "-C", dest]))
        _check(ctx.run(["ln", "-sfn", os.path.join(dest, "Sbx.app", "Contents", "MacOS", "sbx"),
                        os.path.join(prefix, "bin", "sbx")]))
    else:
        ctx.say("Installing sbx %s (Docker's package; apt asks for your password)..." % SBX_VERSION)
        deb = os.path.join(tmp, "docker-sbx.deb")
        _fetch(ctx, deb_url, deb_sha256, deb)
        # apt reads local .deb files as the unprivileged _apt user, which can't enter a private temp
        # dir; it then falls back to reading as root and says so. Make the file readable to keep the
        # output quiet.
        os.chmod(tmp, 0o755)
        os.chmod(deb, 0o644)
        # No recommends: they include systemd-resolved, which can take over /etc/resolv.conf (WSL
        # generates its own) and break DNS. The one that matters, apparmor, is added only where the
        # kernel enforces AppArmor; its profile lets the sandbox shim create user namespaces on
        # Ubuntu 23.10+.
        extra = []
        if os.path.isdir("/sys/kernel/security/apparmor") and not preflight.which(ctx, "apparmor_parser"):
            extra = ["apparmor"]
        # --allow-downgrades: the pinned version wins over a newer one from Docker's apt repo.
        _check(ctx.run(["sudo", "apt-get", "install", "-y", "--no-install-recommends", "--allow-downgrades", deb]
                       + extra))

    path = environ.get("PATH", "")
    if os.path.join(prefix, "bin") not in path.split(":"):
        ctx.say("note: add %s to your PATH, ahead of any other copy of sbx" % os.path.join(prefix, "bin"))

    # Verify what will actually run from PATH.
    checked = copy.copy(ctx)
    checked.environ = dict(environ, PATH="%s:%s" % (os.path.join(prefix, "bin"), path))
    sbx.require_sbx(checked)
    ctx.say("Prerequisites OK: sbx %s" % SBX_VERSION)
    return 0


def main(ctx, argv):
    """`factory _install-prereqs`: what scripts/install-prereqs.sh runs (it takes no arguments)."""
    tmp = tempfile.mkdtemp()
    try:
        return _install(ctx, tmp)
    except Stop as e:
        return e.status
    except OSError as e:
        raise FactoryError("%s: %s" % (e.filename or "I/O error", e.strerror or e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
