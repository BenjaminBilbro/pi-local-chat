# AGENTS.md — pi-chat Technical Reference

Read this file first when working on pi-chat. It is the single source of truth for architecture, testing, RPC format, and development patterns.

## Project Summary

pi-chat is a local web interface for running the `pi` coding agent in RPC mode. It provides:

- Two local profiles (`b` and `r`), each with its own authentication and session history
- Streaming chat output via WebSocket, with image attachments
- Session history loaded from pi's native JSONL files
- Markdown rendering, sub-agent activity cards, and theme toggle
- One persistent `pi --mode rpc` subprocess per authenticated WebSocket connection

**Tech stack:** FastAPI + Starlette backend, plain ES module frontend (no bundler/framework), one `pi --mode rpc` subprocess per connection.

## Quick Start

```bash
# Install Python dependencies
uv sync

# Start the server
uv run python server.py

# Open in browser
# http://localhost:9000

# Run parity tests
npm test
```

## Runtime Invariants

These are non-negotiable. Violating any of these will break the application:

1. The HTTP-only cookie identifies the active profile (`b` or `r`).
2. One browser WebSocket owns one `PiProcess` object.
3. A `PiProcess` does not spawn `pi` until the first prompt or session load.
4. `new_session` and `switch_session` reuse the existing subprocess.
5. Closing the WebSocket terminates its subprocess.
6. Session ownership is determined by the `cwd` in the pi JSONL header.
7. The server keeps authentication sessions in memory; a restart logs browsers out.

**Reconnect behavior:** An automatic WebSocket reconnect creates a new `PiProcess`. The browser may still display messages from the previous connection, but the replacement process does not have that conversation context. After an unexpected reconnect, load a saved session or start a new conversation.

## Backend Components

| File | Responsibility |
|------|----------------|
| `server.py` | Compatibility entry point for `uv run python server.py` |
| `pi_chat/app.py` | FastAPI construction, HTTP routes, auth checks, process ownership, debug endpoint |
| `pi_chat/auth.py` | Password hashing, verification, in-memory login sessions |
| `pi_chat/config.py` | Paths and environment-driven settings |
| `pi_chat/process.py` | `pi --mode rpc` lifecycle, stdin commands, stdout reader, pending RPC responses |
| `pi_chat/websocket.py` | Browser command dispatch and higher-level pi RPC workflows |
| `pi_chat/sessions.py` | Session discovery, account filtering, previews, JSONL parsing |

### Voice Mode Components

| File | Responsibility |
|------|----------------|
| `pi_chat/tts_service.py` | Lazy OmniVoice model, voice cache, serialized inference |
| `pi_chat/tts_chunking.py` | Streaming Markdown-aware speech chunker |
| `pi_chat/voice_session.py` | Per-WebSocket voice state, queue, framing, cancellation |

`pi_chat/app.py` is the composition root. It creates an authenticated `PiProcess` for each WebSocket and tracks active processes for shutdown.

## Frontend Components

Browser-native ES modules. No bundler or framework.

| File | Responsibility |
|------|----------------|
| `static/app.js` | Composition root and server-message router |
| `static/auth.js` | Profile selection, login, cookie-session restoration |
| `static/socket.js` | WebSocket connection, heartbeat, reconnect timer |
| `static/chat.js` | Composer, image attachment, live event state, streamed rendering |
| `static/history.js` | Rendering completed messages from JSONL or pi |
| `static/timeline.js` | Shared assistant timeline DOM primitives and connectors |
| `static/subagent.js` | Shared sub-agent cards, result normalization, receipt parsing |
| `static/sessions.js` | Session drawer, `/api/sessions`, load progress |
| `static/theme.js` | Theme selection and local persistence |
| `static/utils.js` | Formatting and escaping helpers |

### Voice Mode Frontend

| File | Responsibility |
|------|----------------|
| `static/voice.js` | UI state, Web Audio scheduling, binary frame parsing |

`static/app.js` composes voice the same way it composes chat and sessions.

`static/app.js` is the only module that knows how the frontend components fit together. Transport code calls callbacks; it does not directly modify chat or session UI.

## Rendering Parity Contract (CRITICAL)

Historic and live data arrive differently, but a completed run must produce identical assistant DOM. This is enforced by `npm test`.

