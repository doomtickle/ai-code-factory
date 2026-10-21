# Setting up on Windows 11 (WSL2)

This guide takes a Windows 11 PC from nothing to a running factory. Everything
runs inside Ubuntu on WSL2. The factory's sandboxes are small virtual machines,
so WSL has to be allowed to run VMs of its own (nested virtualization).

Plan on about 30 minutes, most of it downloads.

> **Status:** the Linux path is tested in Ubuntu containers but not yet end to
> end on a real WSL machine. If a step goes differently for you, the
> [troubleshooting](#troubleshooting) table covers the likely causes, and
> `factory doctor` names the fix for most of them.

## What you need

- Windows 11, 64-bit Intel or AMD
- Virtualization turned on in the PC's BIOS/UEFI. Most PCs ship with it on.
  Task Manager → Performance → CPU shows **Virtualization: Enabled**.
- About 20 GB of free disk
- A Claude Pro or Max subscription
- A Docker account (free): <https://app.docker.com/signup>

## 1. Prepare Windows

From the transfer folder (or `windows\` in the repo), run in a **normal**
PowerShell window, not as administrator:

```powershell
powershell -ExecutionPolicy Bypass -File .\enable-wsl.ps1
```

It only does what's missing:

- Installs **Ubuntu 24.04**. When the Ubuntu window opens, pick a Linux user
  name and password, then type `exit` to return to PowerShell.
- Adds `nestedVirtualization=true` to `%UserProfile%\.wslconfig`, after backing
  up the old file. Other settings in the file are kept.
- Offers to restart WSL so the setting applies.

If WSL itself isn't installed, the script says so and gives you the one
administrator command to run (`wsl --install --no-distribution`). Run it,
restart Windows, and run the script again.

To do it by hand instead:

1. In PowerShell: `wsl --install -d Ubuntu-24.04`
2. Create or edit `%UserProfile%\.wslconfig` so it contains:
   ```ini
   [wsl2]
   nestedVirtualization=true
   ```
3. In PowerShell: `wsl --shutdown`

From here on, every command runs in the **Ubuntu** terminal. Open it from the
Start menu or Windows Terminal.

## 2. Install the basics

```sh
sudo apt-get update && sudo apt-get install -y git curl jq openssl python3 bsdextrautils
```

## 3. Install Docker and join the right groups

Docker builds the sandbox images. Install Docker Engine inside Ubuntu:

```sh
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker,kvm "$USER"
```

This is the one unpinned step: Docker's script installs the current Docker
Engine. That's acceptable because Docker only builds the images, and the
image contents themselves are pinned.

The group change applies only after a WSL restart. In **PowerShell**:

```powershell
wsl --shutdown
```

Then reopen the Ubuntu terminal and check:

```sh
docker run --rm hello-world   # prints "Hello from Docker!"
ls -l /dev/kvm                # exists, group kvm
```

> Already using **Docker Desktop** for Windows? That works too. Turn on
> Settings → Resources → WSL integration → Ubuntu-24.04, and skip the
> `get.docker.com` line, but still run the `usermod` line for the `kvm` group.
> Don't install both.

## 4. Get the repo into Ubuntu

Keep the repo and your projects in the Linux filesystem (`~/code`), **not**
under `/mnt/c`. Windows drives are slow from WSL, and file permissions don't
survive there.

**From the transfer bundle.** Copy the transfer folder to your Windows
Downloads folder, then:

```sh
mkdir -p ~/code
git clone /mnt/c/Users/<you>/Downloads/ai-code-factory-transfer/ai-code-factory.bundle ~/code/ai-code-factory
cd ~/code/ai-code-factory
# Point origin at the real repo, not the bundle file: .gitmodules names the
# broker submodule with a relative URL, which git resolves against origin, so
# removing origin (or leaving it on the bundle path) leaves that unresolvable.
git remote set-url origin https://github.com/doomtickle/ai-code-factory.git
git submodule update --init
git config user.name "Your Name"
git config user.email "you@example.com"
```

**From GitHub**, once you've pushed it there (see
[Keeping it on GitHub](#keeping-it-on-github)):

```sh
git clone --recurse-submodules git@github.com:<you>/ai-code-factory.git ~/code/ai-code-factory
```

## 5. Check the machine

```sh
cd ~/code/ai-code-factory
./bin/factory doctor
```

Every line should be a ✓. Each ✗ comes with the exact command that fixes it.
sbx and herdr show as "not installed yet", which is expected: setup installs
them.

## 6. Run setup

```sh
./bin/factory setup
```

It takes 5 to 15 minutes. It:

- runs `factory doctor` again and stops if anything is wrong
- installs the pinned `sbx` (Docker's `docker-sbx` package, checksum-verified;
  apt asks for your password) and `herdr` (only the optional Herdr layouts use it)
- signs you in to Docker. A browser window opens. If none does, copy the URL
  it prints into a Windows browser.
- builds the three sandbox images and loads them into sbx
- applies the security baseline: outbound network blocked by default, only
  Anthropic allowed
- links `factory` into `~/.local/bin`

If it says to add `~/.local/bin` to your PATH, open a new terminal; Ubuntu
adds it automatically once the folder exists. Then `factory doctor` from any
folder should show everything ✓.

## 7. Start on a project

```sh
git clone <your project> ~/code/my-project
cd ~/code/my-project
factory start
```

`factory start` opens the dashboard in your Windows browser (if nothing opens,
paste the link it prints into one). Its left-hand screen is the Coordinator's terminal.
**The first time:**

1. Press POWER ON on the dashboard's Coordinator screen, type `/login` and
   sign in with your Claude account. Paste the URL into a Windows browser if
   nothing opens.
2. Run `factory start` again in another Ubuntu terminal (or press RESTART on
   the dashboard). This creates the role sandboxes, which pick up the login.

Then tell the Coordinator what you want done. Ctrl-C in the terminal that runs
the dashboard closes it and leaves the factory running; `factory dashboard`
reopens it. Herdr is the older, optional layout: `factory start --layout
watch` (or `tabs`) opens it instead (README, *Layouts*).

## Optional extras

- **Letting agents push branches and open PRs:** README, *Enabling git push and
  pull requests*. Save the GitHub token once with `factory secret save github`:
  it is kept in the Windows Credential Manager and restored for the broker
  every time the broker is created, so `factory update` never asks for it again.
  GitLab instead (or too): README, *Enabling GitLab*, and
  `factory secret save gitlab`.
- **Personal settings** (repos agents may push to, extra hosts): run
  `factory config init` (it writes commented examples to
  `~/.config/ai-code-factory/`) and uncomment what you need. README,
  *Local settings*.
- **GitLab, Gemini, Jira, private npm registry, approvals webhook:** WSL usually has no Linux
  keyring, so the factory uses the **Windows Credential Manager** instead
  (through `powershell.exe`; the value never appears on a command line). Save
  each credential once with `factory secret save <name>`; every broker start
  delivers it. You can see the entries in Windows under *Credential Manager →
  Web Credentials* (`factory-<name>`). `factory secret set <name>` still exists
  for a one-off value that should not be saved. Everything else works without them.

## Keeping it on GitHub

The transfer bundle is a one-time copy. To keep the repo backed up and easy to
clone again, push it to a **private** GitHub repository:

```sh
# Create an empty private repo named ai-code-factory on github.com first, then:
git remote set-url origin git@github.com:<you>/ai-code-factory.git
git push -u origin main
```

After pulling changes later, run `factory update`. It rebuilds the images and
recreates only the sandboxes the changes affect (it lists them and asks first).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `/dev/kvm not found` | Nested virtualization is off, or WSL wasn't restarted. Run `windows/enable-wsl.ps1` (or check `%UserProfile%\.wslconfig`), then `wsl --shutdown`. Also check virtualization is on in the BIOS (Task Manager → Performance → CPU). |
| `/dev/kvm exists but … can't use it` | Run `sudo usermod -aG kvm $USER`, then `wsl --shutdown` in PowerShell and reopen Ubuntu. |
| `docker: permission denied` | Run `sudo usermod -aG docker $USER`, then `wsl --shutdown` and reopen Ubuntu. |
| Docker daemon not running | WSL started without systemd. Add `[boot]` and `systemd=true` to `/etc/wsl.conf` (`sudo nano /etc/wsl.conf`), then `wsl --shutdown`. |
| No browser opens for a login | Copy the URL from the terminal into a Windows browser. Optionally `sudo apt-get install -y wslu` so links open in Windows. |
| Internet stops working inside Ubuntu after setup | Something replaced WSL's `/etc/resolv.conf`. Setup avoids installing `systemd-resolved`, but if it got installed: `sudo apt-get remove systemd-resolved`, then `wsl --shutdown`. |
| `sbx secret set` or the Claude login fails with a keyring or D-Bus error | sbx stores secrets in the Linux keyring when one is running and on disk otherwise. Note the exact error and run `sbx version`. This is the least-tested part of WSL support. |
| Everything is very slow | Is the project under `/mnt/c`? Move it to `~/code`. If the PC has little RAM, WSL gets half of it by default; you can set `memory=12GB` (for example) under `[wsl2]` in `.wslconfig`. |
| `factory setup` says Ubuntu is too old | The factory needs Ubuntu 24.04+. In PowerShell: `wsl --install -d Ubuntu-24.04`, and use that one. |
