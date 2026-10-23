# Use the dashboard

The dashboard is the factory on one screen in your browser: the Coordinator,
every lane, the writes waiting for you, today's spend and the broker's calls.
This guide walks through each part, says what every key does, and lists the
`factory` command each one stands in for. The full reference is
[Dashboard](../../README.md#dashboard) in the README.

## Open it

`factory start` opens the dashboard (the `web` layout is the default). To open
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
  while a write is held for you.
- **Readouts:** today's spend in dollars, today's job count, the jobs running
  now, and the clock.
- **MANUAL** opens these docs in a tab of their own. The **?** on each module
  opens its part of this guide, in the same tab. The docs are the ones in your
  factory checkout when the dashboard started; restart `factory dashboard` to
  see edits. The manual shares this console's look (case, paper, stripes);
  its theme button, top right of the page, is the same three-way
  system/light/dark toggle.

## Terminal 0: the Coordinator

The big screen on the left is the Coordinator itself, live. Press **POWER ON**
to attach, then type to it as you would in any terminal. Opening the page
starts nothing; only POWER ON does.

- Every tab you open shows the same session. The last tab you resize or click
  into sets its size.
- A tab you open later replays the recent output first, with its keyboard
  muted until the replay is done.
- When the session ends, the screen powers off. POWER ON starts it again.

The screen tells you when it can't attach:

| The screen says | What to do |
|---|---|
| OPEN ELSEWHERE | The Coordinator is open in Herdr or `factory chat`. Close it there, then POWER ON. |
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
number and when its last job ended. With nothing running, the bay shows
STANDBY above it.

Each module shows:

- a status LED and tag: RUN (green, blinking), HELD (amber, blinking: a write
  is waiting for you), REVISE (amber: a review asked for changes), DONE or
  APPROVED (green), CANCELLED or FAILED (red);
- the live tail of its transcript, with tool calls, errors, branches and held
  writes in their own colours;
- its model, cost, context size, and the time the job has run (or took).

A lane keeps its number as a module and as a chip. Press it (1 to 9), or
click the module or chip, to open the **lane monitor**: the
full transcript (**T**) and the job's latest review packet (**P**), which has
the task, each branch's commits and diffstat, the tests it ran, the broker
calls it made, its cost and the role's report. **Esc** closes it.

Stands in for: `factory watch`, `factory logs <role>` and `factory packet
<lane>`.

## Write gate and the control unit

Bottom right. With approvals on, every push and pull request a role makes
waits here for you ([Enabling approvals](../../README.md#enabling-approvals)).
With holds off, the gate always reads NO WRITES HELD and the agents' writes go
through under the broker's policy alone.

The gate's display gives one line: how many writes are held (and how many of
them are queued, below), the first one's tool and role, and the time left
before it expires. Under it, the
**control unit** shows the selected write in full: the tool, the role and its
lanes, the repo, the branch (or head and base for a PR or merge request), its title, the
commits and files, and the time left. **◀ ▶** step through several held
writes.

### Queued writes and their outcomes

A write you haven't decided within the broker's hold (45 seconds by default)
is **queued**: the agent is told it is waiting for approval and moves on, so
the lane no longer shows HELD for it. The card stays on the gate, marked
**QUEUED: the agent has moved on; the gateway writes it when approved**. When
you approve it the gateway makes the write itself, within seconds, and the
card reads **APPROVED, the gateway writes it within seconds**; there is no
retry to wait for.

Under the card, **deferred writes** lists what the gateway did with recent
approvals, failures first: **FAILED** (in red, with the reason, kept three
days), **RUNNING** (the gateway is making it now) and **MADE** (with the
first line of its result, such as the new PR's address; kept a day). A write
that failed was not retried: ask the agent to make the request again. The
event printer shows QUEUED, WRITTEN and FAILED lines for the same steps. This
list only displays; it has no keys. The text in it comes from the gateway's
state files and is shown as plain text.

### Decide a held write

- **APPROVE** lets it through. One press decides.
- **DECLINE** refuses it. Type a reason first: it's required, and the agent
  reads it.

A decision is final for that card. A push, pull request or merge request
(the default held set) is picked up by the call if it is still waiting, and
otherwise written by the gateway within seconds, with no retry to wait for.
For any other held tool the card stays on the gate until the agent retries
its call (the retry picks your decision up), and meanwhile it reads
**APPROVED, waiting for the agent's retry** or **DECLINED**. Either way it
shows who decided and when, and both keys are dark. Pressing again, here or with
`factory approvals`, never changes the first decision; if two people decide
at once, only one decision is written and the other is told who got there
first.

An expired card reads **EXPIRED** with its keys dark: the agent's retry
makes a fresh card. If a newer write replaced the card while you were
reading, or it was decided elsewhere, the unit says so in words and decides
nothing. Check the new card and press again.

> **Note:** A held write names the role, not the lane. If two lanes of the
> developer are running, both show as HELD.

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
  cancel: the running jobs and the held writes it drops. Queued writes are
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

The **SETUP** plate is a placeholder; settings will go there.

Stands in for: `factory doctor`, `factory restart`, `factory update` and
`factory update --plan`.

## Spend and the event printer

- **Spend / hour:** today's cost as a bar per hour since midnight, then per
  role, with today's job count.
- **Event printer:** today's broker calls (allowed, denied, held, queued and
  decided), the gateway's deferred writes (WRITTEN, or FAILED in red) and
  finished jobs, newest at the bottom. Denied, held, queued and failed calls,
  and failed or cancelled jobs, print in red; the header counts the denied ones. Scroll up to read older lines; the printer stays put
  until you scroll back down.

For the full report, run `factory analytics` (or `factory analytics --html`).

Stands in for: part of `factory analytics` and of `factory logs broker` /
`factory logs dispatch`.

## Keys

| Where | Key | Does |
|---|---|---|
| Anywhere | `1`–`9` | Open that lane's monitor |
| Lane monitor | `t` / `p` | Transcript / review packet |
| Lane monitor, patch viewer | Esc | Close |
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

## Is it safe to leave open?

It is local: it listens only on `127.0.0.1`, refuses any request without
this run's token, and makes no network connection of its own. Apart from
Terminal 0 and the control unit it only reads the factory's state folder.
The control unit can write only a decision file (APPROVE, DECLINE) and its
action logs, and run only `factory doctor`, `factory restart`, `factory
update --yes` and `factory update --plan`, each after a confirm screen. The README's
[Dashboard](../../README.md#dashboard) section has the details.

## Next steps

- [Build a feature](02-build-a-feature.md): the full loop, from prompt to
  merge.
- [Troubleshooting](05-troubleshooting.md): when something doesn't work.
