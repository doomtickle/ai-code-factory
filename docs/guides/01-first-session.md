# Your first session

This guide takes you from a fresh checkout of the factory to a finished first
task. You'll check your machine, run the one-time setup, start the factory on a
project, sign in to Claude, give the Coordinator a small job, and watch the
roles do it. At the end you'll know how to leave the factory running in the
background, bring it back, and check what's running.

## Before you start

You need a Mac with Apple silicon (macOS 14 or later) or an x86_64 machine
with Ubuntu 24.04 or later, a Docker account, and a Claude Pro or Max
subscription. The full list is in [Requirements](../../README.md#requirements).

> **Note:** On Windows 11, follow [Setting up on Windows 11 (WSL2)](../setup-wsl.md)
> first. It walks through WSL, Docker and nested virtualization, then joins this
> guide at `factory setup`.

## 1. Check your machine

From the factory checkout, run the doctor. It changes nothing; it only checks
and tells you how to fix what's missing.

```bash
cd ~/code/ai-code-factory
./bin/factory doctor
```

Each check prints a ✓, a `!` (a warning) or a ✗ (a problem) with the fix
indented underneath. Before setup, it's normal to see warnings that `sbx` and
`herdr` aren't installed yet:

```text
==> Factory tools
  ! sbx 0.45.1 not installed yet ('factory setup' installs it)
  ! herdr 0.9.1 not installed yet ('factory setup' installs it)
```

Fix every ✗ and run the doctor again until it ends with `No problems found.`

## 2. Run setup once per machine

```bash
./bin/factory setup
```

Setup runs the doctor again, installs the pinned `sbx` and `herdr`, signs you in
to Docker (a browser window opens), builds the sandbox images, applies the
security baseline, and links `factory` into `~/.local/bin`. It is safe to run
again. When it finishes you'll see:

```text
Setup complete. Next: cd into a project and run factory start
```

> **Tip:** If setup prints `add this to your shell profile: export PATH="$HOME/.local/bin:$PATH"`,
> do that and open a new terminal, so `factory` works from any folder.

## 3. Start the factory on a project

The project must be a git repository, or a folder whose immediate subfolders
are git repositories. The developer works in git clones of it, so it only
ever sees what you have committed.

```bash
cd ~/code/my-api
factory start
```

A folder of repositories works the same way. Start the factory on the parent
folder, and each branch the developer makes comes back into the repository it
belongs to:

```bash
# ~/code/shop holds two repos: api/ and web/
cd ~/code/shop
factory start
```

`factory start` starts the broker and the dispatch bridge, creates the
sandboxes, and opens the dashboard in your browser (it prints the link too,
in case no window appears). It keeps running in that terminal: Ctrl-C closes
the dashboard, not the factory.

## 4. Sign in to Claude, then start again

The very first time, there is no Claude login stored yet, so `factory start`
creates only the Coordinator and tells you so:

```text
note: no Claude login is stored yet, so these role sandboxes were not created: developer researcher reviewer
  Type /login in the Coordinator pane, then run 'factory start' again (e.g. in the Host shell pane).
```

Do exactly that:

1. On the dashboard, press **POWER ON** under the Coordinator's screen (Terminal
   0, on the left), type `/login` there and sign in with your Claude
   subscription.
2. Press **RESTART** on the control unit (bottom right, under the Write gate
   display) and then **CONFIRM**, or run `factory start` again in a terminal.

```bash
factory start
```

The second start creates the role sandboxes, which pick up the stored login.
You only do this once per machine: `sbx` keeps the login for every sandbox it
creates afterwards.

> **In Herdr instead?** With `factory start --layout watch` (or `tabs`;
> remembered for next time), type `/login` in the **Coordinator (VM)** pane
> and run `factory start` again in the **Host shell** pane below it.
>
> **No UI at all?** `factory start --no-ui` starts everything but the
> dashboard or Herdr. Then open the Coordinator with `factory chat` in any
> terminal, type `/login` there the first time, and run `factory start
> --no-ui` again. Follow a role with `factory logs <role>`.

> **Why two starts?** `sbx` gives a sandbox the stored login only when it
> creates the sandbox, and role sandboxes don't take typed input. So the
> factory waits for your login before it creates the roles.

## 5. Find your way around the dashboard

The dashboard is one screen, styled as a 1984 control unit:

