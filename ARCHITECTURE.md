# Architecture

This document is the shortest path to understanding pi chat. The app is a
FastAPI server, a plain JavaScript frontend, and one persistent
`pi --mode rpc` subprocess per authenticated WebSocket connection.

Treat this file as the current source of truth. The `handoff*.md` files are
historical implementation notes and may describe older rendering code.

## Runtime invariants

1. The HTTP-only cookie identifies the active profile (`b` or `r`).
2. One browser WebSocket owns one `PiProcess` object.
3. A `PiProcess` does not spawn `pi` until the first prompt or session load.
4. `new_session` and `switch_session` reuse the existing subprocess.
5. Closing the WebSocket terminates its subprocess.
6. Session ownership is determined by the `cwd` in the pi JSONL header.
7. The server keeps authentication sessions in memory, so a restart logs
   browsers out.

An automatic WebSocket reconnect creates a new `PiProcess`. The browser may
still display messages from the previous connection, but the replacement
process does not have that conversation context. Load a saved session or start
a new conversation after an unexpected reconnect.

## Backend components

| File | Responsibility |
|------|----------------|
| `server.py` | Compatibility entry point for `uv run python server.py` |
| `pi_chat/app.py` | FastAPI construction, HTTP routes, authentication checks, and process ownership |
| `pi_chat/auth.py` | Password hashing, verification, and in-memory login sessions |
| `pi_chat/config.py` | Paths and environment-driven settings |
| `pi_chat/process.py` | `pi --mode rpc` lifecycle, stdin commands, stdout reader, and pending RPC responses |
| `pi_chat/websocket.py` | Browser command dispatch and higher-level pi RPC workflows |
| `pi_chat/sessions.py` | Session discovery, account filtering, previews, and JSONL parsing |

`pi_chat/app.py` is the composition root. It creates an authenticated
`PiProcess` for each WebSocket and tracks active processes so they can be
terminated during server shutdown.

## Frontend components

The frontend uses browser-native ES modules. There is no bundler or framework.

| File | Responsibility |
|------|----------------|
| `static/app.js` | Small composition root and server-message router |
| `static/auth.js` | Profile selection, login, and cookie-session restoration |
| `static/socket.js` | WebSocket connection, heartbeat, and reconnect timer |
| `static/chat.js` | Composer, image attachment, live event state, and streamed rendering |
| `static/history.js` | Rendering completed messages loaded from JSONL or pi |
| `static/timeline.js` | Shared assistant timeline DOM primitives and connectors |
| `static/subagent.js` | Shared sub-agent cards, result normalization, and receipt parsing |
| `static/sessions.js` | Session drawer, `/api/sessions`, and load progress |
| `static/theme.js` | Theme selection and local persistence |
| `static/utils.js` | Small formatting and escaping helpers |

`static/app.js` is the only module that knows how the frontend components fit
together. Transport code calls callbacks; it does not directly modify chat or
session UI.

## Rendering parity contract

Historic and live data arrive differently, but a completed run must produce
the same assistant DOM:

1. One user prompt and all following assistant/tool-result messages form one
   run. The run gets one `ASSISTANT` label and one connected timeline, even if
   pi emits multiple `agent_start`/`agent_end` cycles before `agent_settled`
   while retrying.
2. Live deltas render progressively in `chat.js`.
3. `tool_execution_end.result.details.results[0]` is the authoritative final
   sub-agent snapshot. Live updates rebuild only the small inner sub-agent
   timeline from each full snapshot.
4. `agent_end.messages` is the authoritative completed top-level run.
   `chat.js` keeps only the messages after the last user record and reconciles
   the live timeline through `history.js`. Retry segments are overlap-merged
   until `agent_settled`.
5. `timeline.js` creates thinking, tool, text, connector, and assistant-shell
   elements for both paths. Do not add a second historic-only or live-only DOM
   shape.
6. A live-only sub-agent status row is transient. Settled live and historic
   cards both contain the same message rows, one turns row, and one optional
   receipt summary. Reconciliation preserves a user's collapsed-card and
   keyboard-focus state.

Sub-agent result fields map as follows:

| Display data | Historical/native source | Live RPC source |
|---|---|---|
| Messages | `toolResult.details.results[0].messages` | `tool_execution_end.result.details.results[0].messages` |
| Turns | `details.results[0].usage.turns` | Same path under the final result |
| Turn limit | `details.results[0].maxTurnsLimit` | Same path under the final result |
| Status/summary | `details.results[0].receipt` | Same path under the final result |
| Fallback receipt | `PI_SUBAGENT_*_V1` text | `result.content` marker text |

Always prefer the structured `receipt` object. Some older captures contain
Python-style values such as `False` in the marker text, which is not valid
JSON. `sessions.py` enriches historical sub-agent tool calls before returning
only user and assistant messages; `subagent.js` performs the equivalent
tool-result join for raw `agent_end` messages. Both adapters also join ordinary
tool failure state by `toolCallId` before tool-result records are hidden. A
receipt-less sub-agent result keeps its plain result text, and assistant
messages with `stopReason: "error"` render `errorMessage` rather than an empty
timeline.