### Rules

1. One user prompt and all following assistant/tool-result messages form one **run**. The run gets one `ASSISTANT` label and one connected timeline, even if pi emits multiple `agent_start`/`agent_end` cycles before `agent_settled` while retrying.
2. Live deltas render progressively in `chat.js`.
3. `tool_execution_end.result.details.results[0]` is the authoritative final sub-agent snapshot. Live updates rebuild only the small inner sub-agent timeline from each full snapshot.
4. `agent_end.messages` is the authoritative completed top-level run. `chat.js` keeps only messages after the last user record and reconciles the live timeline through `history.js`. Retry segments are overlap-merged until `agent_settled`.
5. `timeline.js` creates thinking, tool, text, connector, and assistant-shell elements for both paths. Do not add a second historic-only or live-only DOM shape.
6. A live-only sub-agent status row is transient. Settled live and historic cards both contain the same message rows, one turns row, and one optional receipt summary. Reconciliation preserves a user's collapsed-card and keyboard-focus state.

### Sub-agent Result Field Mapping

| Display data | Historical/native source | Live RPC source |
|---|---|---|
| Messages | `toolResult.details.results[0].messages` | `tool_execution_end.result.details.results[0].messages` |
| Turns | `details.results[0].usage.turns` | Same path under the final result |
| Turn limit | `details.results[0].maxTurnsLimit` | Same path under the final result |
| Status/summary | `details.results[0].receipt` | Same path under the final result |
| Fallback receipt | `PI_SUBAGENT_*_V1` text | `result.content` marker text |

Always prefer the structured `receipt` object. Some older captures contain Python-style values such as `False` in the marker text, which is not valid JSON.

`sessions.py` returns raw messages (including `toolResult`) without sub-agent enrichment. `subagent.js` performs all sub-agent parsing via `enrichToolCalls()`, which joins `toolResult` records to their corresponding `toolCall` items and attaches `_timelineMessages`, `_status`, `_summary`, `_turns`, `_maxTurns`, and `_isError`. Both historic and live paths use the same JS rendering modules, so a completed run produces identical DOM regardless of source.

### Terminal Conditions

- A failed prompt `response` or WebSocket disconnect is terminal for the current browser-side run. `chat.js` clears transient streaming state and unlocks the composer.
- A successful prompt response that produces no `agent_start` (e.g., a handled extension command) also unlocks after a short grace period.

## UI Shell Invariants

- Keep palette values and foreground role-swapping in `static/theme.css`.
- Live user bubbles append text before an attached image, matching pi's stored content order.
- Sub-agent disclosure headers are real buttons with `aria-expanded`.
- The session drawer is a labelled dialog, closes on Escape, and returns focus to its opener.
- Keep the mobile media block after base component rules so drawer and timeline overrides win without selector escalation.

## Main Flows

### Login

1. `auth.js` posts selected account and password to `/api/login`.
2. `AuthManager` verifies the password and creates an opaque in-memory token.
3. FastAPI returns the token as an HTTP-only cookie.
4. `auth.js` reveals the chat UI and asks `app.js` to connect the WebSocket.
5. `/api/me` restores this state after a page reload.

### Live Prompt

1. `chat.js` turns composer contents into a browser `prompt` command.
2. `socket.js` sends the JSON command over `/ws`.
3. `websocket.py` lazily spawns the connection's `PiProcess`, if necessary.
4. `process.py` writes the pi RPC command to stdin.
5. The stdout reader wraps every pi event as: `{"type": "pi_event", "event": {"type": "..."}}`
6. `app.js` routes the event to `chat.js`.
7. `chat.js` updates the live thinking, tool, sub-agent, and text timeline.
8. Final tool and agent events reconcile the completed DOM through the shared renderers.

### New Session

If pi is already running, `websocket.py` sends its native `new_session` RPC command and waits for the matching response ID. It does not respawn the subprocess. If pi has not started, the command starts it. The frontend clears its displayed conversation as soon as the browser command is sent.

### Load Saved Session

