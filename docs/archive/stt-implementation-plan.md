> **Status, archived 2026-11-17:** the STT design as planned in the OmniVoice era. Speech input still works, but read this for the reasoning: engine assumptions, model names and the VRAM arithmetic predate Phonon being the default.

# STT Implementation Plan for pi-chat

Speech-to-text integration using RealtimeSTT + faster_whisper, following the same patterns as the existing TTS (OmniVoice) implementation.

## Architecture Decision

**Decision: Embed STT into pi-chat FastAPI server** (Option A).

### Justification

| Factor | Embedded (Option A) | Separate Server (Option B) |
|--------|---------------------|----------------------------|
| Matches TTS pattern | ✅ Exact same pattern | ❌ Different architecture |
| Auth/session sharing | ✅ Single process | ❌ Cross-service auth needed |
| STT → pi prompt flow | ✅ Direct server-side | ❌ Browser round-trip required |
| Deployment complexity | ✅ One service | ❌ Two services to manage |
| WebSocket connections | ✅ One per browser | ❌ Two per browser |
| Testability | ✅ Shared test harness | ❌ Integration tests between services |

We follow the TTS precedent: `TTSService` singleton + per-connection `VoiceSession` becomes `STTService` singleton + per-connection `STTSession`.

### High-Level Architecture

```
Browser
  ├── WebSocket to /ws (existing pi-chat)
  │     ├── Sends audio packets (binary: metadata + PCM)
  │     ├── Sends stt_enable/stt_disable (text)
  │     └── Receives stt_state/stt_realtime/stt_final events
  └── Mic capture via Web Audio API → streams to /ws

pi-chat FastAPI Server
  ├── STTService (singleton, lazy-loaded)
  │     └── Validates RealtimeSTT availability
  ├── STTSession (per WebSocket connection)
  │     ├── Direct audio ingestion (WebSocket → decode → resample → feed_audio)
  │     ├── Text worker thread (recorder.text() → stt_final events)
  │     ├── Callback bridge (recorder threads → asyncio event loop)
  │     └── State: enabled, active, settings
  ├── websocket.py (extended)
  │     ├── Handles stt_* commands
  │     └── Routes binary audio packets to STTSession
  └── app.py (extended)
        └── Creates STTService, wires STTSession to connections
```

## File-by-File Changes

### New Files

| File | Purpose |
|------|---------|
| `pi_chat/stt_service.py` | Lazy-loaded RealtimeSTT recorder factory (mirrors `tts_service.py`) |
| `pi_chat/stt_session.py` | Per-WebSocket STT state, direct audio ingestion, text worker thread (mirrors `voice_session.py`) |
| `static/stt.js` | Frontend STT controller: mic capture, UI state, event handling (mirrors `voice.js`) |
| `tests/fakes/stt.py` | FakeSTTService/FakeSTTSession for tests without RealtimeSTT deps |
| `tests/test_stt_service.py` | STTService lifecycle tests |
| `tests/test_stt_session.py` | STTSession audio feed and callback tests |
| `tests/test_stt_api.py` | WebSocket STT command tests |
| `tests/test_stt_e2e.py` | End-to-end STT integration tests |

### Modified Files

| File | Changes |
|------|---------|
| `pi_chat/config.py` | Add STT config vars (model, device, silence duration, etc.) |
| `pi_chat/app.py` | Create STTService at startup, attach STTSession to each WebSocket |
| `pi_chat/websocket.py` | Handle `stt_enable`, `stt_disable` commands; route binary audio packets to STTSession |
| `static/app.js` | Create STT controller, route `stt_*` server messages |
| `static/chat.js` | Add mic toggle button, disable textarea when listening, handle final transcript |
| `static/styles.css` | STT button styles, listening indicator, waveform visualization |
| `static/socket.js` | Handle binary audio frame sends (reuse existing binary infra from voice) |

### Reference-Only Files (no changes)

| File | Purpose |
|------|---------|
| `experiments/RealtimeSTT/` | Library reference, installed via pip |

## Backend Implementation Details

### pi_chat/config.py

