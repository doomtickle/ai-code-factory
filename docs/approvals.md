# Design note: approvals

Nothing an agent pushes or opens as a pull request leaves the machine until
a person has approved it. This note fixes the shape; the decision log
records what changed on the way. The hold started life as the commercial
Passdesk Pro plugin and moved into core, licence removed, on 2026-10-21
(`decisions.md`); the file layout and the decision contract did not change.

## The approval point and the plugin seam

- **An approval point**, `core/approvals.py`: `require(card)` is called by a
  tool right before its external write, with a card describing what is about
  to happen. Core's default returns immediately. A deployment may install a
  hook with `set_hook(fn)`; the hook returns to allow or raises `Denied`.
  When it allows a write that was held, it may return the outcome,
  `{"id", "approver", "decided", "waited_seconds"}` (core's hold does);
  `require` passes that back to the tool, and returns `None` for anything
  else (no hook, a write that wasn't held, a plugin hook that returns
  `None`), so a plugin written against the old contract still works.
- **The caller's identity** for the card: `PRINCIPAL`, a context variable
  the protocol layer sets for each call.
- **Plugin loading**, `core/plugins.py`: `PASSDESK_PLUGINS` names modules
  (comma-separated) imported at start-up, after the policy loads. Each
  plugin registers what it provides (today: an approval hook). The image
  takes an optional `plugins` build context on `PYTHONPATH`; with nothing
  named, nothing loads and the gateway behaves exactly as before.
- **The call sites.** `git_push` builds its card after every check has
  passed (bundle verified, every commit secret-scanned, limits applied) and
  calls `require` *outside* the tool lock, so a pending approval never
  blocks another role's git call; it then re-takes the lock, confirms the
  ref it validated is unchanged, and pushes. `github_create_pr` and
  `gitlab_create_mr` call `require` after the title and body pass the
  secret scan (`gitlab_create_mr` also after confirming the gateway holds a
  `gitlab` token), and
  `github_create_issue` after its title, body and labels pass it and the
  labels are confirmed to exist (its card carries a `body_sha256`). The
  hold covers only the tools `[approvals] tools` lists; nothing is held
  until that list names something (the usual list is `git_push`,
  `github_create_pr` and `gitlab_create_mr`; add `github_create_issue` to
  hold issues too).

- **The patch to inspect** (doomtickle/passdesk-ops#113). Only when a hook
  is installed, `git_push` writes the pushed commits' patch to
  `STATE/approvals/patches/<sha256>.patch` while it still holds the tool
  lock, from the same mirror and range it scanned:
  `git log -p --stat --cc --reverse --full-index --no-ext-diff --no-textconv --no-color`
  with the format `commit %H%nAuthor: %an <%ae>%nDate:   %aI%n%n%w(0,4,4)%B%n`,
  so every message line is indented four spaces and only a real header
  starts at column 0. Renames are detected (git's default); there is no
  `-a`, so a binary file is git's one `Binary files ... differ` line. The
  text (UTF-8, git's output decoded with replacement) goes through
  `filters.redact` one line at a time (so redaction never merges lines),
  then is cut at the last newline at or before
  `[approvals] patch_max_bytes` (default 262,144; 4,096 to 2,000,000),
  followed by one line `[passdesk: patch truncated at N of M bytes]`. The
  file is written atomically, mode 0644, named by the sha256 of its bytes,
  so the same bundle gives the same name. Before each write, and on each
  idle sweep (below), core removes every patch that no
  `approvals/pending/*.json` card names and that is over two hours old
  (never following a symlink, ignoring unreadable cards). The card gains three fields and no content: `patch_sha256`,
  `patch_bytes` (the file's size) and `patch_truncated`. The webhook, the
  audit log and any future relay see only those; the patch itself is read
  by a console on the same machine (the AI Code Factory's dashboard), which
  checks the file's digest against the card. Without a hook, nothing is
  written and the card is as before. If the patch cannot be built or
  stored, the push is refused (fail-closed) rather than held without it.

The card never carries a credential or content, and nothing in it comes
from the network: it is made of what the gateway itself verified. `require` also
redacts any known secret pattern (`core/filters.py`'s `SECRET_PATTERNS`,
e.g. a `ghp_...` token) found in a string field - recursively through lists,
dicts and tuples, non-string values untouched - before the hook sees the
card, in case a credential-looking string reached a field like the PR
title. Only the redacted copy is shown to a hook; the write itself, which
runs only after `require` returns, still uses the original arguments; only
the copy shown to the hook is scrubbed.

**A `[REDACTED:...]` marker on a card means the real write contains that
string, and an approver should decline it.** In practice it should never
appear: every agent-supplied card field is refused by `block_outbound_secrets`
before `require` is called - the PR and MR title and body (`tools/git.py`,
`tool_github_create_pr`, `tool_gitlab_create_mr`), and every ref name
(`check_branch_name`: the push branch, the PR/MR head, and the base of any), so a branch literally named
`factory/ghp_...` is denied rather than held. File names are covered by the
per-commit patch scan. The redaction in `require` is defense in depth for a
pattern that slipped past those checks, not the first line.

The same holds for the card id: two pushes whose cards differ only in a
redacted substring would get the same id (`core/holds.py`'s `card_id`
hashes the card it is given - the redacted one), but a card that
needed redacting is one the refusals above should have stopped, and the
content that actually gets pushed is still pinned by `head` and
`bundle_sha256`, which redaction never touches.

## The hold (`core/holds.py`, `core/notify.py`)

- **Switched on by policy.** At start-up, after the policy loads and before
  plugins, `holds.install` puts the hook on the approval point if
  `[approvals].tools` is a non-empty list, and records `approvals-enabled`
  with the settings. Absent or empty, nothing is installed: no hook, no
  patch files, no holds, exactly the gateway as it was without the plugin.
  A plugin that installs its own hook replaces this one.
- **The hold.** The hook computes a card id (a hash of the card's stable
  fields, including the bundle's digest), looks for a decision with that id,
  and otherwise records the card as pending, notifies, and waits.
- **A short hold, then the queue** (doomtickle/passdesk-ops#91). An MCP
  tool call cannot stay open for as long as a person may take: some clients
  cut a call at 60 seconds. So the hook waits up to `hold_seconds` (default
  45) for a decision. Approved: the write proceeds in the same call, exactly
  as before. Declined: the agent gets `DENIED` with the approver's reason.
  Still undecided:
  - **`git_push`, `github_create_pr` and `gitlab_create_mr` are queued.**
    Each passes `require` a *job* next to its card: the arguments of its
    write, every one already checked (for a push: repo, branch, base, the
    verified head and the bundle's digest; for a PR or MR: repo, head, base,
    title as sent, the whole body, draft), and registers a completer
    (`approvals.register_deferred`). The hook stores the job as
    `approvals/queued/<id>.json` (`id`, the card's `nonce`, `tool`,
    `queued`, `job`; no credential) and raises `Queued`, which the protocol
    layer returns as an ordinary result, not an error (the call's audit
    record says `decision: queued` and `approval: <id>`):

    > Queued for approval (id 0123456789abcdef); nothing has been sent yet.
    > The gateway has verified this push of factory/x to acme/widgets and
    > will make it itself once a person approves it (e.g. `factory approvals
    > approve 0123456789abcdef`); if it is declined, or not decided by
    > 2026-10-23T09:15:00Z (in 23 h 59 min), nothing is written. Report it
    > as queued, not done. Calling this exact tool again with the same
    > arguments reports its status, and its result once the gateway has made
    > it.

    The verified ref stays where `git_push` put it, in the gateway's
    private mirror (`<REF_NAMESPACE>/<branch>`). On approval the gateway
    makes the write itself, from the sweeper (every 5 seconds) or from a
    retry of the same call, whichever comes first: in one locked step it
    files `done/<id>.json` as `running` and removes the card, decision and
    job, so the write is claimed exactly once (a retry that finds no card
    checks for that record under the same lock before making a new card,
    so it reports a write claimed meanwhile rather than asking again). The
    completer then checks the job again against the current policy (repo
    allowlist, branch prefix and
    protection, secret scan of a title and body) and against the stored
    card (same `head` and `bundle_sha256`; same title, body and draft; the
    card's own fields still hash to its id), and only then writes. A push
    re-takes the tool lock, needs the mirror to still exist and its ref to
    still point at the card's head, secret-scans that range again (so even
    a hand-made job can only push what passes the scan) and pushes exactly
    as `git_push` does: hooks and repo config off, the credential only in
    one child's `GIT_CONFIG_*` environment. A PR or MR is opened from the
    stored arguments exactly as the tool opens it, the token read at that
    moment. Nothing reads the agent's workspace again. Any failed check, a
    missing credential or the forge's refusal means nothing is written, and
    the failure is recorded.
  - **Any other tool** (`github_create_issue`, a plugin's tool) keeps the
    old behaviour: a denial that names the approval id and asks the agent
    to retry the same call once a person has decided. A retry with the same
    arguments has the same id, so a decision made in between is honoured.
- **A retry of a queued write.** The same call again (same card id) never
  queues or writes twice. While the card is undecided it gets the same
  queued result. Approved but not yet made, the retry makes it in that call
  and gets the result; while the gateway is making it, `Approved (id X);
  the gateway is making this write now. Call this exact tool again with the
  same arguments in a minute for its result; until then, report it as
  queued, not done.` Once made, the retry gets the write's result and the
  held line with `; written by the gateway at T` added, for
  `expire_seconds` after the write; after that the same call is a new
  request. Declined, it gets `DENIED ... declined by NAME: REASON (approval
  X, queued)` for the same window. Failed, it gets the failure once
  (`the gateway could not make the approved write (id X): ...`); the call
  after that starts a new approval. Expired, it starts a fresh card, as
  before.
- **The done file of a queued write** is the card plus the decision
  (`decision`, `approver`, `reason`, `decided`) and `queued: true`,
  `write` (`running`, `done`, `failed` or `skipped` for a decline or
  expiry), `result` (the write's result or why it failed, filtered like
  any result, at most 3,000 characters), `finished` (Unix time) and, once
  approved, `outcome` (the held line's fields). A console reads it to show
  what became of an approval. A write still `running` when the gateway
  starts was cut off by a restart: it is filed as `failed` ("it may or may
  not have reached the forge"), never retried.
- **A restart while queued** loses nothing: the card, the job and the
  mirror's ref are files. At start-up the sweeper makes any write approved
  meanwhile and files any card that expired meanwhile.
- **The result says it was held** (doomtickle/passdesk-ops#141). An
  approval that lands within the hold looks to the agent like a slow call,
  so every write that went through a hold (approved in the call, or picked
  up by a retry) ends its result with one more line: `Held for approval
  (id X); approved by "NAME" at 2026-10-22T09:15:00Z after N s.` `decided`
  is the decision's time in UTC and `waited_seconds` runs from the card's
  creation (the first held call) to the decision, so a retry reports how
  long the write really waited for a person; a decision or creation time
  that isn't a usable Unix time (not a number, not finite, out of range)
  counts as now. The approver's name comes from the console's file, so it
  is filtered like any result (secrets redacted, control characters
  removed), also loses Unicode format characters (bidi overrides, zero-width
  characters) and C1 controls, is quoted (a `"` in it becomes `'`) and is
  kept to one line of at most 100 characters. The line is added to every
  result the tool returns after approval, including a push the forge
  rejected or an HTTP error status from the API. A failure the tool
  doesn't turn into a result (no connection, a timeout) comes back as
  `Broker error: ...` without the line; then only the call's audit record
  says it was held, with `approval: <id>`. A write that wasn't held is
  unchanged, byte for byte.
  Pending cards expire after `expire_seconds` (default 86,400, a day;
  60 s to 604,800, a week); an expired card is a denial, and a later retry
  makes a fresh one.
- **Expired cards leave `pending/` on their own.** An expired card is
  audited as `approval-expired` and filed under `approvals/done/` as
  `expired` (with any decision that came too late), whether a retry of the
  same call finds it or a sweep over `pending/` does. The sweep runs inside
  every held call, once when the server starts (before it serves a call)
  and every 5 seconds on a daemon thread (which first makes the approved
  queued writes, so one approved just before its expiry is made), so a card
  left behind by an
  agent that never retried, or one that expired while the gateway was down,
  is filed without waiting for another held write. The idle sweep also
  prunes the patches that leaves unnamed. It runs only while core's hold is
  the installed hook (holds on, no plugin hook over it), and a failing
  sweep is logged by error type and retried on the next tick; it never
  stops the server or fails a held call.
- **Decisions are files.** The state directory is shared between the gateway
  and its deployment and with nothing else, so it is the channel:
  `approvals/pending/<id>.json` is the card, `approvals/decisions/<id>.json`
  is the decision (`approve` or `decline`, approver name, reason, time,
  and the card's `nonce`, so a decision never applies to a later card with
  the same id), written atomically by the deployment's console. No new
  listener, no new port, no new token: whoever can write to the state
  directory is the approver, which is the person running the machine. A
  networked approver API is the standalone deployment's job, later.
- **Notification.** One outbound POST per card to a webhook URL, and one
  more when the gateway has made (or failed to make) a queued write (`the
  gateway made the approved push of B to R: <first line of the result>`, or
  `... FAILED: ...`), with a
  plain-text summary under the keys Slack, Discord and ntfy each read, sent
  on its own thread so a slow webhook never shortens the hold. The URL is
  a credential (a Slack webhook URL grants posting), so it is the
  `approvals-webhook` credential, delivered like the others nothing can
  proxy-inject (`core/credentials.py`: a file in the gateway's own sandbox,
  a Docker secret, or `PASSDESK_SECRET_APPROVALS_WEBHOOK_FILE`), read fresh
  on every use. A failed notification is logged and never blocks or leaks
  the card.
- **The record.** Every step is an audit event: `approval-requested`,
  `approval-queued`, `approval-granted` (with the approver),
  `approval-declined`, `approval-expired` (with any decision that came too
  late; the last three with `queued: true` for a queued card), and for a
  queued write `approval-write-done` (with its result) or
  `approval-write-failed` (with the reason). The tool
  call's own record follows as today, and carries `approval: <id>` when
  the call went through a hold, also if the write then failed (its
  `decision` is `deny` or `error`), so a review of the log can tell a held
  write from an unheld one.
- **No licence.** There is nothing to deliver, verify or renew.

## What the deployment ships (`ai-code-factory`)

- `factory approvals`: list pending cards, show one, approve or decline it
  (with a name and a reason), and a watch mode. Plain files in the state
  directory; standard-library Python 3.9 for the macOS host. With the
  queue it should also mark a pending card that has a `queued/` job ("the
  gateway writes it on approval") and show what became of a queued write
  from its `done/` file (`write`, `result`).
- Credential delivery for the webhook URL, from the host keychain into the
  gateway sandbox, the same way as Jira and Gemini. Inert when absent.
- `[approvals].tools` in its local policy overlay, baked into the image.

## Not in this slice

A localhost approval page (the console covers it), per-approver identity
beyond a name, approvals for other tools, and an approver API for
deployments that share no state directory with the gateway.
