# Design note: approvals (Passdesk Pro)

The paid feature: nothing an agent pushes or opens as a pull request leaves
the machine until a person has approved it. This note fixes the shape before
the code; the decision log records what changed on the way.

## What core ships (Apache-2.0, this repo)

- **An approval point**, `core/approvals.py`: `require(card)` is called by a
  tool right before its external write, with a card describing what is about
  to happen. Core's default returns immediately. A deployment may install a
  hook with `set_hook(fn)`; the hook returns to allow or raises `Denied`.
- **The caller's identity** for the card: `PRINCIPAL`, a context variable
  the protocol layer sets for each call.
- **Plugin loading**, `core/plugins.py`: `PASSDESK_PLUGINS` names modules
  (comma-separated) imported at start-up, after the policy loads. Each
  plugin registers what it provides (today: an approval hook). The image
  takes an optional `plugins` build context on `PYTHONPATH`; with nothing
  named, nothing loads and the gateway behaves exactly as before.
- **Two call sites.** `git_push` builds its card after every check has
  passed (bundle verified, every commit secret-scanned, limits applied) and
  calls `require` *outside* the tool lock, so a pending approval never
  blocks another role's git call; it then re-takes the lock, confirms the
  ref it validated is unchanged, and pushes. `github_create_pr` calls
  `require` after the title and body pass the secret scan.

The card never carries a credential, and nothing in it comes from the
network: it is made of what the gateway itself verified.

## What Pro ships (commercial, `passdesk-pro`)

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
  Pending cards expire after `expire_seconds` (default 1800); an expired
  card is a denial, and a later retry makes a fresh one.
- **Decisions are files.** The state directory is shared between the gateway
  and its deployment and with nothing else, so it is the channel:
  `approvals/pending/<id>.json` is the card, `approvals/decisions/<id>.json`
  is the decision (`approve` or `decline`, approver name, reason, time),
  written atomically by the deployment's console. No new listener, no new
  port, no new token: whoever can write to the state directory is the
  approver, which is the person running the machine. A networked approver
  API is the standalone deployment's job, later.
- **Notification.** One outbound POST per card to a webhook URL, with a
  plain-text summary under the keys Slack, Discord and ntfy each read. The
  URL is a credential (a Slack webhook URL grants posting), so it is
  delivered like the other credentials nothing can proxy-inject: a file in
  the gateway's own sandbox, read fresh on every use. A failed notification
  is logged and never blocks or leaks the card.
- **The record.** Every step is an audit event: `approval-requested`,
  `approval-granted` (with the approver), `approval-declined`,
  `approval-expired`. The tool call's own record follows as today.
- **The license.** A signed token: base64url(JSON claims) `.`
  base64url(RSA-2048 PKCS#1 v1.5 SHA-256 signature). Claims: `sub` (the
  customer), `tier` (`solo` or `team`), `approvers`, `iat`, `exp`, `v`.
  The public key is baked into the Pro package; verification is offline and
  pure standard library. The token is delivered as a credential file like
  the webhook URL. Invalid, expired or missing: the plugin logs why and
  installs no hook, so the gateway keeps working as the free core. Keys
  are minted by the seller with `openssl` from a private key that never
  enters any repository.

## What the deployment ships (`ai-code-factory`)

- `factory approvals`: list pending cards, show one, approve or decline it
  (with a name and a reason), and a watch mode. Plain files in the state
  directory; standard-library Python 3.9 for the macOS host.
- Credential delivery for the webhook URL and the license, from the host
  keychain into the gateway sandbox, the same way as Jira and Gemini.
- The `plugins` build context from the local config folder, and
  `PASSDESK_PLUGINS` set on the gateway sandbox when a plugin is present.
  Both inert when absent.

## Not in this slice

A localhost approval page (the console covers it), per-approver identity
beyond a name, approvals for other tools, and an approver API for
deployments that share no state directory with the gateway.