```python
# Add to existing config
STT_ENABLED = env_bool("PI_CHAT_STT_ENABLED", True)
STT_MODEL = env_str("PI_CHAT_STT_MODEL", "small.en")
STT_REALTIME_MODEL = env_str("PI_CHAT_STT_REALTIME_MODEL", "tiny.en")
STT_DEVICE = env_str("PI_CHAT_STT_DEVICE", "cpu")
STT_COMPUTE_TYPE = env_str("PI_CHAT_STT_COMPUTE_TYPE", "default")
STT_LANGUAGE = env_str("PI_CHAT_STT_LANGUAGE", "en")
STT_SILENCE_DURATION = env_float("PI_CHAT_STT_POST_SPEECH_SILENCE", 0.6)
STT_MIN_RECORDING_LENGTH = env_float("PI_CHAT_STT_MIN_RECORDING_LENGTH", 0.3)
STT_MIN_GAP_BETWEEN_RECORDINGS = env_float("PI_CHAT_STT_MIN_GAP_BETWEEN_RECORDINGS", 0.3)
STT_REALTIME_PROCESSING_PAUSE = env_float("PI_CHAT_STT_REALTIME_PAUSE", 0.2)
STT_MAX_AUDIO_QUEUE_SIZE = env_int("PI_CHAT_STT_MAX_AUDIO_QUEUE_SIZE", 128)
```

### pi_chat/stt_service.py

Mirrors `TTSService` pattern:

```python
class STTService:
    """Server-wide STT service with lazy recorder loading."""

    def __init__(self, config: STTConfig):
        self.config = config
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._load_future = None

    async def load(self) -> None:
        """Idempotent lazy load. Deduplicates concurrent calls."""
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            if self._load_future is None:
                self._load_future = asyncio.shield(self._do_load())
            await self._load_future

    async def _do_load(self) -> None:
        """Import RealtimeSTT, validate, mark loaded."""
        # Lazy import to avoid hard dependency
        from RealtimeSTT import AudioToTextRecorder
        # Smoke test: create minimal recorder config
        self._loaded = True

    async def close(self) -> None:
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded
```

### pi_chat/stt_session.py

Mirrors `VoiceSession` pattern and RealtimeSTT's `RecorderBackedRealtimeSession`. 
**Key design: direct audio feed from WebSocket handler (no background queue thread).**

