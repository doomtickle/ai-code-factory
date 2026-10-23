# Run work in parallel

A role can run several tasks at once, one per **lane**. This guide shows when
that pays off, how to ask the Coordinator for parallel chunks, how to follow
each lane, and how to merge the branches they produce without conflicts. You'll
also see what parallel work costs and how to change the number of lanes.

## How lanes work

By default the developer has 3 lanes, the reviewer 2 and the researcher 1
(`LANES` in each `roles/<name>/role.conf`). Lane 1 is named after the role;
the others add a suffix:

| Role | Lanes |
|---|---|
| developer | `developer`, `developer-lane2`, `developer-lane3` |
| reviewer | `reviewer`, `reviewer-lane2` |
| researcher | `researcher` |

Each lane has its own Claude session and transcript. Each developer lane also
has its own git clones of your project, so lanes never edit the same files on
disk. All lanes of a role share one sandbox, and its CPU and memory.

A lane's branches come back into your project named after the lane, so you can
always tell which lane made what:

```text
factory-developer/factory/export-csv
factory-developer-lane2/factory/export-pdf
factory-developer-lane3/factory/export-settings-ui
```

## When parallel work pays off

Parallel lanes help when the work splits into chunks that are **independent**:
each one touches its own files, can be built and tested on its own, and
doesn't need another chunk's result first. For example:

- Three new endpoints in separate modules.
- A backend change and an unrelated UI fix.
- Tests for several modules that have none.

They don't help, and can hurt, when the chunks touch the same code or depend on
each other. Two lanes editing the same function produce two branches that
conflict, and you pay for both. Keep such work on one lane, in sequence.

## Ask for parallel chunks

The Coordinator already splits large work into independent chunks when it can.
You get better splits when you say where the boundaries are:

```prompt
Add export to CSV, PDF and a settings screen for choosing the default format.
These are independent: CSV in src/export/csv.ts, PDF in src/export/pdf.ts,
the settings UI in src/ui/settings/. Run them in parallel on separate
developer lanes, each on its own factory/export-* branch, each with tests.
Review each branch as it finishes, then tell me the order to merge them.
```

The Coordinator starts each chunk without waiting, on the next idle lane, then
waits for each job. It sends follow-ups for a chunk, like a REVISE, back to the
lane that wrote it, so that lane's session still has the context.

If you ask it which lanes are busy, it calls `list_roles`, which shows each
lane's state:

```text
- developer [3 lanes: developer busy (20261017-150102-1a2b3c), developer-lane2 busy (20261017-150104-4d5e6f), developer-lane3 idle]: Implements features and fixes, writes and runs the tests, and updates the docs (edits private git clones; network tools: npm_fetch, pip_download, git_fetch, git_push, github_create_pr, gitlab_create_mr; model: claude-sonnet-5-5; can also run on: claude-haiku-5-5, opus)
```

## Watch each lane

The dashboard shows every lane as its own module. `factory watch` does the
same in any terminal: it follows every lane of every role at once, with each
active lane in its own pane (a job running, or finished in the last ten
minutes; idle lanes take one status line each), tool calls condensed to one
line and the job's status, elapsed time, model and cost in the pane's title:

```bash
factory watch
```

Press a lane's number to read that lane full-screen, `p` for its latest review
packet, `a` for the grid again (press it on the grid to show every lane, idle
ones included) and `q` to quit.

`factory logs <role>` follows the raw transcripts of all of a role's lanes at
once, each chunk of output headed with the lane's log file:

```text
==> /Users/you/.local/state/ai-code-factory/dispatch/logs/developer-lane2.log <==
15:01:04  ▶ job 20261017-150104-4d5e6f from the Coordinator
```

For the raw log of one role or lane:

```bash
factory logs developer          # every developer lane
factory logs developer-lane2    # only lane 2
```

## Merge several branches

When the chunks are approved, merge them one at a time, in the order the
Coordinator suggests:

```bash
git branch -r | grep factory-
git merge factory-developer/factory/export-csv
git merge factory-developer-lane2/factory/export-pdf
git merge factory-developer-lane3/factory/export-settings-ui
```

If the chunks really were independent, each merge is clean. If one conflicts,
resolve it as you would any merge, or ask the Coordinator to have the developer
rebuild that chunk on top of what you've merged. Commit the merges first, since
the developer only sees committed state:

```prompt
I merged export-csv and export-pdf into main. Rebase the settings UI work onto
main on a new branch factory/export-settings-ui-2 and resolve the conflict in
src/ui/settings/index.ts.
```

## Avoid conflicts up front

- **Split by file or module**, not by step. "CSV in `csv.ts`, PDF in `pdf.ts`"
  parallelizes well; "write the code" and "write the tests" for the same
  module don't.
- **Do shared groundwork first.** If every chunk needs a new shared type or
  helper, have one lane add it, merge it, then start the parallel chunks from
  that committed state.
- **Name a branch per chunk.** The Coordinator does this by default; if you
  give branch names, make them distinct.
- **Watch shared files.** Lockfiles, route tables and barrel files
  (`index.ts`) are where independent chunks most often collide.

## Cost and limits

Every lane is a separate Claude session, so three lanes cost roughly three
times one lane for the same amount of work each. They also share your Claude
subscription's usage limits and the sandbox's CPU and memory, so more lanes
isn't always faster: three test suites running at once in one sandbox can each
run slower.

Each job result shows its `cost_usd` and its lane's `context_tokens`. For the
totals per role and per lane over time:

```bash
factory analytics
```

## Change the number of lanes

Edit `LANES` in the role's `role.conf` (1 to 8; empty means 1), then run
`factory start`. No sandbox is recreated: the dispatch bridge reads `LANES` for
every task, and `factory start` updates the dashboard's lane view and
`factory watch` to show the new lanes.

```conf
# roles/developer/role.conf
LANES="2"
```

```bash
factory start
```

> **Note:** `roles/` is part of the factory checkout. While it has uncommitted
> edits, `factory update` skips pulling new factory code and says so. Commit
> your role changes (and merge upstream changes into them yourself), or stash
> them, run `factory update`, then re-apply them.

See [Roles](../../README.md#roles) for the other role settings.
