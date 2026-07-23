# pi chat

A small, local web interface for running the `pi` coding agent in RPC mode. It
provides two local profiles, streaming chat output, image attachments, session
history, Markdown rendering, and visual handling for sub-agent activity.

The application is intentionally designed for local, personal use.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A working `pi` executable available on your `PATH`

## Run locally

Install the Python dependencies:

```bash
uv sync
```

Start the server:

```bash
uv run python server.py
```

Then open [http://localhost:9000](http://localhost:9000).

The server starts a `pi --mode rpc --approve` subprocess only after a profile
has been selected and the first prompt is submitted. Each profile gets a work
directory under `sessions/<profile>/`; pi's session history remains in
`~/.pi/agent/sessions/` and is filtered by that work directory.

## Project structure

```text
pi-local-chat/
├── pi_chat/
│   ├── app.py          # FastAPI application and HTTP routes
│   ├── auth.py         # Password verification and browser sessions
│   ├── config.py       # Shared paths and environment settings
│   ├── process.py      # pi subprocess lifecycle and RPC responses
│   ├── sessions.py     # Session discovery and JSONL parsing
│   └── websocket.py    # Browser WebSocket command handling
├── static/
│   ├── index.html      # Page structure
│   ├── theme.css       # Color palette and shared design tokens
│   ├── styles.css      # Component and responsive styles
│   ├── app.js          # Frontend composition and message routing
│   ├── auth.js         # Local profile selection
│   ├── socket.js       # WebSocket, heartbeat, and reconnect behavior
│   ├── chat.js         # Composer and live event rendering
│   ├── history.js      # Historical message rendering
│   ├── sessions.js     # Session drawer and load workflow
│   ├── theme.js        # Persistent palette-role toggle
│   ├── utils.js        # Browser-side formatting helpers
│   └── marked.min.js   # Vendored Markdown renderer
├── server.py           # Backward-compatible launch entry point
├── ARCHITECTURE.md     # Runtime flows, invariants, and component ownership
├── TESTING.md          # Camofox visual testing and RPC capture workflow
├── roxy.md             # Extra system prompt for the Roxy profile
├── capture_rpc.py      # RPC event capture utility
└── RPC_EVENT_FORMAT.md # Captured RPC event reference
```

## Development session viewer

Set `PI_CHAT_DEV` to load a saved JSONL session directly in the browser without
starting a `pi` subprocess:

```bash
PI_CHAT_DEV=1 uv run python server.py
```

Then visit:

```text
http://localhost:9000/?session=/absolute/path/to/session.jsonl
```

Both native pi session files and the wrapped RPC capture format are supported.
See `ARCHITECTURE.md` for the runtime flows and `TESTING.md` for the Camofox
visual testing workflow.

## Authentication

Profile passwords are verified by FastAPI. After a successful login, the
browser receives an HTTP-only session cookie; password hashes and session
tokens are not available to browser JavaScript. Sessions expire after seven
days by default and are cleared whenever the server restarts.

The original profile passwords continue to work through legacy server-side
hashes. Before exposing the app through a public hostname, create new salted
scrypt hashes:

```bash
uv run python -m pi_chat.auth
```

Set the generated values in the server environment:

```bash
export PI_CHAT_B_PASSWORD_HASH='scrypt$...'
export PI_CHAT_R_PASSWORD_HASH='scrypt$...'
```

Use `PI_CHAT_SESSION_TTL` to override the cookie lifetime in seconds.

## Cloudflare Tunnel

When `cloudflared` runs on the same computer as pi chat, the app can listen only
on the loopback interface:

```bash
uv run uvicorn server:app --host 127.0.0.1 --port 9000
```

Point the tunnel service URL at `http://127.0.0.1:9000`. This keeps port 9000
unreachable directly from other computers while still allowing the local
`cloudflared` process to proxy requests.

A tunnel publishes connectivity; use a Cloudflare Access self-hosted
application with an allow policy for the two or three approved email addresses
to control who can reach the login page.

## Multiple users

Each authenticated WebSocket connection gets its own `pi` subprocess, so
simultaneous users do not share streaming state or overwrite each other's
browser connection. Session history remains separated by profile work
directory. With two or three users, this in-process design is sufficient as
long as the machine and model backend can handle the concurrent agent requests.

The subprocess is created lazily on the connection's first prompt and stays
alive for that WebSocket. Starting a new chat uses pi's `new_session` RPC
command instead of replacing the subprocess. A small browser heartbeat keeps
idle proxied WebSockets active; a browser disconnect, sign-out, or server
shutdown still ends its subprocess.

## Theme

The palette lives in `static/theme.css`. The interface currently uses soft
periwinkle `#9CA2D1` and Lavender Blush `#FFF2F2`. The theme toggle swaps their
roles: one becomes the full-page background while the other becomes headers,
panels, inputs, buttons, and message surfaces. Foreground tokens also invert to
keep text and icons readable in both modes. The selected theme is saved locally
in the browser.

## Capture utilities

`capture_rpc.py` records a short general RPC interaction, while
`capture_subagent_rpc.py` records a sub-agent-oriented interaction. These are
development utilities and invoke the local `pi` executable directly.