```python
import asyncio
import json
import logging
import math
import struct
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from RealtimeSTT import AudioToTextRecorder

logger = logging.getLogger("pi-chat.stt")
SERVER_SAMPLE_RATE = 16000
MAX_METADATA_BYTES = 64 * 1024


class AudioPacketError(ValueError):
    """Raised when audio packet is malformed."""
    pass


@dataclass(frozen=True)
class AudioPacket:
    """Decoded audio packet with metadata and PCM payload."""
    metadata: Dict[str, Any]
    audio: bytes


def decode_audio_packet(message: bytes) -> AudioPacket:
    """Decode a binary audio packet from the browser.

    Format: [4 bytes: metadata length (uint32 LE)][metadata JSON][PCM audio bytes]
    """
    if not isinstance(message, (bytes, bytearray, memoryview)):
        raise AudioPacketError("audio packet must be binary")

    data = bytes(message)
    if len(data) < 4:
        raise AudioPacketError("audio packet is missing metadata length")

    metadata_length = struct.unpack("<I", data[:4])[0]
    if metadata_length > MAX_METADATA_BYTES:
        raise AudioPacketError("audio packet metadata is too large")
    if len(data) < 4 + metadata_length:
        raise AudioPacketError("audio packet metadata is incomplete")

    metadata_bytes = data[4:4 + metadata_length]
    audio = data[4 + metadata_length:]
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AudioPacketError("audio packet metadata is invalid JSON") from exc
    if not isinstance(metadata, dict):
        raise AudioPacketError("audio packet metadata must be a JSON object")

    return AudioPacket(metadata=metadata, audio=audio)


def require_positive_int(metadata: dict, key: str) -> int:
    """Extract and validate a positive integer from packet metadata."""
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AudioPacketError(
            f"audio packet metadata field '{key}' must be a positive integer"
        )
    return value


def resample_int16(samples, source_rate: int, target_rate: int):
    """Resample int16 PCM audio from source_rate to target_rate.

    Uses scipy.signal.resample_poly when available, falls back to linear interp.
    """
    import numpy as np
    samples = np.asarray(samples, dtype=np.int16)
    if source_rate == target_rate or samples.size == 0:
        return samples.copy()

    try:
        from scipy.signal import resample_poly
        divisor = math.gcd(int(source_rate), int(target_rate))
        up = int(target_rate // divisor)
        down = int(source_rate // divisor)
        resampled = resample_poly(samples.astype(np.float32), up, down)
    except Exception:
        # Fallback: linear interpolation
        duration = samples.size / float(source_rate)
        target_size = max(1, int(round(duration * target_rate)))
        source_positions = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
        target_positions = np.linspace(0.0, 1.0, num=target_size, endpoint=False)
        resampled = np.interp(target_positions, source_positions, samples.astype(np.float32))

    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def packet_to_server_samples(packet: AudioPacket):
    """Decode packet audio and resample to SERVER_SAMPLE_RATE (16000Hz).

    Returns numpy int16 array at 16000Hz.
    """
    import numpy as np

    sample_rate = require_positive_int(packet.metadata, "sampleRate")
    channels = packet.metadata.get("channels", 1)
    if isinstance(channels, bool) or not isinstance(channels, int) or channels <= 0:
        raise AudioPacketError("channels must be a positive integer")
    if channels > 8:
        raise AudioPacketError("channels must be at most 8")

    audio_format = packet.metadata.get("format", "pcm_s16le")
    if audio_format != "pcm_s16le":
        raise AudioPacketError("only pcm_s16le audio packets are supported")

    frame_width = channels * 2
    if len(packet.audio) % frame_width:
        raise AudioPacketError("pcm_s16le audio packet is not aligned to whole frames")

    samples = np.frombuffer(packet.audio, dtype=np.int16)

    # Downmix stereo to mono if needed
    if channels > 1:
        usable = len(samples) - (len(samples) % channels)
        if usable <= 0:
            return np.array([], dtype=np.int16)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1).astype(np.int16)

    # Resample to server sample rate
    return resample_int16(samples, sample_rate, SERVER_SAMPLE_RATE)


class STTSession:
    """Per-WebSocket STT session with direct audio ingestion and callback bridge.

    Audio packets arrive from the WebSocket handler and are fed directly to the
    recorder (no background queue thread). A text worker thread calls recorder.text()
    to retrieve final transcriptions.
    """

    def __init__(
        self,
        stt_service: STTService,
        send_json: Callable,
        config: STTConfig,
    ):
        self.stt_service = stt_service
        self._send_json = send_json
        self.config = config
        self.enabled = False
        self.active = False  # Currently recording speech

        # Recorder instance (created on enable)
        self._recorder = None

        # Text worker thread (retrieves final transcriptions)
        self._text_thread = None
        self._text_thread_active = threading.Event()

        # Main event loop reference for thread-safe sends
        self._loop = asyncio.get_running_loop()

        # Activation ID for race safety (same pattern as VoiceSession)
        self._activation_id = 0

    async def enable(self, settings: Optional[STTSettings] = None) -> None:
        """Enable STT: create recorder, start text worker thread."""
        self._activation_id += 1
        activation_id = self._activation_id

        await self.stt_service.load()

        logger.info("STT: enabling session, activation_id=%d", activation_id)

        # Create recorder with external audio mode
        self._recorder = AudioToTextRecorder(
            spinner=False,
            use_microphone=False,
            model=self.config.model,
            realtime_model_type=self.config.realtime_model,
            device=self.config.device,
            compute_type=self.config.compute_type,
            language=self.config.language,
            post_speech_silence_duration=self.config.silence_duration,
            min_length_of_recording=self.config.min_recording_length,
            min_gap_between_recordings=self.config.min_gap_between_recordings,
            enable_realtime_transcription=True,
            realtime_processing_pause=self.config.realtime_processing_pause,
            on_recording_start=self._on_recording_start,
            on_recording_stop=self._on_recording_stop,
            on_realtime_transcription_update=self._on_realtime,
        )

        # Start text worker thread (retrieves final transcriptions)
        self._text_thread_active.set()
        self._text_thread = threading.Thread(
            target=self._text_worker_loop, daemon=True
        )
        self._text_thread.start()

        self.enabled = True
        logger.info("STT: session enabled")
        await self._send_json({"type": "stt_state", "state": "ready"})

    async def disable(self) -> None:
        """Disable STT: stop text worker, shutdown recorder."""
        logger.info("STT: disabling session")

        # Stop text worker
        self._text_thread_active.clear()
        if self._text_thread:
            self._text_thread.join(timeout=3)
            self._text_thread = None

        # Shutdown recorder: stop() first, then shutdown()
        if self._recorder:
            try:
                self._recorder.stop()
            except Exception:
                logger.debug("STT: recorder.stop() error (non-fatal)")
            try:
                self._recorder.shutdown()
            except Exception:
                logger.debug("STT: recorder.shutdown() error (non-fatal)")
            self._recorder = None

        self.enabled = False
        self.active = False
        await self._send_json({"type": "stt_state", "state": "disabled"})

    def ingest_audio_packet(self, packet: AudioPacket) -> None:
        """Ingest an audio packet: decode, resample, feed to recorder.

        Called directly from the WebSocket handler (event loop thread).
        No queue — recorder manages its own internal audio buffer.
        """
        if not self.enabled or self._recorder is None:
            return

        try:
            samples = packet_to_server_samples(packet)
            self._recorder.feed_audio(samples, original_sample_rate=SERVER_SAMPLE_RATE)
        except AudioPacketError as e:
            logger.warning("STT: invalid audio packet: %s", e)
        except Exception as e:
            logger.error("STT: failed to feed audio: %s", e)
            self._safe_send({
                "type": "stt_error",
                "code": "audio_feed_error",
                "message": str(e),
                "recoverable": True,
            })

    def _text_worker_loop(self) -> None:
        """Background thread: call recorder.text() to get final transcriptions.

        recorder.text() blocks until a final transcription is ready.
        This matches the RealtimeSTT example server pattern.
        """
        while self._text_thread_active.is_set():
            try:
                # Blocks until final transcription ready or shutdown
                text = self._recorder.text()
                if text and text.strip():
                    self._safe_send({"type": "stt_final", "text": text.strip()})
            except Exception as e:
                if self._text_thread_active.is_set():
                    logger.error("STT: text worker error: %s", e)
                    self._safe_send({
                        "type": "stt_error",
                        "code": "transcription_error",
                        "message": str(e),
                        "recoverable": True,
                    })

    def _on_recording_start(self) -> None:
        """Called by recorder thread when speech detected."""
        self.active = True
        logger.debug("STT: recording started")
        self._safe_send({"type": "stt_recording_start"})

    def _on_recording_stop(self) -> None:
        """Called by recorder thread when speech ends.

        Note: final transcription comes from _text_worker_loop via recorder.text(),
        not from this callback.
        """
        self.active = False
        logger.debug("STT: recording stopped")
        self._safe_send({"type": "stt_recording_stop"})

    def _on_realtime(self, text: str) -> None:
        """Called by recorder thread with interim transcription."""
        self._safe_send({"type": "stt_realtime", "text": text})

    def _safe_send(self, event: dict) -> None:
        """Thread-safe send to the main event loop."""
        asyncio.run_coroutine_threadsafe(self._send_json(event), self._loop)

    def close(self) -> None:
        """Cleanup on WebSocket disconnect."""
        logger.info("STT: closing session")
        self.disable()
```

