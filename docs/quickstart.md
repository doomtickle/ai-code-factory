# Passdesk quickstart: Docker Sandboxes (sbx) + Claude Code

> **This is one deployment, not the only one.** This guide covers Passdesk
> deployed with Docker Sandboxes (sbx) and Claude Code agents, via the AI
> Code Factory. Passdesk itself is deployment-neutral (see the main
> [README](../README.md)); [standalone.md](standalone.md) is the plain
> `docker compose` quickstart.

By the end of this page you will have Claude Code agents, each in its own
Docker Sandboxes microVM, that can fetch and push code, open draft pull
requests and pull in packages, without ever holding your GitHub token. You
will have watched Passdesk approve one push, refuse another with a reason,
and written both to the record.

Plan on 30 minutes the first time, most of it image builds. If Docker and
Docker Sandboxes are already set up, it is closer to 15.

> **How Passdesk ships today.** Passdesk is the gateway; it runs as one
> sandbox next to the agents. The agents, their sandboxes and the one
> command that starts everything come from
> [AI Code Factory](https://github.com/doomtickle/ai-code-factory), which
> includes Passdesk as its `broker/`. Without sbx, use the standalone
> `docker compose` setup in [standalone.md](standalone.md) instead.

## What you need

- **A machine Docker Sandboxes supports:** an Apple silicon Mac on macOS 14
  or later, or Ubuntu 24.04 or later on x86_64 with KVM. That includes
  **Windows 11 with WSL2** once nested virtualization is on
  ([step-by-step](https://github.com/doomtickle/ai-code-factory/blob/main/docs/setup-wsl.md)).
- **Docker**, to build the sandbox images, and a free Docker account for the
  one-time `sbx login`.
- **A Claude Pro or Max subscription.** The agents sign in with it.
- **A GitHub repository you can create tokens for.** Any repo you own works;
  a throwaway one is fine for this walk-through.

## 1. Install the factory

The factory's own guides cover each platform in detail
([macOS](https://github.com/doomtickle/ai-code-factory/blob/main/docs/mac-quickstart.html),
[Windows 11 / WSL2](https://github.com/doomtickle/ai-code-factory/blob/main/docs/setup-wsl.md)).
The short version:

```sh
mkdir -p ~/code && cd ~/code
git clone --recurse-submodules git@github.com:doomtickle/ai-code-factory.git
cd ai-code-factory
./bin/factory doctor
```

`factory doctor` changes nothing. It lists each problem with the command
that fixes it. Repeat it until it reports no problems, then run setup once:

```sh
./bin/factory setup
```

Setup installs `sbx` and `herdr` at pinned versions with checked SHA-256
sums, builds the images, applies the default-deny network baseline and
links `factory` into `~/.local/bin`. A browser window opens once for the
Docker sign-in.

## 2. Tell Passdesk which repositories agents may touch

Passdesk's rules are one policy file. The repo's copy is generic; your own
settings go in a file outside every agent workspace, and the two are baked
together into the gateway image so no agent can change them at runtime.

```sh
mkdir -p ~/.config/ai-code-factory
cat > ~/.config/ai-code-factory/policy.toml <<'EOF'
[git]
# Repositories agents may fetch from, push to and open pull requests on.
repos = ["YOUR-GITHUB-USER/YOUR-REPO"]
EOF
factory setup
```

Everything else keeps its default: agents may push only branches starting
with `factory/`, never to `main`, `master` or `release/*`, pull requests
open as drafts, every pushed commit (and every branch name, PR title and PR
body) is scanned for secrets, and `web_fetch` reaches only a short list of
documentation hosts.

## 3. Give the key to Passdesk, not to the agents

Create a **fine-grained** personal access token at
github.com → Settings → Developer settings → Fine-grained tokens:

- Repository access: only the repo from step 2.
- Permissions: **Contents: read and write**, **Pull requests: read and write**.
  Nothing else.

Store it for the gateway's sandbox only. The agents' sandboxes never receive
it; the sandbox egress proxy attaches it to Passdesk's requests to GitHub,
so not even Passdesk's own process reads the value.

```sh
sbx secret set github --sandbox factory-broker
```

## 4. Start the factory on a project

Run it from the project you cloned in step 2 (or any folder, if your repo is
a throwaway):

```sh
cd ~/code/YOUR-REPO
factory start
```

Herdr opens with a **coordinator** tab and one tab per role. The first time,
type `/login` in the Coordinator pane and sign in with your Claude
subscription, then run `factory start` again in the host shell below it.
The sandbox runtime hands a sandbox the login only when it creates it, so
the second start is what creates the role sandboxes.

## 5. Check that the agents cannot reach GitHub

Before giving anyone work, look inside the developer's sandbox:

```sh
sbx exec factory-developer sh -c 'curl -m 8 -sS -o /dev/null -w "GitHub: HTTP %{http_code}\n" https://api.github.com'
```

Expect `GitHub: HTTP 403`. The request never leaves the machine: the
sandbox's egress proxy refuses it, because the agents' network policy
allows only their model provider. Their only other way out is Passdesk's
tools.

You will also find a `GH_TOKEN` variable inside every sandbox. It is a
stand-in the sandbox runtime provides; the host proxy swaps in the real
value only on requests to GitHub from a sandbox the token was stored for.
Since step 3 stored it for `factory-broker` alone, check that nothing wider
exists:

```sh
sbx secret ls
```

The `github` entry must be scoped to `factory-broker`, with no global one.

## 6. Give the Coordinator a job and watch the record

In the Coordinator pane:

> Create a branch `factory/hello` that adds a file HELLO.md containing one
> line, push it, and open a draft pull request titled "Hello from Passdesk".

The Coordinator delegates to the developer. In another terminal, follow the
record as it happens:

```sh
factory logs broker
```

You will see one line per call: the time, the role, the tool, the arguments
with secrets redacted, and the decision. For the push you see an `intent`
record first, written before Passdesk touches the network, then the `allow`
with the push result. The pull request appears on GitHub as a draft, opened
by your token, from a branch the agent could not have pushed anywhere else.

## 7. Ask for something the rules refuse

Back in the Coordinator pane:

> Push the same branch directly to main.

The developer tries, Passdesk declines, and the agent gets the reason:

```
DENIED by broker policy: branch main is protected (main)
```

The record has the denial too, with the same reason. The agent did not need
to know the rule in advance; it heard "no" and why, and can take the
allowed route.

Optional, to see the secret scan: ask for a commit that adds a line reading
`token = ghp_` followed by 36 letters. The push is refused with
`commit <sha> appears to contain secrets (github token)`, every commit in
the push is scanned on its own, and nothing leaves the machine.

## 8. Check the whole deployment

`factory doctor` now ends with the gateway's own checks: the policy's shape,
every host the policy can reach against the sandbox's network allowlist,
the tokens and grants, the audit log, the credential files, and a live
probe of the gateway and of GitHub access for each repo in `[git].repos`.
You can also run the gateway doctor directly, inside its sandbox:

```sh
sbx exec factory-broker sh -c 'cd /opt/factory-broker && python3 -m passdesk.doctor --live'
```

It changes nothing. Each problem comes with its fix.

## What to do next

- **Tell the roles your test command.** A note in
  `~/.config/ai-code-factory/projects/<project-folder>.md` is added to every
  role's instructions, and lives where the roles cannot edit it.
- **Jira, Gemini, private npm registry, and Passdesk Pro** are optional and
  documented in the factory README under "Enabling ...". Each credential lives
  in your OS keychain (macOS Keychain / Linux `secret-tool`) and is delivered
  only into Passdesk's sandbox. On machines without a keychain (WSL2, headless
  Linux), use `factory secret set <name>` to read the value from stdin and
  deliver it the same way (names: `jira`, `gemini`, `npm-registry`,
  `passdesk-license`, `approvals-webhook`). Remove with `factory secret rm <name>`.
  Note: these values are lost if the broker sandbox is recreated
  (`factory reset` or `factory update` if the broker image changes); the run that creates
  the new broker sandbox (`factory start`, or `factory update` itself) lists which to set again.
- **Updating** is one command: `factory update` pulls, rebuilds the images
  and recreates only the sandboxes whose inputs changed. If it recreates the
  gateway, re-run `sbx secret set github --sandbox factory-broker`; it
  warns you first.
- **Switching projects:** `factory start` in another folder offers to switch.

## If something goes wrong

| Symptom | Likely cause and fix |
|---|---|
| `factory doctor` reports a platform problem | Follow the fix it prints; nothing in this guide works until it reports no problems. |
| `git_push` returns `DENIED ... repo ... is not in the broker allowlist` | Step 2: the repo is not in `[git].repos`, or you did not run `factory setup` after editing the policy (it is baked into the image). |
| `git_push` fails with an authentication error from GitHub | Step 3: the token is missing, expired, or not stored with `--sandbox factory-broker`. Run the doctor with `--live`; it names the repo and the permission that is missing. |
| The developer says it cannot reach the gateway | `factory status` should show the broker running; `factory start` restarts it. The doctor's Live section says whether it answers. |
| The gateway doctor says `No module named passdesk` | The gateway image predates the doctor. Run `factory update`. |

## Tested on

- Ubuntu 24.04 on WSL2: steps 5 and 8 were run against a live factory
  (the 403 from the agent sandbox, the gateway doctor's live probe); the
  full cold install from step 1 is scheduled next.
- macOS on Apple silicon: not yet run end to end on a real Mac. `factory
  doctor` names anything missing, and your first `factory setup` is the
  live test.
