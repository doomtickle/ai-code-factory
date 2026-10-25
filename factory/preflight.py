"""Platform checks `factory doctor` makes. Port of scripts/lib/preflight.sh (which stays:
scripts/install-prereqs.sh sources it), of resolve_python in scripts/lib/common.sh and of
keychain_backend in scripts/lib/secrets.sh. factory/test_preflight.py runs the bash and
the Python on the same fixtures. Each check raises Problem with the text bash prints on
stderr (no final newline), and changes nothing.
"""

import os
import pwd
import re
import shutil

from factory.context import FactoryError

# Keep in sync with scripts/lib/common.sh (a test compares them).
SBX_VERSION = "0.45.1"
SBX_DEB_UBUNTU2404_URL = "https://github.com/docker/sbx-releases/releases/download/v%s/DockerSandboxes-linux-amd64-ubuntu2404.deb" % SBX_VERSION
SBX_DEB_UBUNTU2404_SHA256 = "aaceda8ade7bdbb285a8e5b27e29d5af1aeef55c57bf4db892966ea47ecc11c1"
SBX_DEB_UBUNTU2604_URL = "https://github.com/docker/sbx-releases/releases/download/v%s/DockerSandboxes-linux-amd64-ubuntu2604.deb" % SBX_VERSION
SBX_DEB_UBUNTU2604_SHA256 = "db12d894a9d92da7c33afb5a4654bb341a92a90189b2095490ed3128d58f9c3a"

KVM_UNDER_WSL = """Under WSL2, turn on nested virtualization on the Windows side. Run
windows/enable-wsl.ps1 from this repo in PowerShell, or by hand:
  1. Add to %UserProfile%\\.wslconfig:
       [wsl2]
       nestedVirtualization=true
  2. In PowerShell: wsl --shutdown, then reopen your WSL terminal.
Virtualization (Intel VT-x / AMD-V) must also be on in the PC's BIOS/UEFI."""


class Problem(Exception):
    """A check failed; str() is what to tell the user, the first line being the summary and
    the rest what to do about it."""


def which(ctx, name):
    """`command -v NAME` for a program: its path in ctx's PATH, or None."""
    return shutil.which(name, path=ctx.environ.get("PATH") or os.defpath)


def user_name():
    """`id -un`"""
    uid = os.geteuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def is_wsl(proc_version="/proc/version"):
    try:
        with open(proc_version, "rb") as f:
            text = f.read().decode("utf-8", "replace").lower()
    except OSError:
        return False
    return "microsoft" in text or "wsl" in text


def os_release_field(field, os_release="/etc/os-release"):
    """The value of FIELD= in os-release with its double quotes dropped ("" if absent)."""
    try:
        with open(os_release, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in text.split("\n"):
        if line.startswith(field + "="):
            return line[len(field) + 1:].replace('"', "")
    return ""


def sbx_deb_pin(os_release="/etc/os-release", wsl=None):
    """(url, sha256) of the pinned sbx .deb for this Ubuntu release. Raises Problem on
    anything else: Docker supports sbx on Linux only on Ubuntu 24.04 or later, and not on
    derivatives (Mint, Pop!_OS)."""
    wsl = is_wsl() if wsl is None else wsl
    distro = os_release_field("ID", os_release)
    version = os_release_field("VERSION_ID", os_release)
    major = version.split(".")[0]
    number = re.fullmatch(r"[ \t]*([+-]?[0-9]+)[ \t]*", major)
    if distro != "ubuntu" or number is None or int(number.group(1)) < 24:
        text = "sbx on Linux needs Ubuntu 24.04 or later; this is '%s' %s." % (
            distro or "unknown", version or "(unknown version)")
        if wsl:
            text += "\nInstall it from PowerShell: wsl --install -d Ubuntu-24.04 (see docs/setup-wsl.md)"
        raise Problem(text)
    if int(number.group(1)) >= 26:
        return SBX_DEB_UBUNTU2604_URL, SBX_DEB_UBUNTU2604_SHA256
    return SBX_DEB_UBUNTU2404_URL, SBX_DEB_UBUNTU2404_SHA256


def kvm_check(dev="/dev/kvm", wsl=None, user=None):
    """Raise Problem unless KVM is usable by this user."""
    wsl = is_wsl() if wsl is None else wsl
    if not os.path.exists(dev):
        text = "%s not found: sbx needs KVM to run its microVMs.\n" % dev
        text += KVM_UNDER_WSL if wsl else "Turn on virtualization (Intel VT-x / AMD-V) in your BIOS/UEFI settings and reboot."
        raise Problem(text)
    if not (os.access(dev, os.R_OK) and os.access(dev, os.W_OK)):
        text = "%s exists but %s can't use it. Join the kvm group:\n  sudo usermod -aG kvm $USER\n" % (
            dev, user_name() if user is None else user)
        text += ("then run 'wsl --shutdown' in PowerShell and reopen your WSL terminal." if wsl
                 else "then log out and back in.")
        raise Problem(text)


def resolve_python(ctx):
    """The path of a Python 3.9+ for host-side tools. On macOS always the system Python
    (Xcode Command Line Tools), the pinned runtime for the dispatch bridge. Raises
    FactoryError, as bash dies."""
    if ctx.uname[0] == "Darwin":
        python = "/usr/bin/python3"
        hint = "install the Xcode Command Line Tools: xcode-select --install"
    else:
        python = which(ctx, "python3") or ""
        hint = "install python3 3.9+ (e.g. `sudo apt install python3`)"
    if not python:
        raise FactoryError("python3 not found in PATH (%s)" % hint)
    if ctx.capture([python, "-c", "import sys; sys.exit(sys.version_info < (3, 9))"]).status != 0:
        raise FactoryError("%s 3.9+ is required (%s)" % (python, hint))
    return python


def wincred_powershell(ctx):
    """Windows PowerShell when running under WSL with interop, else "". FACTORY_POWERSHELL
    overrides it (tests use a fake)."""
    ps = ctx.environ.get("FACTORY_POWERSHELL", "")
    if not ps:
        if not is_wsl(ctx.host_files["proc_version"]):
            return ""
        ps = which(ctx, "powershell.exe") or ""
        fallback = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        if not ps and os.access(fallback, os.X_OK) and os.path.isfile(fallback):
            ps = fallback
    return ps


def keychain_backend(ctx):
    """macos | secret-tool | wincred | none: where this machine keeps the factory's
    credentials. FACTORY_KEYCHAIN overrides the choice (tests)."""
    if ctx.environ.get("FACTORY_KEYCHAIN"):
        return ctx.environ["FACTORY_KEYCHAIN"]
    if ctx.uname[0] == "Darwin":
        return "macos"
    if which(ctx, "secret-tool"):
        return "secret-tool"
    if wincred_powershell(ctx):
        return "wincred"
    return "none"