- **Terminal 0** (left, full height): the Coordinator. Press POWER ON, then
  talk to it here. Every browser tab you open shows the same session.
- **Lanes** (right): a module for every lane with a job, with its live
  transcript; idle lanes wait as chips in a strip under them. Press a lane's
  number (or click it) for the full transcript and its latest review packet.
- **Write gate** (bottom right): the pushes and pull requests waiting for you (with
  Passdesk Pro), and under it the **control unit**: APPROVE and DECLINE for
  the selected one, and RESTART and UPDATE for the factory, which show what
  they'd do and wait for CONFIRM before running anything.
- **Spend** and the **event printer** (under the lanes): today's cost, and
  broker calls and finished jobs as they happen.

[Use the dashboard](06-dashboard.md) goes through every part and key, and
which `factory` command each one stands in for.

Prefer the terminal? `factory start --layout watch` (remembered for next
time) opens Herdr instead, with the Coordinator (top-left), a host shell
(bottom-left) and a live `factory watch` view of every lane (right);
`--layout tabs` gives one Herdr tab per role instead of the Watch pane. See
"Layouts" and "Dashboard" in the [README](../../README.md#layouts).

## 6. Give the Coordinator a small first task

Start with something small and easy to check, so you can see the whole loop
without spending much. For example:

```prompt
Add unit tests for the slugify helper in src/utils/slug.ts: empty strings,
unicode input and repeated dashes. Don't change the helper itself. Get the
tests reviewed, then tell me which branch to merge.
```

The Coordinator can't edit files or run commands. It plans the work and
delegates it: here, to the developer, then the reviewer. It tells you which
model and effort it picked for each job, so you can overrule it.

## 7. Watch the roles work

Look at the developer's lane on the dashboard to follow the job live (or the
**Watch** pane in Herdr, or the **developer** tab with `--layout tabs`). Every transcript line
starts with the time. Between the start and the end you'll see the role's
messages, each tool call and a one-line summary of its result (left out here):

```text
14:02:11  ▶ job 20261017-140211-a3f9c2 from the Coordinator
14:02:11     > Add unit tests for slugify in src/utils/slug.ts on a new branch factory/slug-tests.
14:02:11     preparing lane developer (git clone/fetch from the project)
14:02:19     model: claude-sonnet-5
14:06:40  ⎇ branch factory-developer/factory/slug-tests at 3f9c2ab
14:06:40  ■ job 20261017-140211-a3f9c2 done after 269s
```

You can follow the same transcript from any terminal, too:

```bash
factory logs developer
```

When the reviewer approves, the Coordinator tells you the branch to merge.
The developer's branch is already in your repository as a remote-tracking
branch, so you can inspect it and merge it:

```bash
git log -p main..factory-developer/factory/slug-tests
git merge factory-developer/factory/slug-tests
```

[Build a feature](02-build-a-feature.md) covers this loop in depth.

## 8. Leave it running, stop it, bring it back

- **Leave it running in the background:** press Ctrl-C where `factory start`
  (or `factory dashboard`) is running, or close that terminal: the dashboard
  closes, the agents keep working. In Herdr, press **Ctrl+B**, then **Q**.
- **Reopen it:** `factory dashboard`, or run `factory start` again in the
  same project.
- **Restart it:** RESTART on the control unit, or `factory restart`.
- **Stop everything:** `factory stop` stops the agents, the broker and the
  dispatch bridge. Sandboxes and their state are kept.
- **After a reboot:** `factory start`.

```bash
factory stop
factory start
```

## 9. Check what's running

```bash
factory status
```

```text
Project   /Users/you/code/my-api
Local settings none (see config.example/)
  ✓ broker running (127.0.0.1:18765)
  ✓ dispatch bridge running (pid 48211)
  ✓ Claude login stored
Coordinator broker tools none
Roles developer (3 lanes), researcher (1 lane), reviewer (2 lanes)

Sandboxes
  factory-broker       running
  factory-coordinator  running
  factory-developer    running
  factory-researcher   running
  factory-reviewer     running
```

`factory roles` lists each role's project access, lanes and network tools.

## Next steps

- [Build a feature](02-build-a-feature.md): a feature end to end, from prompt to merge.
- [Use the dashboard](06-dashboard.md): every part of the dashboard and its keys.
- To work on another project, run `factory start` in its folder. It offers to
  switch for you, removing the agent sandboxes but keeping your login.
- [Commands](../../README.md#commands): every `factory` command.
