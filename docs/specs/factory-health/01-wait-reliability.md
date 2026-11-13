# 01: Make waiting robust instead of long

**Needs your sign-off:** adds one bridge tool (`wait_for_any`). It reads job
state only and does nothing a role controls, but by `CLAUDE.md` the bridge's
tool surface is security-relevant.

## Diagnosis

The current design is *hold the call open as long as possible*:

- `dispatch/server.py`: `MAX_WAIT_SECONDS = 600`, and `MIN_WAIT_SECONDS = 300`
  stretches any shorter wait up to 300 s. Defaults for both `delegate` and
  `wait_for_job` are 600 s.
- Two fixes have already been layered on top:
  - `2495b33` (21 Oct) set `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=660000` in the
    Coordinator's managed settings.
  - `76e8938` (29 Oct) sends SSE `: keepalive` comments every 15 s, and
    progress notifications only when the client sent a `progressToken`.
- The path is Coordinator → **sbx MCP gateway** → bridge. A keepalive only
  helps if every hop passes it through. A gateway that parses SSE into
  messages will drop comment lines, because they aren't messages. If the
  client sent no `progressToken`, the hop after the gateway then sees 600 s of
  silence, and any idle timeout shorter than that kills the call.
- An old sandbox is a second possible cause: one created before 21 Oct still
  has the ~330 s default timeout (the troubleshooting guide's "sent no
  response or progress for 329s" entry).

Which hop drops the call is still unconfirmed. Before changing code, check
the logs (README, *Before you start*):

| You see | Meaning |
|---|---|
| `client closed the wait_for_job stream early` in `dispatch.log` | Something between the gateway and the client hung up. Keepalives aren't getting through. |
| `sent no response or progress for 329s` in the Coordinator | Old Coordinator sandbox. Run `factory update`. |
| Same message, but at about 660 s | The idle timeout is in effect but nothing reaches the client during the wait: the gateway is eating keepalives. |
| No bridge log line, Coordinator error at a round number (300 s, 600 s) | A gateway request timeout. |

**From your logs (12 Nov):** the bridge recorded only **3** dropped streams
in two days (11 Nov 12:18 and 15:47, 12 Nov 20:36), all `BrokenPipeError`.
The bridge writes a keepalive every 15 s, so if the gateway closed the
bridge's side of the connection, the bridge would notice within 15 s. Most of
the drops you see therefore never reach the bridge. The call is being cut
**between the Coordinator and the gateway**, while the gateway keeps its own
connection to the bridge open. That points to the client's idle abort, or a
gateway timeout on the client side. (The other `grep` hits were transcript
quotes of an old reviewer finding that `ed7d040` already fixed.) The exact
error text in the Coordinator's terminal will tell you which.

Whatever the cause, the robust fix is the same: **never hold one call longer
than the shortest timeout in the chain**, and make a "still running" return
cheap and expected.

### Cost check

Each poll re-reads the Coordinator's conversation, which is why
`MIN_WAIT_SECONDS` exists. But that conversation is prompt-cached, and Claude
Code's cache usually lasts **5 minutes**. A 600 s wait lets the cache expire,
so the wake-up pays to re-cache the whole conversation. A poll under 5 minutes
reads from a warm cache, which costs about a tenth as much. Shorter slices may
cost *less* than the current long waits. I'm not sure what cache lifetime
applies to a subscription login in a sandbox, so **measure it with spec 03**
rather than taking my word for it.

## Change

1. **Wait slices.** Add `WAIT_SLICE_SECONDS` (default **240**) and make it the
   cap on any single blocking call. Replace the 300 s floor with a floor of 60.
   `delegate` and `wait_for_job` default to the slice. Keep
   `MAX_WAIT_SECONDS` only as a hard upper bound, or delete it.
2. **Useful "still running" replies.** When a slice ends with the job still
   running, return the job summary plus `elapsed_seconds`,
   `seconds_since_last_output` (the lane transcript's mtime,
   `Transcript(job.lane).path`, as `interrupt_job()` already reads it) and
   `turns_so_far` if the stream-json events provide it. Then the Coordinator
   can tell "busy" from "stuck" without a separate status call.
3. **`wait_for_any(job_ids, timeout_seconds?)`.** Returns as soon as *any* of
   the listed jobs ends (or the slice expires), with that job's full result
   and the status of the others. Today, three parallel lanes mean waiting on
   job A while B and C may have finished long ago.
4. **Coordinator briefing** (`kits/coordinator/spec.yaml` and
   `server_instructions()`): replace the "don't poll, waits are long" guidance
   with: *"A wait returns after at most 4 minutes. `still running` is normal;
   call the same wait again with no other reply. A wait that errors or drops
   does not mean the job failed: the job keeps running, so call
   `wait_for_job` with the same id. For parallel jobs, use `wait_for_any`."*
5. **Docs:** update the README section on `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`
   and the troubleshooting entries. Keep the 660 s idle timeout as a safety
   margin; it becomes harmless.

## Acceptance criteria

- No `delegate`, `wait_for_job` or `wait_for_any` call blocks longer than
  `WAIT_SLICE_SECONDS` plus 2 s (a unit test with a fake job that never ends).
- `wait_for_any` returns within 2 s of the first job ending (a unit test with
  three fake jobs).
- A still-running reply includes `elapsed_seconds` and
  `seconds_since_last_output`.
- `may_block()` treats `wait_for_any` as blocking (SSE path).
- Existing tests pass. Tests for the old 300 s stretch are updated, not deleted.

**Out of scope:** changing the SSE keepalive, gateway configuration, or job
execution.

## Prompt for the Coordinator

```
Read docs/specs/factory-health/01-wait-reliability.md. Implement its "Change"
section, items 1-5, as one change on factory/wait-slices, on the developer
(role default model). Meet the acceptance criteria exactly; anything not in
them is out of scope. Then send it to the reviewer, and tell the reviewer to
REVISE only for unmet acceptance criteria, a real bug with a reproduction, or a
security issue with an attack path. Everything else is a suggestion.
```
