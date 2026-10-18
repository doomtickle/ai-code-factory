# Decision log

Dated entries, for the user to review. Newest first.

## 2026-10-18

- **Credentials come from ordered providers (`core/credentials.py`,
  doomtickle/passdesk-ops#36, Phase 3 plan W2.1).** `get(name)` asks
  `proxy`, `file` (the configured path or `/etc/factory-broker/secrets/<name>`,
  then `/run/secrets/<name>`) and `env-file` (`PASSDESK_SECRET_<NAME>_FILE`)
  in the order `PASSDESK_CREDENTIAL_PROVIDERS` gives, and plugins add
  providers with `register_provider` from `activate()`. Jira, Gemini and the
  npm registry read through it. Owner decision recorded with it: in
  standalone mode the gateway itself will hold the GitHub token (wired up
  by W2.2, not here), and users are advised to use fine-grained tokens
  limited to chosen repos. The gateway stays outside every agent sandbox, so
  "credentials never enter agent sandboxes" still holds, but redaction and
  keeping secrets off argv, disk and logs carry more weight once it holds
  GitHub's token too. Judgement calls:
  - **No provider reads a value from an environment variable**, only a path:
    values in the environment leak into child processes (git, pip) and
    `docker inspect`.
  - **`proxy` stops the lookup.** It returns a sentinel (`INJECTED`) for
    `github`, so with the default order a stray `secrets/github` file is
    never picked up; standalone mode (W2.2) drops `proxy` from the list.
  - **Surrounding whitespace is stripped with `.strip()`, as the old reader
    did** (the plan says no change in behaviour for the moved reads; an
    earlier draft stripped only trailing newlines, which turned `'key\t\n'`
    into a refused value and kept spaces around a key). The control-character
    check runs on the stripped value, and applies to plugin providers' values
    too.
  - **A value with a control character inside it is refused** (treated as
    absent, audited). Before, a Gemini key or bare npm token with a second
    line reached `http.client`, whose "Invalid header value" error quotes the
    whole header back to the agent.
  - **File safety:** symlinks are followed (as before; Kubernetes secret
    volumes are symlinks), but the file reached must be a regular file of at
    most 64 KiB of UTF-8. A non-regular path is refused before it is opened
    (so a device node is never opened) and again on the open descriptor; the
    open is non-blocking so a FIFO swapped in between cannot hang a call. Group- or world-readable files
    are accepted, since Docker secrets are 0444 by default; the doctor still
    warns about loose modes in the secrets directory. A file inside the agent
    workspace (directly or through a symlink) is refused, checked on the
    resolved path and then on the descriptor's `/proc/self/fd` path, which
    catches a swap between check and open (without `/proc` only the first
    check runs). The checks are on paths, so a bind mount of the workspace at
    another path, or a hard link to a file in it, is not detected; keeping
    the workspace out of the gateway's secret locations is the deployment's
    job. Missing or
    unreadable files are absent as before; refused ones are skipped and
    recorded as a `credential_unusable` audit event without the value.
  - **Names are `[a-z][a-z0-9]*(-[a-z0-9]+)*`, at most 64 characters**, so a
    name is always one file name and maps to exactly one env-file variable
    (no `_`, which the variable uses for `-`).
  - **Plugin providers cannot replace a built-in, and one not named in
    `PASSDESK_CREDENTIAL_PROVIDERS` is asked last**, so loading a plugin
    never changes what today's files return. An unknown name in the list
    stops the server at start-up.
  - **A provider that raises becomes a `CredentialError` naming only the
    provider and credential**, raised `from None`, since a plugin's own
    message might hold the value.
  - **The secrets directory is read only through `secrets_dir()`**, so making
    it configurable (a separate change) is a one-line edit there.

- **Identity/protocol layer de-factory-ized (doomtickle/passdesk-ops#36),
  per `docs/broker-extraction.md` (ai-code-factory) Phase 1 items 1, 2 and
  6.** `grant_hint`'s hard-coded `coordinator` special case is gone; a grant
  in the grants file may now be either the old bare list of tool names or an
  object `{"tools": [...], "source": "..."}`, and the deny message
  (`principal 'x' is not granted tool 'y' (see ...)`) quotes that `source`
  when one is set, else a generic "its BROKER_TOOLS grant". Judgement calls:
  - **The factory's current hint wording (`roles/<name>/role.conf`,
    `kits/coordinator/broker.conf`) is not reproduced automatically.** It
    could be restored exactly by having the factory's `start-broker.sh`
    write a `source` per grant, but that script is out of scope here (it
    lives in `ai-code-factory`, a separate lane owns it). Until it's
    updated, every factory deny message shows the generic hint instead of
    the old path - a wording change only; grants, auth and the audit record
    are unaffected. This is the "documented fallback" the task allowed in
    place of reproducing the old text.
  - **Grants file: `STATE/grants.json` if present, else `STATE/roles.json`**
    (`core/auth.py:grants_path`), same format either way. The factory's
    `scripts/start-broker.sh` keeps writing `roles.json` unchanged and the
    fallback picks it up; a future deployment (or a later factory change)
    can write `grants.json` instead, including the `source` form above,
    without a flag day.
  - **Audit field: `principal` is canonical, `role` stays as a duplicate
    alias with the same value.** `ai-code-factory`'s `scripts/analytics.py`
    reads `role`; dropping it would silently break that consumer's reports.
    Both keys are written on every record until that script reads
    `principal` instead.
  - **Origin allowlist defaults to empty, not wide-open.** `BROKER_ALLOWED_ORIGINS`
    (comma-separated) gates any request that carries an `Origin` header,
    answering 403 before the body is read; a request with no `Origin`
    (every CLI-driven MCP client seen so far) is unaffected. Defaulting to
    empty means an unconfigured deployment refuses every Origin-bearing
    request rather than accepting any - DNS-rebinding protection the MCP
    spec recommends for HTTP transports is opt-in-to-relax, not opt-in-to-add.
  - **Bind host stays `0.0.0.0` by default.** `BROKER_BIND_HOST` is now
    readable from the environment, but the default is unchanged so sbx's
    port-publishing keeps working without every deployment having to set it.

## 2026-10-17

- **Ref names are secret-scanned: `check_branch_name` (`tools/git.py`) now
  calls `block_outbound_secrets` on every branch name an agent supplies**,
  closing the follow-up left by the card-redaction entry below
  (doomtickle/passdesk-ops#2). A credential in a branch name is refused the
  same way one in a PR title or body is, instead of being held for approval
  with a `[REDACTED:...]` marker and then published to GitHub verbatim.
  Judgement calls:
  - **Scanned in `check_branch_name`, not only `check_push_branch`.** The
    issue asked for the push branch and the PR head; those both go through
    `check_push_branch`, which calls `check_branch_name`, so either place
    covers them. The general function also covers `git_fetch`'s branch and
    the `base` argument of `git_push` and `github_create_pr`, which are sent
    to GitHub as ref names in a fetch. Refusing a fetch of a branch that
    already exists under a credential-looking name costs nothing we want,
    and one choke point is easier to reason about than three.
  - **Refuse, don't redact or rename.** Redaction on the card never changed
    what was pushed, and silently renaming the branch would make the agent's
    local branch and the remote disagree. `Denied` tells the agent exactly
    which pattern matched so it can rename and retry.
  - Branch names allow `_`, so `factory/ghp_...` really was pushable; the
    tests cover each `SECRET_PATTERNS` entry whose alphabet fits a ref name
    (GitHub, AWS, Anthropic, Slack, Google), on the push branch, PR head and
    both `base` arguments, and check that the approval hook is never reached.
  - With this, every agent-supplied string on an approval card (branch, head,
    base, title, body; file names via the per-commit patch scan) is refused
    before `require` is called. The redaction in `require`, and the
    card-id collision it could cause, are now defense in depth rather than
    the first line; `docs/pro-approvals.md` says so.

- **Cloudflare Web Analytics beacon script on passdesk.dev carries no
  Subresource Integrity hash.** The marketing site (`passdesk/` repo, GitHub
  Pages) loads the beacon from `https://static.cloudflareinsights.com/beacon.min.js`
  (gated on public env var `VITE_CF_ANALYTICS_TOKEN`, validated as 32 hex chars,
  shipped in doomtickle/passdesk#5). Cloudflare updates `beacon.min.js` in
  place at an unversioned URL, so a pinned hash would silently break analytics
  on the next update — this is a deliberate exception to the workspace rule
  "every pin is by digest or checksum," applying only to the marketing site,
  never to the gateway or its image.
  - **Mitigation:** the script runs on a static page with no credentials, no
    login and no user data beyond the waitlist form; worst case of a
    compromised beacon is script injection on the public marketing page.
  - **Revisit if:** the site gains a login, payment flow, or anything
    secret-bearing, or if Cloudflare publishes versioned beacon URLs.
  - **Documented in:** `passdesk/README.md` "Analytics".

- **`core/approvals.py`'s `require(card)` now redacts known secret patterns
  in every string value of the card before any hook sees it**, instead of
  dropping or masking the field a credential might land in (e.g. the PR
  title). A card field is not sanitized the way `web_fetch` output is —
  nothing stops an agent from putting a leaked token into a PR title or
  branch name, and that card is then echoed verbatim to a Slack/Discord
  webhook, written to the pending-card file, and printed on an approver's
  console (`passdesk-pro`'s `notify.py` and `approvals.py`). Reusing
  `filters.redact` (already applied to audit values in `core/audit.py`) at
  this one choke point covers all three sinks today and any future approval
  plugin, without Pro needing its own copy of the patterns or core importing
  Pro. The redaction is recursive into lists/dicts/tuples (a card's `files`
  list, say) but leaves non-string values alone, and only the copy passed to
  the hook is touched. The write itself, which runs only after `require`
  returns (`passdesk/tools/git.py:187,209`), still uses the original
  arguments; only the copy shown to the hook is scrubbed, so nothing about
  what actually gets pushed or opened changes.
  Dropping the title outright (the alternative in the backlog item) was
  rejected: an approver needs to read the title to decide; redacting the one
  substring that looks like a credential keeps the rest legible. Redaction
  is a pure function of the card's content, so a retry of the same call
  produces the same redacted card and thus the same `card_id` in Pro — this
  was checked against `passdesk_pro/approvals.py`'s `card_id`, which hashes
  a fixed set of stable fields (including `bundle_sha256`, a hex digest that
  matches none of `SECRET_PATTERNS`) from whatever card it's given.
  - **A `[REDACTED:...]` marker means the real write still contains that
    string; an approver should decline, not just note it.** This matters
    most for `git_push`'s `branch` field: `github_create_pr`'s title and
    body are already refused outright by `block_outbound_secrets` before
    `require` is ever called (`tools/git.py:206-207`), but a push's branch
    name was not secret-scanned at the time (`check_push_branch`,
    `tools/git.py:42-51`) — a branch named `factory/ghp_...` would have been
    published to GitHub exactly as typed if approved. Blocking secrets in
    branch names the same way was left as a follow-up; it is done in the
    entry above (same day, doomtickle/passdesk-ops#2).
  - **Two writes whose cards differ only in the redacted substring get the
    same card id** (`passdesk_pro/approvals.py`'s `card_id` hashes whatever
    card it's given, which is the redacted one), so one decision could cover
    either. Accepted for this change: the content that actually gets pushed
    is still pinned by `head` and `bundle_sha256`, neither of which
    redaction touches (a hex digest matches no `SECRET_PATTERNS` entry), and
    closing this gap means blocking secrets in branch names outright (the
    follow-up above, since done), not redacting harder.

- **CI (`.github/workflows/test.yml`).** Builds the Dockerfile with empty
  `factory-config`/`plugins` build contexts and runs `python3 -m unittest -v`
  inside the built image, exactly as the README's "Running the tests" section
  does: `--network none` and a read-only bind mount, so the suite runs with
  no network and cannot write into the checkout. Judgement calls:
  - **Only `actions/checkout` is used**, pinned to the one SHA already
    verified in `passdesk/.github/workflows/deploy.yml`. Every other step
    (building, running the tests, the eventual Docker Hub push) uses the
    runner's own `docker` CLI via `run:` rather than a third-party action
    whose commit we have not independently verified.
  - **Triggers are `push` on `main` and `factory/**` plus `v*` tags, and
    `pull_request` and `workflow_dispatch`.** Push branch coverage follows
    this repo's own branch naming convention; `pull_request` already covers
    any other branch name or a fork, so widening `push` to every branch
    would just duplicate runs. The `v*` tag filter is listed separately
    from `branches`, since a `push.branches` filter does not match tag
    pushes — without it, the (currently disabled) `publish` job could never
    trigger even once it's turned on.
  - **The tag-triggered `publish` job is disabled** (`if: false && ...`)
    until the owner adds `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` as repository
    secrets. It logs in with `docker login --password-stdin`, piping the
    token from the env, never argv, and reuses the same empty build
    contexts as the test job.

## 2026-10-16

- **The approval point and plugins (`core/approvals.py`, `core/plugins.py`;
  design in `docs/pro-approvals.md`).** Core gains a hook that external
  writes pass through, and a way to load named plugin modules at start-up;
  the hold itself ships in the commercial `passdesk-pro` package. Judgement
  calls:
  - **The card is built after every check, and `git_push` waits outside
    the tool lock.** A pending approval must not block other roles' git
    calls; after the hold the push re-takes the lock and refuses if the
    mirror ref it validated has moved. The card holds only what the broker
    verified (counts, names, a diff stat, the bundle's digest), never
    content from the network and never a credential.
  - **Plugins activate through `activate()`, not on import**, so importing
    one (the doctor does) has no side effects, and a plugin whose license is
    invalid installs nothing and says why; the gateway then runs as the
    free core. A plugin that fails to *import* stops the server: a
    deployment that named it should not run without it unnoticed.
  - **The image takes a `plugins` build context on `PYTHONPATH`** and a
    `PASSDESK_PLUGINS` name list; both empty by default, so the image and
    the server are unchanged for everyone who does not opt in.
  - **A held call is a denial, not an open call.** MCP clients cut long
    tool calls (60 seconds in some), so the hook waits `hold_seconds`
    (45) and then denies with the approval id and "retry this exact call";
    a retry with the same bundle has the same id and picks up the decision.
    The agent-facing text says "Nothing was sent" because nothing was.
  - **Decisions are files in the state directory**, written by the
    deployment's console on the host. No new listener or token: whoever
    can write there is the approver. The retry design also means a crash
    or restart of the gateway loses nothing a person decided.
  - **The license is read at every held call, not at start-up.** The
    factory delivers credentials after it starts the server, so a key that
    arrives later must take effect without a restart. While no valid key
    has ever been seen the gateway behaves as the free core (logged once);
    once a key has been valid and then lapses, held writes are refused
    rather than silently released: an operator who paid for the gate would
    rather have it stay closed. The audit log and the doctor both report
    which state applies.
  - **License tokens are RSA-2048 PKCS#1 v1.5 over SHA-256**, verified
    with one modular exponentiation in pure standard library (the core's
    rule), signed offline with `openssl`. Ed25519 would be smaller but has
    no standard-library verifier.

- **`passdesk doctor` (`passdesk/doctor.py`, `python3 -m passdesk.doctor`)
  and `docs/quickstart.md`.** The doctor checks a deployment's gateway
  configuration and prints a fix per problem; `ai-code-factory`'s
  `factory doctor` runs it inside the broker sandbox and compares the
  policy's reachable hosts with the broker kit on the host. Judgement calls:
  - **It reads the credential files** (to check they are non-empty, mode
    0600 and, for Jira, `email:token`) but never prints a value. That is
    the same read the tools make, inside the same sandbox; on a host the
    secrets directory does not exist and the doctor says so instead of
    warning about every missing file.
  - **The sbx kit reader is deliberately not a YAML parser**: it takes the
    plain `- host` items under `allow:` and nothing else. A kit that needs
    more than that is a reason to run a real parser, not to grow this one.
  - **Policy wildcards (`*.example.com`) are a warning against a kit**,
    since kits list exact hosts; the doctor cannot know which subdomains
    are meant.
  - **`docs.github.com` is removed from `[web].allow_hosts` and the broker
    kit.** The doctor's first live run found it: the hardening commit's
    credentialed-host denylist refuses every `*.github.com` host in
    `web_fetch`, so the entry had been dead since then while the README
    still listed it as a documentation host. Removing it is the
    conservative fix; letting `web_fetch` reach a `github.com` subdomain
    again would need evidence that the egress proxy attaches no credential
    there, which nothing here establishes.

- **`ai-code-factory`: the broker image is now built from a `git archive`
  export of the pinned commit, and nothing trusts `broker/.git`.** A review
  of the previous fix (below) found that its checks ran `git -C broker`,
  which follows `broker/.git` - a file an agent can rewrite. Pointed at a
  fake repository whose HEAD is the pin, it made the guard pass while edited
  files sat in the build context; a fake repository whose config sets
  `core.fsmonitor` ran that command on the host during `git status`. The
  dirty check also missed git-ignored files, so a planted
  `passdesk/__pycache__/*.pyc` reached the image and was loaded in place of
  the reviewed `.py`. Judgement calls:
  - **Build context.** `scripts/00-build-templates.sh` exports the pin
    (`git rev-parse HEAD:broker` in the superproject) with `git archive`
    from the module's git directory into a temp dir, and uses that as the
    Docker context and Dockerfile source. The working tree is never read,
    so edited, untracked and ignored files can't reach the image. The build
    still refuses while `broker/` has uncommitted or untracked work or is
    off the pin; it is not what gets copied, but building over unreviewed
    work there is almost certainly a mistake.
  - **Git directory.** Resolved from the superproject
    (`git rev-parse --git-path modules/broker`), never from `broker/.git`.
    Every git command against the submodule passes an explicit
    `--git-dir`/`--work-tree` plus `-c core.fsmonitor=false
    -c core.hooksPath=/dev/null`. `broker/.git` must be a regular file
    holding exactly `gitdir: ../.git/modules/broker` (as git writes it), or
    the build and `factory update` refuse. A linked-worktree layout, where
    that path would be absolute, is refused rather than supported.
  - **Ignored files.** The dirty check now includes `--ignored`, but
    ignored files (e.g. a `__pycache__/` from running the tests) don't block
    the build or the pull: the export never includes them, and a pull or
    checkout leaves them alone.
  - **`factory update`.** The pull is now `git pull --no-recurse-submodules`.
    An initialized `broker/` is then fetched and checked out at the new pin
    through the same explicit `--git-dir`, not with `git submodule update`,
    which follows `broker/.git` and so could be redirected between the check
    and the update. Only a never-initialized submodule (a plain clone) goes
    through `git submodule update --init`. The superproject's `git status`,
    `pull` and `submodule update` also get `core.fsmonitor=false` and
    `core.hooksPath=/dev/null`, and git passes both on to the git processes
    it runs for submodules.
  - **Uninitialized submodule.** The build refuses it outright, with a clear
    message (it used to look "clean" and fail later by accident).
    `factory update` still initializes it, since there is nothing in it to
    lose.
  - **`ROOT`.** `bin/factory` sets `ROOT` unconditionally again; an
    exported `ROOT` used to redirect a real run, including its final
    `exec "$ROOT/bin/factory"`. Tests set `ROOT` after sourcing it.
  - **Open risk (not fixed here): the superproject's own `.git` is inside
    the agent-writable working tree** when the factory works on its own
    repo. Its `config`, `hooks/`, `info/attributes` and `modules/broker/`
    (objects, refs, config) are all trusted by these checks. An agent that
    can write there could change the commit `HEAD:broker` resolves to, swap
    objects, or add config that runs commands (filter drivers,
    `core.sshCommand`, a remote's `uploadpack`, credential helpers) the next
    time a person or the factory runs git there. The fsmonitor and hooks
    overrides cover only the two most direct of these. Closing it needs the
    agent workspace not to expose `.git` writably (or the build to work
    from a separate host-only clone), which is a wider change.

- **Security review follow-up fixes.** Judgement calls:
  - **`check_commit_patches` now splits patches on `"\n"` only, not
    `str.splitlines()`.** `splitlines()` also breaks on `\r`, `\x0c`, `\x1c`,
    U+0085 and U+2028; an added line containing one of those right before a
    secret (e.g. `+ok\r-ghp_<token>`) used to be cut into a second piece
    starting with `-`, which the scan then skipped as a removed line,
    letting the secret through to `git push`. Reproduced with real git.
    Checked every other place secret scanning splits text (`filters.py`,
    the commit-message scan, `npm.py`'s lockfile parser): only
    `check_commit_patches` had this bug: `filters.find_secrets` regex-scans
    whole strings, never splits, and `npm.py`'s `splitlines()` use is
    structural yarn.lock parsing, not secret scanning.
  - **`pip.py`'s requirements regex now requires the marker before the
    hashes** (`name==version ; marker --hash=...`), not after. The old
    order (`name==version --hash=... ; marker`) is not what either
    `pip-compile --generate-hashes` or `uv pip compile --generate-hashes`
    emit, so real lockfiles with an environment marker were refused outright
    outside of a hand-crafted test. The marker charset is unchanged (still
    no `-`, `$` or `#`, so nothing in a marker can read as a pip option);
    `\`-continued hash lines after the marker still work, since those are
    already folded onto one logical line before the regex ever runs. The
    old marker-after-hashes order is no longer accepted; the one test that
    exercised it was testing a format no real tool produces, so it was
    rewritten to the realistic order instead of extended.
  - **`git.py`'s `git()`/`git_capped()` helpers now wrap their failing
    command's stderr with `untrusted()`, not just `sanitize()`.** This
    revises this same log's earlier "Finding 9" entry below, which kept
    these two helpers sanitize-only on the reasoning that they're "the
    broker's own git client talking about local state, not content from the
    internet." That reasoning doesn't hold for every caller: both helpers
    also run `git fetch` against the GitHub mirror (`ensure_mirror`,
    `tool_git_fetch`, `tool_git_push`), so their stderr can carry text GitHub
    (or a compromised/typosquatted mirror) chose to send back. Rather than
    split `git()`/`git_capped()` into a remote-facing and a local-only
    variant, every failure is wrapped uniformly; a purely local failure
    (e.g. `git init --bare`) gets marked as untrusted content it technically
    isn't, which is the safe direction to be imprecise in. `pip.py`'s pip
    failure output is wrapped the same way, since pip's own error text can
    echo back content PyPI served for the requested package name/version.
  - **`web.py`'s credentialed-host denylist now lists `github.com` plus
    `*.github.com`**, replacing the old pair of exact hosts `github.com` and
    `api.github.com`. The old list missed `uploads.github.com`,
    `codeload.github.com` and `gist.github.com` - all github.com
    subdomains the egress proxy credentials the same way - which an
    operator could have added to `policy.toml`'s `[web].allow_hosts` and
    had `web_fetch` reach with credentials attached, skipping `git_fetch`/
    `git_push`/`github_create_pr`'s own allowlists. `*.githubusercontent.com`
    is unchanged.
  - **`handle_rpc` now rejects a non-object `params`** with a JSON-RPC
    `-32602` error instead of calling `.get()` on it and raising
    `AttributeError` out of `do_POST`, unaudited and uncaught. Checked before
    the `tools/call`/`tools/list`/`initialize` dispatch, so every method
    benefits, not just `tools/call`.
  - **`git.py:173`'s f-string with no placeholder** (`f"Push rejected by
    GitHub:\n"`) is now a plain string; ruff's F541.
  - **Pin hygiene: `security-hardening` is merged into `main` with
    `--no-ff` (no squash), locally, not pushed.** `ai-code-factory`'s
    `broker` submodule pin must point at a commit reachable from a real
    branch, not an unmerged feature branch that could later be rebased away
    or deleted out from under the pin. A squash merge was ruled out because
    `--no-ff` keeps every individual hardening commit (and this log's
    per-commit context) in `main`'s history instead of collapsing it into
    one commit; `--no-ff` specifically (not a fast-forward) keeps the merge
    commit itself as the one `ai-code-factory` pins to, independent of
    whatever `main` does next.

- **`ai-code-factory` side of the same security review.** Judgement calls
  (recorded here per the review's instructions, even though the code lives
  in `ai-code-factory`, not this repo):
  - **`factory update`'s dirty check uses `git status --porcelain
    --ignore-submodules=all`, not `--ignore-submodules=dirty`.** The review
    suggested `=dirty` as an example; tested both against a real repo with a
    submodule pin bump and no local submodule checkout update (the exact
    state right after a plain `git pull --ff-only` with no submodule
    handling) and `=dirty` still reports the submodule as modified (`
    M broker`) - `--ignore-submodules=dirty` only hides a submodule's own
    uncommitted work-tree changes, not a commit mismatch between the
    recorded pin and what's checked out. `=all` hides that mismatch while
    still showing a real uncommitted change elsewhere in the superproject
    (verified the same way). `factory update` immediately runs
    `git submodule update --init` after the pull in the same step anyway,
    so by the time another `factory update` runs, `broker/` always matches
    its pin and `=all` vs `=dirty` makes no difference there - "all" only
    matters for the one case this bug was about: a pin bump that reached the
    superproject but, before this fix, never reached `broker/`.
  - **`scripts/lib/roles.sh` and `scripts/00-build-templates.sh` now try
    `git submodule update --init -- broker` once, silently, before dying**
    on a missing `broker/tools.txt` or `broker/Dockerfile`. `roles.sh` is
    sourced by every `bin/factory` command (not just `setup`), so this also
    covers a plain `git clone` (no `--recurse-submodules`) being fixed
    automatically the first time any `factory` command runs, not just
    `factory update`'s own pull. The check stays a fast local file-existence
    test before attempting the fix, so once the submodule is initialized
    this costs nothing on every subsequent command.
  - **No shell test harness exists for `bin/factory`** (it is driven
    entirely by `sbx`/`herdr`/`docker`, none of which this review could
    exercise), so the regression test for both of the above
    (`scripts/test_factory_update.sh`) runs the real git plumbing - the
    exact commands `pull_factory` and `roles.sh` now use - against
    disposable local fixture repos, rather than invoking `bin/factory`
    itself or adding a new test framework.

- **`ai-code-factory`: `factory update`'s dirty check ignoring `broker/`
  entirely (`--ignore-submodules=all`) also hid a real edit inside `broker/`
  itself, not just the pin-only mismatch it was meant to hide.** An agent
  working on `ai-code-factory`'s own repo can write into `broker/` like any
  other file; an uncommitted change or untracked file there survived
  `git pull --recurse-submodules` plus `git submodule update`, and
  `scripts/00-build-templates.sh` (context `$ROOT/broker`) would then bake
  unreviewed broker code into the image, while a local commit in `broker/`
  that outran the pin got silently detached. `pull_factory` (`bin/factory`)
  now keeps `--ignore-submodules=all` for the superproject check, but adds a
  separate guard that refuses the pull when `broker/` has uncommitted or
  untracked changes, or when its checked-out commit is not the pin and not
  an ancestor of it (being behind the pin is still fine - that's what
  `git submodule update --init` fixes). `scripts/00-build-templates.sh`
  refuses the build outright on the same dirty check, plus an exact-pin
  check with no "behind is fine" leniency, via a shared
  `require_broker_at_pin` helper in `scripts/lib/common.sh` so `factory
  setup` cannot bake in working-tree edits either. `bin/factory` itself was
  refactored slightly (resolving its own path from `BASH_SOURCE`, not `$0`,
  and guarding its command dispatch so it only runs when executed directly)
  so `scripts/test_factory_update.sh` can source it and call the real
  `pull_factory` against disposable fixture repos instead of replaying its
  git commands by hand, as it did before this fix.

- **Website copy (accuracy pass on the passdesk marketing site).** Judgement
  calls:
  - **FAQ "Can an agent go around Passdesk?" answer was replaced outright**
    with the brief's exact sentence ("All of an agent's tool traffic goes
    through Passdesk; apart from its model provider, it has no other way
    out."), dropping the old answer's second clause ("Passdesk itself can
    only reach an approved list of hosts"). That clause is still true (the
    broker's own egress is gated by the sandbox's kit `spec.yaml`, separate
    from any tool's own host allowlist), but the brief specified this exact
    replacement text, so it was not preserved; it could be re-added as a
    second sentence if the site wants to keep making that claim.
  - **`Problem.tsx`'s "Only to Passdesk" cell** became "Only to Passdesk;
    apart from its model provider, it has no other way out" — the same
    idea as the FAQ fix, shortened to fit a table cell rather than pasted
    verbatim.
  - **`UnderTheHood.tsx`'s example was split into two files**, not just
    relabelled: an org-wide `policy.toml` block (`repos`, `branches`,
    `push_prefix`, `max_diff_mb`, `npm.allow`/`deny`, `web_fetch.hosts`,
    `max_response_mb`) and a separate `grants/release-bot` block that is
    just an agent name plus a flat list of allowed tool names. A single
    relabelled file would still have implied repo lists, branch rules and
    size limits vary per agent, which they don't — only the tool grant
    does.
  - **Dropped the sample audit record's redaction demo** (`jira.comment`
    with a `[REDACTED]` body) instead of swapping in a different line that
    still shows redaction. No real Passdesk tool takes a free-text body
    argument that would plausibly carry a secret, so there was no accurate
    stand-in; removed the now-dead `redacted` field and the `.tok-redacted`
    CSS rule that only that demo used.
  - **Availability "Works today" guardrail note** ("Pull requests open as
    drafts; force-pushes and branch deletion aren't possible; Jira is
    read-only.") was added as a prose note under the list, not a fourth
    bullet, since it qualifies three different existing entries rather
    than naming one more tool.
  - **New capability/roadmap IDs S9, S10 and R6** were added to
    COPY-NOTES.md (built-in guardrails, today's deployment model, and
    per-agent rules respectively) to back copy the brief asked for that
    didn't map onto S1-S8/R1-R5.
  - **FAQ "What do I need to run it?" answer, "One Passdesk serves one
    project, on your machine,"** is taken as given from the review brief.
    `passdesk/core/config.py`'s single `BROKER_WORKSPACE` path is
    consistent with one broker process serving one project, but this repo's
    own docs don't state the one-project constraint explicitly, so it
    wasn't independently re-derived — flagging it in case it needs a
    firmer source before shipping.

- **Security hardening (review findings 5, 7-9).** Judgement calls:
  - **`web_fetch`'s hard-coded credentialed-host denylist**: `github.com`,
    `api.github.com`, `*.githubusercontent.com` (git tooling); `api.atlassian.com`,
    `*.atlassian.net` (Jira); `generativelanguage.googleapis.com` (Gemini);
    and whatever host `[packages].npm_private_registry` resolves to, read
    fresh from policy on every call since that setting is a deployment
    overlay. This list is hosts the egress proxy or another broker tool
    attaches a credential to - not every host those tools happen to talk
    to - because the risk is specifically an unauthenticated-looking
    `web_fetch` call turning into an authenticated read that bypasses that
    tool's own allowlist (e.g. `[git].repos`). It is checked before
    `[web].allow_hosts` and cannot be overridden by editing that list; the
    denial message names the dedicated tool to use instead.
  - **"Tools that write externally" (finding 7) is the hard-coded set
    `{git_push, github_create_pr}`** in `passdesk/core/protocol.py`, not a
    new field on every registered tool. Only these two have an effect
    outside the broker's own state; a future such tool must be added to
    that set by hand. The pre-run "intent" audit record is best-effort
    logging of what is about to happen, not a tamper-evident log (that
    remains out of scope / roadmap) - it just means the attempt is on
    record a step earlier than before, and the call is refused outright if
    even that first write fails.
  - **Content-Length handling (finding 8) treats missing, non-numeric and
    negative values alike as 400,** before the existing 1 MB
    (`MAX_REQUEST_BYTES`) cap is applied. None of the three is a "request
    too large"; conflating them with it previously let a header-less or
    malformed request silently read 0 bytes instead of being rejected.
  - **Finding 9's wrapping is limited to bodies that genuinely come from a
    remote service or `git push`'s own stderr/stdout** (GitHub's PR-create
    error, Jira's and Gemini's HTTP error bodies, and the push rejection
    text GitHub forwards through `git push`): local git/bundle errors
    (`git.py`'s `git()`/`git_capped()` helpers, bundle `verify` failures)
    stay sanitize-only, since that text is the broker's own git client
    talking about local state, not content from the internet.

- **Security hardening (review findings 1-4 and 6).** Judgement calls:
  - **`pip_download` now requires `--hash=sha256:` on every line.** This is
    a breaking change for callers whose requirements files are pinned but
    unhashed; they get a DENIED naming `--generate-hashes`. Without hashes
    pip would accept whatever PyPI serves for the pin. Only sha256 is
    accepted (what pip-compile and uv emit). Environment markers are kept
    but restricted to a charset with no `-`, so nothing in one can read as a
    pip option; extras are dropped from the file pip sees, since
    `--no-deps` makes them meaningless.
  - **`git_push` bundle cap: `[filters].max_bundle_bytes` = 100 MB.** Large
    enough for a whole-branch bundle of a mid-sized repo (the tool
    description still allows "the whole branch"), small enough that a
    copy into broker-private space is cheap. Deployments with big repos can
    raise it in their overlay.
  - **`max_diff_bytes` now counts every pushed commit's patch**, binary
    files included, not the net diff. A branch that rewrites the same file
    many times can hit it sooner than before; the per-commit scan is what
    closes the add-then-delete hole, and the cap keeps the broker from
    reading an unbounded `git log -p` into memory (the output is streamed
    and the process killed at the cap). Commit messages are capped at the
    same size.
  - **Merges are scanned with `--cc`**, which shows only what a merge
    changes beyond its parents; content from a parent is scanned in that
    parent's own commit or is already upstream.
  - **Symlinks are refused everywhere under the workspace, even ones that
    point inside it.** Simpler to reason about than resolving and
    re-checking, and race-free: directories are opened component by
    component with `O_NOFOLLOW` from the workspace root. A caller with a
    symlinked `requirements.txt` or lockfile directory must use the real
    path.
  - **Broker-private temp space is `$BROKER_STATE/tmp/`** (mode 0700, one
    directory per call, removed afterwards), not the system temp dir, so
    large wheels and bundles land on the state volume.
  - **Not fixed (out of scope):** secrets in UTF-16 text or inside
    compressed files (zip, gzip) are not visible to the pattern scan.

- **Extraction.** Pulled out of `ai-code-factory` with history preserved, via
  a subtree split of its former `broker/` directory rather than a plain copy.
- **Consumption.** `ai-code-factory` consumes this repository as a git
  submodule at `broker/`, pinned by commit — not by an image digest, because
  there is no registry yet to push a built image to. See that repo's
  `docs/broker-extraction.md` (Phase 2) for the plan to move to a
  digest-pinned image once one exists.
- **Submodule URL.** `ai-code-factory`'s `.gitmodules` points at the relative
  URL `../passdesk-broker.git`, so it resolves next to whatever host
  `ai-code-factory`'s own `origin` is on (GitHub, in this case). **Not done
  yet:** the user needs to create the `passdesk-broker` repository on GitHub
  and push both repositories there before the submodule link is live for
  anyone other than this sandbox.
- **Naming.** The project and Python package are renamed from `agentbroker`
  to **Passdesk** (package `passdesk/`), and the MCP server now reports
  itself as `"passdesk"` (`serverInfo.name`, see `passdesk/core/config.py`).
  Deployment identity is kept as-is on the consumer side: `ai-code-factory`
  still calls the sandbox `factory-broker` and uses the paths
  `/opt/factory-broker` and `/etc/factory-broker` — those are its deployment
  choices, not this project's identity.
- **License.** Apache-2.0, copyright holder "Daron Adkins" (**the user
  should confirm this is correct**).
