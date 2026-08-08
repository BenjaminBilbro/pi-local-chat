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

Optional:

- Node.js 20.19+, 22.13+, or 24+ for the renderer-parity tests
- CUDA-capable GPU with ~6GB VRAM for production TTS (voice mode)

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
uv run python server.py
```

Point the tunnel at `http://127.0.0.1:9000` and use Cloudflare Access to
control who can reach the login page.

## Voice Mode (TTS)

Opt-in text-to-speech output for assistant responses using OmniVoice.
Voice mode begins synthesis while pi is still streaming, so speech starts
before the full response completes.

### Install voice dependencies

Voice is optional. Install only when needed:

```bash
uv sync --extra voice
```

### Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `PI_CHAT_TTS_MODEL` | `k2-fsa/OmniVoice` | HF model ID or local snapshot |
| `PI_CHAT_TTS_DEVICE` | `cuda:0` | Torch device |
| `PI_CHAT_TTS_DTYPE` | `float16` | Precision (`float16` or `float32`) |
| `PI_CHAT_TTS_NUM_STEPS` | `16` | Diffusion steps (fewer = faster) |
| `PI_CHAT_TTS_FLASHINFER` | `0` | Enable FlashInfer acceleration |
| `PI_CHAT_TTS_CUDA_GRAPH` | `0` | Enable CUDA graph |
| `PI_CHAT_TTS_CPU_THREADS` | `4` | Torch CPU threads for CPU mode |

### Custom Voices

Upload voice samples to create custom voices for TTS. Samples are normalized to
24 kHz mono WAV and stored in `~/.pi/voices/`.

| Endpoint | Purpose |
|---|---|
| `POST /api/voice/upload` | Upload a voice sample file |
| `GET /api/voice/list` | List available custom voices |
| `PUT /api/voice/{voice_id}` | Rename a custom voice |
| `DELETE /api/voice/{voice_id}` | Delete a custom voice |

### Development with fake PCM

Test the full voice stack without GPU or real model:

```bash
uv run python tools/run_fake_voice_server.py
```

### Validation

Real CPU validation (CUDA hidden, slow):

```bash
CUDA_VISIBLE_DEVICES="" uv run --extra voice python tools/run_voice_cpu_validation.py
```

Real GPU validation (run after unloading implementing LLM):

```bash
uv run --extra voice python tools/run_voice_gpu_validation.py
```

Benchmark:

```bash
uv run --extra voice python tools/benchmark_voice.py --iterations 10
```

## Development

### Parity tests

```bash
npm test
```

### Python tests

```bash
uv run pytest -q -m "not voice_cpu and not voice_gpu"
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
contract, voice protocol), see `AGENTS.md`.

## Architecture

### High-Level Overview

```mermaid
graph TB
    subgraph Browser["Browser"]
        UI["Chat UI"]
        WS["WebSocket Client"]
    end

    subgraph Server["pi-chat Server (FastAPI)"]
        API["HTTP Routes"]
        WSH["WebSocket Handler"]
        AUTH["Auth Manager"]
        TTS["TTS Service"]
        STT["STT Service"]
    end

    subgraph Pi["pi Agent"]
        RPC["pi --mode rpc"]
        LLM["LLM Backend"]
        TOOLS["Tool Execution"]
    end

    subgraph Storage["Storage"]
        SESSIONS["Session JSONL Files"]
        VOICES["Voice Samples"]
    end

    UI --> WS
    WS -->|"JSON commands + binary audio"| WSH
    UI -->|"login, sessions, uploads"| API
    API --> AUTH
    API --> SESSIONS
    API --> VOICES
    WSH -->|"RPC commands"| RPC
    RPC -->|"streaming events"| WSH
    RPC --> LLM
    RPC --> TOOLS
    WSH -->|"text to synthesize"| TTS
    TTS -->|"PCM audio frames"| WSH
    WSH -->|"audio packets"| STT
    STT -->|"transcripts"| WSH
    WSH -->|"pi_event messages"| WS
    WSH -->|"write sessions"| SESSIONS
