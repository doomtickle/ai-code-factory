# scripts/: Dashboard Security Rules

These rules cover the dashboard: `scripts/dashboard.py`, `scripts/dashboard_controls.py`,
`scripts/dashboard_term.py`, `scripts/patchview.py`, and the `dashboard-ui/` front-end
they serve. Read this before changing any of those files.

Other files in `scripts/` (e.g. `analytics.py`, `watch.py`, `start-broker.sh`,
`lib/`) are not dashboard code and aren't covered here, except where the rules below
name them directly: `approvals.py` (`write_decision`), `docs.py` (`build_served()`),
and `factory chat` (`coordinator_attached`) must match the forms those rules name.

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

Its state view only reads the state folder; at start-up it starts the browser opener
(fixed argv, no shell: `rundll32.exe url.dll,FileProtocolHandler <url>` then `wslview`
on WSL, else Python's `webbrowser`; never `explorer.exe`, `cmd.exe` or
`powershell -Command`). Agent-written text reaches the main page only as JSON and is
inserted with `textContent`, never as HTML. Cookies ignore ports, so any other server
on 127.0.0.1 the browser visits also receives the cookie: the cookie alone must never
authorize an action. The main page only displays: it reaches no endpoint that acts and
holds no key. Its actions are Terminal 0 and the control unit.

Terminal 0 is the Coordinator's terminal (`scripts/dashboard_term.py`, enabled by
`--coordinator NAME`, which bin/factory passes): it runs exactly the fixed argv
`sbx run --name <NAME>` (NAME matches `[a-z0-9][a-z0-9-]*`) in a PTY, no shell and
never a shell fallback, only when a page explicitly asks (`{"type":"power","on":true}`;
attaching a page starts nothing), only after `sbx ls -q` lists the sandbox and
`ps -axo pid=,ppid=,args=` shows no other `sbx run` naming it (`--name NAME`,
`--name=NAME` or positional; one attachment: a second would start a second Coordinator
in the same VM; `factory chat` refuses
likewise via `coordinator_attached`, which must match the same forms as
`runs_sandbox`), and never once shutdown has begun. It fans that one PTY out to every
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
set aside under a lock and never blocks the new card. A decided card of a tool the
gateway doesn't queue stays pending until the agent retries the call; a queued card
(`approvals/queued/<id>.json` with the card's id and nonce: a push, PR or MR whose hold
ran out) is written by the gateway within seconds of approval, not by a retry. The unit
shows either as decided with its keys dark, and a queued one is marked QUEUED. A decided,
expired or queued card never marks a lane HELD (`Dashboard.held()`): the broker's hook
waits only `hold_seconds`, so the agent's call has already returned. The unit's
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
type is logged. Settings, when they come, go in the control unit too, and anything
that moves the security boundary (broker policy and hosts, credentials, kits) is shown
there read-only, never edited.

Anything new that lets a page act must meet the same bar: one fixed argv with no
shell, a key in a request header (not just the cookie) on a POST or WebSocket with its
`Origin` checked, kept out of reach of the page that shows agent-written content, and a
confirm step before any host command.

Demo mode (`factory dashboard --demo`, `scripts/dashboard_demo.py`) is the same server
on a throwaway copy of the committed fixture `scripts/dashboard-demo/` and meets every
rule above. It reads no `FACTORY_STATE` or broker state and refuses `--state`,
`--project`, `--coordinator` and `--factory` (`bin/factory` passes none of them and
`exec`s Python so a signal reaches it). The copy is a fresh 0700 `mkdtemp` folder, never
inside the checkout, removed when the dashboard exits; the ticker appends audit records
only there. There is no Terminal 0. Its control unit is `DemoControls`: `decide`,
`prepare` and `run` raise `Refused(403)` before any decision file, token or host command,
and `argv()` raises. `state`, `log` and `patch` are inherited unchanged, so INSPECT reads
the fixture's patch through the same `read_patch` path (digest-named, `O_NOFOLLOW`,
SHA-256 checked) inside the temp folder. Keep it that way: demo code must never add a
write outside that folder or a way to start a command.
