# Tool call lifecycle: appear, inspect, stop

Three changes to the assistant timeline, shipped 2026-11-10:

1. A tool line appears as soon as pi names the call, not when its arguments finish streaming.
2. Clicking a tool line opens a detail dialog (arguments, state, elapsed, result) with a control that
   stops the run.
3. A contiguous run of thinking and tool lines folds into one group headed `Thinking...`.

Plan of record while this was in flight: `~/.pi/agent/plans/pi-chat-tool-lifecycle.md`.

## What pi actually puts on the wire (measured 2026-11-11, pi 1.1.0)

The first version of item 1 read `update.partial.content[contentIndex]`, which is the **SDK** shape. It
is not the **wire** shape. `toJsonAssistantMessageEvent` (`dist/modes/json-event.js`) deletes every
cumulative `partial` snapshot from `message_update` so a stream stays linear, and hand-adds two fields
to `toolcall_start`:

| Event | On the wire | Not on the wire |
|---|---|---|
| `toolcall_start` | `{contentIndex, id, toolName}` | `partial`, `arguments` |
| `toolcall_delta` | `{contentIndex, delta}` | `id`, `partial`, `arguments` |
| `toolcall_end` | `{contentIndex, toolCall}` | `partial` |

Three consequences that are easy to get wrong again:

- `toolcall_delta` carries no id. The `contentIndex` from `toolcall_start` is the only tie between a
  fragment of argument text and a call, and it is per assistant message, so `chat.js` keeps
  `toolBlocksByIndex` and clears it on each `message_start`.
- `arguments` exists only at `toolcall_end`. Anything shown mid-stream has to be read out of the raw
  JSON text (`earlyArgTarget`), which is why the hint regexes the first useful string rather than
  parsing.
- Every fixture and stub written before 2026-11-11 sent `partial`, because pi 1.1.0 had not stripped it
  when they were recorded. The renderer read a field that never arrived, the checks passed anyway, and
  a long `write` stayed invisible for 0.2s to 6.5s per call (measured, see below). `tools/capture.py`
  exists so that cannot happen quietly again, and
  `tests/test_fake_rpc_stream.py::test_the_reference_capture_is_a_wire_the_stub_agrees_with` fails if
  the stub and a real capture disagree about a field name.

### The capture

`uv run python tools/capture.py --scenario markers` runs one real `pi --mode rpc` against a scratch
workspace and writes `data-samples/markers_rpc_capture.jsonl` (parsed events, in arrival order, each
tagged with the stdout read chunk that carried it), `…jsonl.gz` (the byte stream verbatim, because
chunk boundaries are a property of the pipe and not of the protocol), `…meta.json` (argv, model, cwd,
timings, session header) and `markers_session.jsonl` (pi's own file for the same run, so the live and
reopened paths can be rendered from one run). `--census "data-samples/*.jsonl"` reports marker
coverage offline and exits 2 when a core marker is missing; it is how the pre-1.1.0 fixtures were found
out. `tests/fixtures/capture_probe.ts` is loaded for the run and emits `entry_appended`,
`extension_ui_request` and `extension_error` from slash commands, which cost no inference.

The aborted step asks for a slow streaming `bash` and aborts 2.5s in, because that is the only shape
that leaves the interesting state on the wire. It also showed the abort story is two shapes, not one:

- aborted **mid-generation**: the assistant message ends `stopReason: "aborted"` and the call is
  unanswered. This is what `enrichToolCalls` derives `cancelled` from.
- aborted **while a tool ran**: pi answers the call itself with `tool_execution_end isError: true` and
  the text `Command aborted`, writes a normal `toolResult`, and the run's last assistant message ends
  `stopReason: "error"`. `tools/fake_rpc_pi.py` models only the first.

## Collapsing a group, and what a hover means

A group is collapsible from two handles and from nothing else: the `Thought` stub, and a bracket down
the group's right edge (`.group-edge`, a real `<button>` drawn by its own `::before`). Hovering the
group used to tint the whole run and clicking a line of thinking folded it away, so reaching for a tool
row answered nothing about whether that row was clickable. Hover now belongs to the row: wash, inset
ring, accent name and chevron on `.tool-line`. `--stage groups` measures all of it, including that a
hover paints something and that clicking the thinking does nothing.

