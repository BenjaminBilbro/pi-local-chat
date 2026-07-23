# Testing Guide for LLM Agents

This guide uses `camoufox-browser` to exercise and screenshot the pi chat UI.
It intentionally does not use Playwright, Chromium, or a frontend build tool.

The preferred rendering test loads a saved JSONL session with
`PI_CHAT_DEV=1`. This exercises authentication, the WebSocket, server-side
session parsing, and historical rendering without starting a live pi
subprocess.

## Paths and URLs

The examples assume the Linux paths used by the local agent environment.
Change these variables if either repository lives elsewhere:

```bash
export PI_CHAT_ROOT=/home/bbilbro/pi-chat
export CAMOFOX_ROOT=/home/bbilbro/camoufox-testing/camofox-browser
export PI_CHAT_URL=http://127.0.0.1:9000
export CAMOFOX_URL=http://127.0.0.1:9377
```

## Start pi chat in test mode

Create a temporary scrypt hash for the known test password `test-only`, then
pass it only to this server process:

```bash
cd "$PI_CHAT_ROOT"

PI_CHAT_TEST_HASH="$(
  uv run python -c \
    "from pi_chat.auth import hash_password; print(hash_password('test-only'))"
)"

PI_CHAT_DEV=1 \
PI_CHAT_B_PASSWORD_HASH="$PI_CHAT_TEST_HASH" \
uv run python server.py > /tmp/pi-chat.log 2>&1 &

PI_CHAT_SERVER_PID=$!
```

This is real server-side authentication, not a browser bypass. The password
hash is generated at startup, is not written to the repository, and disappears
with the shell variable.

Verify the server:

```bash
curl -fsS "$PI_CHAT_URL/" | head -3
```

If startup fails:

```bash
tail -50 /tmp/pi-chat.log
```

## Start camoufox-browser

```bash
source ~/.bashrc
cd "$CAMOFOX_ROOT"
node server.js > /tmp/camofox-browser.log 2>&1 &
CAMOFOX_SERVER_PID=$!
```

Allow roughly 10–12 seconds for the browser to pre-warm, then check it:

```bash
sleep 12
curl -fsS "$CAMOFOX_URL/health"
```

Expected fields include:

```json
{
  "ok": true,
  "engine": "camoufox",
  "browserConnected": true,
  "browserRunning": true
}
```

If the Camofox binary is missing:

```bash
cd /home/bbilbro/camoufox-testing
uv run python -m camoufox fetch
```

## Core screenshot workflow

### 1. Create a tab

```bash
PI_CHAT_TAB_ID="$(
  curl -fsS -X POST "$CAMOFOX_URL/tabs" \
    -H 'Content-Type: application/json' \
    -d "{\"userId\":\"agent\",\"sessionKey\":\"pichat\",\"url\":\"$PI_CHAT_URL\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['tabId'])"
)"
```

### 2. Log in through the real UI

The evaluation only fills and submits the existing form. FastAPI verifies the
password and Camofox stores the returned HTTP-only cookie.

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

print(json.dumps({
    "userId": "agent",
    "expression": expression,
}))
PY
)"

curl -fsS -X POST \
  "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/evaluate" \
  -H 'Content-Type: application/json' \
  -d "$PI_CHAT_LOGIN_PAYLOAD"

sleep 2
```

Unlike the old bypass script, this checks the same login path used by a normal
browser. The cookie remains attached to this Camofox tab across navigation.

### 3. Load a recorded session

Use an absolute path visible to the pi chat server:

```bash
export PI_CHAT_SESSION_PATH="$PI_CHAT_ROOT/data-samples/subagent_rpc_capture.jsonl"
export PI_CHAT_SESSION_URL="$PI_CHAT_URL/?session=$PI_CHAT_SESSION_PATH"
test -f "$PI_CHAT_SESSION_PATH"

PI_CHAT_NAVIGATION_PAYLOAD="$(
python3 - <<'PY'
import json
import os

print(json.dumps({
    "userId": "agent",
    "url": os.environ["PI_CHAT_SESSION_URL"],
}))
PY
)"

curl -fsS -X POST \
  "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/navigate" \
  -H 'Content-Type: application/json' \
  -d "$PI_CHAT_NAVIGATION_PAYLOAD"

sleep 4
```

There is no second login step. The existing cookie lets `auth.js` restore the
profile, connect `/ws?session=...`, and receive `session_loaded`.
If the `test -f` check fails, choose a native pi session or generate a fresh
capture using the commands later in this guide.

### 4. Inspect before taking a screenshot

The accessibility snapshot is useful for confirming that expected content
rendered:

```bash
curl -fsS \
  "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/snapshot?userId=agent"
```

Look for the first user prompt, assistant text, and any expected tool or
sub-agent labels.

### 5. Capture both themes

```bash
curl -fsS \
  "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/screenshot?userId=agent" \
  > /tmp/pi-chat-theme-a.png
```

Switch themes through the real toggle:

```bash
PI_CHAT_THEME_PAYLOAD="$(
python3 - <<'PY'
import json

print(json.dumps({
    "userId": "agent",
    "expression": """
document.querySelector('#chat-screen [data-theme-toggle]').click();
'theme switched';
""",
}))
PY
)"

curl -fsS -X POST \
  "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/evaluate" \
  -H 'Content-Type: application/json' \
  -d "$PI_CHAT_THEME_PAYLOAD"

sleep 1