```

### Module-Level Data Flow

```mermaid
graph TB
    subgraph Frontend["Browser (static/)"]
        APP["app.js<br/>message router"]
        AUTH_JS["auth.js<br/>login/profile"]
        SOCKET["socket.js<br/>WebSocket + heartbeat"]
        CHAT["chat.js<br/>composer + live rendering"]
        HISTORY["history.js<br/>saved session rendering"]
        TIMELINE["timeline.js<br/>DOM primitives"]
        SUBAGENT["subagent.js<br/>cards + enrichment"]
        SESSIONS_JS["sessions.js<br/>drawer + load"]
        VOICE_JS["voice.js<br/>TTS playback"]
        STT_JS["stt.js<br/>mic capture"]
    end

    subgraph Backend["Server (pi_chat/)"]
        APP_PY["app.py<br/>FastAPI + routes"]
        WS_PY["websocket.py<br/>command dispatch"]
        AUTH_PY["auth.py<br/>passwords + tokens"]
        PROC["process.py<br/>pi subprocess lifecycle"]
        SESSIONS_PY["sessions.py<br/>JSONL parsing"]
        TTS_PY["tts_service.py<br/>OmniVoice synthesis"]
        VOICE_PY["voice_session.py<br/>per-conn TTS state + AEC ref"]
        STT_PY["stt_session.py<br/>per-conn STT state + AEC"]
        STT_SVC["stt_service.py<br/>RealtimeSTT loader"]
    end

    subgraph Pi["pi RPC"]
        PI["pi --mode rpc"]
    end

    %% Frontend wiring
    AUTH_JS -->|"POST /api/login"| APP_PY
    SESSIONS_JS -->|"GET /api/sessions"| APP_PY
    SOCKET -->|"WS /ws"| APP_PY
    APP -->|"routes events"| CHAT
    APP -->|"routes session_loaded"| SESSIONS_JS
    CHAT -->|"prompt command"| SOCKET
    CHAT -->|"voice_enable/disable"| SOCKET
    CHAT -->|"stt_enable/disable"| SOCKET
    VOICE_JS -->|"play PCM frames"| CHAT
    STT_JS -->|"binary audio packets"| SOCKET
    CHAT -->|"render runs"| HISTORY
    HISTORY --> TIMELINE
    HISTORY --> SUBAGENT
    CHAT --> TIMELINE
    CHAT --> SUBAGENT

    %% Backend wiring
    APP_PY -->|"auth"| AUTH_PY
    APP_PY -->|"session listing"| SESSIONS_PY
    APP_PY -->|"WS upgrade"| WS_PY
    WS_PY -->|"spawn/control"| PROC
    PROC -->|"stdin RPC"| PI
    PI -->|"stdout events"| PROC
    WS_PY -->|"pi_event relay"| PROC
    WS_PY -->|"TTS orchestration"| VOICE_PY
    VOICE_PY -->|"synthesize"| TTS_PY
    WS_PY -->|"STT orchestration"| STT_PY
    STT_PY -->|"load RealtimeSTT"| STT_SVC
    STT_PY -->|"drain AEC reference"| VOICE_PY

    %% Cross-boundary
    SOCKET <-->|"WebSocket"| WS_PY
```

### Voice Mode (TTS + STT + AEC) Flow

```mermaid
sequenceDiagram
    participant Browser
    participant WS as websocket.py
    participant Voice as voice_session.py
    participant TTS as tts_service.py
    participant STT as stt_session.py
    participant AEC as pywebrtc-audio
    participant Recorder as RealtimeSTT

    Note over Browser,Recorder: TTS Playback
    Browser->>WS: voice_enable
    WS->>Voice: enable(settings)
    Voice->>TTS: prepare voice handle
    TTS-->>Voice: handle ready
    Voice->>Voice: is_speaking = false

    pi->>Voice: agent_start + text deltas
    Voice->>TTS: synthesize(chunk)
    TTS-->>Voice: PCM audio
    Voice->>Voice: is_speaking = true<br/>buffer PCM to _tts_reference_buffer
    Voice->>Browser: binary PCM frame
    Browser->>Browser: play audio (speaker)

    Note over Browser,Recorder: STT with Echo Cancellation
    Browser->>WS: binary mic audio packet
    WS->>STT: ingest_audio_packet()
    STT->>STT: voice.is_speaking?
    alt agent speaking
        STT->>Voice: drain_reference_bytes(n)
        Voice-->>STT: TTS reference PCM
        STT->>AEC: process(mic, reference)
        AEC-->>STT: cleaned audio
        STT->>Recorder: feed_audio(cleaned)
    else agent idle
        STT->>Recorder: feed_audio(raw mic)
    end
    Recorder->>STT: transcription ready
    STT->>Browser: stt_final(text)

    Note over Browser,Recorder: Stream End
    Voice->>Voice: is_speaking = false
    Voice->>Browser: voice_stream_end
```

## License

MIT
