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
- **Synchronous hold, then retry.** An MCP tool call cannot stay open for as
  long as a person may take: some clients cut a call at 60 seconds. So the
  hook waits up to `hold_seconds` (default 45) for a decision. Approved: the
  push proceeds in the same call. Declined: the agent gets `DENIED` with the
  approver's reason. Still pending: the agent gets a denial that says the
  call is held, names the approval id, and asks it to retry the same call
  once a person has decided. A retry with the same bundle has the same id,
  so a decision made in between is honoured and the push goes through.
- **The result says it was held** (doomtickle/passdesk-ops#141). An
  approval that lands within the hold looks to the agent like a slow call,
  so every write that went through a hold (approved in the call, or picked
  up by a retry) ends its result with one more line: `Held for approval
  (id X); approved by NAME at 2026-10-22T09:15:00Z after N s.` `decided`
  is the decision's time in UTC and `waited_seconds` runs from the card's
  creation (the first held call) to the decision, so a retry reports how
  long the write really waited for a person. The approver's name comes
  from the console's file, so it is filtered like any result (secrets
  redacted, control characters removed) and kept to one line of at most
  100 characters. The line is added to every result the tool returns after
  approval, including a push the forge rejected or an API error; a write
  that wasn't held is unchanged, byte for byte.
  Pending cards expire after `expire_seconds` (default 1800); an expired
  card is a denial, and a later retry makes a fresh one.
- **Expired cards leave `pending/` on their own.** An expired card is
  audited as `approval-expired` and filed under `approvals/done/` as
  `expired` (with any decision that came too late), whether a retry of the
  same call finds it or a sweep over `pending/` does. The sweep runs inside
  every held call, once when the server starts (before it serves a call)
  and every 60 seconds on a daemon thread, so a card left behind by an
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
- **Notification.** One outbound POST per card to a webhook URL, with a
  plain-text summary under the keys Slack, Discord and ntfy each read, sent
  on its own thread so a slow webhook never shortens the hold. The URL is
  a credential (a Slack webhook URL grants posting), so it is the
  `approvals-webhook` credential, delivered like the others nothing can
  proxy-inject (`core/credentials.py`: a file in the gateway's own sandbox,
  a Docker secret, or `PASSDESK_SECRET_APPROVALS_WEBHOOK_FILE`), read fresh
  on every use. A failed notification is logged and never blocks or leaks
  the card.
- **The record.** Every step is an audit event: `approval-requested`,
  `approval-granted` (with the approver), `approval-declined`,
  `approval-expired` (with any decision that came too late). The tool
  call's own record follows as today, and carries `approval: <id>` when
  the call went through a hold, also if the write then failed (its
  `decision` is `deny` or `error`), so a review of the log can tell a held
  write from an unheld one.
- **No licence.** There is nothing to deliver, verify or renew.

## What the deployment ships (`ai-code-factory`)

- `factory approvals`: list pending cards, show one, approve or decline it
  (with a name and a reason), and a watch mode. Plain files in the state
  directory; standard-library Python 3.9 for the macOS host.
- Credential delivery for the webhook URL, from the host keychain into the
  gateway sandbox, the same way as Jira and Gemini. Inert when absent.
- `[approvals].tools` in its local policy overlay, baked into the image.

## Not in this slice

A localhost approval page (the console covers it), per-approver identity
beyond a name, approvals for other tools, and an approver API for
deployments that share no state directory with the gateway.
