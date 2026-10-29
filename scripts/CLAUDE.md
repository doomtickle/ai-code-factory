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
`sandbox="allow-scripts allow-same-origin"` with `allow="clipboard-write"`, below); the terminal page's CSP allows framing
only by the main port, and that page (the one carrying the key) is served only with
`Sec-Fetch-Dest: iframe` and `Sec-Fetch-Site` `same-site` or `same-origin`, never to a
top-level navigation. Its WebSocket is accepted only with that port's `Host`, an
`Origin` exactly `http://127.0.0.1:<termport>` or `http://localhost:<termport>`, the
main cookie, and a per-run terminal key in `Sec-WebSocket-Protocol`
(`factory-term.<key>`), which the server writes only into the terminal page it serves,
so the main page (which shows agent text) never has it.

COPY (doomtickle/passdesk-ops#142, `dashboard-ui/term.js`): Terminal 0 shows agent-written
text, so nothing it prints reaches the clipboard by itself (a poisoned clipboard may be pasted
into a host shell). No xterm.js clipboard addon is loaded; term.js's own OSC 52 handler is the
only one, and it is write-only: a query (`?`) or an empty payload is refused and never answered,
the selection target is ignored, the payload is at most 64 KiB of strict base64 and strict UTF-8
(a well-formed one over that, sized from its base64 length and never decoded, only shows
"COORDINATOR COPIED ABOUT N KiB · TOO LARGE TO COPY (LIMIT 64 KiB)" with DISMISS and no COPY key),
and one carrying a C0 or C1 control (other than tab and newline; CR is made a newline) is dropped,
as is any OSC 52 parsed during the muted replay. A valid one only raises a prompt in the terminal
frame ("COORDINATOR COPIED N CHARACTERS · PRESS TO COPY") with a preview drawn by `textContent`
(cut at 240 characters, invisible and bidi characters shown as `⟨U+XXXX⟩` and flagged); a newer
copy replaces the pending one, DISMISS or a new session drops it, and only a click on its COPY key
writes it. Each new text disables COPY and sets a `holdUntil` 2.5 s on (the control unit's
`HOLD_MS`), and a click during the hold does nothing, so a copy swapped in just before a click
is never the one written unread. The click writes it (`navigator.clipboard.writeText`, else
`execCommand("copy")` from a selected textarea, else that textarea left selected for the user's own copy). A grid selection is copied only by a
copy the user starts (the browser's copy event, Cmd+C on a Mac, Ctrl+C, Ctrl+Shift+C or
Ctrl+Insert elsewhere, only while there is a selection), each line's trailing spaces trimmed, and
the selection is then cleared, so the next Ctrl+C is the Coordinator's interrupt; with no
selection, and always on a Mac, Ctrl+C goes to the PTY. The page never reads the clipboard. The
terminal iframe alone carries `allow="clipboard-write"` (never `clipboard-read`); the control unit's frame gets no `allow`.

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
time), the `Content-Type` is JSON and the body is at most 8 KiB (128 KiB for `config-prepare` alone,
which carries a notes file: CONFIG WRITE below), checked on `Content-Length` before any of it is read; every other method
(`OPTIONS` included) is refused, and no response carries CORS headers. The main page
can't script the frame (another origin); the frame may tell it
exactly three strings by `postMessage` (`cu:expand`, `cu:collapse`, `cu:reload`),
accepted only from that frame's window and origin. The main page may tell the frame
exactly one thing, `{type: "cu:select", id: <card id>}` (a printer line, #190), sent to
the unit's exact origin; the unit accepts it only from `window.parent` with
`e.origin` exactly the main origin, as a plain object with exactly those two keys and an
id that fullmatches the card-id format (`[A-Za-z0-9_-]{1,64}`, checked on both sides). It
is a display hint and nothing else: it acts only on the deck (not over another screen,
an open APPROVE question or a decision in flight), only picks among the cards the unit
already holds (an unknown id flashes a fixed "no longer held" line), at most once per
400 ms, never presses a key or moves focus, and treats the card like one that arrived
unasked (typed reason cleared, keys dark for the 2.5 s hold). Agent-written strings (card titles,
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
a few seconds and read outside the board's lock. Its read-only view adds no endpoint and runs nothing
(its one write is CONFIG WRITE below); it takes
only named policy keys (never a credential, and a URL's user, password, query and fragment are
cut), and every string is cleaned to one printable line and drawn as text. Anything that moves
the security boundary (broker policy and hosts, credentials, kits) is shown there read-only,
never edited.

CONFIG WRITE (doomtickle/passdesk-ops#185, #186, owner decision 29 Oct 2026,
`scripts/dashboard_configwrite.py`) is SETUP's one write, and the only file the control unit
writes besides a decision and an action's log: a role's tuning override `FACTORY_CONFIG/roles/<role>.conf` and the bound
project's notes `FACTORY_CONFIG/projects/<project>.md` and `<project>.<role>.md`, data the factory
parses or appends to a prompt, never runs. One code path for both, three endpoints behind the same
checks as every other: `config-read {"kind":"notes","role"}` (a notes file's text, read capped at
`NOTES_READ_MAX`), `config-prepare` (the change, checked; returns the diff, the display path,
`changed`, `applies` and a single-use 120 s token for action `"config"`, in RESTART's one token
slot, bound to the exact new bytes and the SHA-256 of the file's current bytes or `absent`) and
`config-run {"token"}` (under a lock, re-reads the file and writes only if its digest is unchanged,
else a 409 `changed`). The target is named only from server-side facts (`FACTORY_CONFIG`, the
dashboard's project folder name if `usable_project`, a role `list_roles` returns), never a path
from the request. A role override takes only `ROLE_OVERRIDE_KEYS` (never `WORKSPACE`, `TEMPLATE`,
`BROKER_TOOLS` or any other key), each value one line of at most 200 characters with no quote,
backslash or control character; the existing file is updated in place (comments and order kept,
new keys appended, unset keys removed) and the result passes `roles.validate_role` (what
`factory start` runs) in a throwaway config folder before the token is issued, so a bad value or a
bad existing file is refused and the file left as it is. Notes are at most 20,000 characters of
strict UTF-8 with no NUL, and are refused (409, naming the role) if they would take any running
role's instructions plus notes (counted by `dashboard_setup._notes`, as the bridge counts them)
past `NOTES_LIMIT` and higher than it is now. Below `FACTORY_CONFIG` nothing is followed:
`roles/` and `projects/` are opened `O_NOFOLLOW|O_DIRECTORY` (made 0700 if missing), the file
`O_NOFOLLOW|O_NONBLOCK` and fstat'ed as a regular file, and the write is a 0600 temp file in that
folder (`O_CREAT|O_EXCL|O_NOFOLLOW`, by its fd), fsynced and `os.replace`d. The bridge reads only
`factory start`'s snapshot in `FACTORY_STATE`, so every change says `applies: "RESTART"`, and a
write drops the SERVICE DUE and SETUP caches so the lamp lights at once. Nothing reads
`FACTORY_CONFIG` live because of this, and nothing else in the config folder is written.

OUTBOX (doomtickle/passdesk-ops#189, `scripts/dashboard_outbox.py`) lists the `factory/*`
branches the bridge imported, newest first, as `outbox` in the main page's state: the repo,
the branch, the commits ahead of host `HEAD`, the latest reviewer verdict, the push state and
the PR or MR. It reads `dispatch/jobs.json` and `dispatch/packets/` (regular files only, opened
`O_NOFOLLOW`, size-capped) and the audit and deferred writes the dashboard already holds, and runs
only three git forms, all read-only, hooks and fsmonitor off (`-c core.hooksPath=/dev/null -c
core.fsmonitor=false`), argv list, no shell, `GIT_*` stripped, `GIT_OPTIONAL_LOCKS=0`:
`for-each-ref --format=%(objectname) %(refname) refs/remotes/factory-<lane>/factory/`,
`rev-list --count HEAD..<object id>` and, only for a branch that is ahead (count above 0),
`cherry HEAD <object id>`. A branch is MERGED when the count is 0, or when `cherry` succeeds and
prints at least one line, every one starting `- ` (each commit has a patch-equivalent in `HEAD`:
rebased, or re-pushed under another name); any `+` line, an error or a timeout leaves it OPEN
(never UNCHECKED, the count worked). Since `HEAD` isn't known without a fourth form, an answer is
cached per (repo, object id) for 60 s and dropped when the ahead count differs; a failure is cached
likewise, and `cherry` runs only for rows that will be shown. A branch name is only compared with what `for-each-ref`
printed and never reaches git, and the only thing passed on is a full object id the listing gave
(`[0-9a-f]{40,64}`, so it can't be an option). A name that isn't a plain ref gives an UNCHECKED
row and no git run. The result is cached 8 s, there are at most 20 rows and 40 branches looked
at, one run is cut at 5 s and a recomputation at 12 s. A MERGED or GONE row retires 24 h after its
job ended, an OPEN or UNCHECKED one (abandoned work) after 7 days, and a row past 7 days costs no git run. A link is built only from the gateway's own
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

SHIFT BUDGET (doomtickle/passdesk-ops#195, `app.js` `renderBudget`) is display only and adds no endpoint or `/api/state` field:
a dollars-a-day figure typed on the spend module and kept in this browser (`localStorage`, in the knobs' `fc1984-knobs` entry,
validated as a finite number from 0 to 100000, otherwise ignored; 0 or empty is no budget) that tints the header's spend readout
(a class from a fixed pair, amber past the figure, red past twice it) and fills a meter. It sends nothing and stops nothing.

DASHBOARD LOOK (doomtickle/passdesk-ops#196, `scripts/dashboard_config.py`, `dashboard-ui/look.js`) is
`FACTORY_CONFIG/dashboard.toml` (the dashboard's `--config`): a nameplate and floor plate, three shift names, the CRT
and sound defaults and a colour per role. It is read once at start and served as `look` in `/api/state`; it adds no
endpoint, runs nothing and reads nothing else. The reader is strict (an unknown key or table, a repeated key, a bad
value, a symlink or non-regular file, a file over 16 KiB or non-UTF-8 text refuses the whole file, opened `O_NOFOLLOW`
and non-blocking, read capped) and reuses `factory/policy.py`'s string reader; a refusal logs to stderr, shows the
defaults and appears on SETUP (`config.look`), and never stops the dashboard. Demo mode reads no real config. `look.js`
checks every field again in the browser; a colour reaches the page only as a validated `#rrggbb` through
`style.setProperty` (the CSP has no inline style and keeps none), strings only through `textContent`, and nothing from
the file becomes a class name, attribute or HTML. `crt` and `sound` only start the knob and the key until the
browser has a choice of its own.

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

EVENT PRINTER (doomtickle/passdesk-ops#190, `app.js` `renderPaper`) lines are buttons. They add no endpoint:
`/api/state` events carry `job_id` and `lane` on a JOB line (only when they match `JOB_ID` and the lane pattern) and
`card_id` on approval lines (only when it fullmatches `approvals.CARD_ID`). A JOB line opens the lane monitor through the
#191 `/api/lane?name=&job=` path; STAGED, WRITTEN and FAILED send the `cu:select` hint above; DENY and FAILED open in place
to their whole text, still `textContent`. The page re-checks every id against its pattern before using it.

FIRST SHIFT (doomtickle/passdesk-ops#181, `scripts/dashboard_hints.py`, `factory/hints.py`) puts `factory start`'s
start-up hints on the main page: `hints` in `/api/state` (`[{code, text}]`, fixed host strings cleaned to one
printable line, at most 6; the first-login states carry a fixed banner) and `first_shift` in the control unit's
state, which makes its RESTART LED blink (`cu.js`). The page draws one line over Terminal 0 with `textContent`;
nothing acts, and Terminal 0's rules above are unchanged. Two checks run `sbx`, and only these fixed argv forms:
`sbx secret ls` (the Claude login, the GitHub token) and `sbx ls -q` (the role sandboxes). `Probe`, a factory
`Context` that refuses any other argv, cuts each at 5 s and runs each once per refresh; a background thread
refreshes at most every 30 s, one at a time, never on a request and never under the board's lock, and a failed
or timed-out call keeps the last answer (so a hiccup never shows FIRST SHIFT). The stale-sandbox hint reuses the
SERVICE DUE cache (files only). Keychain checks are left out. Demo mode has no source and shows none.

SHIFT BRIEFING (doomtickle/passdesk-ops#182, `dashboard_setup.briefing`, `app.js` `renderBriefing`) turns the STANDBY plate
into a briefing: `briefing` in `/api/state`, the subset of the SETUP `config` (one builder, `dashboard_setup.setup`, not a second
parser) plus the project's repos from `factory/repos.py` `project_repos` on the bound project folder (read from
`broker/project` and used only if its basename is the dashboard's project). It holds host-derived facts only: the project name,
the repos' names (at most 12), each notes file's name, role and size (never its contents), each role's lanes and whether it is on,
the `[approvals].tools` names, the notes path to create, and one example prompt from the fixed `PROMPTS` list. No credential, no
`[git].repos`, no GitLab URL, no expiry. It adds no endpoint and runs no command; it is built outside the board's lock, cached
5 s (`BRIEFING_TTL`), every string cleaned to one printable line and drawn with `textContent`, no class or attribute from it. Demo
mode reads the fixture's `setup/` folder and the empty `.git` folders `dashboard_demo.build` makes under `setup/project/`.

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
`prepare`, `run`, `config-prepare` and `config-run` raise `Refused(403)` before any decision file, token, config write or host command,
and `argv()` raises. `state`, `log`, `patch` and `config-read` are inherited unchanged (SETUP and `config-read` read the
fixture's `setup/` folder through `setup_source()`), so INSPECT reads
the fixture's patch through the same `read_patch` path (digest-named, `O_NOFOLLOW`,
SHA-256 checked) inside the temp folder. Keep it that way: demo code must never add a
write outside that folder or a way to start a command.