1. `sessions.js` fetches `/api/sessions`.
2. `sessions.py` scans `~/.pi/agent/sessions/` and keeps files whose header `cwd` matches `sessions/<profile>/`.
3. The selected absolute path is sent as a browser `load_session` command.
4. `websocket.py` validates that the path belongs to the authenticated profile.
5. The server sends `switch_session` and then `get_messages` to the existing pi subprocess.
6. `sessions.py` extracts raw messages from the JSONL (user, assistant, toolResult) without sub-agent enrichment.
7. The browser receives `session_loaded`; `history.js` calls `enrichToolCalls` to join `toolResult` records to `toolCall` items, then renders runs.

## Browser WebSocket Protocol

### Browser Commands (handled by `pi_chat/websocket.py`)

| Type | Purpose |
|------|---------|
| `prompt` | Send text and optional images to pi |
| `new_session` | Start a clean pi session without replacing the process |
| `load_session` | Switch pi to a validated saved session |
| `get_messages` | Request messages from the current pi session |
| `abort` | Abort the active pi request |
| `ping` | Keep the proxied WebSocket active |

### Server Messages (routed by `static/app.js`)

| Type | Purpose |
|------|---------|
| `pi_event` | A raw pi RPC event for live rendering |
| `session_started` | Confirms that pi or a new session is ready |
| `session_loaded` | Contains historical messages to render |
| `messages_retrieved` | Contains messages from an explicit request |
| `error` | Reports browser-command or pi RPC failure |
| `pong` | Heartbeat response; no UI action |

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

## Testing

### Run Parity Tests

```bash
npm test
```

`npm test` runs `tests/compare_render.py`, which uses jsdom to:

1. Replay checked-in RPC fixtures through the production live renderer (`chat.js`)
2. Render the same completed messages through the production history renderer (`history.js`)
3. Require the settled assistant HTML to match exactly

It also verifies: structured receipt preference, final sub-agent snapshot replacement, generic failure normalization, retry lifecycle parity, multi-block streaming order, prompt/connection recovery, and settled-card interaction state.

**Always run `npm test` after changing any parser, event handler, timeline primitive, or sub-agent renderer.**

### Fast Non-Visual Checks

Run these before opening a browser:

```bash
npm test

# Compile-check Python files
python3 -m compileall -q pi_chat server.py tools/capture.py

# Syntax-check JS files
for file in static/*.js; do
  if [ "$(basename "$file")" != "marked.min.js" ]; then
    node --check "$file"
  fi
done

# Check files served by FastAPI (requires server running)
curl -fsS http://localhost:9000/ > /dev/null
curl -fsS http://localhost:9000/static/app.js > /dev/null
curl -fsS http://localhost:9000/static/chat.js > /dev/null
curl -fsS http://localhost:9000/static/history.js > /dev/null
```

## Development Setup

### Start pi-chat in Test/Dev Mode

Create a temporary scrypt hash for a known test password, then pass it only to this server process:

```bash
cd /home/bbilbro/pi-chat

PI_CHAT_TEST_HASH="$(
  uv run python -c "from pi_chat.auth import hash_password; print(hash_password('test-only'))"
)"

PI_CHAT_DEV=1 \
PI_CHAT_B_PASSWORD_HASH="$PI_CHAT_TEST_HASH" \
uv run python server.py > /tmp/pi-chat.log 2>&1 &

PI_CHAT_SERVER_PID=$!
```

This is real server-side authentication, not a browser bypass. The password hash is generated at startup, is not written to the repository, and disappears with the shell variable.

Verify the server:

```bash
curl -fsS http://127.0.0.1:9000/ | head -3
```

If startup fails:

```bash
tail -50 /tmp/pi-chat.log
```

### Acquire a Cookie via curl

```bash
curl -fsS -X POST http://127.0.0.1:9000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"account":"b","password":"test-only"}' \
  -c /tmp/pi-chat-cookies.txt
```

Use `-b /tmp/pi-chat-cookies.txt` in subsequent requests to authenticate.

### Debug Endpoint

The `/api/debug/session` endpoint renders a session through the full Python→JS pipeline and returns both raw messages and final HTML. It reuses the same `tools/render_message.js` harness that `npm test` uses, so the output is identical to what the browser renders.

```bash
curl -fsS -b /tmp/pi-chat-cookies.txt \
  "http://127.0.0.1:9000/api/debug/session?session_path=/absolute/path/to/session.jsonl" \
  | python3 -m json.tool
```

Response shape:

