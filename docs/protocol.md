# Protocol

Everything that crosses a boundary: the browser WebSocket, pi's RPC event stream, the queue and steer
semantics in the middle, and the two session file formats on disk. Extracted from `AGENTS.md` on 2026-11-17
so that the card stays a card; the split is section-for-section, not a rewrite.

Two rules govern the whole file. **The browser must never trust pi's shape:** every payload is parsed
defensively on both ends, because pi's wire changed under this application twice (2026-11-10 and 2026-11-11)
and each time the failure was silent rather than loud. **The queue is pi's, not ours:** this application
mirrors pi's `steering` and `followUp` lists so the chips on screen can agree with it, and when they
disagree pi is right.

If you change anything in here, run `tools/smoke_test_ws.py` and at least one
`tools/screenshot_ui.mjs --stage ...` that exercises the changed path.

## Browser WebSocket Protocol

### Browser Commands (handled by `pi_chat/websocket.py`)

| Type | Purpose |
|------|---------|
| `prompt` | Send text and optional images to pi; `delivery` is `"queue"` (default, waits for the run) or `"now"` (steer at the next boundary) |
| `steer_now` | Promote one queued message to steering: `{kind, index, text}` |
| `retract_queued` | Pull one queued message out of pi's queue: `{kind, index, text}` |
| `new_session` | Start a clean pi session without replacing the process |
| `load_session` | Switch pi to a validated saved session |
| `get_messages` | Request messages from the current pi session |
| `abort` | Abort the active pi request |
| `ping` | Keep the proxied WebSocket active |
| `voice_enable` | Enable voice output with settings |
| `voice_disable` | Disable voice output |
| `voice_settings` | Update voice settings (model, voice ID, etc.) |
| `voice_stop` | Stop current voice playback |
| `voice_prepare` | Pre-load voice with settings (timeout-protected) |
| `voice_playback_end` | Browser confirms TTS playback for a stream has ended (drained or stopped); closes the server-side AEC window for STT |

### Server Messages (routed by `static/app.js`)

| Type | Purpose |
|------|---------|
| `pi_event` | A raw pi RPC event for live rendering |
| `queue_state` | Mirror of pi's queues: `{steering: [text], followUp: [text]}`, sent on every `queue_update` |
| `queue_action` | Ack for `steer_now` / `retract_queued` |
| `session_started` | Confirms that pi or a new session is ready |
| `session_loaded` | Contains historical messages to render |
| `messages_retrieved` | Contains messages from an explicit request |
| `error` | Reports browser-command or pi RPC failure |
| `pong` | Heartbeat response; no UI action |
| `voice_state` | Voice system state (loading, enabled, disabled, error) |
| `voice_prepared` | Voice successfully prepared with settings |
| `voice_stream_start` | TTS streaming audio has begun |
| `voice_stream_end` | TTS streaming audio completed |
| `voice_error` | Voice system error with message |


## Mid-run Message Delivery (queue and steer)

pi refuses a bare `prompt` while it is streaming: "Agent is already processing. Specify
streamingBehavior". That refusal used to reach the browser as a failed prompt response whose handler
unlocked the composer by dropping the streaming timeline, so one typed message destroyed the run in
flight. Three rules keep that impossible:

1. Every prompt declares a `streamingBehavior`. pi ignores it when idle and queues to the named place
   when not, so the refusal has no reachable path and a stale client view cannot cause one.
2. `websocket.py` keeps a run mirror (`is_streaming`, from `agent_start` / `agent_settled`) and a queue
   mirror (from `queue_update`), because a queue action needs to know whether there is a run to steer
   into. `agent_settled` fires only after pi has drained both queues.
3. A failed `prompt` response with a timeline still live reports the error and changes nothing else.

pi's primitives, from `pi/docs/rpc-commands.md`:

| pi command | semantics | response |
|---|---|---|
| `prompt` + `streamingBehavior: "followUp"` | delivered when the agent stops, after steering messages | `disposition: "queued"` |
| `prompt` + `streamingBehavior: "steer"` | delivered after the current turn's tool calls, before the next model call | `disposition: "queued"` |
| `steer` / `follow_up` | queue directly; reject extension commands | `disposition: "queued"` |
| `queue_update` (event) | **the complete queue**, not a delta, both buckets | - |
| `clear_queue` | empties both buckets and returns them | `data: {steering, followUp}` |

