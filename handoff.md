# pi-chat Handoff — Session Loading Feature

## Overview

pi-chat is a FastAPI + single-page HTML frontend that runs `pi --mode rpc` as a subprocess and proxies events over WebSocket. Two accounts: **Ben** (`b`) and **Roxy** (`r`), each with their own persistent work directory and session history.

## Architecture

```
browser (static/index.html)
  ↕ WebSocket /ws
server.py (FastAPI + uvicorn, port 9000)
  ↕ stdin/stdout JSON-RPC
pi --mode rpc --approve  (subprocess)
```

### Key Directories

| Path | Purpose |
|------|---------|
| `./sessions/b/` | Ben's work directory (cwd for pi subprocess) |
| `./sessions/r/` | Roxy's work directory |
| `~/.pi/agent/sessions/` | Where pi stores ALL session `.jsonl` files, subdirs named by cwd (e.g., `--home-bbilbro-pi-chat-sessions-r--/`) |

### Session Files

Each `.jsonl` has a header line with `cwd` field. The server filters sessions by matching this `cwd` to the account's work dir.

## Recent Changes (Session Loading Feature)

### `server.py` — Server-side

**Persistent work dirs** (was ephemeral `/tmp`):
- `spawn()` creates `./sessions/<account>/` instead of temp dirs
- Removed `--no-session` flag so pi manages sessions normally
- `kill()` no longer cleans up the work dir

**RPC request/response matching**:
- `_pending_requests: dict[str, asyncio.Future]` — maps request IDs to Futures
- `_send_and_wait(command, request_id, timeout)` — sends a command with an `id` field, awaits the matching response from stdout
- `_read_stdout()` intercepts events with matching `id` and resolves/rejects the Future
- On kill, pending Futures get an exception

**New REST endpoints**:
- `GET /api/sessions` — scans `~/.pi/agent/sessions/*.jsonl`, filters by cwd matching account's work dir, returns `{sessions: [{id, timestamp, cwd, path, messageCount, firstMessage}]}` sorted newest first
- `GET /api/sessions/preview?session_path=` — returns header, first message preview, and message count

**New WebSocket commands**:
- `load_session` — spawns pi if needed, sends `switch_session` to pi, then `get_messages`, sends `session_loaded` with messages to UI
- `get_messages` — fetches current session messages, sends `messages_retrieved` to UI

### `static/index.html` — Frontend

**Session panel** (slide-in from left):
- Hamburger menu button (`#session-menu-btn`) in header
- Panel (`#session-panel`) with backdrop overlay
- Fetches sessions from `/api/sessions` when opened
- Each item shows: first message preview, message count, relative timestamp
- Click to load; loading state prevents double-clicks
- Loading overlay (`#session-load-overlay`) with spinner

**WebSocket event handling** (updated `connectWS()`):
- `session_loaded` — clears messages, renders history, closes panel, dismisses overlay
- `messages_retrieved` — renders messages
- `error` — shows inline error if loading was in progress

**Historical message rendering**:
- `renderHistoricalMessages(messages)` — iterates messages, routes by role
- `renderHistoricalUserMessage(msg)` — right-aligned bubble with text + images
- `renderHistoricalAssistantMessage(msg)` — thinking blocks (dimmed), tool call icons (⚡), markdown text via `marked.js`
- `toolResult` messages are skipped (internal)

**Pi RPC message content format**:
```
thinking: { type: 'thinking', thinking: '...', thinkingSignature: 'reasoning_content' }
toolCall: { type: 'toolCall', id: '...', name: 'bash', arguments: {...} }
text:     { type: 'text', text: '...' }
image:    { type: 'image', data: base64, mimeType: 'image/png' }
```

## Files

| File | Description |
|------|-------------|
| `server.py` | FastAPI server, pi subprocess manager, REST + WS endpoints |
| `static/index.html` | Single-page frontend (HTML + CSS + JS) |
| `static/marked.min.js` | Markdown renderer |
| `roxy.md` | Custom system prompt appended for account `r` |

## Known Details

- Sessions endpoint returns empty until `set_account` is received (user logs in)
- Old sessions in `~/.pi/agent/sessions/--home-bbilbro-pi-chat--/` (cwd: `/home/bbilbro/pi-chat`) won't match any account — they were created before the persistent work dir changes
- Ben's sessions dir (`--home-bbilbro-pi-chat-sessions-b--/`) doesn't exist yet (no b sessions created via the server)
- Roxy has 6 sessions in `--home-bbilbro-pi-chat-sessions-r--/`