### pi_chat/websocket.py

Add command handlers in `_dispatch_command`:

```python
elif command_type == "stt_enable":
    if stt is None:
        await _send_error(websocket, "STT is not configured")
    else:
        settings = command.get("settings")
        await stt.enable(settings)

elif command_type == "stt_disable":
    if stt is not None:
        await stt.disable()
```

Handle binary WebSocket messages (audio packets with metadata):

```python
# In handle_websocket, extend message handling:
# FastAPI websocket.receive() returns {"bytes": <bytes>} for binary messages
message = await websocket.receive()
if "bytes" in message and message["bytes"] is not None:
    if stt is not None and stt.enabled:
        try:
            packet = decode_audio_packet(message["bytes"])
            stt.ingest_audio_packet(packet)
        except AudioPacketError as e:
            await websocket.send_json({
                "type": "stt_error",
                "code": "invalid_packet",
                "message": str(e),
                "recoverable": True,
            })
```

Note: `decode_audio_packet` is imported from `pi_chat.stt_session`.

### pi_chat/app.py

```python
# At startup
from pi_chat.stt_service import STTService

stt_service = None
if config.STT_ENABLED:
    stt_service = STTService(config)

# In WebSocket handler
from pi_chat.stt_session import STTSession

stt = None
if stt_service is not None:
    stt = STTSession(stt_service, send_json, config)

# On disconnect
if stt is not None:
    stt.close()

# On shutdown
if stt_service is not None:
    await stt_service.close()
```