### Where a message renders

`message_start` with `message.role === "user"` **is** the delivery moment. AgentSession pops the entry
from its queue inside that same event and persists the message there, so the transcript holds the
message between the turn it interrupted and the turn that followed it. The browser therefore draws the
bubble from that event and nowhere else:

- A message sent to an idle pi is drawn immediately (its echo arrives a round trip later and is
  swallowed via the `outgoing` FIFO in `chat.js`, which is why an echo is not a second bubble).
- A message sent into a live run is **not** drawn: the chip above the composer is its only presence
  until pi takes it, because until then it is not in the transcript.
- The open assistant container is closed when a mid-run delivery is drawn, so the rest of the run
  streams into a fresh assistant block below it. `ensureTimeline()` opens it again on the next delta,
  and a container that streamed nothing is dropped instead of leaving a bare "assistant" label.

Drawing the bubble at send time is the bug this section exists to prevent: the run kept streaming into
the assistant block *above* the bubble, so a steered message sat at the bottom of the transcript for
the rest of the session and only looked right after a reload.

### Several messages at once

pi runs two queues and drains one entry per boundary (`steeringMode` / `followUpMode` default to
`one-at-a-time`; `"all"` is a settings option this app does not set). So:

- Two `Send now` messages typed during one long tool batch arrive at **consecutive boundaries**, not
  together: the first before the next model call, the second after that turn's tools.
- Two `Enter` messages arrive one per run end, each becoming its own turn, and `agent_settled` fires
  only once both queues are empty, so the composer stays armed until then.
- A steering entry always wins the next boundary over a waiting one. Promoting a waiting entry puts it
  at the **front** of the steering queue: the chip was clicked, so it jumps.
- The chip labels say which is which: `next turn` / `after run` for the head, `then` for the rest.

The gaps that shaped the design:

- There is no per-entry requeue. Promoting one waiting message means `clear_queue`, then `steer` the
  chosen text, then re-push the rest in order. That costs nothing in correctness: `clear_queue`'s
  answer is the authoritative queue, which also settles any disagreement between the chip the user
  clicked and what pi actually holds. If the text is gone, the message was already delivered - the
  queue is restored and nothing is ever resent, because a delivered message must not appear twice.
- Extension commands (`/memory status`) bypass the queue even mid-run, so `delivery` is ignored for
  them by pi, not by us.
- `abort` does not clear the queues: pi continues queued messages afterwards. The header **Stop** and
  the dialog's Cancel send `cancel_tool`, which does `clear_queue` first and hands the cleared text
  back to the composer (see `docs/tool-call-lifecycle.md`). Voice barge-in still sends a bare `abort`,
  because interrupting a run to speak is not a request to throw away queued messages.
- While a run is live the stdin loop keeps serving, so a mid-run `steer_now` cannot deadlock behind
  the streaming turn. `abort` is the exception: its response waits for idle.

Tests: `tests/test_queue_dispatch.py` (which commands go out, in what order),
`tests/test_child_argv.py` (prompt shape), and six checks in `npm test` for the composer states, the
chips, the delivery ordering above, and the two regressions. `tools/fake_rpc_pi.py` keeps real queues,
serves stdin while it streams, splits its turn at a real boundary and replays queued messages as user
messages there, so `FAKE_RPC_TURN_MS=1600 PI_CHAT_PI_BIN=tools/fake_rpc_pi.py` drives the whole feature
in a browser with no model and no network. See it in `tools/smoke_test_ws.py` (26 checks) and in
`--stage queue` / `--stage queue-landed`.


## pi RPC Event Format

All JSON events emitted by `pi --mode rpc`, wrapped by the server as `{"type": "pi_event", "event": {"type": "..."}}`.

### Lifecycle Events

| Event | Description |
|-------|-------------|
| `response` | Acknowledges a command was accepted. `{type: "response", command: "prompt", success: true}` |
| `agent_start` | Agent loop begins. `{type: "agent_start"}` |
| `agent_end` | Agent loop finishes. Includes full message history in `messages` array. |
| `agent_settled` | Final signal — agent is fully done and idle. `{type: "agent_settled"}` |
| `extension_ui_request` | Extension UI state requests (e.g., telegram status). |

