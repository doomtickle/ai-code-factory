# Decision log

Dated entries, for the user to review. Newest first.

## 2026-10-30

- **GitLab group entries in `[git].repos`, and group access tokens** (owner decision 30 Oct 2026).
  The owner works across many GitLab projects in one group; every switch meant editing
  `[git].repos`, saving a new project access token (`factory secret save gitlab`) and running
  `factory update`. Now one policy edit and one token cover the group. Choices:
  - **Syntax: `"<gitlab host>/<group path>/*"`**, e.g. `"gitlab.example.com/my-group/*"` or
    `"gitlab.example.com/my-group/sub/*"`, with `[gitlab] url = "https://gitlab.example.com/"`
    (trailing `/` required; the entry's host must be that instance). It matches any project under the group at any depth
    (subgroups included), case-insensitively like exact entries. One matcher, `repo_allowed` in
    `tools/git.py`, serves `check_repo` (so `git_fetch`, `git_push`, `gitlab_create_mr` and every
    completer of a held write) and the doctor; no consumer does its own exact membership.
  - **Still baked into the image.** The allowlist is policy, so it is read from the image's
    `policy.toml` and never from a runtime-editable file or a tool argument; a change is a policy
    edit and `factory update`, as before. Nothing about `push_branch_prefix`, `protected_branches`,
    approvals holds or the token's file delivery and `[gitlab].url` scoping changed.
  - **Match on path-component boundaries.** The entry's prefix keeps its trailing `/`, so
    `my-group/*` covers `my-group/x` and `my-group/sub/x` but not `my-group-evil/x`, `my-group.x/y`
    or `my-group` itself. The concrete repo name still goes through the name checks first
    (`check_repo_name`) and `remote_of` still confirms it is on `[gitlab].url`; matching never
    sees an unvalidated name. Those checks now use `fullmatch`: with `re.match` and `$`, a name
    ending in a newline passed the regex, which no exact entry could ever equal but a group entry
    would have matched.
  - **Malformed wildcards are refused, and match nothing.** `*` is valid only as the final `/*`
    after a host and at least one group segment (at most 21, GitLab's top group plus 20
    subgroups). Refused: `host/*` (the whole instance), `**`, a `*` anywhere else, empty, `.`/`..`
    and `-`-leading segments, a `.git`/`.atom` end, a bad host or port. The doctor
    (`check_repos_entry`) and the host (`factory/policy.py`, which the image build, `factory
    start`, `factory status` and `factory doctor` use) report them; the matcher independently
    skips an invalid entry, so a bad entry fails closed even if nobody ran either check.
  - **GitHub wildcards are deliberately not supported.** An `owner/*` entry is refused with a
    clear error. A GitHub token (fine-grained, per repo) does not scale with an owner wildcard the
    way a GitLab group token does, and a public owner would let an agent push to, or open PRs on,
    repos nobody listed. Out of scope; list GitHub repos one by one.
  - **Doctor: groups are checked by the token's role in the group.** `GET api/v4/user` gives the
    token's (bot) user id, then `GET api/v4/groups/<url-encoded path>/members/all/<id>` gives its
    `access_level` (30, Developer, or above passes). Same no-redirect opener and token handling as
    the project check; the token is never in a message. Anything short of a confirmed Developer+
    is a warning, never an OK: below Developer, a 404 on the member lookup (not a member: likely a
    project access token, which reaches one project only, with the fix), a failed or odd `/user`
    answer, or any other failure ("its group role could not be confirmed"). The first version read
    `permissions.group_access` from `GET /groups/:id`, which the reviewer believes GitLab returns
    only on the projects API, so a correct group token would have drawn a false warning; the
    members lookup is the documented way to read a role. Still unverified against a live GitLab
    (no web access): that a group access token can read its own `/user` and `members/all`.
  - **Credential exception reworded** in `CLAUDE.md` and `broker/CLAUDE.md`: "a GitLab project
    access token" is now "a GitLab project or group access token". It is the same credential
    delivered the same way (a file in the broker sandbox only, scoped to `[gitlab].url`); nothing
    else in those rules changed.

## 2026-10-29

- **The control unit writes two kinds of host config file, from SETUP** (owner decision 29 Oct
  2026, doomtickle/passdesk-ops#185, #186; backend `scripts/dashboard_configwrite.py`). SETUP was
  read-only; tuning a role or writing project notes meant editing files by hand. Choices:
  - **Only data files, one code path.** A role's tuning override `FACTORY_CONFIG/roles/<role>.conf`
    (`ROLE_OVERRIDE_KEYS` only; never `WORKSPACE`, `TEMPLATE` or `BROKER_TOOLS`) and the bound
    project's notes `projects/<project>.md` and `<project>.<role>.md`. Both are parsed as data or
    appended as prompt text, never executed. Policy, credentials, kits and `enabled-roles` stay
    hand-edited.
  - **Targets from server facts only**: `FACTORY_CONFIG`, the dashboard's project folder name and
    a role `list_roles` returns. Nothing below `FACTORY_CONFIG` is followed if it is a symlink; the
    file must be a regular one; the write is a 0600 temp file in the same folder, fsynced, then
    `os.replace`.
  - **Validated as `factory start` validates.** The new override passes `roles.validate_role` in a
    throwaway config folder before a token is issued; a bad existing file is refused, not
    repaired. Comments and key order are kept. (`validate_role` does not require MODEL to be one
    of MODELS; the bridge adds MODEL to the list itself, so neither does this.)
  - **Notes limit, with one leniency.** At most 20,000 characters; refused if any running role's
    instructions plus notes would pass the bridge's 20,000 (`MAX_INSTRUCTIONS_CHARS`) — unless the
    change lowers a total already over it, since that is the way back under.
  - **Confirm step and concurrent edits.** Prepare returns a diff and a single-use 120 s token
    (RESTART's token slot, action `"config"`) bound to the new bytes and the SHA-256 of the file
    as it was; run writes only if the digest still matches (else 409 `changed`).
  - **The 128 KiB exception.** Every control request body stays capped at 8 KiB except
    `config-prepare`, which carries a notes file (20,000 characters is up to 80 KB as UTF-8 JSON) and
    may be up to 128 KiB, checked on `Content-Length` before the body is read.
  - **Applies on RESTART.** The bridge reads only the snapshot `factory start` makes into
    `FACTORY_STATE` (`role-overrides/`, `instructions/`); LANES also sets up lane sessions at
    start. So every change reports `RESTART`, the SERVICE DUE lamp (already comparing those files
    with the snapshot) lights at once, and nothing was made to read `FACTORY_CONFIG` live.
  - **The editor is its own served file** (UI half, `dashboard-ui/cu-notes.js`, in `STATIC`, loaded
    before `cu.js`). `cu.js` lends it only its `api` function and the console nodes, so the per-run
    key never leaves `cu.js`; the editor makes no request of its own and sends only
    `config-read`, `config-prepare` and `config-run`.
  - **SAVE is dark while any role the file reaches would be over 20,000**, not only the role on the
    open tab: saving a shared file that pushes another role over would make the bridge refuse that
    role's tasks. The gauge copies the bridge's arithmetic (headings included, code points); after
    a save the prepare reply's `totals` stand in until the polled state catches up. A change that
    lowers a total already over the limit is allowed by the backend, so the editor lets SAVE
    through when no total it would raise is over.
  - **A refusal of prepare in demo mode** is drawn as the fixed text "DEMO · CHANGES DISABLED",
    never the server's wording.
  - **"Edit notes" sits in the SETUP body**, not on the shared GO key, so the tuning knobs can
    use that key without a clash.

## 2026-10-28

- **`github_update_pr`: title and body of a pull request, nothing else**
  (doomtickle/passdesk-ops#188). `github_create_pr` can open a draft PR but nothing could
  change one afterwards (a review packet of 26 Oct ended "someone with GitHub access will
  need to rename #180"). Choices:
  - **Same policy as `github_create_pr`:** the repo must be in `[git].repos` (GitHub only;
    a GitLab project is refused), the title and body are secret-scanned, the call is audited
    with an intent record, and it is held when `[approvals].tools` names it. The card has the
    body's first 2,000 characters and the digest of the whole body, which is part of the card
    id and checked against the job again before the write, so two edits whose bodies differ
    only past the excerpt are different cards. A title or body that was not given is `null`
    on the card and the job, which is not the same card as an empty one.
  - **The PATCH is built from title and body alone.** It never sends `draft`, `state`, `base`
    or any other field, and the tool does not accept them (the call layer refuses unknown
    arguments). Marking a draft ready for review stays the owner's decision
    (doomtickle/passdesk-ops#8: agents must not open non-draft PRs, and the same reasoning
    covers readiness).
  - **Limits:** title 1 to 256 characters on one line, body at most 65,536 characters (GitHub's
    own limits), at least one of the two, `number` a positive `integer` (not a bool, at most
    10^9). A body replaces the whole description: GitHub's PATCH has no append.
  - **Only PRs the gateway could have opened (reviewer finding, medium).** The tool first
    GETs `/repos/{repo}/pulls/{number}` and refuses unless `head.repo.full_name` equals the
    repo (case-insensitive; a fork or a deleted fork repo is refused) and `head.ref` starts
    with `[git].push_branch_prefix`, the only branches `github_create_pr` opens PRs from and
    `git_push` writes. Without this the tool could rewrite the title or body of the owner's own
    PRs or a fork's (a prompt-injected developer with holds off, the default, could add
    "Fixes #N" silently), which is more than create or push allow; an earlier version of
    this entry wrongly said that write was already possible. The head is on the approval card
    (and in its id), the completer GETs again and refuses unless the head is the card's, and
    both checks fail closed: any error reading the PR refuses. The GET uses the same
    `_github_api` path (Bearer token and no redirects where the gateway holds the token) and
    needs Pull requests read, which the PATCH's read/write includes. A PR of this role's
    that someone changed to another head branch is refused too.
  - **Token handling is `github_create_pr`'s:** `_github_api` with a Bearer header and no
    redirects where the gateway holds the token, the proxy's authentication under sbx. The
    token needs Pull requests read/write on the repo, as for `github_create_pr`.
  - **No replay of a made update.** `github_create_issue` and the other writes answer the
    same call again with the earlier result (`Made`) so a repeat never writes twice. For
    `github_update_pr` that was wrong: title A, then B, then A again has the same card id as
    the first A, so the third call was answered "Made" while the PR stayed titled B. A PATCH
    to the same text is harmless to repeat, so `core/holds.py` `_settled` does not replay a
    finished `github_update_pr`: the same update after one was made stages a fresh card. A
    call still pending, or being made right now, is answered as for any write.
  - **Grant:** the developer's `BROKER_TOOLS` in `roles/developer/role.conf`. A changed
    `BROKER_TOOLS` of a role that already has broker access applies on `factory start`; no
    sandbox is recreated (only giving a role its first broker tool does).

- **`dashboard.toml` is read once, strictly, and a bad file means the defaults, not a stopped dashboard.**
  doomtickle/passdesk-ops#196. `scripts/dashboard_config.py` reads `FACTORY_CONFIG/dashboard.toml`
  (the dashboard's `--config`) at start and serves the result as `look` in `/api/state`; there is no
  new endpoint and no reload. Unknown keys and tables, repeated keys, bad values, a symlink or
  non-regular file, a file over 16 KiB and non-UTF-8 text each refuse the whole file (the first
  error only, with the file, line and key), rather than half-apply it: a typo in `crt` should not
  quietly leave `[roles.colors]` in force. The refusal is logged to stderr and shown on SETUP, and
  the page gets the defaults. Strings reuse `factory/policy.py`'s string reader (now the public
  `toml_string`) instead of a second TOML parser, so there is no `tomllib` (3.9 on the host); values
  are one line each, `shifts` included, which keeps the reader small. Demo mode takes the defaults.
- **The page re-checks `look` and sets colours only with `style.setProperty`.** `look.js` validates
  every field again (a colour is `#rrggbb`, a role name the role-name pattern, strings capped printable
  ASCII), so a tampered state can't put anything but a validated colour into a style. No class name,
  attribute or HTML comes from the file, and the CSP still has no inline style. The role-colour keys
  may name any role, built-in ones included: the colour is display only, and refusing `developer` would
  be an arbitrary rule. A role without a colour keeps its fixed hue or the hashed one from #162.
- **`crt` and `sound` are defaults, not settings.** The browser's own choice wins. To make that
  true for the CRT knob, the knobs' `localStorage` entry now stores only the knobs the user has turned
  (it used to write all four on the first change), so a stored `crt` always means "my choice" and the
  file's level shows until then. The knob's reset goes to the file's level. `sound = true` acts once,
  from the first state, and only when `fc1984-sound` is absent.
- **`plant` feeds both the header nameplate and the floor's plate**, as one key, as the issue's
  comment says. The 24-character cap is what both fit (checked in the browser test).

- **OUTBOX also asks `git cherry`, and retires abandoned rows after a week (2026-10-29).** A branch
  re-pushed under another name (`-v2`) or rebased landed as different commits, so `rev-list` showed
  it UNMERGED forever; and OPEN rows never retired. When `rev-list` says ahead, the module runs
  `cherry HEAD <object id>` (the same pinned flags, the same listed id): all `- ` lines (and at least
  one) is MERGED, any `+`, an error or a timeout stays OPEN. A squash of several commits is still not
  detected (the patch differs). Caching: HEAD's id would need `rev-parse`, a fourth form, so the answer
  is keyed by (repo, object id), valid 60 s (`CHERRY_TTL`) and dropped when the ahead count differs.
  The ahead count alone can't be the key: a squash or rebase landing moves `HEAD` and leaves it
  unchanged, which is exactly the case this exists for. Failures are cached the same way so a
  long-diverged branch costs one 5 s run a minute, not one per poll. Cherry runs only for rows that
  will be shown (newest first, stop at 20, nothing past 7 days). OPEN and UNCHECKED rows retire
  7 days after the job ended (`RETIRE_OPEN_SECONDS`); MERGED and GONE keep 24 h.
- **The dashboard's OUTBOX asks git two fixed questions and never gives git a name.**
  doomtickle/passdesk-ops#189. The module (`scripts/dashboard_outbox.py`) lists the refs the
  bridge imported with one `for-each-ref --format=%(objectname) %(refname)` over the
  `refs/remotes/factory-<lane>/factory/` namespaces, and counts each branch with `rev-list
  --count HEAD..<object id>`. That is `rev-list --count`, not the `merge-base --is-ancestor` the
  issue suggested: one run gives both "merged" (0) and "commits ahead" (the row's number). The
  only thing passed to git from a record is a full object id the listing itself printed, so a
  hostile branch name (a leading `-`, a newline, `..`, shell characters) is shown as an UNCHECKED
  row and reaches no argv; the ref is only compared with the listing's names. Both forms are in
  `CLAUDE.md` and `scripts/CLAUDE.md`. Results are cached 8 s, capped at 20 rows (40 branches, 5 s
  a run, 12 s a recomputation), and demo mode reads `dispatch/demo-refs.json` instead (it runs no
  git).
- **Push state is read from what the gateway recorded, and STAGED needs a live card.** A push is
  STAGED only while its approval card is still pending (an expired card shows nothing, because
  nobody can approve it), PUSHED or FAILED when the gateway's write finished (the audit line or
  the deferred-writes list), and WRITTEN when a PR/MR was opened for the branch with no push on
  record. A link is built only from the gateway's own `Opened PR #N: <url>` result and only if it
  is a `github.com` pull request, or a merge request on the policy's GitLab host, with the same
  number; the page checks again before setting `href`. A verdict is the newest reviewer packet
  whose Task section names the exact branch (a prefix such as `foo` vs `foo-bar` does not match);
  it is marked old when that review began before the developer's job ended.
- **Rows retire on what git can see, a day after the job.** MERGED (contained in `HEAD`) and GONE
  (the ref no longer exists) rows drop a day after their job ended; an OPEN row stays. A squash or
  rebase merge never makes a branch MERGED, so the row stays until the owner deletes the imported
  ref; detecting that would need a patch-id comparison per branch, which is more git than the
  state view should run. The module sits under the lane strip in the Lanes bay (the issue allowed
  bay or rail; the rail is full), and a row opens the job's packet through `/api/lane?job=`.

## 2026-10-27

- **`github_get_issue` and `github_list_issues`: read-only, with their own repo list,
  `[github].issue_read_repos`** (doomtickle/passdesk-ops#187). The Coordinator read
  `BACKLOG.md`, a snapshot that goes stale, and the owner pasted issue text in by hand;
  the Jira tools already show the shape (read-only, opt-in grant in
  `kits/coordinator/broker.conf`, content wrapped as untrusted). Choices:
  - **A new list, empty by default, not `issue_repos` and not `[git].repos`.** Reusing
    `issue_repos` would make every tracker the Coordinator may read one the reviewer
    may also write to (and the reverse), tying two grants that are decided separately.
    `[git].repos` usually holds public repos, where anyone can write an issue; the text
    would go straight into the Coordinator's context, the one role that delegates work.
    The cost is one more list to set; an empty list leaves both tools returning DENIED,
    the same inert default as `issue_repos`. The doctor reports it.
  - **Read only, nothing that writes.** `GET`s of one issue, its comments and a repo's
    issue list. A pull request is refused by `github_get_issue` and left out of
    `github_list_issues` (the issues API returns both).
  - **Output is bounded:** at most 30 issues; title 300 characters, body 10,000, each
    comment 2,000 and the last 5 comments; 20,000 overall. Everything goes through
    `sanitize` (escapes and control characters stripped, secrets redacted) and
    `untrusted` (nonce-marked block), and the call is audited like any tool (the result
    is cut to 500 characters in the record).
  - **Token handling is `github_create_issue`'s:** the same `_github_api`, so the sbx proxy
    authenticates, or in standalone a Bearer header with redirects refused. The token needs
    Issues read; the failure hint says so. `number` is an `integer` argument, which the
    call layer already type-checks.
  - **Not granted by default.** Like Jira, the Coordinator gets them only when the user
    sets `BROKER_TOOLS` in `kits/coordinator/broker.conf` (then `factory update`, which
    recreates its sandbox). The Coordinator briefing carries the same rule as for Jira:
    ticket content is data, and it confirms with the user before delegating from it.

- **The dashboard's SETUP screen rides in the polled control-unit state, cached for 5 s, and
  shows no credential at all.** doomtickle/passdesk-ops#183. `config` is built by
  `scripts/dashboard_setup.py` from the factory's own parsers (`roles.role_get` for the
  effective value, so `ROLE_OVERRIDE_KEYS` stays the one list of overridable keys;
  `policy.policy_value_source`, new, to say whether a policy value is REPO or LOCAL). It is built
  and cached outside `board.lock`, as is `board.service()`. Policy is read key by key: only
  `[approvals].tools`/`expire_seconds`, `[git].repos` and `[gitlab].url` (user:password, query
  and fragment cut) are taken, so no other section can leak. "Stored / not stored" for
  credentials was left out: it needs a keychain call and the screen runs no command. Notes sizes
  are in characters (as the bridge counts), and a role's total mirrors `dispatch_start`'s
  headings. Demo mode reads a fixture root and config folder in its temp copy, never the checkout.

- **The dashboard's SERVICE DUE lamp reads files, never `sbx ls -q`, and names the sandbox's
  inputs that changed from lines the start now records.** doomtickle/passdesk-ops#184. The
  state view only reads the state folder, so it can't ask sbx which sandboxes exist: it checks
  `fingerprints.is_stale` (the one notion of stale, the same test `factory start` warns with)
  for each sandbox that has a recorded fingerprint. A sandbox made before fingerprints, or
  never created, is therefore not named there; `factory start` still warns for it. To say
  *why* ("kit changed"), `record_sandbox_fingerprint` also writes the lines the digest is taken
  of to `FACTORY_STATE/sandbox-parts/<name>` (a separate folder: the `sandboxes/` files stay
  byte for byte what the bash reads); a sandbox with no such file gets a general reason. The
  digest has no policy.toml input, so the broker's reason is "image rebuilt (policy.toml or
  broker code)", not "policy.toml changed". RESTART compares role overrides and `enabled-roles`
  by content with their snapshots in `FACTORY_STATE/dispatch/`, and project notes by mtime
  against the snapshot's last write; deleted notes aren't noticed. It is display only: UPDATE
  wins when both apply, and a failure while computing it reads as nothing due.


## 2026-10-26

- **`start-broker.sh` is Python (`factory/broker_start.py`); a failed credential delivery
  still only warns there, but `factory secret save` now says it failed.** Step 7 part B of
  `docs/py-cli-migration.md` (doomtickle/passdesk-ops#169), and #179.
  `secrets.sync_from_keychain` now returns `SYNCED` (0), `NOT_DELIVERED` (1: the delivery
  failed, or the keychain could not be read) or `REVOKE_FAILED` (2). It used to return 0 for a
  failed delivery, so `save` printed "delivered" for a value that never reached the broker.
  `save` prints that line only on `SYNCED` and exits with the status. The broker start keeps
  its behaviour: a failed delivery or an unreadable keychain warns and the start goes on (a
  denied keychain prompt, or an old broker image, must not stop the whole factory), and only a
  failed revocation is fatal, as the credential may still be active. Two things are stricter
  than the bash on failure paths. A `factory secret set` marker that can't be removed when a
  new broker sandbox is created now stops the start; before, the start went on with a stale
  marker. And the name of a former role, which comes from `sbx mcp ls`, is used as a file name
  under `tokens/` only if it has no `/`, so sbx output can't get a file outside the state dir
  removed. Values still go only to a program's stdin, and the one shell, the fixed
  server-start script, runs inside the broker sandbox with no value in it.

## 2026-10-25

- **`stop`, `restart`, `reset` and `chat` are Python; `03-stop-factory.sh` stays shell.**
  Step 6 of `docs/py-cli-migration.md` (doomtickle/passdesk-ops#169). The stop script is also
  run by `update` and by hand (the troubleshooting docs), so the Python commands run it as a
  fixed argv and take its exit status rather than copy its `ps` and `sbx stop` logic; it goes
  with `common.sh` at the end. `restart` runs bash `start` as the fixed argv
  `[bin/factory, start, <project>, --no-dashboard]` until `start` is ported. `chat`'s
  attached check is the dashboard's `runs_sandbox` rule in `factory/sbx.py`, with a test that
  runs both on the same fixtures (the dashboard-security rule in `scripts/CLAUDE.md`).
  Sandbox fingerprints and `stale_sandboxes` wait for `start`/`update`, their only callers.

- **`factory doctor` warns about extra global sbx network allows; it does not fail.**
  An owner's machine had local-scope allows for `*.npmjs.org` and `github.com`
  that let every sandbox reach those hosts without the broker; nothing checked
  for them. Doctor now reads `sbx policy ls --wide --source local --type network
  --decision allow` and warns for each host other than `api.anthropic.com`, with
  its rule ID and `sbx policy rm network --id`. A warning, not a problem: the
  global policy is shared by all sbx sandboxes on the host, and `factory setup`
  aborts on problems, so a failure would make setup demand deleting rules that
  other work may use. The table is parsed (columns cut at the header offsets)
  because the `--json` layout is not in `docs/reference/sbx-cli.md`.

## 2026-10-24

- **Every held write is staged; the gateway makes it; one result shape**
  (doomtickle/passdesk-ops#170, part of #167; supersedes most of item 2 of
  #141 and the first-line held note of #153). A held call used to wait up
  to `hold_seconds` (45) for a decision, then queue a push, PR or MR and
  ask for a retry of an issue or plugin call. Agents met three shapes
  (`Queued ...`, `Approved ...; the gateway is making this write now`, and
  a result with `; held for approval ... approved by` folded into its first
  line), and the developer instructions carried a rule for telling them
  apart. Now a held call never waits: the hook stores the job and then the
  card in one locked step and returns `Staged for approval (id X); nothing
  has been sent yet. ...` (`errors.Staged`, record `decision: staged`). The
  sweeper makes every approved write from the stored job, as the queue
  did, and files the outcome under `done/`. What changed with it:
  - `github_create_issue` and plugin tools declared `writes_externally`
    now pass a job and register a completer, so they are staged too. The
    issue's job carries the labels as GitHub spells them (looked up at
    staging time); the completer checks them against the card's
    case-insensitively. A plugin tool's completer re-checks the stored
    arguments against the tool's declaration, the secret scan and the
    card (`core/registry.py`), then runs the handler and cleans its output
    as in a call. A held tool with no job or completer is refused with
    nothing staged, rather than falling back to the retry hold.
  - The same call again: while pending, the same staged result (same id,
    no second webhook); once the write is made, for as long as its done/
    record exists, the write's stored result, cleaned again (`errors.Made`,
    record `decision: made`: "Made by the gateway at T once approved
    (approval X); this call staged and sent nothing new. The write's
    result:" and the result, e.g. the PR URL); while running, `DENIED`
    saying so; once declined, `DENIED` for `expire_seconds` after; after a
    failure, an expiry or that window, a fresh card. Owner decision
    (second REVISE of #170): `Made` has no time limit, replacing our
    `expire_seconds` window, because after the window a repeat would stage
    a card that, approved again, files a duplicate issue (or opens a second
    PR on another base). Deleting the done/ record is the way to ask for
    the same write again.
    Either way a write is never made twice and a decline is not silently
    asked again. Owner decision (REVISE of #170), replacing our first
    choice of refusing after a made write too: the Coordinator can send the
    same lane back once, after approval, to collect the PR URL with the
    same call. A done/ record counts as this card's by its stored `id`, not
    by hashing it again (for a plugin tool, the record's own fields would
    change the hash, so a repeat after a made or declined write staged a
    new card).
  - A plugin tool's argument can't be named after a field the gateway sets
    on the card (`tool`, `principal`, `id`, `nonce`, `created`, `expires`)
    or on its done/ record (`decision`, `approver`, `reason`, `decided`,
    `queued`, `write`, `result`, `started`, `finished`, `outcome`): the
    first four of the hold's would be left out of the card id (two calls
    differing only there shared a card, so the second write was dropped)
    and made every approved call fail its card check; the done/ ones would
    let an argument read as, say, the approver of a card that expired
    undecided. Refused at registration. The completer checks each staged
    argument with the call's own type check (`registry.type_ok`), so the
    two can't drift if more JSON types are allowed.
  - Removed: `hold_seconds` (ignored, and the doctor warns to delete it; no
    migration needed), `with_held_note`/`held_note` and the plugin-hook
    outcome contract (whatever a hook returns is ignored now; a plugin
    hook still allows by returning and refuses with `Denied`), the
    `Queued` error, the `approval-queued` audit event (the
    `approval-requested` event is the staging), and the "retry this
    exact call" denial.
  - Kept: the file layout (`pending/`, `queued/` for the job, `decisions/`,
    `done/` with `queued: true`), so `factory approvals` and the
    dashboard read the same files; only their wording changed. A pending
    card with no job (left by a gateway from before this change) is
    discarded and staged afresh on the next call.
  - The PR card carries `body_sha256` and `body_chars` beside its
    2,000-character body excerpt, as the MR and issue cards already did;
    the digest is in its id fields and `complete_github_create_pr` checks
    it against the job. Before, two PRs whose bodies differed only past
    2,000 characters shared a card: the second call got the first's staged
    result (its body silently dropped), and once the first was made, its
    URL. This closes the card side of doomtickle/passdesk-ops#13 for PRs.
  - `card_id` for a tool without listed stable fields (a plugin tool) now
    leaves out the fields the hold adds (`id`, `nonce`, `created`,
    `expires`), so a stored card hashes to its own id; a fresh card has
    none of them, so ids are unchanged.
  What would change it: an agent that needs the write's result without
  repeating the call (or often enough that a status tool is worth adding).

- **The host CLI's Python package is `factory/` at the repo root, and the bridge
  imports from it** (doomtickle/passdesk-ops#169, first PR). Root, not
  `scripts/factory/`: the bridge, the dashboard scripts and the tests all find the
  checkout root already, and `python3 <root>/factory` (what `bin/factory` runs)
  needs no `PYTHONPATH`. `__main__.py` replaces `sys.path[0]` with the root when
  run as a directory, so neither the working directory (a project may contain a
  `factory/` folder) nor a module named like a stdlib one can stand in for the
  package. `dispatch/server.py` puts the checkout root first on `sys.path` and
  imports `factory.conf`, `factory.roles` and `factory.repos`: it is started as
  `$ROOT/dispatch/server.py` from that same checkout, so deployment is unchanged,
  and the bridge's own `role.conf` regex and `project_repos` are gone (one copy
  left beside the bash ones). Differences from before, all toward bash's
  behaviour: the `KEY="value"` line ends at `\n` only and trailing whitespace is
  ASCII only (was `splitlines()` and `\s`), and an `enabled-roles` line is a role
  name by bash's rule (any length) instead of the bridge's 2 to 31 characters;
  the second is harmless because `load_roles` checks the name of every role it
  finds in `roles/` anyway. `list_all_roles` sorts by bytes where bash's glob
  sorts by the user's locale; they differ only for names like `foo` and `foo-bar`
  under a locale that ignores dashes. What would change it: a reason to publish
  the package (then a `pyproject.toml`), or the bridge moving off the host.

## 2026-10-23

- **A write made after a hold says so in one first line**
  (doomtickle/passdesk-ops#153). The note added in #141 was a last line,
  `Held for approval (id X); approved by "NAME" at T after N s.`, under a
  first line like `Pushed 2 commit(s) to o/r:b (sha).` A developer run read
  "Pushed" plus "Held for approval" as ambiguous and refused to open the PR
  until told the push had landed; agents are also taught `Queued for
  approval ...; nothing has been sent yet` and `Approved ...; the gateway is
  making this write now`, so a lone "Held for approval" line looked like a
  third not-yet-done state. `with_held_note` now folds the note into the
  first line (final full stop dropped, then `; held for approval (id X),
  approved by "NAME" at T after N s` and a new full stop), for push, PR, MR
  and issue results, success and failure, and for a queued write's result
  read back by a retry (with `; written by the gateway at T`). Later lines
  are unchanged and an unheld result is byte-identical. `held_note` keeps
  the standalone sentence with a comma instead of a semicolon so both forms
  read alike; a first line ending in a colon (`Push rejected by GitHub:`)
  gets the clause before the colon, so the colon still introduces the error
  text below; the `Queued` and `Approved` strings did not change. The tool
  descriptions' queued note says a first line with "held for approval ...
  approved by" is finished, not queued, and that its first words say whether
  the write succeeded or was refused. Consumers checked: nothing in the broker or
  the AI Code Factory parses the result text (the dashboard's held note and
  `factory approvals` read the cards and decisions). What would change it:
  an agent that still misreads the line, or a consumer that needs the old
  last line.

- **Herdr is removed from the factory** (doomtickle/passdesk-ops#146). The
  owner no longer wants the terminal UI; the dashboard (`--layout web`) has
  been the default since 20 Oct. Gone: the `--layout watch` and `tabs`
  layouts, Herdr's install and version pin (`factory setup` no longer
  downloads it; an existing `herdr` binary is left alone on the machine),
  its `factory doctor` check, its session and `FACTORY_NO_ATTACH`. Kept:
  `factory dashboard`, `factory chat`, the standalone `factory watch`
  printer, `--no-ui`, and the one-attachment rule for the Coordinator's
  terminal (`coordinator_attached`, `runs_sandbox`). A saved or passed
  `watch` or `tabs` layout falls back to the dashboard with a one-line
  warning rather than failing. What would change it: the owner asking for a
  terminal UI again. (Repo: ai-code-factory.)

- **The factory's researcher role is off by default** (owner decision;
  doomtickle/passdesk-ops#147, ai-code-factory branch
  `factory/researcher-opt-in`). The researcher ran about 3% of 371 jobs and
  was rarely useful, yet every machine paid for its sandbox, broker token
  and grants. Choices:
  - **Two switches, not one.** `roles/researcher/role.conf` carries
    `OPTIONAL="1"` (committed, so what is optional is reviewed in the repo);
    the user turns it on with a line `researcher` in the local
    `enabled-roles` file in the factory config folder. `OPTIONAL` is not a
    `roles/<role>.conf` override key, so local config can flip a role on
    but cannot change `BROKER_TOOLS`, `WORKSPACE` or `TEMPLATE`, and cannot mark a
    role optional or not. The file is parsed line by line (never sourced);
    an unknown or malformed line fails `factory doctor`/`start` rather than
    being ignored.
  - **One enumeration.** `list_roles` hides a disabled optional role, so
    start, update, broker tokens and grants, and the dispatch snapshot all
    skip it; the bridge reads a snapshot of `enabled-roles` taken at
    `factory start`, so a running bridge and the sandboxes agree. A missing
    or unreadable file means off.
  - **The bridge refuses, it does not hide.** `delegate` to a disabled role
    says how to enable it; `list_roles` and the Coordinator's instructions
    leave it out.
  - **An existing sandbox is never removed automatically.** `factory
    doctor` notes it and prints the `sbx rm` command. The broker token and
    MCP registration of a role that drops out of the list are cleaned up by
    the existing `start-broker.sh` logic.

## 2026-10-22

- **Approve later: a held push, PR or MR is queued and the gateway makes it
  on approval** (doomtickle/passdesk-ops#91, item 2 of #141). A write
  undecided after `hold_seconds` used to be refused with "retry this exact
  call"; every held push past 45 s then cost the factory a separate job just
  to retry, and the window sometimes expired in between (4 times for 3
  branches in one day). Now `git_push`, `github_create_pr` and
  `gitlab_create_mr` pass `approvals.require` a job (their checked write
  arguments) and register a completer; the hook stores the job as
  `approvals/queued/<id>.json` and raises `errors.Queued`, which the
  protocol layer returns as a non-error result. Choices:
  - **The queued result is plain text, not an error**, starting `Queued for
    approval (id X); nothing has been sent yet.`, naming the write, the
    deadline (ISO time and how long), and saying "Report it as queued, not
    done" and that the same call reports its status and later its result.
    The call's record has `decision: queued`, `approval: <id>`. A retry
    while the gateway is writing gets `Approved (id X); the gateway is
    making this write now...`; after the write, its result plus the held
    clause with `; written by the gateway at T`, in its first line.
  - **Exactly once.** The job is stored under the hold's lock only if no
    decision has arrived (one that has is applied in the call instead), and
    the runner claims a write by filing `done/<id>.json` as `running` and
    removing card, decision and job in one locked step. The sweeper and a
    retry both run the same claim, so whichever is first writes and the
    other reports. A retry looks for the settled outcome again under the
    lock before it makes a fresh card, so a claim landing between its first
    look and the card is reported, never made into a second request. A
    process that dies mid-write leaves `running`; the next
    start files it as failed ("may or may not have reached the forge") and
    never retries it, because a second push or PR could be wrong.
  - **What the deferred write trusts.** Only the gateway's state: the job,
    the card and the mirror. On approval the completer re-checks the job
    against today's policy (allowlist, branch prefix, protected branches, a
    secret scan of title and body) and against the card (head, bundle
    digest, title, body or its digest, draft), the card's fields must still
    hash to its id, and a push needs the mirror and its ref unchanged.
    It also secret-scans the range again: a refused push leaves its commits
    on the incoming ref, so a job forged by someone with write access to
    the state directory could otherwise push them. The threat model trusts
    that person (they are the approver), but the scan costs one `git log`.
    The job holds no credential; the token is read at write time, as before.
  - **A concurrent push to the same branch supersedes the queued one.** Both
    use the branch's one incoming ref, so the first's approval then fails
    ("changed in the broker's mirror after the push was queued") and only the
    newer push can go out. Per-card refs would let an older push go out
    after a newer one, which is the wrong order.
  - **Retries after the outcome.** A written result and a decline are
    reported to the same call for `expire_seconds` after they happened, so
    an agent that checks back never writes twice or re-asks. A failure is
    reported once; the call after that is a new request (a fresh card),
    since the cause, e.g. a moved ref, may have been fixed. An expired card
    makes a fresh one, as before.
  - **Done file shape.** The card and decision as before, plus `queued:
    true`, `write` (`running`, `done`, `failed`, `skipped`), `result`
    (filtered, at most 3,000 characters), `finished` (Unix time) and
    `outcome`. The webhook gets one more line per write made or failed,
    using the result's first line only. New audit events:
    `approval-queued`, `approval-write-done` and `approval-write-failed`; the
    granted, declined and expired events of a queued card carry
    `queued: true`.
  - **Poll cadence: the idle sweeper now ticks every 5 s** (was 60 s), and
    each tick runs the approved queued writes before it expires cards. Each
    tick costs a listdir of `queued/` and `pending/` and a stat of the
    patches, so 5 s is cheap, and a person who approves sees the push
    within seconds. A retry after approval doesn't wait for the tick.
  - **Expiry: default 86,400 s (a day), bounds 60 s to 604,800 s (a week);
    values outside are clamped and the doctor warns.** With the queue, the
    window is how long a person has to decide, not how long an agent keeps
    retrying, so a day covers an overnight agent with no setting. Past a
    week the base has usually moved on, and the card, the mirror ref and
    the patch would sit there for nothing. The old 1800 s default applies
    to anyone who leaves it unset, so upgrading lengthens their window.
  - **Scope.** `github_create_issue` keeps the retry hold. Its labels are
    checked against the repo before the hold and could be gone by
    approval, and filing an issue late has no retry cost worth the extra
    path. A plugin's tools also keep it: core never stores a job for, or
    runs one under, a hook it didn't install.

- **A held write's result says it was held, by whom and when**
  (doomtickle/passdesk-ops#141, item 1). An approval inside
  `hold_seconds` looked to a role like a slow call, so developers reported
  held pushes as "not held". `holds.hook` now returns
  `{id, approver, decided, waited_seconds}` for an approved hold,
  `approvals.require` passes it back, and `git_push`, `github_create_pr`,
  `gitlab_create_mr` and `github_create_issue` end their result with
  `Held for approval (id X); approved by "NAME" at T after N s.` The
  protocol layer puts `approval: <id>` on the call's audit record. Choices:
  - **`waited_seconds` is from the card's creation to the decision**, not
    how long this one call blocked: a retry that picks up a decision made
    meanwhile is a held write too, and the number a person cares about is
    how long it waited for them. A decision time or creation time that
    isn't a usable Unix time counts as now: not a number, not finite
    (json reads `1e999` as inf, and accepts `NaN`) or out of `gmtime`'s
    range. Those raised after the card was filed and audited as granted,
    so the write never ran and a retry needed a second approval.
  - **The note is on every result after approval**, success or not (a
    push the forge rejected, an HTTP error status), because the write was
    held either way. A `Denied` or an exception raised after approval (the
    mirror ref changed, the gitlab credential removed, no connection, a
    timeout) carries no note, but the call's record still has `approval`,
    which is set by `require` itself.
  - **Only a dict with a non-empty string `id` is an outcome.** Anything
    else a hook returns (a plugin's `None`, `True`, a string) means
    "allowed, not held", so old plugins keep working and can't inject a
    note. Every field is filtered and cut to one line (the approver name
    is read from a file the console writes). `filters.sanitize` leaves
    Unicode format characters (category Cf: bidi overrides and isolates,
    zero-width characters) and C1 controls, which can make the line read
    differently on screen, so the note drops those too, and it quotes the
    name (`"` inside becomes `'`) so it reads as data.
  - **Not here:** a hold that times out still raises `Denied` asking for a
    retry; the queued result and the gateway completing approved writes
    itself are item 2, a separate PR (#91).

- **Expired held-write cards are swept while the gateway is idle**
  (doomtickle/passdesk-ops#141). The expiry sweep ran only inside a held
  call, so with no agent pushing an expired card stayed in
  `approvals/pending/` (and on the factory's write gate) indefinitely, even
  across a restart. Choices:
  - **Start-up plus a 60 s daemon timer** (`holds.start_sweeper`, called
    from `protocol.main` after plugins load), reusing `_sweep_expired` and
    its lock and nonce re-check, so expiry, the `approval-expired` record
    and the `done/` file are exactly as before. 60 s because
    `expire_seconds` is at least 60; not a policy setting.
  - **Only while core's hook is the installed one.** With holds off there
    is nothing to sweep, and a plugin that replaces the hook owns
    `pending/`; before this change core never swept under a plugin's hook
    either.
  - **The idle sweep also prunes patches** (`approvals.prune_patches`, same
    two-hour rule), so a patch whose card expired while idle goes too
    instead of waiting for the next `git_push`.
  - **A sweep error is logged by type and swallowed**, on the timer and,
    newly, inside a held call: a failure to file *other* cards no longer
    turns this call into an error. The call's own card is untouched by
    this (its expiry on retry still goes through `_ensure_pending`).

## 2026-10-21

- **One decision per approval card.** A held write's card stays in
  `approvals/pending/` until the agent retries the call, and the decision
  file (`approvals/decisions/<id>.json`, tied to the card by its nonce) is
  what the gateway reads on that retry. The factory's console and dashboard
  used to overwrite any existing decision, so a second press could silently
  flip a decline into an approve before the retry, and decided cards looked
  undecided. Choices:
  - **A decision is final for its card** (same id and nonce). The factory
    writes it with a hard link onto the final name (`os.link`), never a
    replace, so of two concurrent deciders exactly one wins.
  - **A leftover decision from an older card** with the same id but a
    different nonce is cleared under a short lock file in the decisions
    folder, so it never blocks the new card.
  - **A second decide is refused:** 409 `decided` in the dashboard; exit 1
    with "already approved/declined by X at T" in `factory approvals`.
  - **Decided and expired cards are shown as such** and are not counted as
    pending.
  - **Consequence:** the gateway's side is unchanged (same file name, format
    and nonce check). To change your mind before the retry, decline is no
    longer reversible from the console; the agent's retry gets the first
    decision. The implementation is in ai-code-factory (`scripts/approvals.py`,
    the dashboard control unit; branch `factory/gate-decided-cards`).

- **The hold moves from Passdesk Pro into core; the licence is gone
  (doomtickle/passdesk-ops#89, owner decision in passdesk-ops
  `decisions.md` and `docs/strategy/2026-10-lois-merge-plan.md`).** The
  factory is the one product and held writes are part of it; nothing is for
  sale, so there is nothing to license. Choices:
  - **`core/holds.py` and `core/notify.py` are Pro's `approvals.py` and
    `notify.py`** with every licence call removed and nothing else changed:
    same card ids, same `pending/`, `decisions/` and `done/` files, same
    nonce contract, same audit events. A console written against Pro
    (`factory approvals`, the factory dashboard) keeps working, and cards
    pending at upgrade time are still decidable.
  - **Off unless the policy says otherwise.** `holds.install` puts the hook
    on the approval point only when `[approvals].tools` is a non-empty
    list. Pro's default held three tools once licensed; without a licence
    that default would mean every upgrading deployment starts holding
    pushes by surprise, so the list is the switch. The doctor's Approvals
    section says which it is and prints the usual list as the fix.
  - **The old plugin is refused by name.** An old `passdesk_pro` copy would
    still import and put its licence-checking hook over core's, so
    `plugins.load` raises on that name and the doctor fails with the fix.
    The general plugin mechanism (tools, hooks, providers) is unchanged.
  - **The webhook URL is the `approvals-webhook` credential**, read through
    `core/credentials.py` like Jira or GitLab (the file in the secrets
    directory, a Docker secret, or `PASSDESK_SECRET_APPROVALS_WEBHOOK_FILE`;
    Pro's `PASSDESK_APPROVALS_WEBHOOK_FILE` is gone). CLAUDE.md's
    file-credential rule now lists it.
  - **Notification is off the hold path** (doomtickle/passdesk-ops#14): the
    POST runs on a daemon thread, so a webhook that takes ten seconds no
    longer costs ten of the forty-five held seconds.
  - **Policy checks.** The doctor fails a `tools` that isn't a list of
    names or a negative `hold_seconds`/`expire_seconds`, and warns about a
    tool name it doesn't know.
  - **Security review findings fixed before merge:** a webhook URL with
    userinfo is refused (urllib would echo the password in an
    `InvalidURL`), `send` catches `http.client.HTTPException` too and
    records only the exception type for anything but a `URLError`, so the
    thread never raises; `settings` falls back to the defaults for a
    `hold_seconds`/`expire_seconds` that isn't a number (the doctor
    reports it; the server and the doctor no longer crash on it); a
    pending card whose `expires` is unreadable is discarded
    (`approval-discarded`) and replaced instead of wedging every retry.
    Noted, not changed: `holds._read` and the doctor's card reads follow
    symlinks and would block on a FIFO; both need write access to the
    state directory, which the threat model excludes.
  - **Not here:** the in-gateway approver CLI (doomtickle/passdesk-ops#60)
    is a follow-up; the denial message still names `factory approvals` as
    the example console.

## 2026-10-20

- **GitLab, self-hosted or gitlab.com (doomtickle/passdesk-ops#120).** The
  owner needs the factory on a self-hosted GitLab as well as GitHub.
  Choices:
  - **The `gitlab` token is a file credential (owner decision).** sbx can
    proxy-inject GitHub but not GitLab, so the token can't stay outside the
    gateway as GitHub's does. The owner approved widening CLAUDE.md's
    file-credential exception to it. It is handled exactly as a gateway-held
    GitHub token: to git only as an extraHeader scoped to `[gitlab].url`
    (Basic `oauth2:<token>`) in one child's environment, redirects refused,
    no credential helper, trace switches removed; to the API only as a Bearer
    header through `NO_REDIRECT_OPENER`; redacted once read. The two forges'
    tokens never cross. The README says to use a project (or group) access
    token with the Developer role, `api` and `write_repository`: creating a
    merge request needs `api`, which on a personal token reaches everything
    its owner can.
  - **One `[git].repos`, host-qualified names.** A GitLab project is listed
    as `<host>/<group>/.../<project>`. A first segment with a dot or a port
    can't be a GitHub owner (letters, digits and dashes), so the name alone
    says the forge, `git_fetch` and `git_push` stay one pair of tools with
    every check they had, and a host-qualified name must be on
    `[gitlab].url` or it is refused. `[gitlab].url` itself must be https,
    end in `/`, carry no credentials, query or fragment, and not be a
    GitHub host.
  - **A separate `gitlab_create_mr`, not a renamed or forge-agnostic PR
    tool.** Renaming `github_create_pr` would break grants, audit history
    and approval cards. It takes the same arguments, scans the same fields,
    makes the same card (tool `gitlab_create_mr`, with `title` as it will be sent to GitLab,
    `Draft: ` included), and checks the token is there before the hold, so
    nobody approves a call that can't be made. Each PR tool refuses the
    other forge's repos.
  - **GitLab mirrors and bundles are named by digest.** GitHub's mirror name
    (`owner__name`) is unambiguous because owners have no `_`; GitLab paths
    may, so `g/a__b` and `g__a/b` would share a mirror (and its origin).
    `gitlab__<host>__<sha256(url, lower-cased path)[:32]>.git` can't collide,
    and `git_fetch`'s bundle name adds a 12-character digest of the same for
    the same reason (one project's bundle would overwrite the other's).
  - **The card carries the body's digest and length.** As for
    `github_create_issue`: two merge requests whose bodies differ only past
    the 2,000-character excerpt never share a card, and the approver sees how
    much they aren't reading. The body is capped at 65,536 characters
    (GitHub's PR limit), not GitLab's 1 MiB. Only `Draft:`, `[Draft]` and
    `(Draft)` (GitLab's documented markers, any case) count as already a
    draft; any other title gets `Draft: `.
  - **`[gitlab].url`'s host is checked label by label.** No empty, leading-
    or trailing-dash label, no trailing dot (it slipped past exact-host
    matches such as `web_fetch`'s denylist; `host_allowed` now also strips a
    trailing dot from the pattern, not just the host), port 1-65535, and no
    `..`, `.`, `//`, `@`, `%` or `\` segment in its path (the API URL is built by
    appending to it). Projects may sit under up to 20 subgroups (GitLab's
    limit). With only GitLab projects in `[git].repos`, the doctor stops
    counting github.com among `git_fetch`/`git_push`'s hosts and stops
    asking for a GitHub token for them. An IPv4
    address is allowed: an internal instance may have no name, and its
    certificate must verify all the same.
  - **`web_fetch` refuses the `[gitlab].url` host**, read leniently, so even
    a malformed URL keeps its host blocked: a tightening of the
    credentialed-host denylist.
  - **Not done:** a private CA for the instance (the system trust store
    must accept its certificate; documented), and GitLab issues (the owner
    doesn't need them).
- **The approver can read the code: a broker-local patch per held push
  (doomtickle/passdesk-ops#113).** Until now a person deciding a held
  `git_push` saw the repo, branch, file names and a shortstat, and approved
  blind. With an approval hook installed, `git_push` now writes the pushed
  commits' patch to `STATE/approvals/patches/<sha256>.patch` (content
  addressed, atomic, 0644) inside the tool lock, from the same mirror and
  range it secret-scanned, and the card names it by digest. Choices:
  - **Cards never carry content.** The card gains only `patch_sha256`,
    `patch_bytes` and `patch_truncated`. The card, the webhook, the audit
    log and the future Team relay never see the patch; only something on
    the same machine with the state directory (the factory's dashboard)
    reads it, and it checks the digest against the card.
  - **A format a parser can't be fooled by.** `git log -p --stat --cc
    --reverse` with `%w(0,4,4)%B`, so a commit message can't put a line at
    column 0 and pass for a `commit` or `diff --git` header. No `-a`: a
    binary file is git's own one-line notice, never bytes. Renames keep
    git's default detection. `COLUMNS` is removed from git's environment,
    since it changes `--stat`'s width and with it the digest, and
    `--full-index` writes full blob names, since abbreviated ones lengthen
    as the mirror gains objects; the same bundle gives the same file.
  - **Capped and redacted.** The text goes through `filters.redact` one
    line at a time (a removed or context line is never scanned, being
    upstream already; whole-text redaction could match across a newline and
    merge two lines, leaving a hunk short of its count), then
    is cut at the last newline at or before `[approvals] patch_max_bytes`
    (default 256 KiB, clamped to 4,096-2,000,000 so a console's 2 MiB read
    cap always holds it) with one trailer line saying how much was kept.
    git's output past the cap plus a margin is counted, not held.
  - **Pruning lives in core, ahead of #89.** Before each write, a patch is
    removed when no `approvals/pending/*.json` card names it and it is over
    two hours old (so a card the hook has not recorded yet, or a retry,
    keeps its patch). Unreadable cards are ignored; symlinks and anything
    but regular files are never followed or removed.
  - **`[approvals]` is now a core table.** `patch_max_bytes` is core's; the
    doctor knows the table (with Pro's `tools`, `hold_seconds` and
    `expire_seconds`) and warns on an out-of-range value instead of calling
    the table a typo.
  - **Fail-closed:** if the patch can't be built or stored, the push is
    refused rather than held without it.
  - **Nothing changes without a hook:** no git log, no file, no card fields.
  What would change this: a relay that must show code remotely (it would
  need its own decision; content is not to leave the machine), or patches
  big enough that a two-hour orphan window costs real disk.

## 2026-10-18

- **`github_create_issue`, a core tool with its own allowlist
  (doomtickle/passdesk-ops#86).** The owner wants a factory role (the
  reviewer, not the read-only Coordinator) to file tracker issues. A core
  tool, not a plugin: it needs the GitHub credential, which plugin tools
  may never read. Choices:
  - **Its own repo list, `[github].issue_repos`, empty by default.** It
    never falls back to `[git].repos`: those usually include public repos,
    and an issue in a public repo publishes whatever an agent writes, which
    makes it a way out for anything the agent read. List private trackers.
  - **Labels must already exist.** GitHub creates any label a request
    names, so the tool first reads the repo's labels (paged, at most 10
    pages) and refuses unknown ones. It sends GitHub's spelling, but the
    approval card shows the agent's (scanned) names, since nothing on a
    card comes from the network. Labels are a comma-separated string
    because `call_tool` checks only string and boolean arguments.
  - **File only.** No assignees, milestones, edits or closing.
  - **Inputs:** title one line, 1-256 characters, no control characters;
    body at most 65,536 characters (GitHub's limit). Title, body and every
    label are secret-scanned before any request or card.
  - **An external write:** an intent audit record, then the approval point.
    The card carries `body_sha256` beside the 2,000-character excerpt, so
    cards can't collide on long bodies (the problem #13 tracks for PRs).
  - **Token handling is `github_create_pr`'s:** the sbx proxy
    authenticates, or in standalone a Bearer header with redirects refused.
    The token needs Issues read/write.
  What would change this: a need to comment on or close issues (a separate
  tool, so a grant can stay file-only).
- **Compose quickstart and isolation check (doomtickle/passdesk-ops#58,
  Phase 3a W3.4).** `compose.yaml` runs the gateway, the egress guard and
  a Claude Code agent; `scripts/isolation-check.py` runs in the agent's
  network namespace and fails on any escape the Q2 research named;
  `docs/standalone.md` is the walkthrough. Judgement calls:
  - **The `agents` network is `internal: true` and
    `gateway_mode_ipv4: isolated` with IPv6 off**, exactly as Q2
    recommends: internal alone left the Docker host's services reachable
    on the bridge address. That option needs Engine 28.0.0 (Desktop 4.39);
    an older engine refuses the file, which fails closed, and the docs say
    so rather than offering a fallback.
  - **The isolation check is a separate compose service with
    `network_mode: "service:agent"`**, running the Passdesk image (which
    has Python) in the agent container's own network namespace, instead
    of a script executed inside the agent image: the result is the same
    namespace, and it doesn't depend on what the agent image ships. The
    script is mounted read-only from this checkout, never from the
    workspace. It fails on an IPv4 gateway in the routing table, a real
    IPv6 default route (a `::/0` reject route on `lo`, which an IPv6-less
    network leaves behind, is not one), and anything answering on the
    address a gateway would have had.
  - **The agent is `docker/sandbox-templates:claude-code`, pinned by
    digest, with `command: sleep infinity`**; the user attaches with
    `docker compose exec agent claude`. Building our own Node image would
    have meant a version-pinned but not checksum-pinned `npm install`.
    The template's `agent` user is uid 1000, matching the gateway's, so
    the bind-mounted `.factory/` exchange works on a Linux host too.
  - **The agent's MCP token comes in through `PASSDESK_TOKEN` from
    `.env`**, and the docs register the server at user scope with the
    header reading `${PASSDESK_TOKEN}`, so the token is in neither a
    config file nor the compose file. It is the one secret the agent must
    hold; it grants only its own tools.
  - **`BROKER_INSECURE_BIND=1` on the gateway**, since its port is not
    published and only the internal network reaches it; the doctor's
    warning says what to do if that ever changes.
  - **Only `passdesk` has a `build:`; `egress-guard` reuses the tag with
    `pull_policy: never`.** Two services building the same tag in parallel
    collided in the containerd image store.
  - **Not verified here:** a native Linux engine, Docker Desktop for Mac,
    and a real push (no GitHub token was used). The docs' "checked on"
    line says so; doomtickle/passdesk-ops#78 covers the real push.

- **Egress guard: `python3 -m passdesk egress-guard` (doomtickle/passdesk-ops#57,
  Phase 3a W3.3).** A CONNECT-only allowlist proxy (`passdesk/egress_guard.py`,
  stdlib `socketserver`/`selectors`) for an agent container's model traffic
  in a standalone deployment, shipped in the Passdesk image so there is one
  pin. It accepts `CONNECT host:443` for exact hosts in
  `[egress_guard].allow_hosts`, resolves the name itself, refuses any
  non-global-unicast address, connects to the exact address it checked, and
  relays bytes without inspecting them. Judgement calls:
  - **The allowlist is a policy table, not a flag.** It is baked into the
    image like every other policy, overridable through `policy.local.toml`
    at build time and never from the workspace or the command line; the
    doctor validates it in both modes. Entries are bare host names; `:443`
    is implied and a `host:port` entry refuses to start, so there is one
    form to get right.
  - **Default hosts:** Claude Code's `api.anthropic.com` plus the three
    subscription sign-in hosts (`platform.claude.com`, `claude.ai`,
    `claude.com`), since the quickstart audience signs in with a Pro/Max
    subscription, and Codex CLI's `chatgpt.com`, `auth.openai.com`,
    `api.openai.com` (W0 Q1). None of the optional telemetry, update or
    package hosts: an agent that can CONNECT to `registry.npmjs.org` can
    fetch packages around Passdesk. Cursor is host-only (owner decision),
    so no wildcards exist at all.
  - **"Global unicast" is `ipaddress.is_global` minus multicast
    (which Python counts as global), minus every IPv6 form that embeds an
    IPv4 address (v4-mapped, 6to4, Teredo), since those reach the embedded
    address through whatever tunnel exists.** A name with one bad address
    among good ones is refused outright rather than filtered, because a
    resolver that returns a private address for a public name is itself
    the attack.
  - **No second lookup.** `connect()` takes the `(family, sockaddr)` pairs
    `resolve()` checked and never the name; a test makes `getaddrinfo`
    raise after resolution and the tunnel still works.
  - **Reads are unbuffered** (`rbufsize = 0`), so bytes a client sends right
    after its headers are relayed, not swallowed by a read-ahead buffer.
    Costs one syscall per header byte, on an 8 KiB-capped read.
  - **Limits:** request read 10 s and 8 KiB (1 KiB request line), upstream
    connect 10 s, idle tunnel 600 s (model responses can pause; a shorter
    limit would cut long generations), 64 concurrent connections, then
    503. All are module constants, with `--max-connections` on the CLI.
  - **Logging:** one stderr line per refusal (status, reason, target,
    client) and per tunnel (host, address, client); never a byte of
    payload. The guard writes no audit record: it is not a tool call, and
    its container log is the record.
  - **No HTTP/2, no `Proxy-Authorization`, no plain `GET` proxying.** The
    clients from Q1 use plain CONNECT through `HTTPS_PROXY`; anything else
    is attack surface.
- **Optional TLS on the MCP listener, and standalone refuses a non-loopback
  bind without it (doomtickle/passdesk-ops#56, Phase 3a W3.2).**
  `BROKER_TLS_CERT`/`BROKER_TLS_KEY` wrap the listener with the stdlib `ssl`
  module (`protocol.make_server`, TLS 1.2+); `config.validate()` refuses a
  `BROKER_BIND_HOST` that isn't loopback when `BROKER_DEPLOYMENT=standalone`,
  no TLS pair is set and `BROKER_INSECURE_BIND` isn't exactly `1`. The
  doctor's W1.3 TLS stub became `check_tls`. Judgement calls:
  - **The refusal applies only to standalone.** The issue offered "or the
    factory sets the flag in the same change set". sbx's default bind is
    `0.0.0.0` with no TLS, serving the sandbox's own port publishing; making
    the factory set `BROKER_INSECURE_BIND=1` would have needed a factory
    change and a pin bump for no gain in that deployment. `test_tls.py`
    pins that `sbx` + `0.0.0.0` + no TLS still validates.
  - **`BROKER_INSECURE_BIND` is exactly `"1"`.** `true`, `yes` and `0` are
    all off, so a stray value can't switch the check off by accident.
  - **"Loopback" means a loopback IP literal, or a name every address of
    which is loopback** (`config.is_loopback`; `localhost` qualifies via
    `getaddrinfo`, which reads `/etc/hosts`). An empty bind host (every
    interface) and an unresolvable name are not loopback. The doctor's
    own `_is_loopback`, which took `localhost` on trust, is replaced by it.
  - **The handshake runs in the connection's thread, not in `accept()`.**
    `wrap_socket(..., do_handshake_on_connect=False)` plus `do_handshake()`
    in `Handler.setup()` under a 10-second timeout; otherwise one client
    that connects and never speaks would hold the serving thread and every
    other connection with it. A failed handshake is one stderr line
    (`Server.handle_error`), not a traceback.
  - **The context is built before the port is bound**, so a bad pair never
    takes the port; the exit message names both paths (an `OSError` from
    `load_cert_chain` leaves the path out) and never key material.
  - **A TLS file inside the agent workspace is refused at start-up**
    (resolved path, so a symlink into it counts), as credential files
    already are: the key must not be something the agent can write.
  - **No expiry check in the doctor.** The stdlib has no public certificate
    parser short of a connection; `--live` now probes `https://` trusting
    `BROKER_TLS_CERT`, which fails on an expired certificate anyway. The
    `--live` probe trusts that file alone; the GitHub calls keep the system
    store.
  - **The recipe is a self-signed EC P-256 certificate with SANs for
    `passdesk`, `localhost` and `127.0.0.1`**, the names the compose
    quickstart and the doctor use. No ACME (plan: W3.2).
- **Plugins can add tools, declaratively, behind the gateway's own checks
  (doomtickle/passdesk-ops#83).** Users should be able to connect the
  services they use without a core change, so core gains one general hook:
  a plugin module sets `PASSDESK_TOOL_NAMESPACE` and `PASSDESK_TOOLS` (a dict
  of `registry.PluginTool`), and `plugins.load()` validates and registers
  them before `activate()`. Choices:
  - **Declared, not registered by the plugin.** A module attribute, not a
    call from `activate()`, so the doctor and `python -m passdesk principal`
    can read and validate what a plugin offers without activating it. Those
    two would otherwise call a granted plugin tool "unknown". Core owns
    registration, so there is no runtime registration API.
  - **Every plugin tool runs behind `registry.guard`,** which the plugin
    can't skip. It secret-scans every string argument before the handler
    runs. It sends a `writes_externally` tool through the approval point
    with the card `{<arguments>, "tool", "principal"}`, the gateway's
    fields set last and `tool`/`principal` refused as argument names, so an
    agent can't rename the card and slip past Pro's `[approvals] tools`
    match (found in the security review) and gives it an `intent` audit record. It
    cleans the result and wraps it as untrusted, and it cleans a `Denied`
    message too, since a plugin's refusal may quote a remote service.
    Grants, argument checks and audit records are the protocol layer's, as
    for core tools. Scanning *every* string argument is broader than core
    tools, which scan named fields. A plugin can't tell the gateway which
    fields leave the machine, so all of them count.
  - **Names can't collide with core.** A namespace of 2-16 lowercase
    characters, never a core tool prefix or a name that reads as the
    gateway; one plugin per namespace; every tool name is
    `<namespace>_<words>`. `register()` still refuses any duplicate.
  - **Only `string` and `boolean` arguments,** with an optional
    description and nothing else, because those are the types `call_tool`
    enforces. A schema keyword like `enum` would be advertised but not
    checked.
  - **`hosts` is declared for the doctor's egress check only.** The
    gateway can't confine a plugin's network use in-process. In sbx, the
    kit allowlist is the backstop, and the doctor now counts declared
    plugin hosts.
  - **Trust model, stated plainly in `docs/plugin-tools.md`:** a plugin is
    operator-installed code in the gateway's process. The guard protects
    the agent boundary. It doesn't contain a malicious plugin.
  - Credentials: plugin tools read theirs only through `credentials.get`,
    under a name of their own (`CLAUDE.md` updated to say so). Until every
    value `credentials.get` returns is remembered for redaction
    automatically, the guide tells plugin authors to call
    `filters.remember_secret`.
  What would change this: a need for richer argument types (extend
  `call_tool`'s checks first, then allow them here), or for loading plugins
  from somewhere other than the image (not planned: policy and code stay
  baked in).
- **Standalone image: a second Dockerfile target (doomtickle/passdesk-ops#55,
  Phase 3a W3.1).** `--target standalone` builds Passdesk on
  `python:3.13-slim-bookworm` (pinned by its multi-arch index digest) plus
  `git` and `openssl`, installed read-only to `/opt/passdesk`, running as
  a non-root `passdesk` user with `BROKER_DEPLOYMENT=standalone` and the
  state/workspace/secrets paths set in the image. The sbx image is the
  same stage as before, now named `sbx` and kept **last** so ai-code-factory's
  plain `docker build` (no `--target`) still gets it; `tests/test_dockerfile.py`
  pins that order. Judgement calls:
  - **The `factory-config` and `plugins` build contexts get default
    stages of the same name** (an empty overlay, an empty directory). A
    `--build-context NAME=DIR` overrides a stage of that name (BuildKit
    documents this; checked on Docker 29.1.2 both ways), so the factory's
    build is unchanged and `docker build` with no contexts now works for
    either target. The alternative, keeping "pass empty contexts or the
    build fails", would have made the compose quickstart (#58) carry two
    throwaway directories.
  - **`git` and `openssl` come from Debian's repositories, not pinned by
    digest.** CLAUDE.md asks to pin the base by digest and flag what can't
    be: apt has no digest pin short of a snapshot.debian.org mirror, which
    is slow and rate-limited. The base digest is the pin; the build log
    prints the versions installed. `openssl` is there so a user can mint
    the self-signed certificate for TLS (W3.2) inside the container, with
    nothing on the host.
  - **Python 3.13, not 3.14 (the sbx base) or 3.11 (the floor).** The newest
    release the tests have run on here and in the sbx image's neighbourhood;
    3.14's slim image is newer than the rest of the stack has been checked
    against. CI runs the suite inside this image, so a bump is one digest
    and a green run.
  - **uid 1000 by default, overridable with `--build-arg`.** The gateway
    writes the bind-mounted workspace's exchange directory; on a native
    Linux host that needs a matching uid, and 1000 is the first user on
    most distributions. Docker Desktop (Mac, Windows) ignores it.
  - **No `run.sh` loop and no HEALTHCHECK in the image.** The container
    runtime restarts the process; a healthcheck depends on whether TLS
    (W3.2) is on, so it belongs in the compose file (#58), not the image.
  - **CI builds the standalone target with no build contexts** (so the
    default stages are what get tested) and runs the whole test suite inside
    it as the non-root user, after unsetting the image's `BROKER_*` defaults
    (the tests assume a bare checkout's environment). Publishing is #59.

- **`credentials.get` takes an `on_unusable` callback (doomtickle/passdesk-ops#53,
  follow-up on W1.3).** `check_secrets`'s and `check_standalone`'s fallback
  calls to `credentials.get()` could reach `_Unusable` (a directory where a
  credential file belongs, a control character in a value) and, through the
  default `_unusable`, write a `credential_unusable` row to the audit log --
  breaking the doctor's "it changes nothing" promise (`doctor.py:7`) and
  hiding the reason from the operator (the doctor only ever saw "no `name`
  credential"). `get(name, on_unusable=...)` now takes the refusal callback
  as a parameter, default unchanged (`_unusable`, today's audit write); the
  doctor passes one that collects `reason` into a list and shows each as a
  `warn` instead. Kept minimal and generic rather than doctor-specific:
  - `on_unusable(credential_name, provider, reason)` matches `_unusable`'s
    own signature, so the default value doubles as the callback's type.
  - Only the `file` provider's own multi-path fallback (secrets-dir path,
    then the Docker secret) needed to learn about the parameter -- it is
    the only built-in that catches `_Unusable` and keeps looking rather than
    letting it propagate to `get`'s own handler, which already reads
    `on_unusable` uniformly for every provider.
  - `reason` strings never carry a secret value today (paths and fixed
    messages only; see `read_secret_file`'s docstring), so passing them to
    an alternate sink needed no new redaction.
  Also in `doctor.py`, three related gaps the same read fixed or exposed:
  the GitHub fix-hint under the default provider order
  (`proxy,file,env-file`) told an operator to deliver a token through
  `file`/`env-file`, which `proxy` running first makes unreachable for
  "github" regardless -- the hint now checks `provider_order()` and, when
  `proxy` precedes another provider, names dropping `proxy` from
  `PASSDESK_CREDENTIAL_PROVIDERS` instead; a bad
  `PASSDESK_CREDENTIAL_PROVIDERS` is looked up once in `run()` and threaded
  into `check_standalone`/`check_secrets` as `order_error` so it is reported
  once, not once per credential checked; and `check_standalone`'s exchange
  directory check now reads `os.lstat` (as `check_workspace` already did)
  instead of the symlink-following `Path.is_dir()`, so a symlinked
  `.factory/` -- which `check_workspace` already fails -- no longer also
  gets an incongruous "is writable" `OK` from `check_standalone`; the same
  check now covers `incoming/`/`outgoing/` under the exchange directory,
  not just the exchange directory itself.

- **Doctor checks for standalone mode (doomtickle/passdesk-ops#53, plan task
  W1.3).** With `BROKER_DEPLOYMENT=standalone`, `doctor.run()` no longer
  calls `check_egress` (the sbx-kit `--kit` comparison makes no sense
  without an sbx kit) and instead calls a new `check_standalone`: a GitHub
  credential is present through `core/credentials.py` (never printed; just
  present/absent and the provider list checked), the exchange directory is
  writable, and — since TLS itself (plan task W3.2) hasn't landed — a stub
  `info` line when `BROKER_BIND_HOST` is off loopback, naming W3.2 rather
  than inventing a `BROKER_TLS_CERT`/`BROKER_TLS_KEY` pair to check against.
  sbx mode is unchanged (still runs `check_egress`, never runs
  `check_standalone`); this narrows the prior entry below ("`BROKER_DEPLOYMENT`
  gates wording, not behavior") to cover only the GitHub-hint text, since the
  doctor's checks themselves now do differ by deployment. Judgement calls:
  - **The GitHub check reports the real file-based credential's
    presence/absence**, because standalone's default
    `PASSDESK_CREDENTIAL_PROVIDERS` order is `file,env-file` (W2.2, landed):
    `proxy` is left out by default, so `credentials.get("github")` reaches
    the `file`/`env-file` providers instead of returning
    `credentials.INJECTED` unconditionally. The check calls the public
    `credentials.get("github")`/`provider_order()` faithfully rather than
    special-casing `DEPLOYMENT` itself; the "proxy runs first" fix-hint
    (naming dropping `proxy` from `PASSDESK_CREDENTIAL_PROVIDERS`) now only
    fires when a deployment's own setting explicitly puts `proxy` back in
    front of `file`/`env-file`.
  - **Also fixed, in both modes: `check_secrets` only looked at the three
    configured paths** (`BROKER_JIRA_SECRET` etc.), so it reported "no
    credential" when the only copy was a Docker secret
    (`/run/secrets/<name>`) or behind `PASSDESK_SECRET_<NAME>_FILE`. It now
    calls `credentials.get(name)` once the configured path itself is
    missing, before reporting absence, and reports a `CredentialError`'s
    message (never a value) instead of letting it propagate. The file-level
    checks that only make sense for the configured path itself (regular
    file, loose permissions, "is empty", the jira `email:token` format) are
    unchanged and still run first when that path exists, so sbx mode's
    report for every case already covered by `tests/test_doctor.py`'s
    `test_secrets` is pinned identical; `SbxModeUnaffected` in the same file
    pins the healthy-deployment report's section order and OK lines too.
  - **The exchange-writability and TLS-stub checks are standalone-only**,
    not folded into the shared `check_workspace`/`check_egress`, so sbx
    mode's output is untouched by their addition rather than merely
    "usually unchanged."

- **In standalone mode the gateway holds the GitHub token
  (doomtickle/passdesk-ops#54, Phase 3a W2.2).** Owner decision (Phase 3a
  owner decisions in passdesk-ops/decisions.md): with no egress proxy to
  inject it, the gateway reads a `github` credential through
  `credentials.get` like Jira's; users get fine-grained tokens limited to
  chosen repos with Contents and Pull requests write only. The gateway is
  still outside every agent sandbox, so credentials still never enter one.
  With sbx nothing changes: `proxy` answers `github` first, and every code
  path with no token (git environment, `urllib.request.urlopen`, hints) is
  exactly what it was. Judgement calls:
  - **Standalone's default provider order leaves out `proxy`**
    (`file,env-file`); an explicit `PASSDESK_CREDENTIAL_PROVIDERS` always
    wins. The alternative, a separate switch for "gateway holds GitHub",
    would be one more setting to get wrong; standalone already means "no
    sbx proxy". Changed in `core/config.py` only (plus one docstring line in
    `credentials.py`).
  - **git gets `Authorization: Basic base64("x-access-token:<token>")`, not
    Bearer.** Basic with `x-access-token` is the form GitHub documents for
    git over HTTPS with every token type (classic, fine-grained, App
    installation) and the one actions/checkout uses; GitHub's acceptance of
    Bearer on the git smart-HTTP endpoints is not something we could verify
    offline. The REST API call uses `Bearer`, as GitHub documents for it.
  - **The header is `http.https://github.com/.extraHeader`, passed only as
    `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n` in the child's environment**, so it
    is never in argv (`/proc/<pid>/cmdline` is world-readable) or a config
    file, and git sends it to no other host. The environment
    (`/proc/<pid>/environ`) is readable only by the gateway's own user,
    which holds the token anyway. Only the three commands that talk to
    GitHub (`git_fetch`'s fetch, `git_push`'s base fetch and push) get it.
  - **git runs with `http.followRedirects=false` and `credential.helper=`
    (empty) when it carries the token**, matching "authenticated requests
    never follow redirects". Cost: a renamed repository's old URL (GitHub
    answers with a redirect) now fails instead of following; the error says
    302/301 and the fix is the new name in `[git].repos`.
  - **Trace switches are removed, not just turned off.** Every inherited
    `GIT_TRACE*`, `GIT_CURL_VERBOSE` (honoured whatever its value) and
    inherited `GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_KEY_n`/`VALUE_n` are
    dropped from that child's environment (`workspace.run`'s `extra_env`
    now treats a None value as "unset"). A test sets `GIT_TRACE_CURL=1` and
    `GIT_TRACE_REDACT=0` and checks git's raw output holds no header.
  - **Redaction knows the live value.** `filters.remember_secret` keeps the
    exact token and the base64 header value (newest 32 values, so a rotated
    token stays redacted for a while); `redact()` replaces them before the
    patterns run, and `find_secrets()` reports them as `live-credential`, so
    every existing path (`sanitize` on git stderr and API errors, the audit
    log's `summarize_args`, approval cards, outbound PR text) covers a token
    that matches no pattern. The value is remembered when it is read, so
    anything git or GitHub could echo comes after. It lives in
    `tools/git.py` for now; doing it in `credentials.get` would cover Jira,
    Gemini and npm too (left for a follow-up, to keep this change out of
    `credentials.py`).
  - **A `github` value that isn't 20-255 of `[A-Za-z0-9_]` is refused**
    (Denied, without the value): every GitHub token format fits, and it
    rules out anything that could break out of a header or config value.

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
- **Principal CLI added (doomtickle/passdesk-ops#36, plan task W1.2):
  `python3 -m passdesk principal add|list|revoke` (`passdesk/__main__.py`,
  `passdesk/principal.py`).** Mints a token, writes its SHA-256 hash to
  `tokens/<name>.sha256` and an object-form grant to `grants.json` — uses
  the same files and formats `ai-code-factory/scripts/start-broker.sh` writes
  (in a factory deployment, `start-broker.sh` owns `grants.json` and rebuilds
  it on every start). Judgement calls:
  - **Writes only `grants.json`, never the legacy `roles.json`, but reads
    for the duplicate check and as the read-modify-write baseline via
    `core.auth.grants_path()`** (`grants.json` if present, else the older
    `roles.json`), not a `grants.json`-only read. Without this, a
    deployment that has only ever run `start-broker.sh` (so only
    `roles.json` exists) would see `add` start from an empty baseline: the
    first CLI `add` would write a `grants.json` containing only the new
    principal, and since `grants_path()` prefers `grants.json` once it
    exists, every grant `start-broker.sh` had written to `roles.json`
    would stop taking effect from that point on. Reading through
    `grants_path()` carries those entries into the new `grants.json`
    instead.
  - **This CLI is for a standalone/non-factory deployment, or for a
    principal the factory doesn't manage, not for day-to-day use against a
    factory-managed deployment.** In an `ai-code-factory` deployment,
    `scripts/start-broker.sh` rebuilds `grants.json` from scratch on every
    factory start (`grants='{}'`, then one entry per role with
    `BROKER_TOOLS` — a full overwrite, not a read-modify-write); the fix
    above stops the CLI's own `add`/`revoke` from stripping grants, but it
    does not stop the factory's own next start from resetting `grants.json`
    to exactly what `roles/*/role.conf` says, discarding any principal the
    CLI added in between (the token file under `tokens/` survives, since
    `start-broker.sh` never deletes those, but the principal loses its
    grant and every call is denied). Making `start-broker.sh` read-modify-write
    or otherwise aware of CLI-managed principals is a separate follow-up,
    not done here.
  - **`add` refuses to overwrite an existing principal** (token file or
    grants entry) rather than rotating it, so a bare retry can't silently
    invalidate a token already handed to an agent; the caller must
    `revoke` first. `revoke` is all-or-nothing (removes whatever of the
    token/grant pair exists) rather than requiring both to be present,
    since a hand-edited or partially-migrated state may have only one.
  - **Principal name charset is `^[a-z][a-z0-9_-]{0,63}$`**, stricter than
    strictly necessary to block path traversal (no `.`, `/` or NUL) -
    chosen to match the factory's own role-name convention
    (`ai-code-factory/scripts/lib/roles.sh:valid_role_name`) rather than
    inventing a separate rule two tools would need to agree on by
    coincidence.
  - **No locking across concurrent CLI invocations.** `add`/`revoke` do a
    read-modify-write of `grants.json` with no file lock, matching
    `start-broker.sh`'s own non-atomic read-modify-write of the same file;
    this CLI is for an operator running one command at a time, not
    concurrent automation.

- **Paths and names made configurable (doomtickle/passdesk-ops#36, plan task
  W1.1).** `BROKER_SECRETS_DIR` (default `/etc/factory-broker/secrets`) is a
  new base for the three per-secret path defaults; `BROKER_JIRA_SECRET` etc.
  still override individually on top of it, unchanged otherwise.
  `BROKER_REF_NAMESPACE` (default `refs/factory`) replaces the hard-coded
  `refs/factory/` prefix `git_push` stages an incoming branch under in its
  mirror. `BROKER_PUSH_PREFIX` (default `factory/`) is a new fallback for
  `[git].push_branch_prefix`, used only when a policy omits the key (every
  `policy.toml` this repo ships sets it, so this only matters for a
  deployment's own from-scratch policy). Judgement calls:
  - **`BROKER_PUSH_PREFIX` is a code-level fallback, not a second way to set
    the policy value.** `push_branch_prefix` itself stays policy (baked into
    the image, overridable only via `policy.local.toml` at build time, per
    CLAUDE.md's "Policy is baked into the image" rule); the env var only
    changes what `check_push_branch` falls back to when a deployment's own
    from-scratch policy doesn't mention the key, so a standalone deployment
    doesn't have to write a policy file just to rename the convention.
  - **The fallback's default (`factory/`) is a deliberate behaviour change
    from upstream, not a neutral port.** Upstream falls back to `""` (any
    unprotected branch) when `[git].push_branch_prefix` is absent; this repo
    fails closed instead, since an absent key is far more likely to be an
    oversight in a from-scratch policy than an intentional "no prefix"
    choice. The doctor (`doctor.py`'s `_check_policy_shape`) now warns on a
    *missing* key (naming the `BROKER_PUSH_PREFIX` fallback in effect) as
    well as on a key present but set to `""`, so neither case is silent.
  - **`BROKER_DEPLOYMENT` (`sbx` default, or `standalone`) gates wording,
    not behavior.** It only selects whether the `sbx secret set github
    --sandbox factory-broker` hint appears in `git.py`'s and `doctor.py`'s
    GitHub-credential error/fix text; every check still runs the same way
    either value is set.
  - **Validation lives in `config.validate()`, called from
    `protocol.main()` before the directory/policy checks already there, and
    from `doctor.py`'s `run()`**, not at module import: tests already
    override `config.NAME` attributes directly after import (see
    `core/config.py`'s own docstring), so import-time validation would run
    against stale values in that pattern.
  - **Secrets refactor stayed minimal on purpose.** Another lane is building
    `core/credentials.py` (secret providers) concurrently; this task only
    added `SECRETS_DIR` as a shared default for the three existing path
    variables, with no change to how or where the files are read.
  - **`BROKER_REF_NAMESPACE` must not be, or sit under, `refs/heads`,
    `refs/tags`, `refs/remotes`, `refs/notes` or `refs/stash`, not just
    "under `refs/`".** A namespace like `refs/remotes/origin` would still
    match `^refs/...`, but `git_fetch`'s bundle creation (and anything else
    that walks `refs/remotes`) would then see an unverified,
    pending-approval push as origin's own ref. `config.validate()` also
    checks each `/`-separated component against `git check-ref-format`'s
    two cheapest rules (no leading `.`, no trailing `.lock`) rather than
    reimplementing the full command; the existing whole-string checks
    (no `..`, no `//`, no trailing `/` or `.`) are unchanged.
  - **`BROKER_SECRETS_DIR` must be absolute, not just free of `..`.** A
    relative or empty value would resolve each secret path (e.g.
    `BROKER_SECRETS_DIR=` making `JIRA_SECRET_PATH` resolve to `/jira`)
    against whatever the broker's current working directory happens to be
    at call time, which is not a directory this security boundary should
    depend on.

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
