# Development workflow

How to run, test, screenshot and debug this application on a machine that is not the one it was written on.
Extracted from `AGENTS.md` on 2026-11-17 so that the card stays a card; the split is section-for-section, not
a rewrite.

Two things in here are load-bearing enough to say at the top:

**Never kill a `python.exe` by image name on the box this project is developed on.** The local inference
stack's launcher shares that image name, and a `server.py` command-line filter is not a safer filter,
because that is also the inference server's filename. Kill only PIDs you started and recorded, after
checking their command line.

**Bind test servers to `127.0.0.1`.** `server.py` defaults to `0.0.0.0` so a phone can reach the real app
over Tailscale, which also means a test server left running is reachable from the tailnet.

## Development Setup

### Start pi-chat in Test/Dev Mode

Create a temporary scrypt hash for a known test password, then pass it only to this server process:

```bash
cd ~/pi-chat

TEST_HASH="$(
  uv run python -c "from pi_chat.auth import hash_password; print(hash_password('test-only'))"
)"

PI_CHAT_DEV=1 \
PI_CHAT_B_PASSWORD_HASH="$TEST_HASH" \
uv run python server.py > /tmp/pi-chat.log 2>&1 &

SERVER_PID=$!
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

> **Status, 2026-11-17:** this recipe is written for the WSL layout (`~/...`, `uv run`) and has not
> been run from this Windows checkout; camofox is not installed here. The paths are therefore unverified, and
> the harness this box actually uses is the CDP one in **Screenshots Without Camofox** below. The variable
> names in this section are plain shell variables, not `PI_CHAT_*` settings: the prefix belongs to the
> application, and startup now warns about a `PI_CHAT_*` name nothing reads.

This workflow uses `camoufox-browser` to exercise and screenshot the pi-chat UI. It intentionally does not use Playwright or Chromium.

### Paths and URLs

```bash
export REPO_ROOT=~/pi-chat
export CAMOFOX_ROOT=~/camofox-testing/camofox-browser
export APP_URL=http://127.0.0.1:9000
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
cd ~/camofox-testing
uv run python -m camoufox fetch
```

### Core Screenshot Workflow

**1. Create a tab:**

```bash
TAB_ID="$(
  curl -fsS -X POST "$CAMOFOX_URL/tabs" \
    -H 'Content-Type: application/json' \
    -d "{\"userId\":\"agent\",\"sessionKey\":\"pichat\",\"url\":\"$APP_URL\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['tabId'])"
)"
```

**2. Log in through the real UI:**

```bash
LOGIN_PAYLOAD="$(
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

curl -fsS -X POST "$CAMOFOX_URL/tabs/$TAB_ID/evaluate" \
  -H 'Content-Type: application/json' \
  -d "$LOGIN_PAYLOAD"
sleep 2
```

**3. Load a recorded session:**

```bash
export SESSION_PATH="$REPO_ROOT/data-samples/subagent_rpc_capture.jsonl"
export SESSION_URL="$APP_URL/?session=$SESSION_PATH"
test -f "$SESSION_PATH"

NAVIGATION_PAYLOAD="$(
python3 - <<'PY'
import json, os
print(json.dumps({"userId": "agent", "url": os.environ["SESSION_URL"]}))
PY
)"

curl -fsS -X POST "$CAMOFOX_URL/tabs/$TAB_ID/navigate" \
  -H 'Content-Type: application/json' \
  -d "$NAVIGATION_PAYLOAD"
sleep 4
```

**4. Inspect before screenshot:**

```bash
curl -fsS "$CAMOFOX_URL/tabs/$TAB_ID/snapshot?userId=agent"
```

Look for the first user prompt, assistant text, and expected tool/sub-agent labels.

**5. Capture both themes:**

```bash
curl -fsS "$CAMOFOX_URL/tabs/$TAB_ID/screenshot?userId=agent" > /tmp/pi-chat-theme-a.png