### Turn Events

| Event | Description |
|-------|-------------|
| `turn_start` | A new LLM turn begins. `{type: "turn_start"}` |
| `turn_end` | Turn completes. Includes full assistant message with usage stats in `message` object. |

### Message Events

| Event | Description |
|-------|-------------|
| `message_start` | A message begins. `message` object contains full content. Roles: `user`, `assistant`, `toolResult`. |
| `message_end` | Message completed. Same shape as `message_start`. |

### Streaming Updates (`message_update`)

All streaming updates wrap an `assistantMessageEvent`:

| Subtype | Description |
|---------|-------------|
| `thinking_start` | Reasoning block begins |
| `thinking_delta` | Incremental reasoning text (`delta` field) |
| `thinking_end` | Reasoning block complete (`content` field has full text) |
| `text_start` | Response text block begins |
| `text_delta` | Incremental response text (`delta` field) |
| `text_end` | Response text complete (`content` field has full text) |
| `toolcall_start` | Tool call begins |
| `toolcall_delta` | Incremental tool arguments (`delta` field) |
| `toolcall_end` | Tool call complete (`toolCall` field has full object) |

### Tool Execution Events

| Event | Description |
|-------|-------------|
| `tool_execution_start` | Tool begins executing. Has `toolCallId`, `toolName`, `args`. |
| `tool_execution_update` | Progress during execution. May have `partialResult`. |
| `tool_execution_end` | Tool execution complete. Has `result` with content and `isError`. |

### Full Flow Sequence

```
prompt → response → agent_start → turn_start
  → message_start(user) → message_end(user)
  → message_start(assistant)
    → message_update(thinking_start)
    → message_update(thinking_delta) × N
    → message_update(toolcall_start)
    → message_update(toolcall_delta) × N
    → message_update(thinking_end)
    → message_update(toolcall_end)
  → message_end(assistant)
  → tool_execution_start
  → tool_execution_update × N
  → tool_execution_end
  → message_start(toolResult) → message_end(toolResult)
  → turn_end
  → turn_start (next turn)
    → (repeat with text_start/text_delta/text_end)
  → turn_end
→ agent_end → agent_settled
```


## Data Flow

### Historic Path (loaded session)

```
JSONL file → Python (read + extract raw messages) → WebSocket → JS
  → enrichToolCalls (join toolResult → toolCall) → history.js → HTML
```

Python's role is thin: auth, ownership validation, file I/O, raw message extraction. All sub-agent parsing and rendering lives in JS.

### Live Path (pi RPC streaming)

```
pi subprocess → stdout events → Python (wrap as pi_event) → WebSocket → JS
  → chat.js (incremental) → subagent.js (enrichment) → HTML
```

Live events bypass Python parsing entirely. The JS renderer handles both incremental streaming and final reconciliation from `agent_end` messages.

### Debug Endpoint

```
JSONL file → Python (read + extract) → Node subprocess → tools/render_message.js
  → same JS modules → HTML string → HTTP response
```

Start the server, login to get a cookie, then:

```bash
curl -fsS -b /tmp/cookies.txt \
  "http://localhost:9000/api/debug/session?session_path=/absolute/path/to/session.jsonl"
```

Response: `{messageCount, messages[], assistantHtml[]}`. The `assistantHtml` array contains one HTML string per assistant run with fully rendered sub-agent cards.


## Session Formats

`sessions.py` supports two JSONL formats:

**Native pi sessions** — first line is a `session` record with metadata:
```json
{"type": "session", "version": 3, "id": "...", "cwd": "/home/user/pi-chat/sessions/b", ...}
```
Followed by `message` records (`role`: `user`, `assistant`, `toolResult`). Ownership is determined by `cwd`.

**RPC captures** — each line wraps a raw pi RPC event:
```json
{"seq": 1, "ts": "2026-07-28T14:37:40.585", "event": {"type": "agent_start", ...}}
```
No `cwd` or ownership metadata. Generated by `tools/capture.py`. Always starts with a few `extension_ui_request` events (telegram status) before real activity — these are filtered out during rendering.

The development viewer (`?session=`) and debug endpoint (with `PI_CHAT_DEV=1`) accept both formats.