The wrapper is positioned so the bracket can span stub and body together, and it must stay transparent:
`.timeline::before` (the rail) paints before a positioned descendant of the same z-index, so a
background on the group itself cuts the rail in half. That is also why the old hover tint is gone rather
than merely dimmed.


## Where it lives

| Concern | File |
|---|---|
| Status attribute, group primitives, tool row markup (shared by both render paths) | `static/timeline.js` |
| Live event handling, the call record map, index for reopened sessions | `static/chat.js` |
| Historic render, including the cancelled derivation | `static/history.js`, `static/subagent.js` (`enrichToolCalls`) |
| The detail dialog and nothing else | `static/toolDetail.js` |
| The `cancel_tool` command | `pi_chat/websocket.py` (`_cancel_run`) |
| Stop control, dialog markup | `static/index.html`, `static/app.js` |

The parity rule still holds: grouped DOM, the `.tool-line` button and `data-tool-call-id` are produced
by `timeline.js` primitives, so settled live DOM and reopened-session DOM are the same bytes. Neither
renderer is allowed its own shape for a tool row.

## Status is one attribute

`data-tool-status` on `.timeline-item.tool` is the only source of the mark:

| Value | Meaning |
|---|---|
| `pending` | named by `toolcall_start`, arguments still streaming |
| `running` | `tool_execution_start` fired, no result |
| `ok` / `error` | a result came back |
| `cancelled` | the reader stopped the run and this call never answered |
| `unknown` | no result and no reason to claim one (old captures, aborted runs of other shapes) |

`timeline.js` owns the attribute, `history.js` derives it via `toolStatusFromResult(isError, cancelled)`.

## Cancelling: pi has no per-tool primitive

The only lever for a call in flight is the run's abort signal, which kills the tool's process tree and
ends the turn. So "cancel this call" is implemented as "stop everything, hand back what was queued":

```
browser -> {type: "cancel_tool"}
server  -> clear_queue            (authoritative queue, both buckets)
         -> {type: "cancel_result", cleared: [...]}   to the browser
         -> {type: "abort"}       to pi
```

Two details that are easy to get wrong:

- **`cancel_result` goes out before the `abort` is written.** pi answers `abort` only once the session
  goes idle, and the very tool being escaped is what holds idle open. Waiting to acknowledge until
  after it reintroduces the wait the reader is trying to escape.
- **Cleared text is restored to the composer, never resent.** A message pi already delivered must not
  appear twice, and the reader may only want part of it back. `handleCancelResult` in `chat.js` puts
  the text in `#user-input` and deliberately does not clear `cancelInFlight`: the dialog keeps showing
  `Stopping the run...` until the run really settles.

`cancelInFlight` is set by both entry points (header Stop, dialog Cancel) and cleared when the run
settles, so the two controls cannot disagree.

Voice barge-in still sends a bare `abort` (`static/app.js`). That path interrupts so the speaker can
talk; throwing away messages the reader queued on purpose would be wrong there.

## How `cancelled` survives a reload

The hard part was not the live mark, it was making a reopened session say the same thing. The
assumption that broke first: "the containing assistant message has `stopReason: 'aborted'`". That is
true only when the model is interrupted **mid-generation**. When the reader stops a call that is
already executing, pi has long since persisted the assistant message with `stopReason: 'toolUse'`
(it does that before running the tool), so the message looks completely normal and the only trace in
the JSONL is a tool call that never answers.

So the rule, implemented once in `enrichToolCalls` (`static/subagent.js`) and mirrored by
`indexToolCalls` in `chat.js` for the dialog's records:

> A tool call with no result whose assistant message is the last assistant message in the run is
> cancelled. A call with no result anywhere else stays `unknown`.

Position is the signal because nothing else in the file records the stop. It falls out of the shape of
a session file, so the live path (which reaches it at `agent_end`, when nothing more can arrive) and
the history path agree by construction. `tests/compare_render.py` locks both shapes: abort
mid-generation and abort mid-execution, plus the guard that an unanswered call away from the end of a
run is still `unknown` and not guessed at.

