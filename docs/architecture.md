# Architecture

What the pieces are, what they own, and how a message travels through them. Extracted from `AGENTS.md` on
2026-11-17 so that the card stays a card; the split is section-for-section, not a rewrite.

Start here if you are changing module boundaries or looking for where a behavior lives. If you are changing
the wire, read `docs/protocol.md` too. If you are touching the timeline renderer, `docs/rendering-parity.md`
is the contract that has to keep holding.

The import graph is deliberately acyclic: `app.py` is the only composition root, `config.py` is a leaf, and
the voice stack never reaches back up into `websocket.py`. That shape is the thing to preserve.

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
| `pi_chat/tts_service.py` | Lazy model loading, voice cache, serialized inference; engine chosen by `PI_CHAT_TTS_ENGINE` (`phonon` default, `omnivoice`), resolved by `config.resolve_tts_engine` |
| `pi_chat/tts_phonon.py` | Phonon engine (`ptts`, CPU-only q8, five named rate steps). See `docs/phonon-tts-engine.md` |
| `pi_chat/phonon_voices.py` | Phonon voice catalog: committed snapshot of gradium/phonon-voices, name matching, never loads a model |
| `pi_chat/tts_chunking.py` | Streaming Markdown-aware speech chunker |
| `pi_chat/voice_session.py` | Per-WebSocket voice state, queue, framing, cancellation, settings validation |
| `pi_chat/voice_store.py` | Persistent voice sample storage, validation, CRUD |

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
| `static/buddy.js` | Reactive pie mascot ("Buddy"): canvas animation of live pi activity, confetti, WebAudio chimes, header toggle |
| `static/utils.js` | Formatting and escaping helpers |

### Voice Mode Frontend

| File | Responsibility |
|------|----------------|
| `static/voice.js` | UI state, Web Audio scheduling, binary frame parsing |

`static/app.js` composes voice the same way it composes chat and sessions.

`static/app.js` is the only module that knows how the frontend components fit together. Transport code calls callbacks; it does not directly modify chat or session UI.


## Main Flows

### Login

1. `auth.js` posts selected account and password to `/api/login`.
2. `AuthManager` verifies the password and creates an opaque in-memory token.
3. FastAPI returns the token as an HTTP-only cookie.
4. `auth.js` reveals the chat UI and asks `app.js` to connect the WebSocket.
5. `/api/me` restores this state after a page reload.

### Live Prompt

1. `chat.js` turns composer contents into a browser `prompt` command, carrying `delivery`: `"queue"` for Enter and the send button, `"now"` for the send-now arrow and Ctrl/Cmd+Enter. The round button is only Send when nothing is running; while a turn runs it is Stop, so Enter is the way to queue (see `docs/composer.md`).
2. `socket.js` sends the JSON command over `/ws`.
3. `websocket.py` lazily spawns the connection's `PiProcess`, if necessary, and maps `delivery` to pi's `streamingBehavior` (`followUp` / `steer`).
4. `process.py` writes the pi RPC command to stdin.
5. The stdout reader wraps every pi event as: `{"type": "pi_event", "event": {"type": "..."}}`
6. `app.js` routes the event to `chat.js`.
7. `chat.js` updates the live thinking, tool, sub-agent, and text timeline.
8. Final tool and agent events reconcile the completed DOM through the shared renderers.

The composer is never locked by a running agent, because a mid-run message is no longer an error - see
[Mid-run Message Delivery](#mid-run-message-delivery-queue-and-steer).

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
│   ├── voice_session.py # Per-WebSocket voice state, queue, framing
│   └── voice_store.py  # Persistent voice sample storage, validation, CRUD
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
│   ├── buddy.js        # Reactive pie mascot (canvas, confetti, chimes)
│   ├── voice.js        # Voice UI state, Web Audio scheduling, binary parsing
│   ├── utils.js        # Browser-side formatting helpers
│   └── marked.min.js   # Vendored Markdown renderer
├── tests/
│   ├── conftest.py              # Shared pytest fixtures
│   ├── compare_render.py        # Live vs historical rendering parity tests
│   ├── mobile_viewport_test.py  # Mobile viewport screenshot tests
│   ├── fakes/                   # Deterministic test doubles
│   │   └── voice.py             # FakeOmniVoiceRuntime, FakeTTSService
│   ├── fixtures/                # Test fixture generators
│   │   └── generate_voice_fixtures.py # Test fixture WAV generator
│   ├── test_voice_fakes.py      # Fake runtime tests (no torch)
│   ├── test_queue_dispatch.py   # Mid-run delivery: queue, steer, retract, run mirror
│   ├── test_child_argv.py       # The argv a child is spawned with, hence its prompt shape
│   ├── test_tts_chunking.py     # Streaming chunker tests
│   ├── test_tts_service.py      # TTSService tests with fake runtime
│   ├── test_tts_custom_voice.py # Custom voice upload/selection tests
│   ├── test_voice_session.py    # VoiceSession tests
│   ├── test_voice_session_validate.py # Voice session validation tests
│   ├── test_voice_store.py      # VoiceStore persistence tests
│   ├── test_voice_api.py        # HTTP voice endpoint tests
│   ├── test_voice_e2e.py        # End-to-end voice integration tests
│   └── test_voice_fake_e2e.py   # No-GPU pi-event-to-frame integration
├── tools/
│   ├── capture.py                # Unified RPC event capture CLI
│   ├── render_message.js         # jsdom harness for production JS rendering
│   ├── inspect_chunks.py         # Debug TTS chunking behavior
│   ├── run_fake_voice_server.py  # Dev server with audible fake PCM
│   ├── run_voice_cpu_validation.py # Real checkpoint CPU validator
│   ├── run_voice_gpu_validation.py # Human-gated GPU validator
│   └── benchmark_voice.py        # Latency/RTF benchmark
├── experiments/                 # Cloned sources (ignored by git)
│   └── OmniVoice/               # OmniVoice source (not the model checkpoint)
├── data-samples/                # RPC capture fixtures and native sessions
├── voice-validation/            # Validation artifacts (ignored by git)
├── server.py                    # Backward-compatible launch entry point
├── pyproject.toml               # Python project configuration
└── package.json                 # Node devDependencies (jsdom)
```

<!-- graft:start -->