# Switch theme
curl -fsS -X POST "$CAMOFOX_URL/tabs/$TAB_ID/evaluate" \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","expression":"document.querySelector(\'#chat-screen [data-theme-toggle]\').click();"}'
sleep 1

curl -fsS "$CAMOFOX_URL/tabs/$TAB_ID/screenshot?userId=agent" > /tmp/pi-chat-theme-b.png
```

**6. Close the tab:**

```bash
curl -fsS -X DELETE "$CAMOFOX_URL/tabs/$TAB_ID?userId=agent"
```

**Cleanup:**

```bash
kill "$SERVER_PID" "$CAMOFOX_SERVER_PID"
```

### Other Camofox Interactions

Click a control by its accessibility snapshot reference:

```bash
curl -fsS -X POST "$CAMOFOX_URL/tabs/$TAB_ID/click" \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent","ref":"e1"}'
```

For behavior not exposed by a dedicated endpoint, use the same `/evaluate` payload pattern. Prefer clicking real controls over directly mutating application state.


## Screenshots Without Camofox

Headless Chrome driven over raw CDP (Node 24 `fetch`/`WebSocket`, no puppeteer, no WSL):

```bash
node tools/screenshot_ui.mjs --url http://127.0.0.1:9018 --stage chat \
  --theme dark --mascot pi --cdp-port 9344 --out "$TEMP/shots/chat-dark.png"
```

`--stage login|chat|voice` (voice opens the settings dialog), `--stage queue|queue-landed` (see
Mid-run Message Delivery), `--stage stream-render` (see below), `--theme light|dark`,
`--mascot pie|pi`, `--viewport 1440x900`, `--password test-only`, `--pane open|closed` (drives the preview column's toggle, so both resting states are pinnable). Prefs are seeded through
localStorage before the second navigation, so theme and mascot apply on load. Always pass
`PI_CHAT_PI_BIN=tools/fake_rpc_pi.py` to the server: the chat stage spawns a real pi child, and a
real child on the shared inference engine invalidates its KV cache. The tool now warns when the
settled reply does not look like stub output, which is that mistake caught after the fact (it
happened once on 2026-11-09, see `docs/audit/README.md`). Assistant text lands in
`.message.assistant .timeline-item`, not a `.message-bubble`. Kill only the headless Chrome whose
command line carried `--remote-debugging-port=93`; never kill by image name on this box, and do not
filter processes by a `server.py` command-line pattern either, because Strata runs
`Strata/serve/server.py` and that is not yours to stop.

`--stage stream-render` is the WP-06 regression guard: it wraps the `innerHTML` setter and an
animation-frame counter, streams a long stub answer, and asserts there are never more renders than
frames. It needs a server started with `FAKE_RPC_REPLY_CHARS=4200` and `FAKE_RPC_TURN_MS` unset, since
the discriminating shape is a stream that outpaces the display; paced slowly, a per-delta renderer and a
per-frame one are indistinguishable. Measured 2026-11-17: 350 deltas produced 7 render passes and 12 KB
handed to the sanitizer on this branch, against 356 passes and 502 KB with the batching removed.

## Fast Non-Visual Checks

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

## Fresh Clone Check

```bash
sh tools/verify_fresh_clone.sh          # ~4 minutes warm, longer cold
KEEP=1 sh tools/verify_fresh_clone.sh /d/tmp/clone   # keep the clone for inspection
```

CI cannot answer this question, because CI installs into a checkout of a repo that already contains every
directory, on paths proven once, from a workflow file that was itself edited until it stopped failing. What
that hides is a dependency that resolves only thanks to an untracked directory on one machine, and this repo
had exactly one: `experiments/RealtimeSTT` was gitignored and load-bearing, so `uv sync` on a fresh clone
failed while naming a missing distribution instead of a missing clone step.

The script clones HEAD into a throwaway directory, installs with no extras and nothing vendored, runs the
suite, `npm ci` and `npm test`, the three check tools and the WS smoke test, then requires that `server.py`
with no `.env` and no exported hashes **exits non-zero and says how to configure a hash**. A green run on a
refused start is the point: an assertion that merely expects failure would pass on any crash at startup, so it
matches on the message.

Two things it deliberately does not do. It clones HEAD rather than your working tree, so uncommitted edits are
not what got verified, and it says so when it notices them. And it needs network, for `npm ci` and for the
pinned git dependencies, which is the honest cost of the check rather than a flaw in it.

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

**Then scrub, before anything is staged.** `tools/capture.py` runs a real `pi`, so the capture carries the
agent memory the memory extension injected, and it carries this machine's paths. `--no-context-files` turns off
AGENTS.md discovery, not the injection.

```bash
uv run python tools/scrub_captures.py --write data-samples/new_capture.jsonl
uv run python tools/scrub_captures.py --check          # CI runs this over every tracked fixture
```

It removes the injected memory blocks and replaces personal literals, always at the same byte length: the
captures record `offset` and `bytes` for a stream measured on a machine, and a shortening edit turns a byte-exact
fixture into a lying one while still looking clean.

Which strings count as personal is not in the repository, because a committed list of them would be the thing
the tool exists to remove. `Path.home().name` is picked up automatically; anything else this capture carries
goes in `.scrub-literals` at the repo root, gitignored, one `literal = replacement` pair per line, or in
`$PI_CHAT_SCRUB_LITERALS` on a machine with no filesystem to keep it in:

```
# .scrub-literals (never committed)
olduser = example
```

CI has the other half of the rule, the half that needs no names: a fixture holding a home directory that is
not a placeholder fails the check wherever it is pushed from. That rule is what caught a second username, from
the WSL era of this project, that no amount of local configuration would have known about.

Two consequences of that split, both learned the hard way on 2026-11-17 when the first published tree went red
on GitHub while green at home:

- **A check that consults the local list is not a gate.** The corpus passed here and failed there over two
  fragments of a half-typed path: a live stream echoes `ls -la /home/<name>` as `/home/b`, `/home/bb`, then
  whole, and on the machine that knows the name those prefixes are excused. So the scrubber now rewrites the
  fragments too, and `--check` reports what CI would say rather than what this machine can excuse.
- **A replacement has to read as a placeholder.** `charlie = rogerxx` deletes a name and publishes one, and the
  name-free rules cannot tell a replacement from a leak. Use something from the neutral list (`example`, `user`,
  `test`), and `tests/test_scrub_captures.py` will tell you if you do not.

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


