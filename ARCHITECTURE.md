# Architecture

This document is the shortest path to understanding pi chat. The app is a
FastAPI server, a plain JavaScript frontend, and one persistent
`pi --mode rpc` subprocess per authenticated WebSocket connection.

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
| `static/sessions.js` | Session drawer, `/api/sessions`, and load progress |
| `static/theme.js` | Theme selection and local persistence |
| `static/utils.js` | Small formatting and escaping helpers |

`static/app.js` is the only module that knows how the frontend components fit
together. Transport code calls callbacks; it does not directly modify chat or
session UI.

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
6. The browser receives `session_loaded`; `history.js` renders the messages.

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
- Saved-message rendering: `static/history.js`
- Session discovery and parsing: `pi_chat/sessions.py`
- Login/session behavior: `pi_chat/auth.py`, `pi_chat/app.py`, and
  `static/auth.js`
- Colors: `static/theme.css`
- Visual testing and capture generation: `TESTING.md`
