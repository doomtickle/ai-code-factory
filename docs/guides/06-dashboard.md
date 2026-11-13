# Use the dashboard

The dashboard is LOIS on one screen in your browser. You watch the day's work
as a galaxy, ask it questions, talk to the Coordinator and decide the writes
waiting for you.

It is **The Thoughtfield**, the dashboard's only view. (The older FC-1984
control panel and its factory floor were retired in November 2026: see
[History](#history).) This guide says what every part does and which `factory`
command it stands in for. The security details are in
[Dashboard](../../README.md#dashboard) in the README.

## Open it

`factory start` opens the dashboard. To open it again later, while the factory
is running:

```bash
factory dashboard
```

It prints a link and tries to open it in your browser. The link carries a
token for this run, so it works only on this machine and stops working when
you press Ctrl-C where `factory dashboard` runs. Ctrl-C closes the dashboard,
not the factory: the agents keep working, and `factory dashboard` brings it
back. `--no-open` only prints the link; `--port N` picks the port.

The Thoughtfield opens straight onto the sky, with no welcome screen. Until you
click or press a key in the page (or click into Terminal 0), your browser keeps
sound shut: LOIS's first lines show as captions only, and she speaks from your
first click on.

**What it needs.** WebGL, which means WebGL 2 in any current browser. A real
GPU is strongly recommended: with software rendering it runs at single-digit
frames per second. Without WebGL the caption says so ("This view needs
WebGL. Try a browser or setting where WebGL is on."), and the job list, the
decision row and Terminal 0 still work.

**Where it is.** The Thoughtfield is at `/` and at `/field`. A bookmark to the
old `/classic` address lands on `/`.

## What you're looking at

LOIS's bust stands among today's jobs, one crystal each, above the dark curve
of a world. The lit edge of that world is today.

When nothing has happened for about 20 seconds (no job running, no write waiting,
nothing said, and you haven't typed or clicked), LOIS closes her eyes and her head
sinks a little; moving the pointer doesn't wake her, but a key, a click, a new line,
a job or a waiting write does. While a job runs, or while she is thinking about your
line, a ring of light circles her head; with reduced motion on, the ring stands still.

- **The galaxy**, the crystals round LOIS, is today's work. See [Read the galaxy](#read-the-galaxy).
- **The horizon** is today's timeline, with the day's events on it as beads. See
  [Rewind the day](#rewind-the-day).
- **The caption**, under the horizon, is what LOIS is saying. A smaller detail
  line under it carries names and numbers.
- **The question chips** are under the caption. See
  [Ask the sky](#ask-the-sky).
- **Terminal 0** is docked along the bottom once it runs. See
  [Talk to LOIS](#talk-to-lois).
- **The decision row** sits above the chips. With a write waiting, it
  shows that write. With nothing waiting, it shows the service keys. See
  [Decide a held write](#decide-a-held-write).
- **The work list**, lower left, shows how many jobs are running and how many
  writes wait for you. Under that are up to four rows: waiting writes first
  (two at most), then the active jobs, and one more row if jobs failed today.
  Hover a row to light its stars. Press a job's row to open it, or a waiting
  write's row to select it in the decision row.
- **Today**, lower right, has today's spend, today's job count and
  the top three roles by spend. A note appears here if the dispatch
  bridge is down or a service is due.

The top bar has the name of this dashboard and your project, then these keys:

| Key | Does |
|---|---|
| Terminal (or `t`) | Opens Terminal 0, the Coordinator. Its lamp is lit while the Coordinator runs. |
| The book | Opens these docs in a tab of their own. They are the docs from your factory checkout when the dashboard started: restart `factory dashboard` to see edits. |
| Sliders (or `,`) | Opens [Settings](#settings-the-configuration-in-force). |
| `?` | Opens the key to the sky: what each mark means, the stars-to-the-dollar rate, and a link to tune the voice. |
| Speaker (or `v`) | Sound and voice on or off. |
| Corners | Fullscreen. |

A start-up hint, such as a first login still to do or a missing GitHub token,
shows as one line in the top bar. While a write waits, the tab's title shows
the count, such as `(1) LOIS`.

## Read the galaxy

The galaxy is real data, from the dispatch audit, the lanes' transcripts and
the gateway's audit. An action reaches the page only as a time and a kind, so
what you see in the sky can't carry a command or a path. A job's short labels
are the only words, and they are drawn as plain text.

| What you see | What it means |
|---|---|
| A crystal | One job. Bigger is more spend. Hover it to see the job and its order's history; click it to open the job's monitor. |
| A label beside the crystals | One order, always showing: its name (the branch, or a lone job's own words) and where it stands, in words: `Working`, `Reviewing`, `Fixing`, `Changes requested`, `Approved`, `Failed`, `Done` or `Waiting for you`. The words come from the order's newest job. A small diamond in the label has the colour of the order's crystals. |
| A number on a crystal | The job's step in its order, by start time: 1 first. A write waiting on you is the step after the last job. A crystal too small to hold a number shows none. |
| A mark on a crystal's corner | The job's state: a spinning ring is running, a tick is finished, a green tick is approved, `!` is changes requested, a cross is failed, a pulsing gold dot is waiting for you. Gold means only that. |
| A star inside a crystal | A share of the job's spend, placed at the time of one of its actions. The oldest work is at the core of the crystal's spiral. Stars take the order's colour; the pour of kinds is in the question views (`What have they been doing?`). |
| Crystals of one colour joined by a thread | One order. |
| Where a crystal sits | The crystals ring LOIS's head, never behind it. Along a ring, time runs from the left horizon, up round the head, down to the right: left is earlier. With many jobs there are several rings, the newest innermost. |
| A bright spark inside, and a crystal that turns | A running job. Brighter is a bigger model. |
| A small spark and a cool sweep | A review running. |
| A gold crystal | A write waiting for you, beside its order. Its order stands still until you decide. |
| A dark crystal round a void | A failed, cancelled or interrupted job. |
| Red sparks on a crystal's shell | A request the gateway blocked. |
| A ring round a crystal | The job whose monitor is open. A faint ring stays on the last one you opened. |

**An order** is all the jobs on one branch, the review included. A job with no
branch stands alone.

**When there isn't room for every label** (a small window, many orders), the ones
that wait on you, are running or have failed keep theirs, then the newest; the
rest are left out, and their crystals keep their numbers and marks. A label never
covers a crystal or LOIS, except on a phone-sized window where a waiting or
running order's label has to sit over the sky it can find. A label far from its
crystals is joined to the newest one by a dashed line. Labels go away in the
question views and while you rewind the day.

**How many stars.** A job's star count is its spend times a rate. LOIS picks
the rate when the page loads, between 150 and 6,000 stars to the dollar, so
that the day fills the sky. It picks again if the day outgrows the star limit.
The `?` key shows the rate in use. The rate changes with the day's spend, so
compare stars within a day, not between days.

**Where the numbers are less exact:**

- A job's cost is recorded when it ends. Until then its stars are an estimate
  from how many actions it has taken.
- A star is a share of spend, not one action. A job with many stars repeats
  its actions' times and kinds, and a job with few skips some.
- A job whose transcript has rolled away keeps its stars, spread over its run
  time, in the usual mix for its role.
- The page draws the newest 60 jobs of the day and at most 62,000 stars.

**Hover** a crystal to see its order's history: its steps, oldest first, each
with the time, the lane and what happened (`10:15 reviewer-lane2 review started`,
`10:21 asked for changes`), and the step you are on marked. A long order shows its
latest eight steps and counts the rest. Hover a star, a spark or a void to see
what it is: the kind and time, the job's task, its lane and model. **Click** one to open that job
([Open a job](#open-a-job)). Clicking a gold star selects that write in the
decision row instead.

## Ask the sky

Press a chip, or press `1` to `4` (the page, not the terminal, must have focus):

| Ask | The sky |
|---|---|
| What cost the most? | The stars pile up into towers, drawn to scale. LOIS names the share of today's spend. |
| What's stuck? | Healthy work dims. Failed jobs, waiting writes and orders with changes requested stay lit. |
| What have they been doing? | A crown, sorted by kind. LOIS says which kind was most common. |
| When was it busiest? | A skyline over the hour marks. Each column is ten minutes. Type it: it has no chip now. |

To go back to the galaxy, press **Back to the galaxy** or Esc, or leave the
page alone for 14 seconds. Moving the mouse restarts that count.

This is keyword matching, not comprehension. See
[what stays in the browser](#what-stays-in-the-browser).

## What do you need from me?

The last chip, **What do you need from me?**, opens a small panel in the upper
left of the sky with what is waiting on you: decisions, branches to merge, held
writes, one short line each, with commands written out so you can copy them. It
pops up by itself when the Coordinator posts a new list, which it does whenever
something new is waiting on you or an item is done, and the chip opens it
again. The panel shows its age ("2 min ago"), **Copy** and a close key, and you
can select part of the text by hand and copy it with your browser's own copy
(Ctrl+C or right-click, Copy); a poll never clears your selection unless the text
itself changes. It shrinks and dims after 10 minutes but never
closes by itself. Closing it is your choice.

- **Copy** writes the text on screen to the clipboard only when you click it, and
  stays dark for 2.5 seconds after the text changes, so a list swapped in just
  before your click is never the one copied unread. The page never reads your
  clipboard.
- The list comes from the Coordinator's `show` tool, and the Coordinator can be
  influenced by what it reads, so read a command before you paste it into a
  terminal. Control characters are removed, and invisible and direction
  characters appear as `⟨U+XXXX⟩` with a warning above the text.
- The chip sends nothing to the Coordinator or the terminal. To ask, type
  "What do you need from me?" in Terminal 0.
- **A known limit:** the panel shows the last list the Coordinator posted, with
  its age. It can't refresh itself, and the Coordinator doesn't see merges you
  make on your own, so an old list may name something already done. Ask again to
  get a new one.

## Rewind the day

Drag along the horizon. The galaxy rewinds to that moment, and the time and the
nearest event show on the horizon. Let go and it re-forms up to now. Gold stars
hide while you rewind.

The beads on the horizon are the day's events: finished jobs, staged writes,
approvals and declines, writes made, failures and blocked requests. Hover a bead
for its words.
Click a job's bead to open that job. Click a write's bead to select it in the
decision row.

## Open a job

Clicking a star, a spark, a void, a job row or a job's bead opens the monitor
for that lane. It has three tabs:

- **Transcript:** the lane's full transcript, live while a job runs.
- **Packet:** the job's review packet: the task, each branch's commits and
  diffstat, the tests it ran, the broker calls it made, its cost and the role's
  report. A running job has no packet yet, so this tab shows its task, model,
  time so far and cost so far.
- **Changes:** what the job changed, file by file. The patch is the one the
  dispatch bridge made when it imported the branch, so it doesn't change if the
  branch later moves. Characters that can hide what code does show as
  `⟨U+XXXX⟩`.

Opened on a job, the monitor starts on **Packet**. **Older** and **Newer** (or
`[` and `]`) step through the lane's past jobs. Esc or **Close** closes it.

Stands in for: `factory watch`, `factory logs <role>` and `factory packet
<lane>`.

## Talk to LOIS

LOIS is the Coordinator: the face and voice are its own. Terminal 0 sits low in
the world, small by default. Click into it to make it bigger and type; nothing
is sent until you press Enter in it.

- The page never focuses it for you. A state change doesn't steal your keys.
- While you are in it, the sky dims and draws in. When LOIS speaks (its `say`
  tool), the sky blooms back. With reduced motion set, it changes brightness
  with no drift.
- When you press Enter in it, LOIS thinks: the ring of light comes up with
  "Thinking…" and the stars draw in a little. While the terminal is printing, a
  faint shimmer runs along the pane's edge. When it goes quiet the stars ease
  back, and the ring stops when LOIS speaks or after a few minutes. With reduced
  motion set there is no drift and no shimmer, only a steady ring. The terminal
  tells the page just that Enter was pressed, that output is arriving and that it
  stopped, never what was typed or printed, or how much.
- Press **Smaller** on the pane bar, `t`, or click the world to shrink it. `Esc`
  inside it goes to the Coordinator, so the pane bar says so.
- It answers aloud with its `say` tool, in its own words, and the caption shows
  them. Code, paths and lists stay in the reply in the terminal. What is waiting
  on you goes in the pop-up from its `show` tool (see
  [What do you need from me?](#what-do-you-need-from-me)).
- A line from the factory itself, such as a write going out, waits until it has
  finished.
- If it is off, open **Terminal** at the top to power it on.

The Coordinator reads tickets, files and role reports, and text in them could
influence what it says. Speech acts on nothing. To approve a write, look at the
write and hold the light yourself.

If it goes quiet without speaking, its answer is still in **Terminal**. The
bridge's audit log records every line it says.

### Talking to LOIS

You can also talk. It needs the local voice (`factory voice start`) with
listening: the strip along the bottom of Terminal 0 has a microphone key and an
**Always on** switch, both dark (hover for why) while the voice is off. What you
say goes to the voice on your own machine (`127.0.0.1`), never to a cloud
service, and the words it hears go where your typing goes: into Terminal 0, the
text and then Enter, only while the Coordinator runs. Voice approves nothing:
approving is still the held light.

- **Push to talk.** Hold the microphone key, speak, let go. The words are typed
  at once. A hold is cut at 60 s; `Esc` during it drops it.
- **Always on** (off until you switch it on; your browser remembers). The
  microphone stays open while the tab is visible, and its lamp is red while it
  listens. Just talk, as in a conversation: no wake word. Each pause of about a
  second ends what you said, and anything over 30 s is cut there. Every line with
  words in it goes to the 2 s window below. If it starts with "Lois", "Louis" or
  "Computer" (also "Hey Lois"), that name is not sent: "LOIS, run the tests" types
  "run the tests". A line that is only her name, or has no words, is ignored and
  nothing shows. Anything said in the room reaches the window too, so switch it
  off (or press `Esc`) when you are talking to someone else.
- **The 2 s window.** What you said waits in the strip for 2 seconds
  with a countdown, then are typed. Press `Esc` (on the page or in the
  terminal) or click the line to cancel; nothing is sent. A newer line replaces
  a waiting one.
- **While LOIS speaks**, and for 300 ms after, always-on hears nothing ("paused
  while LOIS speaks"), so she never answers her own voice. Push to talk still
  works.
- Pressing either key, or the waiting line, doesn't grow the pane or move your
  keys into the terminal.

The browser asks for the microphone once for Terminal 0's address. If you
refuse, the strip says so and both keys stay dark until you allow it in the
site settings and reload.

### What stays in the browser

Nothing you type or say. There is no typed line on the main page: every word
you type or say goes into Terminal 0 and so to the Coordinator, and the page
itself sends no text anywhere.

The sky's views are keys and clicks instead:

| Key or click | View |
|---|---|
| `1` | What cost the most |
| `2` | What's stuck |
| `3` | What they've been doing |
| `4` | When it was busiest |
| Double-click the horizon | When it was busiest |

The keys work while the main page has the keyboard, not while you are typing in
Terminal 0 (click the world first). The questions under the caption do the same.

### Terminal 0

Terminal 0 is the pane low in the world (see [Talk to LOIS](#talk-to-lois)).
Press **Terminal** in the top bar, or `t`, to make it bigger. It is the
Coordinator's live terminal: type to it as you would in any terminal.

It is built to read easily. The text is JetBrains Mono on a solid dark
background, not the sky, and the sky dims while you type in it. The Coordinator
chooses many of its own colours, so the terminal lifts any text that would be
hard to read against its background, including dimmed text. Box and block
drawings keep their colours. To make the text larger, use your browser's zoom
(Ctrl and +, or ⌘ and + on a Mac).

Pressing **Terminal** or `t` gives it the keyboard, so `t` and Esc then go to
the Coordinator. Press **Smaller** to shrink it, or click the world first and
press `t` or Esc.

Opening the dashboard brings the Coordinator up once per page load, continuing
its last session (a Resume), if it is off and not open in `factory chat`.
Later, when the pane says "LOIS is off", **Resume** continues the last session
and **Power on** starts a fresh one.

- Every tab you open shows the same session. The last tab you resize or click
  into sets its size.
- A tab you open later replays the recent output first, with its keyboard
  muted until the replay is done.
- **Power on starts a fresh session.** Checked on 30 October 2026: the
  Coordinator was told a word to remember, the dashboard was closed, Power on
  was pressed again, and the new session had no memory of it. That is the only
  thing that check covered. **Resume** (`sbx run --name <name> -- --continue`)
  has not been tried on a live host yet, so it is untested there.
- **Restart** or **Update** from the decision row hangs up Terminal 0 and, when
  it finishes successfully, attaches again with Resume, not Power on, so the
  day's context survives. If the Coordinator wasn't attached when you pressed
  it, nothing is attached afterwards.
- In a plain terminal, `factory chat` is a fresh session and `factory chat
  --resume` is Resume (`sbx run --name factory-coordinator -- --continue`).
  If Resume ever shows a fresh session (nothing to continue), type `/resume`
  inside the session to pick an earlier one from Claude Code's own list.

**Copying out of Terminal 0.** There are two ways:

- **The Copy prompt.** When the Coordinator copies something to your
  clipboard (Claude Code sends its copies through the terminal), a strip at the
  bottom of the pane says "LOIS copied N characters. Press to copy." and shows
  the start of the text. Press **Copy** to put it on your clipboard, or
  **Dismiss**. This copies the exact text, without the screen's line breaks or
  padding, so prefer it for commands. Nothing reaches your clipboard until you
  press Copy, because the text comes from an agent; check the preview first.
  Copy stays dark for 2.5 seconds after the text appears or changes, so a
  newer copy can't slip in just before your click. A newer copy replaces the
  one waiting, and "⚠ Contains hidden characters" means the preview shows
  invisible or right-to-left characters as `⟨U+XXXX⟩`. If the browser blocks
  the copy, the prompt selects the text in a box and asks you to press Ctrl+C
  (⌘C on a Mac). A copy over 64 KiB can't be taken: the strip says "LOIS
  copied about N KiB. Too large to copy (limit 64 KiB).", with Dismiss only.
- **Select it on the screen.** The Coordinator takes the mouse, so a plain
  drag selects nothing. Hold **Shift** while you drag on Windows (WSL) and
  Linux, or **Option** on a Mac. Then copy with **Ctrl+C**, **Ctrl+Shift+C** or
  **Ctrl+Insert**, or **⌘C** on a Mac, or the browser's Copy menu. Copying
  clears the selection, so the next Ctrl+C goes to the Coordinator as an
  interrupt, as Ctrl+C always does with nothing selected (and on a Mac). Trailing spaces are trimmed
  from each line, but what you get is what the screen shows: a long command
  the Coordinator wrapped comes out as several lines with their indentation,
  and anything cut off at the edge is missing. Use the Copy prompt for those.

The dashboard never reads your clipboard. Pasting into Terminal 0 works as in
any terminal.

**FIRST SHIFT.** One line in the top bar tells you what `factory start` would
print in the terminal you started it from, since you're looking at the browser:

- No Claude login stored yet: "FIRST SHIFT · 1. POWER ON and type /login · 2.
  press RESTART". The Restart key's light blinks until you've done it.
- Login stored, but the role sandboxes aren't created yet (the state right
  after your first `/login`): it says so, and Restart still blinks. Press it
  and Confirm; the restart creates the roles.
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

When the pane can't attach, it says why:

| The pane says | What to do |
|---|---|
| Open elsewhere | The Coordinator is open in `factory chat`. Close it there, then Power on or Resume. |
| LOIS isn't set up yet | Its sandbox doesn't exist yet: run `factory start`. |
| sbx can't run | `sbx` couldn't run: run `factory doctor`. |
| LOIS couldn't start | Power on to try again. |
| LOIS is off | The session ended. Power on starts a fresh session; Resume continues the last one. |
| Restarting or updating | A Restart or Update is running. LOIS comes back when it's done. |
| Lost the dashboard | The page lost the dashboard. If `factory dashboard` stopped, run it again and open the new link it prints. |

Stands in for: `factory chat`.

## Decide a held write

With approvals on, every push and pull request a role makes waits for you
([Enabling approvals](../../README.md#enabling-approvals)). When one arrives, a
chime sounds, a gold star appears and LOIS says so. The decision row shows the
write: its title, repo and branch, and when it expires. Hover the gold star for
its commits and files. **◀ ▶** step through several.

**To approve,** press and hold the gold light for 2.5 seconds. The ring fills,
then Approve is pressed for you. Letting go early sends nothing. To use the
keyboard, Tab to the light and hold Space or Enter.

- The light is dark for 2.5 seconds after a new card arrives, so a card that
  appears just before you press can't be approved unseen.
- If the push is too big for the gateway to have stored all of it, the first
  hold asks "Approve without seeing N files?". Hold the light again to approve
  anyway, or press **Cancel**.

**To decline,** press **Decline**. A box opens for your reason, which is
required, and the agent reads it. Press Enter to decline. Esc backs out and
clears the box.

**A decision is final** for that card. The gateway makes an approved write (or
files a declined one) within seconds. Pressing again here or with
`factory approvals` never changes the first decision, and if two people decide
at once only one decision is written. If a newer write replaced the card while
you were reading, or it was decided elsewhere, the row says so and decides
nothing.

**You don't have to be quick.** Every held write is staged: the agent's call
returned when the write was staged, and the agent has moved on. Nothing is sent
until you approve. An undecided card expires after a day by default, and LOIS
says so. Nothing is written then, and the agent's next call of that write stages
a fresh card.

**If an approved write fails**, LOIS says so and the event appears in red on the
horizon. The Thoughtfield doesn't list the reason in full: run `factory
approvals`. A failed write is not retried: ask the agent to make the request
again. Once a write is done, the agent that staged it gets the same result (the
PR's address, say) if it makes the same call again, and nothing is sent twice.

Stands in for: `factory approvals`, `factory approvals approve <id>` and
`factory approvals decline <id> --reason "..."`.

### Inspect a push before approving it

**Inspect** appears on a held push whose patch the broker stored. It fills the window with the push's commits:
the commits and files on the left, the changes on the right.

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
viewer then says "Cut short", lists the files it doesn't show, and ends with
"✂ Cut here". Approve on a truncated push asks once more, "Approve without
seeing N files?". The light goes dark for a second, then says **Hold again to
approve anyway**, and only that second hold decides.

You can decide inside the viewer too, and it works as it does on the decision
row: hold the gold light for 2.5 seconds to approve, or type a reason and press
**Decline**. Letting go early, moving off the light or the card changing sends
nothing. If a newer write replaces the one on screen, the viewer loads its patch
and keeps the light dark for a moment so you don't approve something you haven't
seen.

## Diagnose, restart or update the factory

These keys are in the decision row. With nothing held they are already
showing. With a write held, press **Service**, and **Back to the decision**
returns. None runs on the first press.

- **Diagnose** runs `factory doctor`: it checks this
  machine, its tools, Docker, the sandboxes and the gateway, and prints the fix
  for each problem. It changes nothing, so it cancels no jobs, drops no held
  writes and leaves Terminal 0 attached. Its report ends with "no problems
  found" or "problems found"; fix what it lists, then press it again.
- **Restart** runs `factory restart`. First the unit shows what it will cancel:
  the running jobs and the held writes it drops. Staged writes are files the
  gateway keeps, so they are listed as kept, not dropped.
- **Update** runs `factory update --yes`. First the unit shows the incoming
  commits, the sandboxes it will recreate and what each loses (from `factory
  update --plan`), plus the jobs and held writes it drops.

Only **Confirm** on that screen starts the command. Its output scrolls on the
same screen, and **Log** shows the last run again later. One runs at a time.
While Restart or Update runs, Terminal 0 hangs up, and it reattaches afterwards
if it was attached and the command succeeded. If an update changed the dashboard
itself, the unit asks you to reload the page (or to restart `factory dashboard`,
if its server code changed).

The command keeps running if you close the dashboard. It runs without a
terminal, so anything that asks for a password fails instead of waiting. If an
update needs `sudo` (to install tools, say), run `factory update` in a terminal.

When a sandbox is out of date or a setting read at start-up has changed, the
light on the key that applies it comes on, and a note appears under Today.

Stands in for: `factory doctor`, `factory restart`, `factory update` and
`factory update --plan`.

## Settings: the configuration in force

Press the sliders key in the top bar, or `,` (comma), or **Settings** among the
service keys. (It was called SETUP in earlier versions.) Esc closes it.

The screen is titled "Settings: saved and running" because it shows two things
for a setting: what is **saved** (the files in your config folder, read with the
same code `factory start` uses) and what is **running** (the copy `factory
start` made for the dispatch bridge, which is all the bridge ever reads). A
line under the title says what you can do here: most of it is read-only (edit
the files as [Make it yours](04-customize.md) describes), and two things can be
changed, a role's tuning ([below](#tuning-a-role-from-settings)) and the
[project notes](#the-notes-editor). Nothing is live: a change is saved at once
and runs after **Apply now** (from each role's next job) or **RESTART**.

When a saved value differs from the running one, its line says **saved X ·
running Y**, and a bar at the top of the screen counts them: "3 changes saved,
not yet applied · applies on Restart · Apply now applies from the next job". The bar is there whenever you open
Settings and goes away when nothing is waiting, so you can tell whether a
change has taken effect. It compares tuning keys, which optional roles are on,
each project notes file (a deleted one counts) and a role's `instructions.md`;
note text is compared on the host and never sent to the page. The same changes light the Restart key. Only your overrides and notes go through the start's snapshot; the bridge reads the committed `role.conf` live, so a key you haven't overridden runs at its current `role.conf` value. A notes file that is a symlink isn't compared. Before the first
`factory start` there is nothing running to compare, and the screen says so.
Its **Apply now** key applies the saved tuning, notes and instructions
without a restart: it shows the changes it will apply and those that still
need a restart (an optional role switched on or off), and once you press
**CONFIRM** (dark for 2.5 seconds) runs `factory config apply`, which
refreshes the bridge's copy and nothing else. No job is cancelled: a job that
is running finishes with what it started with, and each role's next job uses
the new values. Terminal 0 stays attached, and the bar and the running values
update on the next refresh. Changing the lane git identity (`[git]` in
`policy.toml`) still needs a restart.
Each line ends with one of four plain tags: **next job (after Apply)** (tuning
keys and notes), **applies on Restart** (which optional roles are on), **needs
Update** or, for the look, **needs dashboard restart**. The screen has four
tabs, one section at a time, and refreshes every few seconds while open, except while you are in the
middle of turning a knob or editing the notes. Switching tabs keeps your
unsent knob changes.

- **Roles** tab. One block per role: whether it is on (an optional role is on only
  if it is listed in `enabled-roles`), and `MODEL`, `MODELS`, `EFFORT`,
  `LANES`, `MAX_SESSION_TOKENS`, `MAX_TURNS` and `MAX_JOB_SECONDS`, each
  marked **REPO** (from the role's own `role.conf`) or **LOCAL** (your
  override in `roles/<role>.conf`). A LOCAL line also shows the value it
  replaced, and a key that is not yet running shows what is. `WORKSPACE`,
  `TEMPLATE` and `BROKER_TOOLS` are marked **FIXED**:
  an override can't change them, and a block says so if your override file
  tries.
  Also on this tab: the **optional roles** and the names in `enabled-roles` (a name that matches no
  role is flagged).
- **Gateway** tab. The held tools and their expiry (`[approvals]`), the allowed
  repos (`[git].repos`), the GitLab address if one is set (without any user
  name, password or query), each marked REPO or LOCAL by the file it came
  from, and the Coordinator's broker tools.
- **Project notes** tab. The notes files for the project the dashboard is bound to,
  and for each running role its instructions plus the notes against the
  20,000-character limit. A role over it is flagged: the dispatch
  bridge refuses that role's tasks. A file that is new, edited or deleted
  since the start says so, with the size the bridge still has. **Edit notes** opens the
  [notes editor](#the-notes-editor).
- **Dashboard look** tab. Whether `dashboard.toml` was read at start-up, or the
  reason it was ignored, and the nameplate the dashboard is running with.
  There is no view to choose: the Thoughtfield is the only one. Read-only, and
  read once when `factory dashboard` started, so a change to the file shows
  after you restart the dashboard.

The tag at the end of a line says what applies a change, as in the table in
[Make it yours](04-customize.md#which-command-applies-a-change): **applies on
Restart** (`factory start`) or **needs Update** (`factory update`; a role's
`BROKER_TOOLS` needs only a restart if it had some and keeps some).
Tokens, passwords and other credentials are never shown, and it never runs a
command. If the files can't be read the screen says it is unavailable.

### Tuning a role from Settings

On the Roles tab, each role block has a small rack of controls for the keys people change most.
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
changed is marked `→ will be x` or `→ back to the repo value`.

Nothing is written when you turn a knob. **PREPARE** (one per role, lit when
something changed) sends only the changed keys and shows the result: the
override file's path, and the file's change as `+` and `-`
lines, as the host computed it. **CONFIRM APPLY** stays dark for 2.5 seconds
after the diff appears, so a diff is never confirmed unread, and the check
expires after two minutes. **CANCEL** (or Esc) goes back to the knobs with your
changes kept; nothing was written. If the file changed on disk after the
check, the host refuses to write over it: the screen says so, and you prepare
again.

When it is written, the screen says when it takes effect. **Saved · applies on
Restart** means press RESTART: these keys are read at `factory start`
(see the table in [Make it yours](04-customize.md#which-command-applies-a-change)),
and RESTART runs `factory restart`, which cancels running jobs, so its usual
check shows first. Every change here waits for a restart: the dispatch bridge
reads only the copy `factory start` took, so nothing applies live. While the
write is in flight CANCEL and Esc are dark, so the note always shows; the knobs
then show the values just written. In demo mode PREPARE answers
**Demo: changes are off** and writes nothing. Broker policy, hosts, kits and
credentials have no knobs here: they stay read-only.

### The notes editor

**Edit notes** (on the Project notes tab of Settings) opens an editor for the project's
notes files, the ones [Make it yours](04-customize.md#per-project-notes)
describes. Like the tuning knobs, it is a write from Settings, after a diff and a confirm.
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
- **After a save** the editor says "Saved · applies on Restart":
  the new notes reach the roles when the factory next starts, so the Restart
  key's light comes on. Press it (or run `factory start`), or press **Apply
  now** in the Settings bar (or run `factory config apply`) to use them from
  each role's next job without a restart.
- **A file that changed meanwhile.** If the file changed on disk between SAVE
  and CONFIRM (you edited it in a terminal, say), nothing is written and the
  editor says so. **RELOAD** reads the file again; **KEEP MY TEXT** goes back
  to your edit.
- **Demo mode** shows the editor but writes nothing: SAVE answers
  "Demo: changes are off".

The text is drawn with `textContent` and the diff's lines get their colour from
a fixed list, so nothing in a notes file can act on the page. The page holds no
key of its own: the control unit sends every request, and the file is written
by the host only after the token SAVE returned.

## Voice

Voice is optional. Without it LOIS shows captions, and nothing else changes. To
turn it on:

```bash
factory voice build     # first time only: several GB, takes a while
factory voice start     # or: factory voice start --build
factory voice status    # running or not, and the device
factory voice test      # speaks a test passage, silently: does it work, and keep up here?
factory voice stop      # stops and removes the container, keeps the image
```

It is a local Kokoro speech service in one Docker container, on
`127.0.0.1:8873`. It needs no network at run time, and no text or audio leaves
this machine. The model takes a few seconds to load after `start`, and `status`
says when it answers. The page checks for it every 30 seconds, or when you
reload.

- It doesn't start with `factory start`, and it doesn't come back after a
  reboot: run `factory voice start` again.
- On Linux x86_64 the image is large, about 12 GB on disk, even on a machine with
  no GPU. It uses the GPU when `nvidia-smi` works there.
- On an Apple silicon Mac, `factory voice build` makes a much smaller image that
  runs on the CPU, since Docker on a Mac can't reach the GPU. Run
  `factory voice test` to see whether it keeps up on your Mac. If it doesn't, LOIS
  still speaks, with pauses between sentences.
- English only.

**What LOIS says.** Two things. The Coordinator's own lines, from its `say` tool
(see [Talk to LOIS](#talk-to-lois)), and fixed sentences from the factory, with
numbers and lane names filled in: "A push is waiting for you. Its crystal is gold
until you decide." Nothing else an agent wrote is spoken. Names such as a branch
or a repo go in the small detail line, as text.

**Other sound.** There is also the chime when a write starts waiting. It plays
without the voice service.

**To mute,** press `v` or the speaker key. The browser remembers it.

**To tune the voice,** open **Tune the voice** in the `?` key, or `/voice`. It
has sliders for speed, pitch, reverb and the rest, and a box to try words. The
settings are kept in this browser.

More: `voice/README.md` in the factory checkout.

## Your own look: dashboard.toml

`dashboard.toml` in your config folder (`~/.config/ai-code-factory/`; `factory
config init` writes a commented example) sets the nameplate. The dashboard
reads it once when it starts, so restart `factory dashboard` to see a change.
The one key is optional:

```toml
plant = "LOIS WORKS"                    # the nameplate, up to 24 characters
```

- The nameplate is plain text, one line of printable ASCII, in `"double"` or
  `'single'` quotes without escapes.
- The keys of the retired classic view (`ui`, `shifts`, `crt`, `sound` and the
  `[roles.colors]` table) are still accepted, so an older file keeps working,
  but they do nothing. The dashboard prints one line naming them when it
  starts; you can delete them.
- Nothing in it is run, and it cannot touch the broker, a role or a permission.
- It is strict. An unknown key or table, a bad nameplate, a key set twice, a
  file over 16 KiB, a symlink or text that is not UTF-8 makes the dashboard
  ignore the whole file, show the defaults, print a warning naming the file,
  line and key, and show the same reason on the Settings screen. The dashboard
  still starts. With no file you get the defaults. `--demo` never reads your
  file.

## Keys

These work when the focus is on the page. Inside the decision row or Terminal 0,
keys go there instead: click the sky first.

| Where | Key | Does |
|---|---|---|
| The page | `t` | Terminal 0 bigger and focused, or smaller |
| The page | `1` to `4` | Show what cost the most, what's stuck, what they have been doing, the busiest time |
| The page | `v` | Sound and voice on or off |
| The page | `,` | Open Settings |
| The page | Esc | Shrink Terminal 0, else back to the galaxy, else close the `?` key |
| The monitor | `[` / `]` | Older / newer job |
| The monitor | Esc | Close |
| The gold light | Space, Enter (hold) | Hold to approve |
| The reason box | Enter / Esc | Decline / back out |
| The write's details | ← → | Previous / next held write |
| Inspect | `j` `k` ↓ ↑ | Next / previous file |
| Inspect | `n` / `p` | Next / previous hunk |
| Inspect, Settings | Esc | Close |
| Settings tabs | ← → | Previous / next tab |

## What still needs a terminal

The dashboard covers watching, talking to the Coordinator, approving, and
diagnosing, restarting or updating. Run these in a terminal:

| Command | Why it isn't on the dashboard |
|---|---|
| `factory start`, `factory stop` | Starting the factory on a project, and stopping it. |
| `factory status` | The full status. Diagnose runs the health check. |
| `factory secret ...` | Credentials never go through the browser. |
| `factory role new`, `factory roles` | Roles and their kits are part of the security boundary. |
| `factory reset`, `factory setup` | They remove sandboxes or install tools, and may ask for a password. |
| `factory analytics`, `factory packet --list` | The full cost report and older review packets. |
| `factory voice ...` | The voice service is a Docker container of its own. |

## Demo mode

To look at the dashboard before you set anything up, or to take a screenshot
without running agents:

```bash
factory dashboard --demo
```

Nothing here acts. It needs no `sbx`, broker, Coordinator or `factory setup`. It
shows made-up data (`example/app`, a few lanes, one held push, a denied broker
call and a branch) taken from `scripts/dashboard-demo/`, with times counted from
the moment you start it, and adds a broker call 8 seconds after the start and
then every 20 seconds. Open the link it prints: the Thoughtfield says it is the
demo, with its held push waiting for a decision.

Roles that need a picture of the dashboard (a developer or reviewer checking a
change to it) use `factory-demo start`, `factory-shot "$(factory-demo url)" out.png`
and `factory-demo stop` inside their sandbox, which run this same demo on loopback
and stop only the process they started.

Demo mode is display only. It never reads your real state folder: it copies
the fixture into a fresh temporary folder and deletes it when you press
Ctrl-C. There is no Terminal 0, so the pane is not shown. Approve, Decline, Restart, Update, Diagnose and the notes editor's SAVE
answer that nothing here acts, Inspect shows the fixture's patch, and Settings
shows the fixture's configuration (`scripts/dashboard-demo/setup/`), not yours,
and its PREPARE says "Demo: changes are off". It also ignores your
`dashboard.toml` and shows the defaults. The same token, cookie and content rules
apply as for a real run. `--demo` can't be combined with `--coordinator`,
`--state`, `--project` or `--factory`; `--no-open` and `--port N` work as usual.

## Is it safe to leave open?

It is local: it listens only on `127.0.0.1`, refuses any request without this
run's token, and makes no network connection of its own. Apart from Terminal 0
and the control unit it only reads the factory's state folder.

- The page never types into Terminal 0 and never focuses it without your click.
- Terminal 0 can tell the page only three fixed words (Enter pressed, output arriving, output stopped), and only to make the ring and the stars react.
- The control unit can write only a decision file (Approve, Decline), its action
  logs and, from Settings, a role's local tuning override or the project's notes
  (each after a diff and a confirm). It can run only `factory doctor`, `factory
  restart`, `factory update --yes` and `factory update --plan`, each after a
  confirm screen.
- If the voice service is running, the page sends it fixed sentences and the
  Coordinator's `say` lines on `127.0.0.1:8873`, without the dashboard's cookie.
  Text the Coordinator read could influence those lines. That service has no key:
  any program on this machine can make it speak, and it can only speak.

The README's [Dashboard](../../README.md#dashboard) section has the details.

## Next steps

- [Build a feature](02-build-a-feature.md): the full loop, from prompt to
  merge.
- [Troubleshooting](05-troubleshooting.md): when something doesn't work.

## History

The classic FC-1984 view and its factory floor were retired in November 2026,
when the Thoughtfield became the only view. Screenshots of the classic view are
kept in [docs/history/classic-view](../history/classic-view/README.md), and the
reasons are in [the dashboard decisions](../decisions/dashboard.md).