```json
{
  "messageCount": 6,
  "messages": [...],
  "assistantHtml": ["<div class=\"message assistant\">...", ...]
}
```

The `assistantHtml` array contains one HTML string per assistant run, with sub-agent cards fully rendered (tool calls, status, summary, turns). This is the fastest way to inspect what a session will look like without opening a browser.

**Ownership check:** The endpoint validates that the session's `cwd` belongs to the authenticated profile. With `PI_CHAT_DEV=1`, this check is skipped so RPC captures (which have no `cwd`) can be debugged. Without dev mode, only native sessions owned by the current profile are accepted.

**What it does not do:** It does not spawn a `pi` subprocess, and it does not execute live events. It only renders saved sessions through the historic path.

### Development Session Viewer

Load a saved JSONL session directly in the browser without starting a pi subprocess:

```bash
PI_CHAT_DEV=1 uv run python server.py
```

Then visit: `http://localhost:9000/?session=/absolute/path/to/session.jsonl`

Both native pi session files and wrapped RPC capture format are supported. Normal browser authentication is still required. Unlike `/api/debug/session`, the `?session=` path has no ownership check in dev mode — any readable `.jsonl` file works.

### What the Historical-Render Test Covers

With `PI_CHAT_DEV=1` and `?session=/absolute/file.jsonl`:

1. Browser performs the normal login flow.
2. `auth.js` restores the HTTP-only cookie after navigation.
3. `socket.js` opens the authenticated WebSocket.
4. `websocket.py` reads the development session path.
5. `sessions.py` parses native pi records or wrapped RPC events.
6. The server sends one `session_loaded` message.
7. `app.js` routes it through `sessions.js`.
8. `history.js` renders user, assistant, tool, Markdown, and sub-agent content.

No pi subprocess is spawned for this path. Use a normal live prompt separately when testing `chat.js` streaming behavior or changes to `PiProcess`.

## Camofox Visual Testing

This workflow uses `camoufox-browser` to exercise and screenshot the pi-chat UI. It intentionally does not use Playwright or Chromium.

### Paths and URLs

```bash
export PI_CHAT_ROOT=/home/bbilbro/pi-chat
export CAMOFOX_ROOT=/home/bbilbro/camoufox-testing/camofox-browser
export PI_CHAT_URL=http://127.0.0.1:9000
export CAMOFOX_URL=http://127.0.0.1:9377
```

### Start Camoufox

```bash
source ~/.bashrc
cd "$CAMOFOX_ROOT"
node server.js > /tmp/camofox-browser.log 2>&1 &
CAMOFOX_SERVER_PID=$!

# Wait for browser pre-warm
sleep 12
curl -fsS "$CAMOFOX_URL/health"
```

Expected: `{"ok": true, "engine": "camoufox", "browserConnected": true, "browserRunning": true}`

If the Camofox binary is missing:

```bash
cd /home/bbilbro/camoufox-testing
uv run python -m camoufox fetch
```

### Core Screenshot Workflow

**1. Create a tab:**

```bash
PI_CHAT_TAB_ID="$(
  curl -fsS -X POST "$CAMOFOX_URL/tabs" \
    -H 'Content-Type: application/json' \
    -d "{\"userId\":\"agent\",\"sessionKey\":\"pichat\",\"url\":\"$PI_CHAT_URL\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['tabId'])"
)"
```

**2. Log in through the real UI:**

```bash
PI_CHAT_LOGIN_PAYLOAD="$(
python3 - <<'PY'
import json
expression = """
document.querySelector('#icon-b').click();
document.querySelector('#passphrase').value = 'test-only';
document.querySelector('#login-btn').click();
'login submitted';
"""
print(json.dumps({"userId": "agent", "expression": expression}))
PY
)"

curl -fsS -X POST "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/evaluate" \
  -H 'Content-Type: application/json' \
  -d "$PI_CHAT_LOGIN_PAYLOAD"
sleep 2
```

**3. Load a recorded session:**

