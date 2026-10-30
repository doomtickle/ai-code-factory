# Use the dashboard

The dashboard is the factory on one screen in your browser: the Coordinator,
every lane, the writes waiting for you, today's spend and the broker's calls.
This guide walks through each part, says what every key does, and lists the
`factory` command each one stands in for. The full reference is
[Dashboard](../../README.md#dashboard) in the README.

## Open it

`factory start` opens the dashboard. To open
it again later, while the factory is running:

```bash
factory dashboard
```

It prints a link and tries to open it in your browser. The link carries a
token for this run, so it works only on this machine and stops working when
you press Ctrl-C where `factory dashboard` runs. Ctrl-C closes the dashboard,
not the factory: the agents keep working, and `factory dashboard` brings it
back. `--no-open` only prints the link; `--port N` picks the port.

## The layout

```text
┌───────────────────── header: lamps · spend today · jobs · clock · MANUAL ───────────────────────┐
│ Terminal 0                │ Lanes                                                               │
│ (the Coordinator)         │  ┌ 1 developer ─────────────────┐ ┌ 2 developer-lane2 ───────────┐  │
│                           │  └──────────────────────────────┘ └──────────────────────────────┘  │
│                           │  ·3 developer-lane3  ·4 reviewer   (idle lanes)                     │
│                           ├──────────────┬────────────────────┬─────────────────────────────────┤
│                           │ Spend / hour │ Event printer      │ Write gate + control unit       │
└───────────────────────────┴──────────────┴────────────────────┴─────────────────────────────────┘
```

On a window narrower than about 1280 pixels the panel unfolds into a column
you scroll: the Coordinator and the lanes side by side, and the bottom row
underneath them.

## The header

- **Lamps:** LINK is green while the page can reach the dashboard (red and
  blinking when it can't). BRIDGE is green while the dispatch bridge runs,
  the part that hands the Coordinator's jobs to the roles. GATE blinks amber
  while a write is held for you. SERVICE goes amber and reads **UPDATE due**
  when a sandbox is out of date (made from an older image or kit): hover it
  for the sandboxes and why, e.g. `factory-broker: broker-kit/ changed`; the
  control unit's UPDATE key lights too. It reads **RESTART due** when a
  setting read at start-up changed since the last `factory start` (a role
  override file, `enabled-roles`, or your project notes), and RESTART lights.
  Dark means nothing is due. It only tells you; you press the key (the
  control unit asks first). It reads files, so a sandbox made before the
  factory recorded fingerprints isn't named; `factory start` still warns.
- **Readouts:** today's spend in dollars, today's job count, the jobs running
  now, and the clock.
- **MANUAL** opens these docs in a tab of their own. The **?** on each module
  opens its part of this guide, in the same tab. The docs are the ones in your
  factory checkout when the dashboard started; restart `factory dashboard` to
  see edits. The manual shares this console's look (case, paper, stripes);
  its theme button, top right of the page, is the same three-way
  system/light/dark toggle.

## Terminal 0: the Coordinator

The big screen on the left is the Coordinator itself, live. Opening the page
brings the Coordinator up once per load, continuing its last session (if it is
off and not open in `factory chat`); then type to it as you would in any
terminal. Later, when the screen is off, **RESUME** continues the last session
and **POWER ON** starts a fresh one.

- Every tab you open shows the same session. The last tab you resize or click
  into sets its size.
- A tab you open later replays the recent output first, with its keyboard
  muted until the replay is done.
- When the session ends, the screen powers off. POWER ON starts a new session.
  **RESUME**, next to it, continues the last one instead (it runs Claude Code's
  `--continue`), so what you told the Coordinator earlier should still be in
  its context. The automatic start on page load is a RESUME. All three check
  the same things: the Coordinator's sandbox exists and nothing else has it
  open.
- **POWER ON starts a fresh session.** Checked on 30 October 2026: the
  Coordinator was told a word to remember, the dashboard was closed, POWER ON
  was pressed again, and the new session had no memory of it. That is the only
  thing that check covered. RESUME's `sbx run --name <name> -- --continue` has
  not been tried on a live host yet, so it is untested there.
- A **RESTART** or **UPDATE** from the control unit hangs up Terminal 0 and,
  when it finishes successfully, attaches again with RESUME, not POWER ON, so
  the day's context survives. If the Coordinator wasn't attached when you
  pressed it, nothing is attached afterwards.
- In a plain terminal, `factory chat` is a fresh session and `factory chat
  --resume` is RESUME (`sbx run --name factory-coordinator -- --continue`).
  If RESUME ever shows a fresh session (nothing to continue), type `/resume`
  inside the session to pick an earlier one from Claude Code's own list.

**Copying out of Terminal 0.** There are two ways:

- **The COPY prompt.** When the Coordinator copies something to your
  clipboard (Claude Code sends its copies through the terminal), a strip at the bottom
  of the screen says "COORDINATOR COPIED N CHARACTERS · PRESS TO COPY" and
  shows the start of the text. Press **COPY** to put it on your clipboard, or
  **DISMISS**. This copies the exact text, without the screen's line breaks or
  padding, so prefer it for commands. Nothing reaches your clipboard until you
  press COPY, because the text comes from an agent; check the preview first.
  COPY stays dark for 2.5 seconds after the text appears or changes, so a
  newer copy can't slip in just before your click. A
  newer copy replaces the one waiting, and "⚠ CONTAINS HIDDEN CHARACTERS" means
  the preview shows invisible or right-to-left characters as `⟨U+XXXX⟩`. If
  the browser blocks the copy, the prompt selects the text in a box and asks
  you to press Ctrl+C (⌘C on a Mac). A copy over 64 KiB can't be taken: the
  strip says "COORDINATOR COPIED ABOUT N KiB · TOO LARGE TO COPY (LIMIT 64 KiB)",
  with DISMISS only.
- **Select it on the screen.** The Coordinator takes the mouse, so a plain
  drag selects nothing. Hold **Shift** while you drag on Windows (WSL) and
  Linux, or **Option** on a Mac. Then copy with **Ctrl+C**, **Ctrl+Shift+C** or
  **Ctrl+Insert**, or **⌘C** on a Mac, or the browser's Copy menu. Copying
  clears the selection, so the next Ctrl+C goes to the Coordinator as an
  interrupt, as Ctrl+C always does with nothing selected (and on a Mac). Trailing spaces are trimmed
  from each line, but what you get is what the screen shows: a long command
  the Coordinator wrapped comes out as several lines with their indentation,
  and anything cut off at the edge is missing. Use the COPY prompt for those.

The dashboard never reads your clipboard. Pasting into Terminal 0 works as in
any terminal.

**FIRST SHIFT.** A line above the screen tells you what `factory start` would
print in the terminal you started it from, since you're looking at the browser:

- No Claude login stored yet: "FIRST SHIFT · 1. POWER ON and type /login · 2.
  press RESTART". The control unit's RESTART key blinks until you've done it.
- Login stored, but the role sandboxes aren't created yet (the state right
  after your first `/login`): it says so, and RESTART still blinks. Press it
  and CONFIRM; the restart creates the roles.
- Roles exist: the same line carries any other start-up hint in one line,
  such as sandboxes that are out of date (`factory update`) or a missing
  GitHub token (`factory secret save github`), with "(+N more)" when there are
  several. Jira, npm and GitLab token hints aren't checked here, because they
  would ask your keychain; `factory start` still prints them.

The line only tells you; it presses nothing. It's worked out on your machine
from fixed text (never from anything an agent wrote), and the two `sbx` checks
behind it run in the background every 30 seconds at most, so a slow or failing
`sbx` leaves the last answer in place instead of flashing FIRST SHIFT. Demo
mode never shows it.

The screen tells you when it can't attach:

| The screen says | What to do |
|---|---|
| OPEN ELSEWHERE | The Coordinator is open in `factory chat`. Close it there, then POWER ON or RESUME. |
| NO COORDINATOR | Its sandbox doesn't exist yet: run `factory start`. |
| NO SBX | `sbx` couldn't run: run `factory doctor`. |
| SERVICE | A RESTART or UPDATE is running. The Coordinator comes back when it's done. |
| NO SIGNAL | The page lost the dashboard. If `factory dashboard` stopped, run it again and open the new link it prints. |

**The knobs** under the screen change only how it looks, in this browser:
BRIGHT and CONTRAST; V-HOLD, which makes the picture roll when you turn it off
centre; and CRT, from 0 (a flat screen) to 10 (a full tube with scanlines and
glow; it starts at 3). Drag a knob, scroll on it or use the arrow keys;
double-click or Home resets it.

> **Tip:** If the tube effect makes long replies hard to read, turn CRT down
> to 0.

Stands in for: `factory chat`.

## Lanes

Every role has one lane per job it can run at once: a developer with three
lanes has `developer`, `developer-lane2` and `developer-lane3`. As in `factory
watch`, only the **active** lanes get a module: a job running, or one that
finished in the last ten minutes. The modules share the bay, so one active
lane fills it. When a lane gets a job it powers on in the bay, and the others
move over to make room. Ten minutes after its job ends it powers down into
the strip below.

The **strip** under the modules has a chip for every idle lane, with its
number, the branch its developer last worked on (dim) and when its last job
ended. With nothing running, the bay shows
STANDBY above it, as a **shift briefing**. It reads only what is already on
this machine, changes nothing, and refreshes every few seconds:

- **Project.** The folder `factory start` is bound to, and its git repos (the
  same list the developer clones). A project that isn't a git repo or a folder
  of them shows none: the developer can't work in it.
- **Notes.** The project's notes files (`projects/<folder>.md` and
  `projects/<folder>.<role>.md` in your config folder) with their sizes. The
  contents never appear here. Without any, it reads "no project notes: roles
  start every task knowing only their instructions" and gives the path to
  create. Notes are read at `factory start`; see [Make it yours](04-customize.md).
