# Testing Guide for LLM Agents

Visual testing workflow for the pi-chat web UI using camoufox-browser.

## Prerequisites

### 1. Start the pi-chat server (dev mode)

```bash
cd /home/bbilbro/pi-chat
PI_CHAT_DEV=1 uv run python server.py &
```

The `PI_CHAT_DEV=1` env var enables the `?session=PATH` URL parameter for loading session files directly without spawning a pi subprocess.

Verify:
```bash
curl -s http://localhost:9000/ | head -3
# → <!DOCTYPE html>...
```

### 2. Start camoufox-browser

```bash
# Source env vars (includes CAMOFOX_API_KEY)
source ~/.bashrc

# Start the server
cd /home/bbilbro/camoufox-testing/camofox-browser
node server.js > /tmp/camofox-browser.log 2>&1 &

# Wait for browser to pre-warm (~10-12s)
sleep 12

# Verify
curl -s http://localhost:9377/health
# → {"ok":true,"engine":"camoufox","browserConnected":true,"browserRunning":true,...}
```

If camoufox binary is not installed:
```bash
cd /home/bbilbro/camoufox-testing
uv run python -m camoufox fetch
```

## Core Workflow

### Step 1: Create a Tab

```bash
TAB=$(curl -s -X POST http://localhost:9377/tabs \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","sessionKey":"pichat","url":"http://localhost:9000"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['tabId'])")
```

### Step 2: Bypass Login

The app requires password authentication. For testing, bypass it via JS evaluation:

```bash
# Execute the bypass script (stored in static/bypass_login.js)
python3 -c "import json; print(json.dumps({'userId':'agent','expression':open('/home/bbilbro/pi-chat/static/bypass_login.js').read()}))" | \
  curl -s -X POST "http://localhost:9377/tabs/$TAB/evaluate" \
  -H 'Content-Type: application/json' -d @-
```

### Step 3: Navigate to a Session

Use the `?session=PATH` param to load a JSONL file (pi session or RPC capture):

```bash
# Load an RPC capture file
curl -s -X POST "http://localhost:9377/tabs/$TAB/navigate" \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","url":"http://localhost:9000/?session=/home/bbilbro/pi-chat/subagent_rpc_capture.jsonl"}'

# Wait for page to reload + WS to connect + session to load
sleep 5

# Bypass login again (page reload resets JS state)
python3 -c "import json; print(json.dumps({'userId':'agent','expression':open('/home/bbilbro/pi-chat/static/bypass_login.js').read()}))" | \
  curl -s -X POST "http://localhost:9377/tabs/$TAB/evaluate" \
  -H 'Content-Type: application/json' -d @-

# Wait for WS to deliver session_loaded event
sleep 3
```

### Step 4: Take a Screenshot

```bash
curl -s "http://localhost:9377/tabs/$TAB/screenshot?userId=agent" > /tmp/screenshot.png
```

### Step 5: Cleanup

```bash
curl -s -X DELETE "http://localhost:9377/tabs/$TAB?userId=agent"
```

## Available Session Files

| File | Description |
|------|-------------|
| `~/pi-chat/subagent_rpc_capture.jsonl` | Sub-agent spawn with 3 bash tool calls |
| `~/pi-chat/rpc_capture.jsonl` | Basic RPC capture with ls + file read |
| `~/.pi/agent/sessions/--home-bbilbro-pi-chat--/*.jsonl` | Live pi-chat sessions |
| `~/.pi/agent/sessions/--home-bbilbro--/*.jsonl` | Live home directory sessions |

Sessions are organized by working directory in subdirectories under `~/.pi/agent/sessions/`. Each subdirectory name is the URL-encoded path (e.g., `--home-bbilbro-pi-chat--` = `/home/bbilbro/pi-chat/`).

## Generating RPC Capture Files

The agent can generate its own RPC JSONL files using the Python scripts in this repo. This is useful for creating test data or capturing specific interactions.

### capture_rpc.py — Standard RPC Capture

Captures a full pi RPC session with tool calls, thinking, and responses:

```bash
cd /home/bbilbro/pi-chat
uv run python capture_rpc.py
# → outputs: rpc_capture.jsonl
```

This script:
1. Spawns `pi --mode rpc --no-session --approve`
2. Sends predefined prompts and captures all events
3. Waits for `agent_settled` between prompts
4. Writes each event as JSONL with sequence numbers and timestamps

