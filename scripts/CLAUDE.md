# scripts/: Dashboard Security Rules

These rules cover the dashboard: `scripts/dashboard.py`, `scripts/dashboard_controls.py`,
`scripts/dashboard_term.py`, `scripts/patchview.py` and the other `scripts/dashboard_*.py` modules, and the `dashboard-ui/` front-end
they serve. Read this before changing any of those files.

Other files in `scripts/` (e.g. `analytics.py`, `watch.py`, `start-broker.sh`,
`lib/`) are not dashboard code and aren't covered here, except where the rules below
name them directly: `approvals.py` (`write_decision`), `docs.py` (`build_served()`),
and `factory chat` (`coordinator_attached` in `factory/sbx.py`) must match the forms those rules name.

## Dashboard rules

The dashboard (`scripts/dashboard.py`, `factory dashboard`) is the one host component
that listens for a browser. It binds 127.0.0.1 only, requires its per-run token
(swapped for an HttpOnly, SameSite=Strict cookie named `factory_dashboard_<port>`) on
every request, refuses any `Host` that isn't itself, serves only an allowlist of its
own files under a CSP with no inline script or style (plus the factory's own docs at
`/docs/` for the MANUAL key: README, `docs/setup-wsl.md` and `docs/guides/` from the
factory checkout, rendered once at start-up by `docs.py` `build_served()` with CSS and
JS as separate files, held in memory, served under the same CSP minus `frame-src`, no
git run and no `file://` links), and is GET-only on its main port.

Its state view reads the state folder, plus the OUTBOX's git (below); at start-up it starts the browser opener
(fixed argv, no shell: `rundll32.exe url.dll,FileProtocolHandler <url>` then `wslview`
on WSL, else Python's `webbrowser`; never `explorer.exe`, `cmd.exe` or
`powershell -Command`). Agent-written text reaches the main page only as JSON and is
inserted with `textContent`, never as HTML. Cookies ignore ports, so any other server
on 127.0.0.1 the browser visits also receives the cookie: the cookie alone must never
authorize an action. The main page only displays: it reaches no endpoint that acts and
holds no key. Its actions are Terminal 0 and the control unit.

