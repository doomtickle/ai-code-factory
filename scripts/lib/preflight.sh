# Platform checks shared by install-prereqs.sh and `factory doctor`. Source
# after common.sh. Each check returns non-zero on a problem and prints what to
# do about it on stderr; none changes system configuration itself.
# shellcheck shell=bash

# is_wsl [PROC_VERSION_FILE] -> success under WSL
is_wsl() {
  grep -qiE 'microsoft|wsl' "${1:-/proc/version}" 2>/dev/null
}

# os_release_field FIELD [OS_RELEASE_FILE] -> value of FIELD= in os-release
os_release_field() {
  sed -n "s/^$1=//p" "${2:-/etc/os-release}" 2>/dev/null | tr -d '"' | head -n1
}

# sbx_deb_pin [OS_RELEASE_FILE] -> "URL SHA256" of the pinned sbx .deb for this
# Ubuntu release. Fails on anything else: Docker supports sbx on Linux only on
# Ubuntu 24.04 or later, and not on derivatives (Mint, Pop!_OS).
sbx_deb_pin() {
  local f="${1:-/etc/os-release}" id version major
  id="$(os_release_field ID "$f")"
  version="$(os_release_field VERSION_ID "$f")"
  major="${version%%.*}"
  if [ "$id" != ubuntu ] || ! [ "$major" -ge 24 ] 2>/dev/null; then
    echo "sbx on Linux needs Ubuntu 24.04 or later; this is '${id:-unknown}' ${version:-(unknown version)}." >&2
    if is_wsl; then
      echo "Install it from PowerShell: wsl --install -d Ubuntu-24.04 (see docs/setup-wsl.md)" >&2
    fi
    return 1
  fi
  if [ "$major" -ge 26 ]; then
    echo "$SBX_DEB_UBUNTU2604_URL $SBX_DEB_UBUNTU2604_SHA256"
  else
    echo "$SBX_DEB_UBUNTU2404_URL $SBX_DEB_UBUNTU2404_SHA256"
  fi
}

# kvm_check [KVM_DEV] -> success if KVM is usable by this user
kvm_check() {
  local dev="${1:-/dev/kvm}"
  if [ ! -e "$dev" ]; then
    echo "$dev not found: sbx needs KVM to run its microVMs." >&2
    if is_wsl; then
      cat >&2 <<'EOF'
Under WSL2, turn on nested virtualization on the Windows side. Run
windows/enable-wsl.ps1 from this repo in PowerShell, or by hand:
  1. Add to %UserProfile%\.wslconfig:
       [wsl2]
       nestedVirtualization=true
  2. In PowerShell: wsl --shutdown, then reopen your WSL terminal.
Virtualization (Intel VT-x / AMD-V) must also be on in the PC's BIOS/UEFI.
EOF
    else
      echo "Turn on virtualization (Intel VT-x / AMD-V) in your BIOS/UEFI settings and reboot." >&2
    fi
    return 1
  fi
  if [ ! -r "$dev" ] || [ ! -w "$dev" ]; then
    echo "$dev exists but $(id -un) can't use it. Join the kvm group:" >&2
    echo "  sudo usermod -aG kvm \$USER" >&2
    if is_wsl; then
      echo "then run 'wsl --shutdown' in PowerShell and reopen your WSL terminal." >&2
    else
      echo "then log out and back in." >&2
    fi
    return 1
  fi
}