```bash
export PI_CHAT_SESSION_PATH="$PI_CHAT_ROOT/data-samples/subagent_rpc_capture.jsonl"
export PI_CHAT_SESSION_URL="$PI_CHAT_URL/?session=$PI_CHAT_SESSION_PATH"
test -f "$PI_CHAT_SESSION_PATH"

PI_CHAT_NAVIGATION_PAYLOAD="$(
python3 - <<'PY'
import json, os
print(json.dumps({"userId": "agent", "url": os.environ["PI_CHAT_SESSION_URL"]}))
PY
)"

curl -fsS -X POST "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/navigate" \
  -H 'Content-Type: application/json' \
  -d "$PI_CHAT_NAVIGATION_PAYLOAD"
sleep 4
```

**4. Inspect before screenshot:**

```bash
curl -fsS "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/snapshot?userId=agent"
```

Look for the first user prompt, assistant text, and expected tool/sub-agent labels.

**5. Capture both themes:**

```bash
curl -fsS "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/screenshot?userId=agent" > /tmp/pi-chat-theme-a.png

# Switch theme
curl -fsS -X POST "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/evaluate" \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","expression":"document.querySelector(\'#chat-screen [data-theme-toggle]\').click();"}'
sleep 1

curl -fsS "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/screenshot?userId=agent" > /tmp/pi-chat-theme-b.png
```

**6. Close the tab:**

```bash
curl -fsS -X DELETE "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID?userId=agent"
```

**Cleanup:**

```bash
kill "$PI_CHAT_SERVER_PID" "$CAMOFOX_SERVER_PID"
```

### Other Camofox Interactions

Click a control by its accessibility snapshot reference:

```bash
curl -fsS -X POST "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/click" \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","ref":"e1"}'
```

For behavior not exposed by a dedicated endpoint, use the same `/evaluate` payload pattern. Prefer clicking real controls over directly mutating application state.

## Useful Session Files

JSONL files are ignored by Git; their availability depends on the local machine:

| Path | Purpose |
|------|---------|
| `data-samples/subagent_rpc_capture.jsonl` | Historical sub-agent card and nested tool calls |
| `data-samples/rpc_capture.jsonl` | General thinking, tool, and Markdown rendering |
| `data-samples/*.jsonl` | Other locally saved fixtures |
| `~/.pi/agent/sessions/**/*.jsonl` | Native sessions recorded by pi |

Find recent native sessions:

```bash
find ~/.pi/agent/sessions -name '*.jsonl' -type f | tail -20
```

## Generate RPC Fixtures

```bash
uv run python tools/capture.py --scenario general    # Thinking, tools, text
uv run python tools/capture.py --scenario subagent   # Sub-agent spawn events
uv run python tools/capture.py --scenario nested     # Nested sub-agents + session copy
uv run python tools/capture.py --prompt "custom"     # One-off custom prompt
uv run python tools/capture.py --prompt "x" --output data-samples/custom.jsonl
uv run python tools/capture.py --prompt "x" --timeout 300
```

Output files go to `data-samples/<scenario>_capture.jsonl` by default.

**`--no-session` behavior:** The `general` and `subagent` scenarios use `--no-session`, so pi writes no native session file — only the RPC capture is produced. The `nested` scenario omits `--no-session` and copies the native session to `data-samples/nested_subagent_session.jsonl`. Custom `--prompt` captures always use `--no-session` (RPC capture only).

## Mobile Viewport Testing

```bash
uv run python tests/mobile_viewport_test.py --device iphone16
uv run python tests/mobile_viewport_test.py --all
```

Requires Camoufox running and pi-chat server on port 9000. Captures screenshots and accessibility snapshots for real mobile device viewports.

## Troubleshooting

### The login screen remains visible

- Confirm the server was started with `PI_CHAT_B_PASSWORD_HASH`.
- Confirm the test password is exactly `test-only`.
- Inspect `/tmp/pi-chat.log`.
- Use the Camofox snapshot endpoint to confirm that the expected login controls exist before evaluating the login expression.

### The page is logged in but no session appears

- Confirm `PI_CHAT_DEV=1` was set on the server process.
- Confirm the session path is absolute and readable by the server.
- Reload the same URL after confirming the cookie was created.
- Inspect `/tmp/pi-chat.log` for `Failed to parse session file`.

### Camofox is unavailable

- Check `$CAMOFOX_URL/health`.
- Inspect `/tmp/camofox-browser.log`.
- Allow the initial browser pre-warm to finish before creating a tab.

### A screenshot is blank or premature