## Frontend Implementation Details

### static/stt.js

Mirrors `voice.js` structure:

```javascript
export function createSTTController(sendCommand, onFinalTranscript) {
  let enabled = false;
  let backendState = "disabled"; // disabled/loading/ready/error
  let audioContext = null;
  let mediaStream = null;
  let processor = null;
  let ws = null;

  return {
    handleServerMessage(message) {
      switch (message.type) {
        case "stt_state":
          backendState = message.state;
          updateUI();
          break;
        case "stt_recording_start":
          setRecordingState(true);
          break;
        case "stt_recording_stop":
          setRecordingState(false);
          break;
        case "stt_realtime":
          updateRealtimeText(message.text);
          break;
        case "stt_final":
          setRecordingState(false);
          onFinalTranscript(message.text);
          break;
        case "stt_error":
          handleError(message);
          break;
      }
    },

    async enable(websocket) {
      ws = websocket;
      sendCommand({ type: "stt_enable" });
      enabled = true;
      await startMicrophone();
    },

    async disable() {
      enabled = false;
      stopMicrophone();
      if (ws) sendCommand({ type: "stt_disable" });
    },

    toggle(websocket) {
      if (enabled) this.disable();
      else this.enable(websocket);
    },
  };
}

// Encode audio packet with metadata header
// Format: [4 bytes: metadata length (uint32 LE)][metadata JSON][PCM audio bytes]
function encodePacket(metadata, audioBuffer) {
  const metadataBytes = new TextEncoder().encode(JSON.stringify(metadata));
  const packet = new ArrayBuffer(4 + metadataBytes.byteLength + audioBuffer.byteLength);
  const view = new DataView(packet);
  view.setUint32(0, metadataBytes.byteLength, true);  // little-endian
  new Uint8Array(packet, 4, metadataBytes.byteLength).set(metadataBytes);
  new Uint8Array(packet, 4 + metadataBytes.byteLength).set(new Uint8Array(audioBuffer));
  return packet;
}

async function startMicrophone() {
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      sampleRate: 16000,  // Requested, but browser may use hardware rate
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  audioContext = new AudioContext({ sampleRate: 16000 });
  // CRITICAL: Use actual sample rate (browsers rarely honor the requested rate)
  const actualSampleRate = audioContext.sampleRate;

  const source = audioContext.createMediaStreamSource(mediaStream);
  const bufferSize = 4096;
  processor = audioContext.createScriptProcessor(bufferSize, 1, 0);

  // Waveform analysis
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 256;
  source.connect(analyser);
  analyser.connect(processor);
  processor.connect(audioContext.destination);

  const dataArray = new Uint8Array(analyser.frequencyBinCount);

  processor.onaudioprocess = (e) => {
    const inputData = e.inputBuffer.getChannelData(0);

    // Update waveform visualization
    analyser.getByteFrequencyData(dataArray);
    updateWaveform(dataArray);

    // Convert float32 to int16 PCM
    const int16 = new Int16Array(inputData.length);
    for (let i = 0; i < inputData.length; i++) {
      int16[i] = Math.max(-32768, Math.min(32767, inputData[i] * 32768));
    }

    // Encode packet with metadata (sampleRate is critical for server-side resampling)
    const metadata = {
      sampleRate: actualSampleRate,
      channels: 1,
      format: "pcm_s16le",
      frames: int16.length,
    };

    // Send as binary WebSocket message
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(encodePacket(metadata, int16.buffer));
    }
  };
}

function stopMicrophone() {
  if (processor) processor.disconnect();
  if (audioContext) audioContext.close();
  if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
  processor = null;
  audioContext = null;
  mediaStream = null;
}
```