Terminal 0 is the Coordinator's terminal (`scripts/dashboard_term.py`, enabled by
`--coordinator NAME`, which `factory dashboard` passes: `factory/commands/wrappers.py`): it runs exactly the fixed argv
`sbx run --name <NAME>` (NAME matches `[a-z0-9][a-z0-9-]*`) in a PTY, no shell and
never a shell fallback, only when a page explicitly asks (`{"type":"power","on":true}`;
attaching a page starts nothing), only after `sbx ls -q` lists the sandbox and
`ps -axo pid=,ppid=,args=` shows no other `sbx run` naming it (`--name NAME`,
`--name=NAME` or positional; one attachment: a second would start a second Coordinator
in the same VM; `factory chat` refuses
likewise via `coordinator_attached` (`factory/sbx.py`), whose `runs_sandbox` must match the same forms as
the dashboard's, `factory/test_sbx.py` runs both on the same fixtures), and never once shutdown has begun. It fans that one PTY out to every
tab; a tab that attaches late gets the ring buffer as a `{"type":"replay"}` text frame
followed by exactly one binary frame, during which the page mutes its input so
terminal query replies in old output aren't sent to the Coordinator again.

It is served from a second 127.0.0.1 port, its own origin, which the main page frames
(main CSP adds only `frame-src` for that port; the iframe is
`sandbox="allow-scripts allow-same-origin"`); the terminal page's CSP allows framing
only by the main port, and that page (the one carrying the key) is served only with
`Sec-Fetch-Dest: iframe` and `Sec-Fetch-Site` `same-site` or `same-origin`, never to a
top-level navigation. Its WebSocket is accepted only with that port's `Host`, an
`Origin` exactly `http://127.0.0.1:<termport>` or `http://localhost:<termport>`, the
main cookie, and a per-run terminal key in `Sec-WebSocket-Protocol`
(`factory-term.<key>`), which the server writes only into the terminal page it serves,
so the main page (which shows agent text) never has it.

Every other control lives in the control unit (`scripts/dashboard_controls.py`, served
by `dashboard_term.py` on that same second origin, which runs whenever the dashboard
does, with or without `--coordinator`): a page at `/cu` the main page frames in the
rail, with its own per-run key (never the terminal's). That page is served only to a
same-site iframe load (the same `Sec-Fetch-Dest`/`Sec-Fetch-Site` rule as the terminal
page), under a CSP with no inline script or style that allows `connect-src` only to its
own origin and framing only by the main port; its script refuses to run top-level,
reads its key from the page and removes it from the DOM. Every action is a POST to
`/cu/api/<name>` with a JSON body, read only when `Host` and `Origin` name the second
origin exactly, `Sec-Fetch-Site` (if sent) is `same-origin`, exactly one dashboard
cookie matches, the key is in the `X-Factory-Control` header (compared in constant
time), the `Content-Type` is JSON and the body is at most 8 KiB; every other method
(`OPTIONS` included) is refused, and no response carries CORS headers. The main page
can't script the frame (another origin) and never posts to it; the frame may tell it
exactly three strings by `postMessage` (`cu:expand`, `cu:collapse`, `cu:reload`),
accepted only from that frame's window and origin. Agent-written strings (card titles,
branches, the action log) are inserted as text only.

APPROVE and DECLINE are one press each, as `factory approvals approve` is one command:
a decision file written through `approvals.write_decision` (exact card id, the card's
fingerprint `sha256(id NUL nonce)[:16]` must still match, never an expired card, a
decline needs a reason). One decision per card: `write_decision` hard-links a fully
written file onto the final name (never replaces it), so a second decision for the
same id and nonce is refused (`decided`, a 409 naming the verdict that stands) and of
two at once exactly one wins; a decision with another nonce (an earlier card) is
set aside under a lock and never blocks the new card. Every held write is staged
(`approvals/queued/<id>.json` with the card's id and nonce, its job) and written by the
gateway within seconds of approval; the agent's call returned when it was staged. The
unit shows a decided card with its keys dark until the gateway acts on it, and an
undecided staged one is marked STAGED. No card marks a lane held: the agent's
call returned when the write was staged. The unit's
deferred-writes list (`writes` in its state, from `approvals.read_writes`) is display
only, every field cleaned to one line and drawn as text.

RESTART and UPDATE run host commands, so no single request can start one: a `prepare`
request shows what it would do (the jobs it cancels, the held writes it drops; for
UPDATE the read-only `factory update --plan`) and returns a single-use token valid for
120 s and bound to that action, and only a `run` request carrying it starts the fixed
argv `<factory> restart` or `<factory> update --yes` (no shell; stdin `/dev/null`; its
own session; output to a 0600 log under `FACTORY_STATE/dashboard/actions/`), one at a
time, after hanging up Terminal 0 and holding its POWER ON until the action ends.

DIAG (doomtickle/passdesk-ops#118) runs `factory doctor` through the same
`prepare`/`run` token and the same runner (fixed argv `<factory> doctor`, logged the
same way, one at a time with RESTART and UPDATE); doctor changes nothing (apart from fetching an empty broker/), so its
`prepare` lists no jobs or held writes and it leaves Terminal 0 attached.

INSPECT (doomtickle/passdesk-ops#113) shows a held push's patch, and acts on nothing:
`POST /cu/api/patch {"id", "fp"}` behind the same checks finds the card exactly as a
decision does (exact id, fingerprint, `gone`/`changed`/`ambiguous` are 409), takes the
card's `patch_sha256` only if it fullmatches `[0-9a-f]{64}` (else 400; a card without
one is a 404), reads `approvals/patches/<sha>.patch` from the broker's state folder
with every component below it opened `O_NOFOLLOW` (the broker can write there), a
regular file only (non-blocking open, so a FIFO can't hang it) of at most 2 MiB, and
serves it only if its SHA-256 matches the card (else 409). The dashboard parses and
sanitises it itself (`scripts/patchview.py`: hunks consumed by their header counts,
never by what a line looks like; bidi, invisible, control characters made visible
`⟨U+XXXX⟩` tokens and flagged, lines cut at 2000 characters, the response capped at
about 3 MiB), so the page only draws text with `textContent`, and nothing from the
patch reaches an attribute, URL, class name or title. The main page never receives the
patch: the main page's state carries no patch field (the unit's state has only
`patch` and `patch_truncated` booleans per card, never the digest), and the patch
never goes into a card, webhook or audit record. When the patch is truncated the
response names the card's files it doesn't show (`files_missing`, sanitised) and its
last hunk ends with a "✂ CUT HERE" line. APPROVE and DECLINE inside the viewer are the
same `decide` request, sent with the fingerprint of the card whose patch is on the
screen, held 2.5 s after that card changes; APPROVE on a truncated push (in the
viewer, or on the deck when the card's `patch_truncated` is set) first asks "Approve
without seeing N files?" with CANCEL and CONFIRM APPROVE (dark for 1 s), and the card
changing or going cancels the question.

An unexpected exception in any control is a 500 with a fixed message, and only its
type is logged. SETUP (doomtickle/passdesk-ops#183) shows the effective configuration read-only: `config`
in the unit's state, built by `scripts/dashboard_setup.py` from the factory's parsers, cached
a few seconds and read outside the board's lock. It adds no endpoint and runs nothing; it takes
only named policy keys (never a credential, and a URL's user, password, query and fragment are
cut), and every string is cleaned to one printable line and drawn as text. Anything that moves
the security boundary (broker policy and hosts, credentials, kits) is shown there read-only,
never edited.

OUTBOX (doomtickle/passdesk-ops#189, `scripts/dashboard_outbox.py`) lists the `factory/*`
branches the bridge imported, newest first, as `outbox` in the main page's state: the repo,
the branch, the commits ahead of host `HEAD`, the latest reviewer verdict, the push state and
the PR or MR. It reads `dispatch/jobs.json` and `dispatch/packets/` (regular files only, opened
`O_NOFOLLOW`, size-capped) and the audit and deferred writes the dashboard already holds, and runs
only two git forms, both read-only, hooks and fsmonitor off (`-c core.hooksPath=/dev/null -c
core.fsmonitor=false`), argv list, no shell, `GIT_*` stripped, `GIT_OPTIONAL_LOCKS=0`:
`for-each-ref --format=%(objectname) %(refname) refs/remotes/factory-<lane>/factory/` and
`rev-list --count HEAD..<object id>`. A branch name is only compared with what `for-each-ref`
printed and never reaches git, and the only thing passed on is a full object id the listing gave
(`[0-9a-f]{40,64}`, so it can't be an option). A name that isn't a plain ref gives an UNCHECKED
row and no git run. The result is cached 8 s, there are at most 20 rows and 40 branches looked
at, one run is cut at 5 s and a recomputation at 12 s. A link is built only from the gateway's own
`Opened PR #N: <url>` result and only when it is `https://github.com/<owner>/<repo>/pull/N` or
`https://<the policy's GitLab host>/<project>/-/merge_requests/N` with the same number (no
userinfo, query or fragment); anything else is shown as text, and the page re-checks that it is
an https URL before it sets `href`. Every other string is drawn with `textContent`, and classes
come from fixed maps, never from the data. A row opens that job's packet through `GET
/api/lane?name=&job=` (the job id must match `[0-9]{8}-[0-9]{6}-[0-9a-f]{6}`, else 400, and be a job of that lane, else 404; the
packet is a regular file `dispatch/packets/<id>.md`, opened `O_NOFOLLOW`, capped). The module acts on nothing. Demo mode runs
no git: `FixtureGit` reads `dispatch/demo-refs.json`.

ATTENTION (doomtickle/passdesk-ops#192, `app.js`, with the cues in `sound.js`) is display only and adds no
endpoint: the title and favicon badge, the cues and the optional notifications come from `/api/state`
diffs the page already polls. Only counts, a fixed tool list and the lane-name pattern ever reach the
title, the favicon (drawn on a canvas, so the main page's CSP has `img-src 'self' data:` and nothing wider)
or a `Notification`; agent-written text never does. Notification permission is asked only from the NOTIFY
key's click, never on load.

LANE LABELS (doomtickle/passdesk-ops#194, `scripts/dashboard_labels.py`, `dashboard-ui/lanelabel.js`) put a developer's
branch and the lane a reviewer is checking on the lane modules and chips, as the floor already did: `branch`, `desk` and
`reviews` in each lane of `/api/state`, derived by one module from the task text and the job's `⎇ branch` log lines. They
add no endpoint, run no git, and are drawn with `textContent`; `lanelabel.js` is a plain served file in the allowlist,
loaded before `app.js` and `floor.js` (no inline script).

LANE JOBS (doomtickle/passdesk-ops#191, `scripts/dashboard_jobs.py`, `app.js`) let the lane monitor's PACKET tab step through a
lane's past jobs and show a pending view for a running one. They add no endpoint: `/api/lane?name=&job=` (the #189 path, one
`Dashboard.job_packet`) now returns `jobs` (the lane's newest 50 job ids, newest first), `job` (the one whose packet this is; none
asked for means the newest) and `sel` (state, task, model, cost, elapsed, when), and `pending` (the job is running and has no
packet yet). The ids come from `dispatch/jobs.json` (regular file, `O_NOFOLLOW`, capped), the audit and the lane's transcript, only
those matching the bridge's job-id pattern; a `job` that is not one of that lane's is a 404 (`NoSuchJob`), a malformed one still a
400, and the id is joined onto `packets/` only after it matched. Tasks are cut to one line; everything is drawn with `textContent`
and the page sets no class or attribute from it.

Anything new that lets a page act must meet the same bar: one fixed argv with no
shell, a key in a request header (not just the cookie) on a POST or WebSocket with its
`Origin` checked, kept out of reach of the page that shows agent-written content, and a
confirm step before any host command.

Lane state is read-only display derived from transcripts. A lane shows RUN only while the dispatch bridge's pid is alive (`watch.py` `Watch.refresh` marks running lanes `interrupted` otherwise); the bridge also closes dangling transcripts when it starts. Test fixtures that expect a RUN lane must write a live `dispatch/pid`.

Demo mode (`factory dashboard --demo`, `scripts/dashboard_demo.py`) is the same server
on a throwaway copy of the committed fixture `scripts/dashboard-demo/` and meets every
rule above. It reads no `FACTORY_STATE` or broker state and refuses `--state`,
`--project`, `--coordinator` and `--factory` (`factory dashboard --demo` passes none of them and
`exec`s Python so a signal reaches it). The copy is a fresh 0700 `mkdtemp` folder, never
inside the checkout, removed when the dashboard exits; the ticker appends audit records
only there. There is no Terminal 0. Its control unit is `DemoControls`: `decide`,
`prepare` and `run` raise `Refused(403)` before any decision file, token or host command,
and `argv()` raises. `state`, `log` and `patch` are inherited unchanged (SETUP reads the
fixture's `setup/` folder through `setup_source()`), so INSPECT reads
the fixture's patch through the same `read_patch` path (digest-named, `O_NOFOLLOW`,
SHA-256 checked) inside the temp folder. Keep it that way: demo code must never add a
write outside that folder or a way to start a command.