A failed prompt `response` or WebSocket disconnect is terminal for the current
browser-side run. `chat.js` clears the transient streaming state and unlocks
the composer; reconnecting does not pretend the replacement subprocess has the
lost conversation context. A successful prompt response that produces no
`agent_start` (for example, a handled extension command) also unlocks after a
short grace period.

## UI shell invariants

- Keep palette values and foreground role-swapping in `static/theme.css`.
- Live user bubbles append text before an attached image, matching pi's stored
  content order used by historical rendering.
- Sub-agent disclosure headers are real buttons with `aria-expanded`.
- The session drawer is a labelled dialog, closes on Escape, and returns focus
  to its opener.
- Keep the mobile media block after base component rules so drawer and timeline
  overrides win without selector escalation.

## Main flows

### Login

1. `auth.js` posts the selected account and password to `/api/login`.
2. `AuthManager` verifies the password and creates an opaque in-memory token.
3. FastAPI returns the token as an HTTP-only cookie.
4. `auth.js` reveals the chat UI and asks `app.js` to connect the WebSocket.
5. `/api/me` restores this state after a page reload.

### Live prompt

1. `chat.js` turns the composer contents into a browser `prompt` command.
2. `socket.js` sends the JSON command over `/ws`.
3. `websocket.py` lazily spawns the connection's `PiProcess`, if necessary.
4. `process.py` writes the pi RPC command to stdin.
5. The stdout reader wraps every pi event as:

   ```json
   {"type": "pi_event", "event": {"type": "..."}}
   ```

6. `app.js` routes the event to `chat.js`.
7. `chat.js` updates the live thinking, tool, sub-agent, and text timeline.
8. Final tool and agent events reconcile the completed DOM through the shared
   renderers described in the parity contract.

### New session

If pi is already running, `websocket.py` sends its native `new_session` RPC
command and waits for the matching response ID. It does not respawn the
subprocess. If pi has not started, the command starts it.

The frontend currently clears its displayed conversation as soon as the
browser command is sent.

### Load saved session

1. `sessions.js` fetches `/api/sessions`.
2. `sessions.py` scans `~/.pi/agent/sessions/` and keeps files whose header
   `cwd` matches `sessions/<profile>/`.
3. The selected absolute path is sent as a browser `load_session` command.
4. `websocket.py` validates that the path belongs to the authenticated profile.
5. The server sends `switch_session` and then `get_messages` to the existing
   pi subprocess.
6. `sessions.py` normalizes the public list to user/assistant messages and
   attaches completed sub-agent details.
7. The browser receives `session_loaded`; `history.js` groups and renders runs.

## Browser WebSocket protocol

Browser commands handled by `pi_chat/websocket.py`:

| Type | Purpose |
|------|---------|
| `prompt` | Send text and optional images to pi |
| `new_session` | Start a clean pi session without replacing the process |
| `load_session` | Switch pi to a validated saved session |
| `get_messages` | Request messages from the current pi session |
| `abort` | Abort the active pi request |
| `ping` | Keep the proxied WebSocket active |

Server messages routed by `static/app.js`:

| Type | Purpose |
|------|---------|
| `pi_event` | A raw pi RPC event for live rendering |
| `session_started` | Confirms that pi or a new session is ready |
| `session_loaded` | Contains historical messages to render |
| `messages_retrieved` | Contains messages from an explicit request |
| `error` | Reports browser-command or pi RPC failure |
| `pong` | Heartbeat response; no UI action |

See `RPC_EVENT_FORMAT.md` for the raw events inside `pi_event`.

## Session formats

`sessions.py` supports:

- Native pi JSONL files containing `session` and `message` records.
- RPC capture JSONL files whose records wrap events under an `event` key.

The development viewer accepts either format:

```text
http://localhost:9000/?session=/absolute/path/to/file.jsonl
```

Set `PI_CHAT_DEV=1` before starting the server. The historical messages are
rendered without spawning pi, although normal browser authentication is still
required.

## Where to make common changes

- Browser/server commands: `pi_chat/websocket.py` and `static/app.js`
- Process lifetime or pi CLI flags: `pi_chat/process.py`
- Live event rendering: `static/chat.js`
- Saved-message grouping: `static/history.js`
- Shared timeline markup/connectors: `static/timeline.js`
- Shared sub-agent normalization/cards: `static/subagent.js`
- Session discovery and parsing: `pi_chat/sessions.py`
- Login/session behavior: `pi_chat/auth.py`, `pi_chat/app.py`, and
  `static/auth.js`
- Colors: `static/theme.css`
- Evaluated UI dependency options: `UI_LIBRARY_RESEARCH.md`
- Visual testing and capture generation: `TESTING.md`

Run `npm test` after changing any parser, event handler, timeline primitive, or
sub-agent renderer. It replays the checked-in RPC captures through live
rendering, renders their final messages historically, compares the settled
assistant HTML exactly, verifies native turn grouping, and covers the malformed
receipt fixture plus retry, multi-block streaming, generic failure, disconnect,
and interaction-state regressions.