### static/chat.js

Add mic button and integration:

```javascript
// Add mic toggle button next to send button
const micButton = document.createElement('button');
micButton.id = 'mic-toggle';
micButton.type = 'button';
micButton.setAttribute('aria-label', 'Toggle voice input');
micButton.innerHTML = '<svg>...</svg>'; // Mic icon
inputContainer.appendChild(micButton);

// On mic toggle
micButton.addEventListener('click', () => {
  if (sttController) {
    sttController.toggle(socket);
  }
});

// On final transcript
function handleFinalTranscript(text) {
  if (text && text.trim()) {
    textarea.value = text.trim();
    // Auto-submit after brief delay for UX
    setTimeout(() => submitPrompt(), 300);
  }
}

// Disable textarea when STT is active
function onSTTStateChange(enabled, recording) {
  textarea.disabled = recording;
  micButton.classList.toggle('recording', recording);
}
```

### static/app.js

```javascript
// Create STT controller
import { createSTTController } from './stt.js';

const stt = createSTTController(
  (cmd) => socket.sendCommand(cmd),
  (text) => chat.onFinalTranscript(text)
);

// Route STT messages
function routeServerMessage(message) {
  // ... existing routes ...
  const sttTypes = new Set([
    "stt_state", "stt_recording_start", "stt_recording_stop",
    "stt_realtime", "stt_final", "stt_error"
  ]);
  if (sttTypes.has(message.type)) {
    stt?.handleServerMessage(message);
  }
}
```

### static/styles.css

```css
/* Mic toggle button */
#mic-toggle {
  /* Match existing send button style */
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 0.5rem;
  border-radius: 50%;
  transition: all 0.15s;
}

#mic-toggle:hover {
  background: var(--surface-2);
}

#mic-toggle.recording {
  background: var(--accent-red);
  animation: pulse 1s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

/* Waveform visualization (simple bars) */
.stt-waveform {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 24px;
  margin: 0.5rem 0;
}

.stt-waveform .bar {
  width: 3px;
  background: var(--accent);
  border-radius: 2px;
  transition: height 0.05s ease;
}

/* Listening indicator */
.stt-listening {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.875rem;
  color: var(--muted);
}
```

## WebSocket Protocol

### Browser → Server Commands

| Type | Payload | Description |
|------|---------|-------------|
| `stt_enable` | `{settings?: STTSettings}` | Enable STT with optional settings |
| `stt_disable` | — | Disable STT, release recorder |
Binary messages (when STT enabled): Audio packets with metadata header.

**Binary packet format:**
```
[4 bytes: metadata length (uint32 LE)][metadata JSON][PCM audio bytes]
```

**Metadata fields:**
| Field | Type | Description |
|-------|------|-------------|
| `sampleRate` | int | Actual AudioContext sample rate (critical for resampling) |
| `channels` | int | Number of channels (1 for mono) |
| `format` | string | Audio format, always `"pcm_s16le"` |
| `frames` | int | Number of PCM frames in payload |

### Server → Browser Events

| Type | Payload | Description |
|------|---------|-------------|
| `stt_state` | `{state: "loading"\|"ready"\|"disabled"\|"error"}` | STT system state |
| `stt_recording_start` | — | Speech detected, recording started |
| `stt_recording_stop` | — | Speech ended, recording stopped |
| `stt_realtime` | `{text: string}` | Interim transcription |
| `stt_final` | `{text: string}` | Final transcription (user finished speaking) |
| `stt_error` | `{code: string, message: string, recoverable: boolean}` | STT error |

### STTSettings Schema

```typescript
interface STTSettings {
  model?: string;           // "small.en", "medium.en", etc.
  device?: "cpu" | "cuda";  // Inference device
  language?: string;        // Language code
  silenceDuration?: number; // Post-speech silence in seconds
  minRecordingLength?: number; // Minimum recording duration
  enableRealtime?: boolean; // Enable interim transcription
}
```