curl -fsS \
  "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/screenshot?userId=agent" \
  > /tmp/pi-chat-theme-b.png
```

Theme selection is stored in browser local storage, so “theme A” is whichever
mode that Camofox session last used; “theme B” is the opposite mode.

### 6. Close the tab

```bash
curl -fsS -X DELETE \
  "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID?userId=agent"
```

### Other Camofox interactions

Use a reference from the accessibility snapshot to click a control:

```bash
curl -fsS -X POST \
  "$CAMOFOX_URL/tabs/$PI_CHAT_TAB_ID/click" \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","ref":"e1"}'
```

For behavior that is not exposed by a dedicated endpoint, use the same
`/evaluate` payload pattern shown in the login and theme examples. Prefer
clicking real controls over directly mutating application state.

When the full test run is finished, stop only the processes started by the
current shell:

```bash
kill "$PI_CHAT_SERVER_PID"
kill "$CAMOFOX_SERVER_PID"
```

## Useful session files

The JSONL files are ignored by Git, so their availability depends on the local
machine:

| Path | Purpose |
|------|---------|
| `$PI_CHAT_ROOT/data-samples/subagent_rpc_capture.jsonl` | Historical sub-agent card and nested tool calls |
| `$PI_CHAT_ROOT/data-samples/rpc_capture.jsonl` | General thinking, tool, and Markdown rendering |
| `$PI_CHAT_ROOT/data-samples/*.jsonl` | Other locally saved fixtures |
| `~/.pi/agent/sessions/**/*.jsonl` | Native sessions recorded by pi |

Find recent native sessions with:

```bash
find ~/.pi/agent/sessions -name '*.jsonl' -type f | tail -20
```

The development viewer can load both the wrapped RPC capture format and native
pi session JSONL. The query parameter is an absolute server-side path, not a
path inside Camofox.

## Generate fresh RPC data

The capture scripts call the locally installed `pi` executable and overwrite
their output file in the current working directory.

### General RPC capture

```bash
cd "$PI_CHAT_ROOT"
uv run python capture_rpc.py
```

Output:

```text
rpc_capture.jsonl
```

The default prompts exercise thinking, tool calls, multiple turns, and final
assistant text.

### Sub-agent capture

```bash
cd "$PI_CHAT_ROOT"
uv run python capture_subagent_rpc.py
```

Output:

```text
subagent_rpc_capture.jsonl
```

The default prompt spawns a sub-agent and asks it to run three commands. This
exercises live sub-agent updates and historical nested-tool extraction.

To keep generated files together:

```bash
mv -i rpc_capture.jsonl data-samples/
mv -i subagent_rpc_capture.jsonl data-samples/
```

To create a different scenario, edit the `prompts` list in `capture_rpc.py` or
the `prompt` value in `capture_subagent_rpc.py`, then regenerate the file.

## Fast non-visual checks

Run these before opening Camofox:

```bash
cd "$PI_CHAT_ROOT"

python3 -m compileall -q \
  pi_chat \
  server.py \
  capture_rpc.py \
  capture_subagent_rpc.py

for file in static/*.js; do
  if [ "$(basename "$file")" != "marked.min.js" ]; then
    node --check "$file"
  fi
done

git diff --check
```

Check the files served by FastAPI:

```bash
curl -fsS "$PI_CHAT_URL/" > /dev/null
curl -fsS "$PI_CHAT_URL/static/app.js" > /dev/null
curl -fsS "$PI_CHAT_URL/static/chat.js" > /dev/null
curl -fsS "$PI_CHAT_URL/static/history.js" > /dev/null
curl -fsS "$PI_CHAT_URL/static/sessions.js" > /dev/null
curl -fsS "$PI_CHAT_URL/static/socket.js" > /dev/null
```

## What the historical-render test covers

With `PI_CHAT_DEV=1` and `?session=/absolute/file.jsonl`:

1. Camofox performs the normal login flow.
2. `auth.js` restores the HTTP-only cookie after navigation.
3. `socket.js` opens the authenticated WebSocket.
4. `websocket.py` reads the development session path.
5. `sessions.py` parses native pi records or wrapped RPC events.
6. The server sends one `session_loaded` message.
7. `app.js` routes it through `sessions.js`.
8. `history.js` renders user, assistant, tool, Markdown, and sub-agent content.

No pi subprocess is spawned for this path. Use a normal live prompt separately
when testing `chat.js` streaming behavior or changes to `PiProcess`.

## Troubleshooting

### The login screen remains visible

- Confirm the server was started with `PI_CHAT_B_PASSWORD_HASH`.
- Confirm the test password is exactly `test-only`.
- Inspect `/tmp/pi-chat.log`.
- Use the Camofox snapshot endpoint to confirm that the expected login controls
  exist before evaluating the login expression.

### The page is logged in but no session appears

- Confirm `PI_CHAT_DEV=1` was set on the server process.
- Confirm `PI_CHAT_SESSION_PATH` is absolute and readable by the server.
- Reload the same URL after confirming the cookie was created.
- Inspect `/tmp/pi-chat.log` for `Failed to parse session file`.

### Camofox is unavailable

- Check `"$CAMOFOX_URL/health"`.
- Inspect `/tmp/camofox-browser.log`.
- Allow the initial browser pre-warm to finish before creating a tab.

### A screenshot is blank or premature

- Request a snapshot first.
- Wait for known text from the fixture rather than relying only on a fixed
  delay.
- Keep the same `userId`, `sessionKey`, and tab ID throughout the run.