## Reproducing it

The stub holds a tool open so there is something to cancel, and it aborts for real:

```bash
PI_CHAT_PI_BIN="$PWD/tools/fake_rpc_pi.py" \
FAKE_RPC_STREAM_TOOLS=1 FAKE_RPC_TOOL_MS=25000 FAKE_RPC_WRITE_KB=2 FAKE_RPC_STREAM_MS=1 \
PI_CHAT_DEV=1 PI_CHAT_B_PASSWORD_HASH="$HASH" PI_CHAT_PORT=9026 uv run python server.py

node tools/screenshot_ui.mjs --url http://127.0.0.1:9026 --stage tool-cancel --cdp-port 9365 \
  --out "$TEMP/shots/tool-cancel.png" --password test-only
```

`--stage tool-cancel` clicks the row through CDP's real input pipeline (never `el.click()`), asserts
the dialog's title, state, elapsed, arguments and the warning about what cancel cannot undo, clicks
Cancel, then waits for `data-tool-status="cancelled"`, checks that a queued message came back to the
composer, that the stop control disarmed, that Escape closes the dialog and that focus returns to the
row it opened from. Run it with `--viewport 390x844` and it also asserts the page does not scroll
sideways. Related stages: `tool-streaming` (arguments still arriving) and `groups` (folding).

Suites: `npm test` (25 checks), `uv run python -m pytest tests/test_queue_dispatch.py
tests/test_fake_rpc_stream.py tests/test_security_baseline.py -q`, `uv run python
tools/smoke_test_ws.py`.

## Gotchas found building this

- **A control is bound in exactly one place.** `chat.js` owns the header Stop button; the dialog owns
  its own Cancel. Anything else that wants to stop a run sends the command, it does not touch the DOM.
- **Both render paths must stamp `data-tool-call-id`**, including the history path. Without it a call
  reopened from disk opens a dialog with no arguments, and focus cannot find its way back.
- **Settlement rebuilds the timeline**, so the row a dialog was opened from may be detached by the time
  it closes. `toolDetail.js` follows the call id instead of holding the old element.
- **The modal says what cancel does not undo.** pi kills a process tree; it does not roll back a
  partial `write` or `edit`. That sentence is in the dialog on purpose.
- Body carries `tool-detail-open` while the dialog is up (scroll lock), and the dialog is a labelled
  `role="dialog"` closed by Escape, matching the drawer.

## What the screenshot pass caught

Text assertions passed on all three of these. Only measuring boxes, or looking at a pixel, showed them:

- **The argument and result blocks collapsed to one line.** `flex: 1 1 0` on a `<pre>` inside an
  auto-height flex column claims zero basis, so with the dialog under its own `max-height` the blocks
  shrank away. The panel scrolls (`overflow: hidden auto`), every child is `flex: 0 0 auto`, and each
  block caps itself at `max-height: 22vh` with its own scroll.
- **The cancel warning stopped mid-sentence.** A missing `+` between two string fragments is legal
  JavaScript: the first expression ends at the newline and the rest becomes a discarded statement.
  `node --check` is happy, `textContent` reads as a real string. The stage now requires the sentence to
  end at `composer.`.
- **Cancel rendered as a blank box on a phone.** The mobile media block turns header icon buttons
  label-free with `font-size: 0`, and the dialog's text-only button inherited it. `.tool-detail-actions
  .btn-icon` opts back in at higher specificity, and the stage asserts the button's label is not
  clipped and the control is at least 26px tall.

- **Starting a new session, or clicking into one, left the header Stop armed.** `resetConversation()` and
  `loadHistoricalMessages()` cleared `isAgentRunning` and refreshed the send button but never called
  `updateStopButton()`, so the control kept the armed look of a run that no longer existed, and a cancel
  in flight could strand it disabled. Both now disarm it; the stage arms the control with a second run,
  starts a new session, and requires it hidden and enabled.

The general rule: a dialog's job is to make content readable, so assert the geometry of the content,
not that a string exists somewhere in the DOM.