The output contains the full event stream: `agent_start`, `turn_start`, `message_start/end`, `message_update` (thinking/toolcall/text deltas), `tool_execution_*`, `turn_end`, `agent_end`, and `agent_settled`.

### capture_subagent_rpc.py — Sub-Agent RPC Capture

Captures a sub-agent spawn with nested tool calls:

```bash
cd /home/bbilbro/pi-chat
uv run python capture_subagent_rpc.py
# → outputs: subagent_rpc_capture.jsonl
```

### Creating Custom Captures

To create a custom capture with your own prompts, edit the `prompts` list in `capture_rpc.py`:

```python
prompts = [
    "Your first prompt here.",
    "Your second prompt here.",
]
```

Then run the script. Each prompt triggers a full agent turn with reasoning and tool use.

## Helper Functions

### Get Snapshot (Accessibility Tree)

```bash
curl -s "http://localhost:9377/tabs/$TAB/snapshot?userId=agent"
```

Returns element refs (`e1`, `e2`, ...) for clicking/interaction.

### Click an Element

```bash
curl -s -X POST "http://localhost:9377/tabs/$TAB/click" \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","ref":"e1"}'
```

### Execute JS in Page

```bash
# Write JS to temp file (avoids nested JSON quoting issues)
cat > /tmp/script.js << 'EOF'
// Your JS here
"result"
EOF

python3 -c "import json; print(json.dumps({'userId':'agent','expression':open('/tmp/script.js').read()}))" | \
  curl -s -X POST "http://localhost:9377/tabs/$TAB/evaluate" \
  -H 'Content-Type: application/json' -d @-
```

## Typical Testing Loop

```bash
# 1. Make code changes
# 2. Restart server (kill + start)
pkill -f "uv run python server.py" 2>/dev/null
sleep 1
cd /home/bbilbro/pi-chat && PI_CHAT_DEV=1 uv run python server.py &
sleep 3

# 3. Create tab + bypass login
TAB=$(curl -s -X POST http://localhost:9377/tabs \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","sessionKey":"pichat","url":"http://localhost:9000"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['tabId'])")

python3 -c "import json; print(json.dumps({'userId':'agent','expression':open('/home/bbilbro/pi-chat/static/bypass_login.js').read()}))" | \
  curl -s -X POST "http://localhost:9377/tabs/$TAB/evaluate" \
  -H 'Content-Type: application/json' -d @-

# 4. Navigate to session
curl -s -X POST "http://localhost:9377/tabs/$TAB/navigate" \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","url":"http://localhost:9000/?session=/home/bbilbro/pi-chat/subagent_rpc_capture.jsonl"}'

sleep 5

# 5. Bypass login again (page reload)
python3 -c "import json; print(json.dumps({'userId':'agent','expression':open('/home/bbilbro/pi-chat/static/bypass_login.js').read()}))" | \
  curl -s -X POST "http://localhost:9377/tabs/$TAB/evaluate" \
  -H 'Content-Type: application/json' -d @-

sleep 3

# 6. Screenshot
curl -s "http://localhost:9377/tabs/$TAB/screenshot?userId=agent" > /tmp/result.png
```

## Server Architecture

### Dev Mode (`PI_CHAT_DEV=1`)

When enabled, the WebSocket endpoint accepts a `?session=PATH` query parameter. On connect:

1. Server parses the JSONL file (supports both pi session and RPC capture formats)
2. Extracts messages from `agent_end` event or collects all user/assistant messages
3. Sends a single `session_loaded` WebSocket event with the messages
4. Frontend renders them via `renderHistoricalMessages()`

No pi subprocess is spawned — it's a static render of recorded data.

### JSONL Formats Supported

**RPC Capture** (`subagent_rpc_capture.jsonl`, `rpc_capture.jsonl`):
```json
{"seq": 1, "ts": "...", "event": {"type": "agent_end", "messages": [...]}}
```
Server looks for `agent_end` event and extracts its `messages` array.

**Pi Session** (`~/.pi/agent/sessions/--home-bbilbro-*/`):
```json
{"type": "message", "message": {"role": "user", "content": [...]}}
{"type": "message", "message": {"role": "assistant", "content": [...]}}
```
Server collects all user/assistant messages in order.