## Interruption Strategy

Based on research: **pi-rpc supports abort** via `{"type": "abort"}` command, and pi-chat already wires it in `websocket.py` (stops TTS + sends abort to pi).

### Implementation: Full Interruption (Option A)

When user speaks during agent TTS playback:

1. STT detects speech → finalizes transcript → sends `stt_final`
2. Frontend receives `stt_final`:
   - Calls `socket.sendCommand({type: "abort"})` — stops TTS + aborts pi
   - Waits for `agent_settled` or abort response
   - Sends `socket.sendCommand({type: "prompt", text: transcript})`
3. Agent processes new prompt

### Frontend Flow

```javascript
function handleFinalTranscript(text) {
  if (!text || !text.trim()) return;

  // Check if agent is currently speaking/active
  if (agentIsActive || ttsIsPlaying) {
    // Interrupt: abort current agent turn
    socket.sendCommand({ type: "abort" });

    // Wait briefly for abort to take effect, then send new prompt
    setTimeout(() => {
      socket.sendCommand({ type: "prompt", text: text.trim() });
    }, 200);
  } else {
    // Normal: just submit the prompt
    socket.sendCommand({ type: "prompt", text: text.trim() });
  }
}
```

### Fallback: Block Mic During Agent Turn (Option B)

If abort proves unstable, simpler approach:

```javascript
// In app.js, track agent state
function onPiEvent(event) {
  if (event.type === "agent_start") {
    stt?.setAgentActive(true); // Disable mic input
  }
  if (event.type === "agent_settled") {
    stt?.setAgentActive(false); // Re-enable mic input
  }
}

// In stt.js
function setAgentActive(active) {
  this.agentActive = active;
  if (active && this.enabled) {
    // Show "agent speaking" indicator, pause mic visually
    updateUI();
  }
}
```

**Recommendation: Start with Option A (full interruption).** The abort infrastructure exists and works. Add Option B as a toggleable fallback if needed.

## Configuration

### Environment Variables

```bash
# Enable/disable STT
PI_CHAT_STT_ENABLED=true

# Model selection
PI_CHAT_STT_MODEL=small.en
PI_CHAT_STT_REALTIME_MODEL=tiny.en

# Device
PI_CHAT_STT_DEVICE=cpu  # or cuda

# VAD tuning
PI_CHAT_STT_POST_SPEECH_SILENCE=0.6
PI_CHAT_STT_MIN_RECORDING_LENGTH=0.3
PI_CHAT_STT_MIN_GAP_BETWEEN_RECORDINGS=0.3

# Realtime transcription
PI_CHAT_STT_REALTIME_PAUSE=0.2

# Internal buffer limits (recorder-managed)
PI_CHAT_STT_MAX_AUDIO_QUEUE_SIZE=128
```

### User-Configurable Settings

Stored in localStorage per account (same pattern as voice settings):

```json
{
  "stt": {
    "enabled": true,
    "autoSubmit": true,
    "showRealtime": true
  }
}
```

## Test Plan

### Unit Tests (no RealtimeSTT dependency)

**tests/fakes/stt.py**
- `FakeSTTService`: Mocks load/close, tracks state
- `FakeSTTSession`: Mocks enable/disable/feed_audio, emits synthetic events

**tests/test_stt_service.py**
- Test lazy loading is idempotent
- Test concurrent load deduplication
- Test close releases state

**tests/test_stt_session.py**
- Test enable creates recorder, starts text worker thread
- Test disable stops text worker, shuts down recorder
- Test ingest_audio_packet decodes and feeds audio correctly
- Test decode_audio_packet handles valid/invalid packets
- Test resample_int16 converts sample rates correctly
- Test callback bridge sends events to event loop
- Test close cleanup on disconnect

### Integration Tests (with RealtimeSTT)

**tests/test_stt_api.py**
- Test `stt_enable` command → `stt_state: ready` event
- Test `stt_disable` command → `stt_state: disabled` event
- Test audio packet handling (feed packets with metadata, verify no errors)
- Test invalid packet rejection → `stt_error` event
- Test WebSocket disconnect cleanup

