<#
.SYNOPSIS
  Prepares Windows for the AI Code Factory: WSL2, Ubuntu 24.04, and nested
  virtualization (the factory's sandboxes are microVMs, which need KVM inside WSL).

.DESCRIPTION
  Run from a normal (not administrator) PowerShell window:
    powershell -ExecutionPolicy Bypass -File .\enable-wsl.ps1

  It only does what's missing, and says what it changes:
    1. Installs Ubuntu 24.04 under WSL, if it isn't there. (If WSL itself
       is missing, it tells you the one administrator command to run.)
    2. Sets nestedVirtualization=true under [wsl2] in %UserProfile%\.wslconfig,
       keeping a backup of the old file and every other setting in it.
    3. Restarts WSL (after asking), so the setting takes effect.
  See docs/setup-wsl.md in the repo for the whole walkthrough.
#>
[CmdletBinding()]
param(
  [string]$Distro = "Ubuntu-24.04",
  [string]$WslConfig = (Join-Path $env:USERPROFILE ".wslconfig")
)
$ErrorActionPreference = "Stop"

function Say([string]$Text) { Write-Host $Text }
function Ok([string]$Text) { Write-Host "  [ok] $Text" -ForegroundColor Green }
function Todo([string]$Text) { Write-Host "  [!!] $Text" -ForegroundColor Yellow }

# Set-WslConfigKey PATH SECTION KEY VALUE -> $true if the file changed.
# Keeps every other line as it was; adds the section if it's missing.
function Set-WslConfigKey([string]$Path, [string]$Section, [string]$Key, [string]$Value) {
  $lines = @()
  if (Test-Path $Path) { $lines = @(Get-Content -Path $Path) }
  $out = New-Object System.Collections.Generic.List[string]
  $inSection = $false; $sectionSeen = $false; $done = $false
  foreach ($line in $lines) {
    $t = $line.Trim()
    if ($t -match '^\[(.+)\]$') {
      if ($inSection -and -not $done) { $out.Add("$Key=$Value"); $done = $true }
      $inSection = ($Matches[1].Trim() -ieq $Section)
      if ($inSection) { $sectionSeen = $true }
      $out.Add($line); continue
    }
    if ($inSection -and $t -match "^$([regex]::Escape($Key))\s*=") {
      if (-not $done) { $out.Add("$Key=$Value"); $done = $true }
      continue  # drop the old value (and any duplicates)
    }
    $out.Add($line)
  }
  if ($inSection -and -not $done) { $out.Add("$Key=$Value"); $done = $true }
  if (-not $sectionSeen) {
    if ($out.Count -gt 0 -and $out[$out.Count - 1].Trim() -ne "") { $out.Add("") }
    $out.Add("[$Section]"); $out.Add("$Key=$Value")
  }
  $old = ($lines -join "`n"); $new = ($out -join "`n")
  if ($old -eq $new) { return $false }
  if (Test-Path $Path) {
    $backup = "$Path.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Copy-Item -Path $Path -Destination $backup
    Say "    (backed up the old file to $backup)"
  }
  # WSL reads .wslconfig as UTF-8; write it without a byte-order mark.
  [System.IO.File]::WriteAllText($Path, ($out -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
  return $true
}

function Get-WslDistros {
  # wsl.exe prints UTF-16; drop the NULs PowerShell leaves in.
  $raw = & wsl.exe --list --quiet 2>$null
  if ($LASTEXITCODE -ne 0) { return @() }
  return @($raw | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
}

if ($MyInvocation.InvocationName -eq ".") { return }  # dot-sourced (tests): define functions only

Say "AI Code Factory: preparing Windows"
Say ""

# Windows 11 (build 22000+) is what Docker documents and what nested virtualization in WSL needs.
$build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
if ($build -lt 22000) { throw "Windows 11 is required (this is build $build)." }
Ok "Windows 11 (build $build)"

# 1. WSL and the Ubuntu distro
& wsl.exe --status *> $null
if ($LASTEXITCODE -ne 0) {
  Todo "WSL isn't installed. In an administrator PowerShell run:"
  Say  "         wsl --install --no-distribution"
  Say  "       then restart Windows and run this script again."
  exit 1
}
Ok "WSL installed"

$distros = Get-WslDistros
if ($distros -contains $Distro) {
  Ok "$Distro installed"
} else {
  Todo "$Distro isn't installed; installing it now."
  Say  "       When it opens, choose a Linux user name and password, then type 'exit' to come back here."
  & wsl.exe --install --distribution $Distro
  if ($LASTEXITCODE -ne 0) { throw "wsl --install -d $Distro failed (exit $LASTEXITCODE)." }
  Ok "$Distro installed"
}

# 2. Nested virtualization
if (Set-WslConfigKey -Path $WslConfig -Section "wsl2" -Key "nestedVirtualization" -Value "true") {
  Ok "set nestedVirtualization=true in $WslConfig"
  $restart = $true
} else {
  Ok "nestedVirtualization=true already set in $WslConfig"
  $restart = $false
}

# 3. Restart WSL so the setting applies
if ($restart) {
  $answer = Read-Host "  Restart WSL now so the setting applies? This closes any open WSL terminals [y/N]"
  if ($answer -match '^(y|yes)$') {
    & wsl.exe --shutdown
    Ok "WSL restarted"
  } else {
    Todo "Run 'wsl --shutdown' before continuing, or the setting won't apply."
  }
}

Say ""
Say "Windows is ready. Next, open '$Distro' from the Start menu and follow"
Say "docs/setup-wsl.md from step 3 (installing Docker inside Ubuntu)."
