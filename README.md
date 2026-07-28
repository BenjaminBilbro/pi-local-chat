# pi chat

A small, local web interface for running the `pi` coding agent in RPC mode.
Streaming chat, image attachments, session history, Markdown rendering, and
sub-agent activity cards.

Intentionally designed for local, personal use.

**For LLMs working on this repo:** read `AGENTS.md` first. It is the single
source of truth for architecture, testing, RPC format, and development patterns.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A working `pi` executable on your `PATH`

Optional: Node.js 20.19+, 22.13+, or 24+ for the renderer-parity tests.

## Run locally

```bash
uv sync
uv run python server.py
```

Open [http://localhost:9000](http://localhost:9000). Select a profile, log in,
and start chatting. The `pi` subprocess starts on your first prompt.

## Authentication

Profile passwords are verified by FastAPI. After login, the browser receives an
HTTP-only session cookie; passwords and tokens are not exposed to JavaScript.
Sessions expire after seven days by default and are cleared on server restart.

For local use, the default profile passwords work out of the box. Before
exposing the app publicly, generate new salted scrypt hashes:

```bash
uv run python -m pi_chat.auth
```

Then set them in the server environment:

```bash
export PI_CHAT_B_PASSWORD_HASH='scrypt$...'
export PI_CHAT_R_PASSWORD_HASH='scrypt$...'
```

## Multiple users

Each WebSocket connection gets its own `pi` subprocess. Simultaneous users do
not share streaming state. Session history is separated by profile.

## Theme

Soft periwinkle and Lavender Blush. The theme toggle swaps their roles — one
becomes the page background, the other becomes panels and message surfaces.
Your choice is saved locally in the browser.

## Cloudflare Tunnel

When `cloudflared` runs on the same machine, listen only on loopback:

```bash
uv run uvicorn server:app --host 127.0.0.1 --port 9000
```

Point the tunnel at `http://127.0.0.1:9000` and use Cloudflare Access to
control who can reach the login page.

## Development

### Parity tests

```bash
npm test
```

### RPC capture

```bash
uv run python tools/capture.py --scenario general
uv run python tools/capture.py --scenario subagent
uv run python tools/capture.py --prompt "custom prompt"
```

### Session viewer

```bash
PI_CHAT_DEV=1 uv run python server.py
# Visit: http://localhost:9000/?session=/path/to/session.jsonl
```

For full technical details (architecture, RPC events, testing, rendering
contract), see `AGENTS.md`.