**tests/test_stt_e2e.py**
- Use pre-recorded PCM audio fixtures
- Feed audio → verify `stt_recording_start`, `stt_realtime`, `stt_final` events
- Verify final transcription matches expected text
- Test interruption flow: abort + new prompt

### Manual Testing

Use CPU STT with voice fixtures or live mic:

1. **Basic flow:**
   - Open http://localhost:9000
   - Click mic button → verify listening indicator appears
   - Speak → verify realtime text updates
   - Pause → verify final text auto-submits as prompt
   - Verify pi responds to transcribed prompt

2. **Interruption:**
   - Start a long pi response with TTS enabled
   - Click mic, speak → verify TTS stops, pi aborts, new prompt sent

3. **Edge cases:**
   - Click mic during pi response (without TTS)
   - Rapid enable/disable toggling
   - Very short utterances (< min_recording_length)
   - Background noise / no speech for extended period

4. **Settings:**
   - Adjust silence duration → verify behavior changes
   - Test with different models (tiny.en vs small.en)

### Test Commands

```bash
# Unit tests (fast, no RealtimeSTT)
uv run pytest tests/test_stt_service.py tests/test_stt_session.py -v

# Integration tests (requires RealtimeSTT)
uv run pytest tests/test_stt_api.py tests/test_stt_e2e.py -v

# All STT tests
uv run pytest tests/test_stt*.py -v
```

## Phased Implementation Order

### Phase 1: Core STT (this phase)

1. **Backend skeleton:**
   - `config.py`: Add STT config vars
   - `stt_service.py`: Lazy-loading service
   - `stt_session.py`: Per-connection session
   - `app.py`/`websocket.py`: Wire STT into server

2. **Frontend skeleton:**
   - `stt.js`: STT controller with mic capture
   - `chat.js`: Mic button, disable textarea, handle final transcript
   - `styles.css`: Button and listening indicator styles
   - `app.js`: Route STT events

3. **Tests:**
   - `fakes/stt.py`: Test doubles
   - `test_stt_service.py`, `test_stt_session.py`: Unit tests
   - Manual testing with live mic

4. **Interruption:**
   - Wire abort on final transcript during agent turn

### Phase 2: Polish (future)

- Waveform visualization refinement
- Settings UI for silence duration, model selection
- GPU support toggle
- Wake word support ("Hey Jarvis" style activation)
- STT confidence scoring
- Language auto-detection
- Abort button in UI (for manual interruption)

### Phase 3: Advanced (future)

- Partial transcript display in chat (show what user said)
- STT metrics (latency, accuracy feedback)
- Multi-language support
- Voice activity visualization synced with TTS playback

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| RealtimeSTT recorder crash | STT unavailable | Graceful error handling, `stt_error` event, retry on enable |
| Audio packet decode errors | Dropped audio, STT failure | Validate packet format, send `stt_error`, log details |
| Sample rate mismatch | Garbled transcription | Use actual AudioContext sampleRate in metadata, resample on server |
| Callback thread safety issues | Crashes or lost events | `asyncio.run_coroutine_threadsafe` bridge, test thoroughly |
| Abort leaves pi in bad state | Agent unresponsive | Test abort extensively, fallback to Option B if needed |
| Browser mic permission denied | Feature unusable | Clear UI error message, fallback to keyboard |
| High CPU usage on CPU mode | System slowdown | Start with small.en on CPU, offer cuda option |
| Mic conflict with TTS voice recording | STT or TTS fails | Disable STT when TTS voice recording is active (Phase 2) |

## Dependencies

```toml
# Add as speech pyproject optional extra dependency
[project.optional-dependencies]
stt = ["RealtimeSTT[faster-whisper]", "silero-vad[onnx-cpu]"]
```

## References

- TTS implementation: `pi_chat/tts_service.py`, `pi_chat/voice_session.py`, `static/voice.js`
- RealtimeSTT docs: `experiments/RealtimeSTT/docs/`
- RealtimeSTT API: `experiments/RealtimeSTT/RealtimeSTT/audio_recorder.py`
- pi-rpc docs: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md
- Research artifacts: `/tmp/stt-plan-tts-patterns.md`, `/tmp/stt-plan-interruption.md`
