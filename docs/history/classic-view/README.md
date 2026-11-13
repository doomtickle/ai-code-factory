# The classic view

This is how the dashboard first looked: the **FC-1984 control panel**, a rack of
brushed-metal modules with a green-glass Coordinator terminal, a lane module for
every sandbox, a printer that types out every broker call, and, if you pressed
**F**, the **factory floor**: little programs seated at consoles around a track,
with crates riding to a gate. It lived at `/classic`. In November 2026 the
Thoughtfield became the only view and the classic one was retired. These
pictures are here so the look isn't lost.

All shots are of the built-in demo (`factory dashboard --demo`, made-up data),
taken at 1440x900 unless noted, from `main` at commit
`f80f0228f42ad1e4574ceeb99f5606ea03f81abf`. The panel has only one skin, so
there are no separate light and dark versions: it looked the same either way.

## The whole panel

![The classic view at desktop width](01a-classic-overview-desktop.png)
The full control panel: Coordinator terminal on the left, one module per lane, spend, event printer and write gate along the bottom.

![The classic view on a phone](01b-classic-overview-phone-390.png)
The same page at 390 px wide, one long column.

## The factory floor

![The factory floor switched on](02a-factory-floor-on.png)
**FLOOR** (or **F**) swaps the lane modules for the floor, with the rail and write gate still in reach.

![A held crate waiting at the gate](02b-factory-floor-held-crate-at-gate.png)
A held write: the yellow crate waits in front of the gate and the HOLD lamp stays on.

![A denied crate being confiscated](02c-factory-floor-denied-crate-confiscated.png)
A refused broker call is stamped CONFISCATED, the gate goes red and the DENY LED lights.

![The floor in more detail](02d-factory-floor-detail-hires.png)
A closer look at the track, stations, dispatch console, light bridge from reviewer to developer, and the gate (shot at twice the resolution).

## The write gate and control unit

![The write gate and control unit](03a-write-gate-control-unit.png)
The held `git_push` with its Decline, Approve and Inspect keys, and the DIAG, RESTART and UPDATE service keys.

![The patch viewer for the held push](03b-inspect-held-push.png)
**Inspect the push** opens the patch the broker stored, file by file, before you approve.

## Spend and the event printer

![Spend per hour and the event printer](04-spend-per-hour-and-event-printer.png)
The spend-per-hour bar graph with per-role totals and the shift budget, and the receipt-roll printer with its ALLOW and DENY lines.

## Lane monitors and the briefing

![A lane monitor showing a transcript](05a-lane-monitor-transcript.png)
Press a lane's number to open its monitor; the transcript is the default tab.

![A lane monitor showing the job packet](05b-lane-monitor-packet.png)
The same monitor on the packet tab (`p`; `d` is the diff).

![The shift briefing](05c-shift-briefing-standby.png)
With no jobs running, the lanes give way to the STANDBY briefing: project, repos, notes, roles, holds and a first work order to give the Coordinator. (Shot through a throwaway proxy that marked every demo lane idle, since the demo always has running lanes.)

## Settings

![Settings, Roles tab](06a-settings-roles.png)
The control unit's **SETTINGS** key opens role knobs: lanes, model, effort and limits.

![Settings, Project notes tab](06b-settings-project-notes.png)
Project notes and how close each is to the size limit.

![Settings, Dashboard look tab](06c-settings-dashboard-look.png)
What `dashboard.toml` set, read-only, including which view opens.

## The classic Terminal page

The demo has no Terminal 0, so `term.html` was served from a scratch folder with a
stub WebSocket that sends the page fixed text. The page itself is the real one.

![The Terminal page with the Coordinator off](06d-terminal-page-coordinator-off.png)
The page's own "The Coordinator is off" state, with POWER ON and RESUME.

![The Terminal page with a session running](06e-terminal-page-running-stub.png)
A running session; the text is the stub's, not a real Coordinator's.

## Where it sat next to the Thoughtfield

![The Thoughtfield with its Classic key](07-thoughtfield-with-classic-key.png)
The Thoughtfield's top bar: **Classic** (top right) was the way over to the panel above, and the panel's **Thoughtfield** key was the way back.