- **Roles.** Each role with its number of lanes; an optional role shows
  whether it is on.
- **Holds.** Whether held writes are on, and for which tools (`[approvals]`).
  Credentials, the allowed repos and the GitLab address aren't shown; the
  [Setup](#setup-the-configuration-in-force) screen has the rest.
- **A work order.** One example task to give the Coordinator in Terminal 0,
  picked from a short fixed list (the first is the slugify task from the
  [first session](01-first-session.md#6-give-the-coordinator-a-small-first-task)).
  It changes once a day, and nothing sends it for you.
- **Keys.** The keys of the [Keys](#keys) table.

If the configuration can't be read, only the first line of STANDBY shows.

Each module shows:

- a status LED and tag: RUN (green, blinking), REVISE (amber: a review asked
  for changes), DONE or APPROVED (green), CANCELLED, FAILED or INTERRUPTED (red: the job was cut off
  by a restart, update or disconnect; a lane only shows RUN while the dispatch
  bridge is up and the job is live);
- on a developer's header, the `factory/<topic>` branch it is on, in pink like
  the transcript's branch lines: the branch the task names, else the first
  branch the bridge logged for the job. On a reviewer's, a `▸ DEV 2` tag naming
  the developer lane it is reviewing. The factory floor's console glass shows
  the same tag, and both read one derivation, so they can't disagree;
- the live tail of its transcript, with tool calls, errors, branches and held
  writes in their own colours;
- its model, cost, context size, and the time the job has run (or took).

A lane keeps its number as a module and as a chip. Press it (1 to 9), or
click the module or chip, to open the **lane monitor**: the
full transcript (**T**) and a job's review packet (**P**), which has
the task, each branch's commits and diffstat, the tests it ran, the broker
calls it made, its cost and the role's report. **Esc** closes it.

The packet tab opens on the lane's newest job and steps back through the
lane's past ones: **◀** (or **[**) goes to the next older job, **▶** (or **]**)
to a newer one, down to the newest again, which the tab then follows. The
header shows where you are (`2/7`), and the job's id, state (the reviewer's
APPROVED or REVISE when it has one), cost and when it ran. The list is the
newest 50 jobs of that lane, from the bridge's job list, the audit and the
lane's own transcript; for older ones use `factory packet --list`. While the
newest job is still running there is no packet yet, so the tab shows a
**pending** view in its place: the job's task (the short form the job list
keeps, never the full text), model, time run so far and cost so far (shown as
`--` until the bridge has a figure, which is usually when the job ends).
Stepping in the packet tab never changes the transcript, which keeps updating
live. A job's packet opens only for the lane the job ran on; pressing a row of
the outbox opens its job's packet the same way.

Stands in for: `factory watch`, `factory logs <role>` and `factory packet
<lane>`.

## The outbox

The **outbox** sits under the lanes. It has one row for every `factory/*` branch
the dispatch bridge has imported into your project, newest first, so you can
see at a glance what is finished and what still needs you. It starts folded to
its header bar, which shows how many branches are unmerged (the count lights up
amber when it isn't zero); press the **Outbox** title (or Tab to it and press
Enter or Space) to unfold or fold it. The dashboard remembers your choice in
this browser. A row shows:

- the repository, the branch and how many commits it is ahead of your current
  `HEAD` (`+3`);
- its merge state, one of:
  - **UNMERGED**: the branch has commits that are not in your checked-out
    branch yet (the host's `HEAD`). These are the ones the header counts.
  - **MERGED**: all of the branch's commits are in your checked-out branch,
    or the same changes are in it under other commits (the branch was rebased
    or pushed again under another name).
  - **GONE**: the imported branch no longer exists in your project.
  - **?**: the dashboard couldn't check (an odd branch name, or git didn't
    answer in time). It counts as unmerged until it is checked.
- the reviewer's latest verdict, APPROVED or REVISE, from the reviewer packets
  that name the branch (none if no review mentions it; `(old)` means that review
  started before the developer's last job on the branch ended, so it may not
  have seen the latest commits);
- the push state: **STAGED** (a push is waiting at the write gate for your
  approval), **PUSHED** (the gateway made the push), **FAILED** (it tried and
  failed), or **WRITTEN** (the gateway opened a pull request for the branch but
  has no record of the push itself);
- the pull request or merge request, as a link, once the gateway has opened
  one. The link is used only if it is a `https://github.com/` pull request or a
  merge request on your configured GitLab host; anything else is shown as
  plain text.

MERGED and GONE rows drop off the list a day after the job ended; UNMERGED (and
`?`) rows drop off after 7 days. A rebase merge, or a branch that landed under
another name, shows MERGED because the dashboard compares the changes. A squash
of several commits into one changes them, so that branch stays UNMERGED until
the 7 days pass or you delete the stale remote-tracking ref (`git branch -r -d
<ref>`, for example `git branch -r -d factory-developer/factory/foo`), which
makes the row GONE (see [Get the work](02-build-a-feature.md#get-the-work)).

Press a row to open that job's review packet in the lane monitor. The outbox
only displays: it can't merge, push or open anything. It asks git for a branch
list, a commit count and a changes check at most every few seconds, so a very long list is cut
at 20 rows.

Stands in for: `git branch -r | grep factory-` and `factory packet <job>`.

## The factory floor

**FLOOR** in the Lanes header (or **F**) swaps the lane modules for the
factory floor: the same lanes drawn as one more module of the rack, a brushed
panel with an amber tube in it. Press it again to get the modules back. The
dashboard remembers your choice in this browser. Terminal 0 and the rail don't
move, so the write gate and the control unit stay in reach.

- **The panel.** The plate carries the title, the shift (day, swing or night,
  by your clock), an LCD readout of today's jobs, running lanes and spend, the
  gate's three LEDs (ALLOW, HOLD, DENY), a STAGED lamp with the number of writes
  waiting for you, and a red DENIED counter of calls refused since the page
  loaded. The tube follows Terminal 0's **CRT** knob: scanlines, grille and
  the rolling band go from nothing at 0 to a full tube at 10.
- **The track.** Inside the tube one loop carries every trip, clockwise. The
  Coordinator's dispatch console feeds it on the left; developers are docked
  along the top side, reviewers along the bottom, a lane of any other role (a
  researcher, a custom role) on the right side. The gate stands on the bottom
  side after the reviewers, and the OUT portal, a wall of light, drops off the
  track just past it. Chevrons run along the track while anything is working.
  A small legend inside the loop says which side is which.
- **Stations.** Each lane is a program seated at a console: a dark suit with
  circuit lines in its role's colour (developers amber, reviewers green, the
  researcher purple, other roles a fixed hue each), glowing shades in the same colour, and an
  identity disc behind the head. The disc and the props show the lane's state:
  - disc spinning, hands typing, console lit: running
  - disc dim and a green ✓ by the console: done or approved
  - FIX tag by the console: a review asked for changes
  - disc red and broken, fragments over the head: failed or cancelled
  - asleep, dimmed, z's: idle

  The job is written on the console's glass in a few words: the topic of the
  first `factory/<topic>` branch its task names, else the task's first words
  (up to 40 characters). A title wider than the glass slides sideways to its end
  and back; one that fits stays still.
  Hover the station for the task's first line. A reviewer's console also says
  which developer it is reviewing (`▸ DEV 2` for `developer-lane2`), and while
  that reviewer is running a **light bridge** runs across the inside of the
  loop from the reviewer to that developer, with one right-angle turn. An idle
  lane has nothing on its glass.

  Each station keeps its number on a keycap. Click a station, press its number, or
  focus it and press Enter or Space, to open the lane monitor.
- **Dispatch** (left). The Coordinator's console in matrix green, under the
  I/O beam. While Terminal 0 is attached a white-circuited User sits at it,
  the beam is lit and the plate reads ON DUTY; with the bridge up and nobody
  attached the seat is empty and it reads BACK SOON; with the bridge down the
  console is dark and it reads BRIDGE DOWN. Work orders leave by the spur onto
  the track.
- **How work moves.** Each trip is drawn from what the page already knows about
  a lane, compared with the poll before:
  - a developer lane starts running with a task it didn't have: a work order
    rides the track from dispatch round the left end to its console
  - a developer hands back a branch (a new branch line in its log): a pink
    branch crate is parked on its console, until that lane takes its next job
  - a reviewer starts (or is held) reviewing a developer lane: the branch crate
    rides the track from that developer round the right end to the reviewer
    (one is launched even if none was parked), and the light bridge lights
  - a reviewer approves: a green crate rides on through the gate to the OUT
    portal and drops off the picture, and the portal flares. It passes the gate
    without lighting it: only broker calls light the gate. OUT means approved
    and ready for you to merge, not merged
  - a reviewer asks for changes: a yellow FIX crate rides back the way the
    branch came, to the developer's console
- **Broker calls.** Every broker call is a crate that cuts straight across the
  loop to the bottom side and rides to the gate:
  - an allowed call passes, with the gate flashing green and the ALLOW LED
    lighting, and goes out of the portal
  - a denied call derezzes: it is stamped CONFISCATED and lands in the derez
    field under the gate, the DENY LED lights and the counter ticks up
  - a held write waits on the track in front of the gate, blinking yellow,
    until you decide it; the gate's lamp, the HOLD LED and the STAGED lamp stay
    on while anything waits
  - hover a crate waiting at the gate to read the write it holds (tool, role,
    repo, branch); hover a crate in the derez field to read the broker line
    that put it there. The field keeps the last five. Both kinds take the
    keyboard focus (Tab) and are read out by a screen reader. The field and the
    counter are only in this browser tab: they empty when you reload the page.
- **Size.** The panel is as wide as the bay's shape asks and never narrower
  than its stations need: more lanes on a side make the track longer, and the
  picture scales to fit. The fixed furniture (dispatch, gate, portal) keeps its
  place.

The floor reads the same state as the rest of the page and does nothing on its
own. Moving crates are only animated while the floor is on screen, and only for
what changes after the page has loaded; what was already there is history and
is just shown (a lane that finished before you opened the page keeps its branch
crate on the console). Denials (DENY, NO, FAILED) that happen while it is
hidden are added to the derez field without animation. With reduced motion set
in your system, nothing moves (a long job title keeps an ellipsis, and the
tooltip has the whole line): the stations still show their state, branch
crates still sit on their consoles and confiscated crates appear in the field
without travelling there. At most 14 crates are in flight at once.

### Floor events

For add-ons that react to the floor (the sound layer below, a shift ticket), the
page announces what the floor sees as three events on `document`. The floor ships
no listener of its own. They don't bubble and can't be cancelled; a listener that
throws doesn't disturb the floor.

| Event | Fires when | `detail` |
|---|---|---|
| `factory-floor:passed` | a broker call is let through (ALLOW, OK, WRITTEN) | `kind`, `role`, `line`, `animated` |
| `factory-floor:confiscated` | a broker call is refused (DENY, NO, FAILED) | `kind`, `role`, `line`, `animated` |
| `factory-floor:docked` | a new branch line appears in a lane's log (any branch name, not only `factory/*`; it does not mean the work was delivered): the lane's branch crate is parked on its console | `lane`, `role`, `branch`, `animated` |

Each broker event fires once per call and each branch once per branch, as the
page learns of it. They fire whether or not a crate is drawn (`animated` says
whether one was; see below) and whether the floor is on or off, including with reduced
motion. They don't fire for what the page loaded with (the first poll), nor for
a lane's first appearance. A held write fires nothing until it is decided.
Everything else in `detail` is a string, `animated` a boolean, and the object is
frozen.

For `docked`, `animated` says whether the branch crate was drawn on the console
(the floor is on and the lane has a station). For the broker events,
`animated` reflects the first leg only. A denied call reports `animated: true`
once its crate starts the trip to the gate, even if that crate is then binned
without moving because too many crates are in flight.

`kind` is the broker line's verdict, `role` the role it names, `line` the
printer's line (clipped at 300 characters) and `branch` the branch name (clipped
at 200). All of it is written by agents: **insert it with `textContent` only,
never as HTML.**

```js
document.addEventListener("factory-floor:confiscated", e => {
  document.getElementById("ticker").textContent = e.detail.line;   // never innerHTML
});
```

### Sound

Off by default (`sound = true` in [your `dashboard.toml`](#your-own-look-dashboardtoml) starts it on for a browser that has made no choice). The **SOUND** key next to FLOOR in the Lanes header (or `s`)
turns on a short cue for each floor event, so you can hear the factory from
another window:

| Event | Cue |
|---|---|
| a call is let through | a soft blip |
| a call is confiscated | a low buzz and thud |
| a branch is docked | a rising two-note chime |
| a write is held for you | the alert horn: four square-wave notes |
| a job finished | a short, high ding |

The tones are synthesised in your browser: no audio file, no request, nothing
from the event's text. They are quiet, and at most four play a second: a burst
of calls gives one cue, the most serious of the burst (a held write, then
confiscated, docked, a finished job, passed). They play whether or not the floor is shown. The key
remembers your choice in this browser. Browsers only start audio after you
click or press a key, so a page that reloads with sound on stays silent until
your first click or key press. If the browser has no Web Audio, the key is
greyed out. `factory dashboard --demo` makes events every 20 seconds, enough to
try it.

There is also a hidden **factory radio**: type the Konami code on the main page
(up up down down left right left right, then `b` and `a`) and a looping ambient
track starts, chill sci-fi beats in D dorian at 72 BPM; type it again to stop.
It is synthesised in your browser like the cues (no audio file, no request,
nothing from the page or the events), quieter than they are, and a **RADIO**
key appears next to SOUND while it plays: press it to turn the radio off. The
choice is remembered in this browser, and, as with sound, a page that reloads
with the radio on stays silent until your first click or key press.

### Attention

Jobs run for minutes, so the dashboard tab is often behind another window. Three
signals say when something needs you, all worked out in the page from the state
it already polls (no request, nothing sent anywhere):

- **Title and icon.** While a write waits for a decision the title reads
  `(1 held) LOIS FC-1984` and the tab icon carries a red count; jobs that
  finished while the tab was not in front add `· 2 done`. The finished-jobs
  count clears when you come back to the tab; the held count goes when the
  write is decided.
- **Cues.** With SOUND on, a newly held write sounds the alert horn and a
  finished job a short ding, under the same key and the same limit of four
  cues a second as the floor events.
- **Notifications.** The **NOTIFY** key next to SOUND (or `n`) turns on browser
  notifications for the same two events. The browser asks permission when you
  press the key and never on load; if you refuse, the key greys out until you
  allow it in the browser's settings. A notification appears only while the tab
  is not in front, and says just `Write held · git_push` or `Job finished ·
  developer`: a fixed kind, then a tool from a fixed list or a lane name that
  matches the lane pattern, never text an agent wrote. The key's choice is
  remembered in this browser, and it only counts while the permission is still
  granted. A browser without notifications greys the key out.

`factory dashboard --demo` shows a held write at once, so the title badge is
there to see.

## Write gate and the control unit

Bottom right. With approvals on, every push and pull request a role makes
waits here for you ([Enabling approvals](../../README.md#enabling-approvals)).
With holds off, the gate always reads NO WRITES STAGED and the agents' writes go
through under the broker's policy alone.

The gate's header counts what needs you: the writes staged for a decision
and any that failed. Its display gives one line with every write's state:
**STAGED** (amber, blinking: waiting for you), **WRITING** (approved, and the
gateway is making it), **DONE** (green) and **FAILED** (red). Under it, the
**control unit** shows the selected write in full: the tool, the role and its
lanes, the repo, the branch (or head and base for a PR or merge request), its title, the
commits and files, and the time left. **◀ ▶** step through several staged
writes. (With the control unit hidden, the display shows the first staged
write instead, and the list of the gateway's writes appears under it.)

### Staged writes and their outcomes

Every held write is **staged**: the agent is told it is waiting for approval
and moves on at once, so the lane doesn't show HELD for it. The card stays on
the gate, marked **STAGED: the agent has moved on; the gateway writes it when
approved**. When you approve it the gateway makes the write itself, within
seconds, and the card reads **APPROVED, the gateway writes it within
seconds** until it does.

Under the card, **the gateway's writes** lists what the gateway did with
recent approvals, failures first: **FAILED** (in red, with the reason, kept
three days), **APPROVED · WRITING** (the gateway is making it now) and
**DONE** (with the first line of its result, such as the new PR's address;
kept a day). A write that failed was not retried: ask the agent to make the
request again. Once a write is done, the agent that staged it gets the same
result (the PR's address, say) if it makes the same call again, and nothing
is sent twice: the Coordinator can send the lane back once to collect it. The
event printer shows STAGED, WRITTEN and FAILED lines for the same steps. This
list only displays; it has no keys. The text in it comes from the gateway's
state files and is shown as plain text.

### Decide a held write

- **APPROVE** lets it through. One press decides.
- **DECLINE** refuses it. Type a reason first: it's required, and the
  recorded outcome keeps it.

A decision is final for that card. The gateway makes an approved write (or
files a declined one) within seconds; until then the card reads **APPROVED,
the gateway writes it within seconds** or **DECLINED**, shows who decided and
when, and both keys are dark. Pressing again, here or with
`factory approvals`, never changes the first decision; if two people decide
at once, only one decision is written and the other is told who got there
first.

An expired card reads **EXPIRED** with its keys dark: nothing is written,
and the agent's next call of that write stages a fresh card. If a newer write replaced the card while you were
reading, or it was decided elsewhere, the unit says so in words and decides
nothing. Check the new card and press again.

> **Note:** A card names the role, not the lane, and no card marks a lane
> HELD: the agent's call returned when the write was staged.

### Inspect a push before approving it

**INSPECT THE PUSH** appears on a held push whose patch the broker stored. It
fills the window with the push's commits: the commits and files on the left,
the changes on the right.

| Key | Does |
|---|---|
| `j` / `k` or ↓ / ↑ | Next / previous file |
| `n` / `p` | Next / previous hunk |
| Esc | Close the viewer |

Characters that can hide what code does (bidi controls, invisible and control
characters) appear as visible tokens such as `⟨U+202E⟩`, and the file gets a
badge: BIDI, INVIS or CTRL. LONG marks lines over 2000 characters, which are
cut; CRLF marks Windows line endings; BAD marks a part that couldn't be read.

A big push may be truncated (the broker stores 256 KiB by default). The
viewer then says TRUNCATED, lists the files it doesn't show, and ends with
"✂ CUT HERE". APPROVE on a truncated push asks once more, "Approve without
seeing N files?", and decides only on **CONFIRM APPROVE**, which lights after
a second.

APPROVE and DECLINE work inside the viewer too. If a newer write replaces the
one on screen, the viewer loads its patch and holds the keys for a moment so
you don't approve something you haven't seen.

Stands in for: `factory approvals`, `factory approvals approve <id>` and
`factory approvals decline <id> --reason "..."`.

### Diagnose, restart or update the factory

The **SERVICE** row has three keys. None runs on the first press.

- **DIAG** runs `factory doctor`: it checks this machine, its tools, Docker,
  the sandboxes and the gateway, and prints the fix for each problem. It
  changes nothing, so it cancels no jobs, drops no held writes and leaves
  Terminal 0 attached. Its report ends with NO PROBLEMS FOUND or PROBLEMS
  FOUND; fix what it lists, then press DIAG again.

- **RESTART** runs `factory restart`. First the unit shows what it will
  cancel: the running jobs and the held writes it drops. Staged writes are
  files the gateway keeps, so they are listed as kept, not dropped.
- **UPDATE** runs `factory update --yes`. First the unit shows the incoming
  commits, the sandboxes it will recreate and what each loses (from `factory
  update --plan`), plus the jobs and held writes it drops.

Only **CONFIRM** on that screen starts the command. Its output scrolls on the
same screen, and **LOG** shows the last run again later. One runs at a time.
While RESTART or UPDATE runs, Terminal 0 hangs up, and it reattaches afterwards if it was attached and the
command succeeded. If an update changed the dashboard itself, the unit asks
you to reload the page (or to restart `factory dashboard`, if its server code
changed).

The command keeps running if you close the dashboard. It runs without a
terminal, so anything that asks for a password fails instead of waiting. If
an update needs `sudo` (to install tools, say), run `factory update` in a
terminal.

Stands in for: `factory doctor`, `factory restart`, `factory update` and
`factory update --plan`.

### Your own look: dashboard.toml

`dashboard.toml` in your config folder (`~/.config/ai-code-factory/`; `factory
config init` writes a commented example) sets how the page looks. The
dashboard reads it once when it starts, so restart `factory dashboard` to see a
change. Every key is optional:

```toml
plant = "LOIS WORKS"                    # the header's nameplate and the floor's plate, up to 24 characters
shifts = ["day", "swing", "night"]      # the floor's three shift names, up to 16 characters each
crt = 3                                 # the CRT knob's starting level, 0 to 10
sound = false                           # whether the SOUND key starts on
[roles.colors]
designer = "#d08cff"                    # a role's colour on the floor, its lane module and the spend rows
```

- `crt` and `sound` are only the starting values. Once you turn the knob or press
  SOUND, this browser remembers your choice and the file no longer changes it
  (the knob's reset goes to the file's level).
- A role without a colour keeps its own: a built-in role its fixed one, any
  other role a colour chosen from its name. Role names are the usual
  lowercase letters, digits and dashes.
- The nameplate, the floor plate and the shift names are plain text, one line
  of printable ASCII, in `"double"` or `'single'` quotes without escapes. A
  colour must be `#rrggbb`. Values are one per line, the `shifts` list too.
- Nothing in it is run, and it cannot touch the broker, a role or a permission.
- It is strict. An unknown key or table, a bad colour, a number out of range,
  a key set twice, a file over 16 KiB, a symlink or text that is not UTF-8
  makes the dashboard ignore the whole file, show the defaults, print a
  warning naming the file, line and key, and show the same reason on the
  SETUP screen. The dashboard still starts. With no file you get the defaults.
  `--demo` never reads your file.

### Setup: the configuration in force

The **SETUP** key opens the configuration the factory will use, read on this
machine with the same code `factory start` uses. Most of it is read-only: edit
the files as [Make it yours](04-customize.md) describes. Two things can be
changed here: a role's tuning ([below](#tuning-a-role-from-setup)) and the
[project notes](#the-notes-editor). It refreshes every few seconds while open,
except while you are in the middle of turning a knob or editing the notes.

- **Roles.** One block per role: whether it is on (an optional role is on only
  if it is listed in `enabled-roles`), and `MODEL`, `MODELS`, `EFFORT`,
  `LANES`, `MAX_SESSION_TOKENS`, `MAX_TURNS` and `MAX_JOB_SECONDS`, each
  marked **REPO** (from the role's own `role.conf`) or **LOCAL** (your
  override in `roles/<role>.conf`). A LOCAL line also shows the value it
  replaced. `WORKSPACE`, `TEMPLATE` and `BROKER_TOOLS` are marked **FIXED**:
  an override can't change them, and a block says so if your override file
  tries.
- **Optional roles** and the names in `enabled-roles` (a name that matches no
  role is flagged).
- **Gateway.** The held tools and their expiry (`[approvals]`), the allowed
  repos (`[git].repos`), the GitLab address if one is set (without any user
  name, password or query), each marked REPO or LOCAL by the file it came
  from, and the Coordinator's broker tools.
- **Project notes.** The notes files for the project the dashboard is bound to,
  and for each running role its instructions plus the notes against the
  20,000-character limit. A role over it is flagged: the dispatch
  bridge refuses that role's tasks. **Edit notes** opens the
  [notes editor](#the-notes-editor).
- **Dashboard look.** Whether `dashboard.toml` was read at start-up, or the
  reason it was ignored. It does not show the values; the page itself does.

Each line ends with the command that applies a change to it, **START** or
**UPDATE**, as in the table in [Make it yours](04-customize.md#which-command-applies-a-change).
Tokens, passwords and other credentials are never shown, and it never runs a
command. If the files can't be read the screen says it is unavailable.

#### Tuning a role from SETUP

Each role block has a small rack of controls for the keys people change most.
They write the role's local override (`roles/<role>.conf` in the config
folder), never the repo's `role.conf`:

- **Lanes**: a dial with eight detents (1 to 8). Click it to turn it on, or use
  the arrow keys on it, or the ◀ ▶ keys beside it.
- **Model**: a selector over the role's `MODELS` list (plus the model set now).
- **Effort**: a switch for `low`, `medium`, `high` or `xhigh`.
- **Session tokens**, **Turns**, **Job seconds**: numbers (digits only; `0` is
  no cap or no limit, as in [Tune a role](04-customize.md#tune-a-role)).

A control that has a **LOCAL** value starts there; one that follows the repo
shows the repo's value (a number field is empty, with the repo's value as its
hint). Choosing **Role default**, or emptying a number field, or pressing **↺**
removes your override for that key, so the repo's value applies again. A row you
changed is marked `→ SET x` or `→ UNSET`.

Nothing is written when you turn a knob. **PREPARE** (one per role, lit when
something changed) sends only the changed keys and shows the result on the
amber tube: the override file's path, and the file's change as `+` and `-`
lines, as the host computed it. **CONFIRM APPLY** stays dark for 2.5 seconds
after the diff appears, so a diff is never confirmed unread, and the check
expires after two minutes. **CANCEL** (or Esc) goes back to the knobs with your
changes kept; nothing was written. If the file changed on disk after the
check, the host refuses to write over it: the screen says so, and you prepare
again.

When it is written, the screen says when it takes effect. **SERVICE DUE ·
RESTART TO APPLY** means press RESTART: these keys are read at `factory start`
(see the table in [Make it yours](04-customize.md#which-command-applies-a-change)),
and RESTART runs `factory restart`, which cancels running jobs, so its usual
check shows first. Every change here waits for a restart: the dispatch bridge
reads only the copy `factory start` took, so nothing applies live. While the
write is in flight CANCEL and Esc are dark, so the note always shows; the knobs
then show the values just written. In demo mode PREPARE answers
**DEMO · CHANGES DISABLED** and writes nothing. Broker policy, hosts, kits and
credentials have no knobs here: they stay read-only.

### The notes editor

**Edit notes** (under Project notes on SETUP) opens an editor for the project's
notes files, the ones [Make it yours](04-customize.md#per-project-notes)
describes. Like the tuning knobs, it is a write from SETUP, after a diff and a confirm.
It appears once the factory is bound to a project.

- **Tabs.** **SHARED** is `projects/<folder>.md`, which every role gets; each
  other tab is one role's own file, `projects/<folder>.<role>.md`. Opening a
  tab reads the file as it is on disk (a file that doesn't exist yet is an
  empty box, and saving creates it). Switching tabs with unsaved changes asks
  first: keep editing, or discard.
- **Gauges.** One line per role: that role's instructions plus the shared notes
  plus its own notes, against the 20,000-character limit, counted as the
  dispatch bridge counts it (headings included). It turns amber from 90% and
  red over the limit, and updates as you type. **SAVE** stays dark while any
  role the file reaches would be over the limit and higher than it is now,
  because the bridge would refuse that role's tasks. A change that shortens a
  role that is already over is allowed, since that is the way back under. The
  box takes at most 20,000 characters, counted as the host counts them (an
  emoji is one character).
- **Saving takes two steps.** **SAVE** shows what would change: the file's
  path, the lines added and removed as a diff, and each role's new total.
  Nothing is written yet. **CONFIRM** stays dark for 2.5 seconds, so a press
  can't land on a screen you haven't read, and then writes the file.
  **CANCEL** drops the pending save (while the write itself is in flight CANCEL
  and Esc are dark). A confirmation not used within two
  minutes expires.
- **After a save** the editor says "SAVED · SERVICE DUE · RESTART TO APPLY":
  the new notes reach the roles when the factory next starts, so the SERVICE
  lamp shows RESTART due. Press **RESTART** (or run `factory start`).
- **A file that changed meanwhile.** If the file changed on disk between SAVE
  and CONFIRM (you edited it in a terminal, say), nothing is written and the
  editor says so. **RELOAD** reads the file again; **KEEP MY TEXT** goes back
  to your edit.
- **Demo mode** shows the editor but writes nothing: SAVE answers
  "DEMO · CHANGES DISABLED".

The text is drawn with `textContent` and the diff's lines get their colour from
a fixed list, so nothing in a notes file can act on the page. The page holds no
key of its own: the control unit sends every request, and the file is written
by the host only after the token SAVE returned.

## Spend and the event printer

- **Spend / hour:** today's cost as a bar per hour since midnight, then per
  role, with today's job count.
- **Shift budget:** under the role bars, type the dollars per day you want to
  stay inside (for example `50`; `$12.50` works, cents at most, up to
  100000). The meter beside it fills against today's spend with the budget at
  its midpoint tick, and the **Spend today** readout in the header turns amber
  once spend is past the figure and red once it is past twice the figure
  (exactly at the figure or at twice it doesn't tip over). Up and Down in the
  box step by 5. Empty or `0` means no budget: nothing tints. A figure the box
  can't read is marked and the last good one stays. It is kept in this browser
  with the knob settings (`localStorage`), so it isn't shared between browsers
  or machines, and a stored value that isn't a sane number is ignored. It only
  colours the display: nothing stops, holds or cancels a job. It doesn't show
  per-job cost: the bridge records a job's cost when the job ends, so a
  running job has none to compare yet.
- **Event printer:** today's broker calls (allowed, denied, staged and
  decided), the gateway's deferred writes (WRITTEN, or FAILED in red) and
  finished jobs, newest at the bottom. Denied, staged and failed calls,
  and failed or cancelled jobs, print in red; the header counts the denied ones. Scroll up to read older lines; the printer stays put
  until you scroll back down.
- **Printer lines act.** Click a line, or Tab to it and press Enter:
  - a **JOB** line opens that lane's monitor on that job's review packet
    (Esc closes it and puts you back on the line);
  - a **STAGED**, **WRITTEN** or **FAILED** line shows that card in the
    control unit. It only changes which card is on the glass: nothing is
    approved or declined, any reason you had typed is cleared, and the
    keys stay dark for a moment, as for any card that arrives unasked. A
    card the gateway has already written says "That write is no longer
    held.";
  - a **DENY** or **FAILED** line opens in place to its whole text, wrapped,
    so you can read the reason (for example "bundle does not exist in the
    workspace"). Press Enter again or Esc to shut it.

  ALLOW, OK and NO lines don't act; hover any line for its full text.

For the full report, run `factory analytics` (or `factory analytics --html`).

Stands in for: part of `factory analytics` and of `factory logs broker` /
`factory logs dispatch`.

## Keys

| Where | Key | Does |
|---|---|---|
| Anywhere | `1`–`9` | Open that lane's monitor |
| Anywhere | `f` | Factory floor on / off |
| Anywhere | `s` | Sound on / off |
| Anywhere | `n` | Browser notifications on / off |
| Lane monitor | `t` / `p` | Transcript / review packet |
| Lane monitor, patch viewer | Esc | Close |
| Event printer line | Enter / Esc | Act on the line / shut an opened line |
| Patch viewer | `j` `k` ↓ ↑ | Next / previous file |
| Patch viewer | `n` / `p` | Next / previous hunk |
| A knob | arrows, PgUp, PgDn | Turn it |
| A knob | Home, double-click | Reset it |

While Terminal 0 has the focus, keys go to the Coordinator. Click outside the
screen first to use the dashboard's keys.

## What still needs a terminal

The dashboard covers watching, talking to the Coordinator, approving, and
diagnosing, restarting or updating. Run these in a terminal:

| Command | Why it isn't on the dashboard |
|---|---|
| `factory start`, `factory stop` | Starting the factory on a project, and stopping it. |
| `factory status` | The full status (the lamps give only the summary; DIAG runs the health check). |
| `factory secret ...` | Credentials never go through the browser. |
| `factory role new`, `factory roles` | Roles and their kits are part of the security boundary. |
| `factory reset`, `factory setup` | They remove sandboxes or install tools, and may ask for a password. |
| `factory analytics`, `factory packet --list` | The full cost report and older review packets. |

## Demo mode

To look at the dashboard before you set anything up, or to take a screenshot
without running agents:

```bash
factory dashboard --demo
```

It needs no `sbx`, broker, Coordinator or `factory setup`. It shows made-up
data (`example/app`, a few lanes, one held push, a denied broker call and a
branch, two outbox rows) taken from `scripts/dashboard-demo/`, with times counted from the
moment you start it, and adds a broker call 8 seconds after the start and then
every 20 seconds, so the factory floor has crates to move. Open the link it prints, press the floor key and
you can see a held crate waiting at the gate and a denied one landing in the derez field.

Demo mode is display only. It never reads your real state folder: it copies
the fixture into a fresh temporary folder and deletes it when you press
Ctrl-C. Terminal 0 is off, APPROVE, DECLINE, RESTART, UPDATE, DIAG and the notes editor's SAVE answer
that nothing here acts, INSPECT shows the fixture's patch, and SETUP shows
the fixture's configuration (`scripts/dashboard-demo/setup/`), not yours, and its
PREPARE says DEMO · CHANGES DISABLED. It also ignores your `dashboard.toml` and shows the defaults. The same
token, cookie and content rules apply as for a real run. `--demo` can't be
combined with `--coordinator`, `--state`, `--project` or `--factory`;
`--no-open` and `--port N` work as usual.

## Is it safe to leave open?

It is local: it listens only on `127.0.0.1`, refuses any request without
this run's token, and makes no network connection of its own. Apart from
Terminal 0 and the control unit it only reads the factory's state folder, and
for the [outbox](#the-outbox) asks your project's git, read-only and with hooks
off, which `factory/*` branches exist, how far ahead of `HEAD` they are and
whether their changes are already in `HEAD`.
The control unit can write only a decision file (APPROVE, DECLINE), its
action logs and, from SETUP, a role's local tuning override or the project's notes (each after a diff and a
confirm), and run only `factory doctor`, `factory restart`, `factory
update --yes` and `factory update --plan`, each after a confirm screen. The README's
[Dashboard](../../README.md#dashboard) section has the details.

## Next steps

- [Build a feature](02-build-a-feature.md): the full loop, from prompt to
  merge.
- [Troubleshooting](05-troubleshooting.md): when something doesn't work.
