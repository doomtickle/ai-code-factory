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
  hook with `set_hook(fn)`; the hook returns to allow or raises `Denied`
  (core's hold raises `Staged` instead, see below). Whatever a hook
  returns is ignored; `require` returns `None`. Every tool that writes also
  passes `require` a *job*, the exact checked arguments of its write, which
  only core's hold reads (`approvals.job()` while the hook runs).
- **The caller's identity** for the card: `PRINCIPAL`, a context variable
  the protocol layer sets for each call.
- **Plugin loading**, `core/plugins.py`: `PASSDESK_PLUGINS` names modules
  (comma-separated) imported at start-up, after the policy loads. Each
  plugin registers what it provides (today: an approval hook). The image
  takes an optional `plugins` build context on `PYTHONPATH`; with nothing
  named, nothing loads and the gateway behaves exactly as before.
- **The call sites.** `git_push` builds its card after every check has
  passed (bundle verified, every commit secret-scanned, limits applied) and
  calls `require` *outside* the tool lock, so staging never blocks another
  role's git call; unheld, it then re-takes the lock, confirms the ref it
  validated is unchanged, and pushes. `github_create_pr`,
  `github_update_pr` and `gitlab_create_mr` call `require` after the title
  and body pass the secret scan (`gitlab_create_mr` also after confirming the gateway holds a
  `gitlab` token), and
  `github_create_issue` after its title, body and labels pass it and the
  labels are confirmed to exist. The PR, MR and issue cards show the
  body's first 2,000 characters and carry the whole body's `body_sha256`
  (the PR and MR cards also `body_chars`; a `github_update_pr` card has
  `title` and `body` only for what it changes, and the PR's `head` branch,
  which the gateway read from GitHub and checked: the PR must be from a
  branch of the repo itself starting with `[git].push_branch_prefix`,
  checked again before the write); the digest is part of the card
  id and checked against the job before the write, so two calls whose
  bodies differ only past the excerpt are different cards. The
  hold covers only the tools `[approvals] tools` lists; nothing is held
  until that list names something (the usual list is `git_push`,
  `github_create_pr` and `gitlab_create_mr`; add `github_update_pr` to hold
  title and body edits too, and `github_create_issue` to hold issues).

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
- **Staged, never waiting** (doomtickle/passdesk-ops#170). The hook
  computes a card id (a hash of the card's stable fields, including the
  bundle's digest) and stages the write at once: in one locked step it
  stores the tool's job as `approvals/queued/<id>.json` (`id`, the card's
  `nonce`, `tool`, `queued`, `job`; no credential), then the card as
  `approvals/pending/<id>.json`, so a card is never visible without its
  job. It notifies and raises `Staged`, which the protocol layer returns as
  an ordinary result, not an error (the call's audit record says
  `decision: staged` and `approval: <id>`). Nothing is sent from the call.
  This is the only result a held call gets:

  > Staged for approval (id 0123456789abcdef); nothing has been sent yet.
  > The gateway has verified this push of factory/x to acme/widgets and
  > will make it itself once a person approves it (e.g. `factory approvals
  > approve 0123456789abcdef`); if it is declined, or not decided by
  > 2026-10-23T09:15:00Z (in 23 h 59 min), nothing is written. Report it
  > as staged for approval (id 0123456789abcdef), not done. The gateway
  > records the outcome with the approval (`factory approvals`); once it
  > has made the write, the same call again returns its result (e.g. the
  > URL) and sends nothing new.

  Until 24 Oct 2026 a call first waited up to `hold_seconds` for a
  decision (approved in time, the write went out in the call), then
  queued a push, PR or MR and asked for a retry of anything else. That
  gave agents three result shapes; `hold_seconds` is now ignored and the
  doctor says to delete it.
- **What is staged.** Every tool that writes passes `require` a job and
  registers a completer (`approvals.register_deferred`): `git_push` (repo,
  branch, base, the verified head and the bundle's digest; the verified
  ref stays in the gateway's private mirror at `<REF_NAMESPACE>/<branch>`),
  `github_create_pr` and `gitlab_create_mr` (repo, head, base, title as
  sent, the whole body, draft), `github_update_pr` (repo, number, the new
  title and/or whole body; what was not given is absent and never sent),
  `github_create_issue` (repo, title, the
  whole body, the labels as GitHub spells them) and a plugin tool declared
  `writes_externally` (its arguments, `core/registry.py`). A held tool with
  no job or no completer is refused with nothing staged.
- **The gateway makes approved writes.** The sweeper (every 5 seconds)
  finds each decision: in one locked step it files `done/<id>.json` (as
  `running` for an approval) and removes the card, decision and job, so a
  write is claimed exactly once, however often the decision is written or
  the sweep runs. The completer then checks the job again against the
  current policy (repo allowlist, branch prefix and protection, secret
  scan of every text it sends) and against the stored card (same `head`
  and `bundle_sha256`; same title, body, labels, draft or plugin arguments;
  the card's own fields still hash to its id), and only then writes. A
  push re-takes the tool lock, needs the mirror to still exist and its ref
  to still point at the card's head, secret-scans that range again (so
  even a hand-made job can only push what passes the scan) and pushes
  exactly as `git_push` does: hooks and repo config off, the credential
  only in one child's `GIT_CONFIG_*` environment. A PR, MR or issue is
  opened from the stored arguments exactly as the tool opens it, the token
  read at that moment; a plugin tool's handler runs on the stored
  arguments, its output cleaned as in a call. Nothing reads the agent's
  workspace again. Any failed check, a missing credential or the forge's
  refusal means nothing is written, and the failure is recorded.
- **The same call again** (same card id) never stages or writes twice.
  While the card is pending (decided or not) it gets the same staged
  result and no second notification. Once the write is made it gets that
  write's stored result, filtered again, as an ordinary result
  (`errors.Made`, record `decision: made`): `Made by the gateway at T once
  approved (approval X); this call staged and sent nothing new. The
  write's result:` and the result on the next line (the PR or MR URL, the
  pushed ref). So after approval, the agent that staged the write can
  collect its outcome with the same call. While the gateway is making the
  write, or once it is declined, the call is refused (`DENIED ... this
  exact write was approved (approval X) and the gateway is making it
  now`, `declined by NAME: REASON (approval X); nothing was written and
  nothing new was staged.`). A made write answers so for as long as its
  done record exists, however long ago it was made: a fresh card,
  approved again, would make the write a second time (a duplicate
  issue). The exception is `github_update_pr`: repeating a PATCH is harmless,
  and a replay would report success after title A, then B, then A again
  left the PR titled B, so the same update after one was made stages a
  fresh card. A declined write answers so for `expire_seconds` after it
  finished; after that, or after a failure or an expiry (or once a made
  write's done record is deleted), the same call stages a fresh card for
  a person to decide again. The done record counts only if its stored `id` is the card's. A pending card with no
  job (left by a gateway that held calls open) is discarded
  (`approval-discarded`) and staged afresh.
- **The done file** is the card plus the decision (`decision`,
  `approver`, `reason`, `decided`) and `queued: true`, `write` (`running`,
  `done`, `failed` or `skipped` for a decline or expiry), `result` (the
  write's result or why it failed, filtered like any result, at most 3,000
  characters), `finished` (Unix time) and, once approved, `outcome` (`id`,
  `approver`, `decided` in UTC, and `waited_seconds` from the card's
  creation to the decision; a time that isn't a usable Unix time counts as
  now). A console reads it to show what became of an approval. A write
  still `running` when the gateway starts was cut off by a restart: it is
  filed as `failed` ("it may or may not have reached the forge"), never
  retried.
- **A restart while staged** loses nothing: the card, the job and the
  mirror's ref are files. At start-up the sweeper makes any write approved
  meanwhile and files any card that expired meanwhile. Pending cards
  expire after `expire_seconds` (default 86,400, a day; 60 s to 604,800, a
  week); nothing is written for an expired card.
- **Expired cards leave `pending/` on their own.** An expired card is
  audited as `approval-expired` and filed under `approvals/done/` as
  `expired` (with any decision that came too late), whether the same call
  again finds it or a sweep over `pending/` does. The sweep runs inside
  every held call, once when the server starts (before it serves a call)
  and every 5 seconds on a daemon thread (which first makes the approved
  writes, so one approved just before its expiry is made). The idle sweep
  also prunes the patches that leaves unnamed. It runs only while core's
  hold is the installed hook (holds on, no plugin hook over it), and a
  failing sweep is logged by error type and retried on the next tick; it
  never stops the server or fails a held call.
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
  more when the gateway has made (or failed to make) a staged write (`the
  gateway made the approved push of B to R: <first line of the result>`, or
  `... FAILED: ...`), with a
  plain-text summary under the keys Slack, Discord and ntfy each read, sent
  on its own thread so a slow webhook never delays the call. The URL is
  a credential (a Slack webhook URL grants posting), so it is the
  `approvals-webhook` credential, delivered like the others nothing can
  proxy-inject (`core/credentials.py`: a file in the gateway's own sandbox,
  a Docker secret, or `PASSDESK_SECRET_APPROVALS_WEBHOOK_FILE`), read fresh
  on every use. A failed notification is logged and never blocks or leaks
  the card.
- **The record.** Every step is an audit event: `approval-requested` (the
  write was staged), `approval-granted` (with the approver),
  `approval-declined`, `approval-expired` (with any decision that came too
  late; these three with `queued: true`), `approval-discarded`, and for an
  approved write `approval-write-done` (with its result) or
  `approval-write-failed` (with the reason). Logs from before 24 Oct 2026
  also have `approval-queued`. The tool call's own record follows as
  today, with `decision: staged` and `approval: <id>`, so a review of the
  log can tell a held write from an unheld one.
- **No licence.** There is nothing to deliver, verify or renew.

## What the deployment ships (`ai-code-factory`)

- `factory approvals`: list pending cards, show one, approve or decline it
  (with a name and a reason), and a watch mode. Plain files in the state
  directory; standard-library Python 3.9 for the macOS host. It marks a
  pending card that has its `queued/` job as staged ("the gateway writes it
  on approval") and shows what became of a staged write from its `done/`
  file (`write`, `result`).
- Credential delivery for the webhook URL, from the host keychain into the
  gateway sandbox, the same way as Jira and Gemini. Inert when absent.
- `[approvals].tools` in its local policy overlay, baked into the image.

## Not in this slice

A localhost approval page (the console covers it), per-approver identity
beyond a name, approvals for other tools, and an approver API for
deployments that share no state directory with the gateway.