- Request a snapshot first.
- Wait for known text from the fixture rather than relying only on a fixed delay.
- Keep the same `userId`, `sessionKey`, and tab ID throughout the run.

### The debug endpoint returns 404 for an RPC capture

RPC captures have no `cwd` header, so `session_belongs_to_account` fails. Start the server with `PI_CHAT_DEV=1` to skip the ownership check, or use `?session=` in the browser instead.

## Where to Make Common Changes

- Browser/server commands: `pi_chat/websocket.py` and `static/app.js`
- Process lifetime or pi CLI flags: `pi_chat/process.py`
- Live event rendering: `static/chat.js`
- Saved-message grouping: `static/history.js`
- Shared timeline markup/connectors: `static/timeline.js`
- Shared sub-agent normalization/cards: `static/subagent.js`
- Session discovery and raw parsing: `pi_chat/sessions.py`
- Login/session behavior: `pi_chat/auth.py`, `pi_chat/app.py`, `static/auth.js`
- Colors: `static/theme.css`

## File Structure Reference

```
pi-chat/
├── pi_chat/
│   ├── app.py          # FastAPI app, routes, debug endpoint
│   ├── auth.py         # Password verification, browser sessions
│   ├── config.py       # Paths and environment settings (incl. TTS config)
│   ├── process.py      # pi subprocess lifecycle, RPC responses, event observer
│   ├── sessions.py     # Session discovery and JSONL message extraction
│   ├── websocket.py    # Browser WebSocket command handling (incl. voice cmds)
│   ├── tts_service.py  # Lazy OmniVoice model, voice cache, serialized inference
│   ├── tts_chunking.py # Streaming Markdown-aware speech chunker
│   └── voice_session.py # Per-WebSocket voice state, queue, framing
├── static/
│   ├── index.html      # Page structure (incl. voice controls)
│   ├── theme.css       # Color palette and design tokens
│   ├── styles.css      # Component and responsive styles (incl. voice UI)
│   ├── app.js          # Frontend composition and message routing
│   ├── auth.js         # Local profile selection
│   ├── socket.js       # WebSocket, heartbeat, reconnect, binary frames
│   ├── chat.js         # Composer and live event rendering
│   ├── history.js      # Historical message rendering
│   ├── timeline.js     # Shared timeline DOM primitives
│   ├── subagent.js     # Shared sub-agent cards and result adapters
│   ├── sessions.js     # Session drawer and load workflow
│   ├── theme.js        # Persistent palette-role toggle
│   ├── voice.js        # Voice UI state, Web Audio scheduling, binary parsing
│   ├── utils.js        # Browser-side formatting helpers
│   └── marked.min.js   # Vendored Markdown renderer
├── tests/
│   ├── compare_render.py        # Live vs historical rendering parity tests
│   ├── mobile_viewport_test.py  # Mobile viewport screenshot tests
│   ├── fakes/                   # Deterministic test doubles
│   │   └── voice.py             # FakeOmniVoiceRuntime, FakeTTSService
│   ├── test_voice_fakes.py      # Fake runtime tests (no torch)
│   ├── test_tts_chunking.py     # Streaming chunker tests
│   ├── test_tts_service.py      # TTSService tests with fake runtime
│   ├── test_voice_session.py    # VoiceSession tests
│   └── test_voice_fake_e2e.py   # No-GPU pi-event-to-frame integration
├── tools/
│   ├── capture.py                # Unified RPC event capture CLI
│   ├── render_message.js         # jsdom harness for production JS rendering
│   ├── run_fake_voice_server.py  # Dev server with audible fake PCM
│   ├── run_voice_cpu_validation.py # Real checkpoint CPU validator
│   ├── run_voice_gpu_validation.py # Human-gated GPU validator
│   └── benchmark_voice.py        # Latency/RTF benchmark
├── experiments/                 # Cloned sources (ignored by git)
│   └── OmniVoice/               # OmniVoice source (not the model checkpoint)
├── data-samples/                # RPC capture fixtures and native sessions
├── voice-validation/            # Validation artifacts (ignored by git)
├── server.py                    # Backward-compatible launch entry point
├── roxy.md                      # Extra system prompt for the Roxy profile
├── pyproject.toml               # Python project configuration
└── package.json                 # Node devDependencies (jsdom)
```
