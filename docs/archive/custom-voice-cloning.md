> **Status, archived 2026-11-17:** cloning from reference audio is still an OmniVoice-only capability and still works, but the API shape and settings UI described here predate the per-engine `/api/tts/capabilities` endpoint.

# Custom Voice Cloning Implementation Plan

## Overview

Allow pi-chat users to upload their own voice sample files and use them for OmniVoice cloning, bypassing the existing voice design parameters (gender/age/pitch/accent). OmniVoice already supports reference-audio-based cloning; this feature exposes that capability through the pi-chat UI.

### User Story

As a user, I want to upload a short audio clip of my voice, optionally name it, and have pi-chat speak in my voice instead of the default bootstrapped voices. Custom voices are shared across all pi-chat profiles (b and r), persist across sessions, and are selectable from the voice settings menu.

## Architecture

### High-Level Data Flow

```
Browser (upload)          Server                    OmniVoice
───────────────          ────────                  ─────────
┌─────────────┐    POST   ┌──────────────┐         ┌──────────────────┐
│ File picker │──────────►│ /api/voices  │         │                  │
│ Drag & drop │           │ (multipart)  │         │                  │
└─────────────┘           └──────┬───────┘         │                  │
                                 │                 │                  │
                    ┌────────────▼────────────┐    │                  │
                    │ Validate & decode audio │    │                  │
                    │ Compute cache key       │    │                  │
                    │ Store .wav              │    │                  │
                    └────────────┬────────────┘    │                  │
                                 │                 │                  │
                    ┌────────────▼────────────┐    │                  │
                    │ GET /api/voices         │    │                  │
                    │ → list available voices │◄───│                  │
                    └────────────┬────────────┘    │                  │
                                 │                 │                  │
Browser (voice mode)             │                 │                  │
───────────────                  │                 │                  │
┌─────────────┐    voice_enable  │                 │                  │
│ Voice toggle│─────────────────►│ VoiceSession    │                  │
│ Voice gear  │─── voice_prepare │ → TTSService    │                  │
│ Select custom│                 │   prepare_voice │                  │
└─────────────┘                  │                │ create_voice_     │
                                 │                │ clone_prompt()    │
                                 │◄───────────────│                   │
                                 │   VoiceHandle  │                   │
                                 │                │                   │
agent_start ──► VoiceSession ──► TTSService      │                   │
                                 │ synthesize()  │                   │
                                 │               │ model.generate()  │
                                 │               │ + voice_clone_    │
                                 │               │ prompt            │
                                 │◄──────────────│                   │
                                 │ PCM frames    │                   │
                                 │──────────────► Browser Web Audio  │
```

### Key Insight

The existing voice pipeline uses "voice design" mode:
1. Build an `instruct` string from settings (gender, age, pitch, accent)
2. Generate bootstrap audio via `model.generate(text=bootstrap_text, instruct=instruct)`
3. Create `VoiceClonePrompt` from that bootstrap audio

Custom voice cloning skips steps 1-2 entirely:
1. Load user's reference audio file
2. Create `VoiceClonePrompt` directly via `model.create_voice_clone_prompt(ref_audio=(waveform, sr), ref_text=...)`
3. Use that prompt for all synthesis

This means custom voices **ignore** all voice design parameters — OmniVoice extracts the voice characteristics directly from the reference audio.

## Backend Implementation

### New Files

#### `pi_chat/voice_store.py`

Manages custom voice sample storage, validation, and metadata.

```python
"""Custom voice sample storage and management."""

import hashlib
import io
import logging
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

# Supported input formats (handled by librosa or soundfile)
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".webm"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MIN_DURATION = 2.0   # seconds
MAX_DURATION = 30.0  # seconds
TARGET_SAMPLE_RATE = 24000  # OmniVoice native rate

# Number of seconds from start to hash for cache key
HASH_WINDOW_SECONDS = 3.0


@dataclass(frozen=True)
class VoiceSample:
    """Metadata for a stored voice sample."""
    voice_id: str          # SHA-256 hash of audio content
    filename: str          # Original filename (sanitized)
    display_name: str      # User-assigned name (defaults to sanitized filename)
    duration: float        # Duration in seconds
    upload_time: float     # Unix timestamp
    waveform: Any | None   # Loaded numpy array (lazy, None until needed)


class VoiceStore:
    """Manages custom voice samples on disk.

    Voices are shared across all pi-chat profiles (b and r).

    Storage layout:
        ~/.pi/voices/<voice_id>.wav       # Normalized 16-bit mono WAV at 24kHz
        ~/.pi/voices/<voice_id>.meta.json # Metadata JSON with user-friendly name
    """

    def __init__(self, config_: Any | None = None):
        self._config = config_ or config
        self._voice_dir = self._config.VOICE_SAMPLES_DIR
        os.makedirs(self._voice_dir, exist_ok=True)

    def upload(self, file_bytes: bytes, original_filename: str, display_name: str | None = None) -> VoiceSample:
        """Validate, normalize, and store an uploaded voice sample.

        Args:
            file_bytes: Raw file bytes from UploadFile.read()
            original_filename: Original filename from UploadFile.filename
            display_name: Optional user-friendly name (defaults to sanitized filename)

        Returns:
            VoiceSample with voice_id, duration, etc.

        Raises:
            ValueError: If file is invalid (wrong format, too short, etc.)
        """
        # Validate file size
        if len(file_bytes) > MAX_FILE_SIZE:
            raise ValueError(f"File too large ({len(file_bytes)} bytes). Max is {MAX_FILE_SIZE} bytes.")

        # Validate extension
        ext = Path(original_filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Unsupported format '{ext}'. Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        # Default display name
        if display_name is None:
            display_name = self._sanitize_filename(original_filename)

        # Load and validate audio (lazy import of librosa/soundfile)
        waveform, duration = self._load_audio(file_bytes, ext)

        # Validate duration
        if duration < MIN_DURATION:
            raise ValueError(
                f"Audio too short ({duration:.1f}s). Minimum is {MIN_DURATION}s. "
                "Speak for at least a few seconds."
            )
        if duration > MAX_DURATION:
            raise ValueError(
                f"Audio too long ({duration:.1f}s). Maximum is {MAX_DURATION}s. "
                "Use a short, clear sample."
            )

        # Compute cache key from first HASH_WINDOW_SECONDS of audio
        voice_id = self._compute_hash(waveform)

        # Check if this voice already exists (deduplicate identical samples)
        existing_meta = self._meta_path(voice_id)
        if existing_meta.exists():
            logger.info("Voice %s already exists (duplicate upload), returning existing", voice_id[:8])
            return self._load_meta(voice_id)

        # Normalize to 16-bit mono WAV at target sample rate
        normalized = self._normalize(waveform)

        # Store WAV file
        wav_path = self._wav_path(voice_id)
        self._save_wav(wav_path, normalized, TARGET_SAMPLE_RATE)

        # Store metadata
        sanitized_filename = self._sanitize_filename(original_filename)
        sample = VoiceSample(
            voice_id=voice_id,
            filename=sanitized_filename,
            display_name=display_name,
            duration=duration,
            upload_time=__import__("time").monotonic(),  # Use wall clock for display
            waveform=None,  # Lazy-loaded
        )
        self._save_meta(voice_id, sample)

        logger.info("Stored voice %s (%.1fs, %d bytes wav)", voice_id[:8], duration, wav_path.stat().st_size)
        return sample

    def list_voices(self) -> list[VoiceSample]:
        """List all stored voice samples."""
        samples = []
        for meta_file in Path(self._voice_dir).glob("*.meta.json"):
            voice_id = meta_file.stem
            try:
                samples.append(self._load_meta(voice_id))
            except Exception as e:
                logger.warning("Failed to load meta for %s: %s", voice_id, e)
        return samples

    def get_voice(self, voice_id: str) -> VoiceSample | None:
        """Get a voice sample by ID."""
        meta_path = self._meta_path(voice_id)
        if not meta_path.exists():
            return None
        return self._load_meta(voice_id)

    def load_waveform(self, voice_id: str) -> tuple[Any, int]:
        """Load the waveform for a voice sample.

        Returns:
            (waveform_array, sample_rate) tuple suitable for OmniVoice.
        """
        sample = self.get_voice(voice_id)
        if sample is None:
            raise ValueError(f"Voice {voice_id} not found")

        wav_path = self._wav_path(voice_id)
        if not wav_path.exists():
            raise ValueError(f"WAV file missing for voice {voice_id}")

        # Lazy import
        import soundfile as sf
        waveform, sr = sf.read(str(wav_path), dtype="float32")
        return waveform, sr

    def delete_voice(self, voice_id: str) -> bool:
        """Delete a voice sample and its files."""
        deleted = False
        for path in [self._wav_path(voice_id), self._meta_path(voice_id)]:
            if path.exists():
                path.unlink()
                deleted = True
        return deleted

    def update_display_name(self, voice_id: str, display_name: str) -> bool:
        """Update the user-friendly display name for a voice sample."""
        sample = self.get_voice(voice_id)
        if sample is None:
            return False
        # Create new sample with updated name (frozen dataclass)
        new_sample = VoiceSample(
            voice_id=sample.voice_id,
            filename=sample.filename,
            display_name=display_name,
            duration=sample.duration,
            upload_time=sample.upload_time,
            waveform=None,
        )
        self._save_meta(voice_id, new_sample)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_audio(self, file_bytes: bytes, ext: str) -> tuple[Any, float]:
        """Load audio from bytes using librosa or soundfile."""
        try:
            import librosa
            waveform, sr = librosa.load(
                io.BytesIO(file_bytes),
                sr=TARGET_SAMPLE_RATE,
                mono=True,
                dtype="float32",
            )
        except ImportError:
            import soundfile as sf
            waveform, sr = sf.read(io.BytesIO(file_bytes), dtype="float32")
            # Resample if needed
            if sr != TARGET_SAMPLE_RATE:
                import librosa
                waveform = librosa.resample(waveform, orig_sr=sr, target_sr=TARGET_SAMPLE_RATE)
                sr = TARGET_SAMPLE_RATE

        duration = len(waveform) / sr
        return waveform, duration

    def _compute_hash(self, waveform: Any) -> str:
        """Compute a stable hash from the first HASH_WINDOW_SECONDS of audio.

        Uses raw PCM bytes (not float representation) for stability.
        """
        import numpy as np
        samples = int(HASH_WINDOW_SECONDS * TARGET_SAMPLE_RATE)
        segment = waveform[:samples]
        # Convert to int16 bytes for stable hashing
        segment_int16 = np.clip(segment, -1.0, 1.0)
        segment_int16 = (segment_int16 * 32767).astype(np.int16)
        return hashlib.sha256(segment_int16.tobytes()).hexdigest()[:16]

    def _normalize(self, waveform: Any) -> Any:
        """Normalize waveform to [-1, 1] range."""
        import numpy as np
        max_val = np.max(np.abs(waveform))
        if max_val > 0:
            waveform = waveform / max_val * 0.95  # Leave headroom
        return waveform

    def _save_wav(self, path: Path, waveform: Any, sr: int) -> None:
        """Save waveform as 16-bit mono WAV."""
        import numpy as np
        import soundfile as sf
        samples = (np.clip(waveform, -1.0, 1.0) * 32767).astype(np.int16)
        sf.write(str(path), samples, sr, format="WAV", subtype="PCM_16")

    def _save_meta(self, voice_id: str, sample: VoiceSample) -> None:
        """Save metadata JSON."""
        import json
        meta_path = self._meta_path(voice_id)
        meta = {
            "voice_id": sample.voice_id,
            "filename": sample.filename,
            "display_name": sample.display_name,
            "duration": sample.duration,
            "upload_time": sample.upload_time,
        }
        meta_path.write_text(json.dumps(meta, indent=2))

    def _load_meta(self, voice_id: str) -> VoiceSample:
        """Load metadata JSON."""
        import json
        meta_path = self._meta_path(voice_id)
        meta = json.loads(meta_path.read_text())
        return VoiceSample(
            voice_id=meta["voice_id"],
            filename=meta["filename"],
            display_name=meta.get("display_name", meta["filename"]),  # Backward compat
            duration=meta["duration"],
            upload_time=meta["upload_time"],
            waveform=None,
        )

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for display."""
        name = Path(filename).stem
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:64]

    def _wav_path(self, voice_id: str) -> Path:
        return Path(self._voice_dir) / f"{voice_id}.wav"

    def _meta_path(self, voice_id: str) -> Path:
        return Path(self._voice_dir) / f"{voice_id}.meta.json"
```

### Modified Files

#### `pi_chat/config.py`

Add voice sample storage configuration:

```python
# Custom voice sample storage
VOICE_SAMPLES_DIR = os.environ.get(
    "PI_CHAT_VOICE_SAMPLES_DIR",
    os.path.join(os.path.expanduser("~/.pi"), "voices"),
)
```

#### `pi_chat/tts_service.py`

Key changes:

1. **New `VoiceSettings` variant for custom voices** — Add a `voice_id` field (optional):

```python
# In tests/fakes/voice.py and tts_service.py
@dataclass(frozen=True)
class VoiceSettings:
    gender: str = "female"
    age: str = "young adult"
    pitch: str = "moderate pitch"
    accent: str = "american accent"
    style: str | None = None
    speed: float = 1.0
    language: str = "English"
    voice_id: str | None = None  # NEW: custom voice sample ID
```

2. **New `_voice_key` behavior** — When `voice_id` is set, key is based on voice_id, not settings:

```python
def _voice_key(self, settings: VoiceSettings) -> str:
    if settings.voice_id:
        # Custom voice: key is the voice_id itself
        return f"custom:{settings.voice_id}"
    # Bootstrapped voice: key from design settings
    raw = f"{settings.gender}|{settings.age}|{settings.pitch}|{settings.accent}|{settings.style}|{settings.speed}|{settings.language}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

3. **New `prepare_voice` path for custom voices** — In `_OmniVoiceRuntime.prepare_voice`:

```python
def prepare_voice(self, settings: VoiceSettings, bootstrap_text: str) -> object:
    if settings.voice_id:
        # Custom voice: load reference audio and create prompt directly
        return self._prepare_custom_voice(settings)
    # Existing bootstrapped voice path...
    return self._prepare_bootstrap_voice(settings, bootstrap_text)

def _prepare_custom_voice(self, settings: VoiceSettings) -> object:
    """Prepare a voice clone prompt from a user-uploaded reference audio."""
    import torch
    from pi_chat.voice_store import VoiceStore

    store = VoiceStore(self._config)
    waveform, sr = store.load_waveform(settings.voice_id)

    logger.info("TTS: [DEBUG] _prepare_custom_voice() voice_id=%s waveform_len=%d sr=%d",
                settings.voice_id[:8], len(waveform), sr)

    # Create voice clone prompt directly from reference audio
    # (no instruct string needed — OmniVoice extracts characteristics from audio)
    voice_clone_prompt = self._model.create_voice_clone_prompt(
        ref_audio=(torch.from_numpy(waveform), sr),
        ref_text=None,  # Optional: can be used for better quality if known
    )

    return {
        "voice_clone_prompt": voice_clone_prompt,
        "instruct": None,  # No instruct for custom voices
        "voice_id": settings.voice_id,
    }
```

4. **Modified `generate` path** — When `instruct` is None (custom voice), don't pass it:

```python
def generate(self, text: str, prepared_voice: object, settings: VoiceSettings) -> tuple[object, int]:
    voice_clone_prompt = prepared_voice.get("voice_clone_prompt")
    instruct = prepared_voice.get("instruct")

    kw = {
        "text": text,
        "voice_clone_prompt": voice_clone_prompt,
        "language": settings.language if instruct else None,  # Auto-detect for custom voices
        "num_step": self._num_steps,
        "speed": settings.speed,
        "postprocess_output": True,
        "pad_duration": 0.02,
        "fade_duration": 0.02,
    }
    if instruct:
        kw["instruct"] = instruct

    audio_list = self._model.generate(**kw)
    # ... rest unchanged
```

#### `pi_chat/app.py`

Add voice management endpoints:

```python
from .voice_store import VoiceStore, ALLOWED_EXTENSIONS, MAX_FILE_SIZE

# In create_app():
voice_store = VoiceStore()

@app.post("/api/voices")
async def upload_voice(request: Request, file: UploadFile, display_name: str | None = None):
    """Upload a custom voice sample.

    Args:
        file: Audio file (multipart/form-data)
        display_name: Optional user-friendly name (defaults to sanitized filename)
    """
    _require_account(request, auth)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    content = await file.read()
    try:
        sample = voice_store.upload(content, file.filename, display_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "voiceId": sample.voice_id,
        "filename": sample.filename,
        "displayName": sample.display_name,
        "duration": round(sample.duration, 1),
    }

@app.get("/api/voices")
async def list_voices(request: Request):
    """List available custom voices."""
    _require_account(request, auth)
    samples = voice_store.list_voices()
    return {
        "voices": [
            {
                "voiceId": s.voice_id,
                "filename": s.filename,
                "displayName": s.display_name,
                "duration": round(s.duration, 1),
                "uploadTime": s.upload_time,
            }
            for s in samples
        ]
    }

@app.patch("/api/voices/{voice_id}")
async def update_voice(request: Request, voice_id: str):
    """Update a voice sample's metadata (display name)."""
    _require_account(request, auth)

    body = await request.json()
    display_name = body.get("displayName")

    if display_name is None:
        raise HTTPException(status_code=400, detail="No displayName provided")

    if not voice_store.get_voice(voice_id):
        raise HTTPException(status_code=404, detail="Voice not found")

    voice_store.update_display_name(voice_id, display_name)
    return {"success": True}

@app.delete("/api/voices/{voice_id}")
async def delete_voice(request: Request, voice_id: str):
    """Delete a custom voice sample."""
    _require_account(request, auth)
    if not voice_store.delete_voice(voice_id):
        raise HTTPException(status_code=404, detail="Voice not found")
    return {"success": True}

@app.get("/api/voices/{voice_id}/audio")
async def get_voice_audio(request: Request, voice_id: str):
    """Get the audio file for a voice sample (for preview playback)."""
    _require_account(request, auth)

    sample = voice_store.get_voice(voice_id)
    if not sample:
        raise HTTPException(status_code=404, detail="Voice not found")

    wav_path = Path(voice_store._wav_path(voice_id))
    if not wav_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    from fastapi.responses import FileResponse
    return FileResponse(wav_path, media_type="audio/wav")
```

#### `pi_chat/voice_session.py`

Update `validate_settings` to accept `voice_id`:

```python
def validate_settings(raw: dict) -> VoiceSettings | None:
    try:
        # ... existing validation ...

        voice_id = raw.get("voice_id")
        if voice_id is not None and not isinstance(voice_id, str):
            return None

        return VoiceSettings(
            # ... existing fields ...
            voice_id=voice_id,
        )
    except (TypeError, ValueError):
        return None
```

When a custom voice is selected, the voice design fields (gender/age/etc) are ignored by the backend — they're still sent for schema compatibility but `_prepare_custom_voice` doesn't use them.

### New Dependencies

Add to `pyproject.toml` (under voice extra):

```toml
[project.optional-dependencies]
voice = [
    "torch",
    "omnivoice",
    "librosa",        # NEW: audio loading with resampling
    "soundfile",      # NEW: WAV read/write
]
```

## Frontend Implementation

### Modified Files

#### `static/index.html`

Add custom voice upload section to the voice settings dialog:

```html
<!-- Inside voice-settings-dialog, after existing controls, before actions -->
<div class="voice-settings-divider"></div>
<div class="voice-custom-section">
  <div class="voice-custom-header">
    <span>Custom Voice Sample</span>
    <div class="voice-guidelines-tooltip">
      <span>Guidelines</span>
      <button type="button" class="voice-help-icon" title="Voice sample guidelines">?</button>
    </div>
  </div>

  <!-- Voice selector: bootstrapped OR custom -->
  <label class="voice-setting-label">
    <span>Voice</span>
    <select id="voice-type">
      <option value="bootstrap">Designed voice (adjust settings below)</option>
      <optgroup label="Custom voices" id="voice-custom-optgroup">
        <!-- Populated dynamically: each option has data-display-name, data-duration -->
      </optgroup>
    </select>
  </label>

  <!-- Custom voice actions row (shown when custom voice selected) -->
  <div class="voice-custom-actions" id="voice-custom-actions" style="display:none">
    <button type="button" class="voice-preview-btn" title="Preview this voice sample">▶ Play</button>
    <button type="button" class="voice-delete-btn" title="Delete this voice sample">✕ Delete</button>
    <button type="button" class="voice-rename-btn" title="Rename this voice">✎ Rename</button>
  </div>

  <!-- Upload zone (visible when voice-type is "custom-upload") -->
  <div class="voice-upload-zone" id="voice-upload-zone" style="display:none">
    <div class="voice-upload-prompt">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <span>Drop audio file here or <button class="voice-upload-btn" type="button">browse</button></span>
      <span class="voice-upload-hint">WAV, MP3, FLAC, M4A, OGG, WebM · 2–30 seconds · max 10MB</span>
    </div>
    <input type="file" id="voice-upload-input" accept=".wav,.mp3,.flac,.m4a,.ogg,.webm" style="display:none">
    <div class="voice-upload-status" id="voice-upload-status"></div>
  </div>

  <!-- Recording zone -->
  <div class="voice-recording-zone" id="voice-recording-zone" style="display:none">
    <div class="voice-recording-prompt">
      <span>Or record from microphone:</span>
    </div>
    <div class="voice-recording-controls">
      <button type="button" id="record-btn" class="voice-record-btn">🎤 Record</button>
      <div class="voice-recording-active" id="voice-recording-active" style="display:none">
        <span id="recording-timer">Recording... 00:00</span>
        <button type="button" id="stop-record-btn" class="voice-stop-btn">⏹ Stop</button>
      </div>
    </div>
  </div>

  <!-- Name input (visible when uploading/recording) -->
  <div class="voice-name-input" id="voice-name-input" style="display:none">
    <label class="voice-setting-label">
      <span>Name this voice</span>
      <input type="text" id="voice-display-name" maxlength="40" placeholder="My Voice">
    </label>
  </div>

  <!-- Bootstrap settings (disabled when custom voice selected) -->
  <div class="voice-bootstrap-settings" id="voice-bootstrap-settings">
    <!-- Existing gender/age/pitch/accent/style/language/speed controls -->
  </div>
</div>

<!-- Guidelines tooltip popup -->
<div class="voice-guidelines-popup" id="voice-guidelines-popup" style="display:none">
  <div class="voice-guidelines-title">Voice Sample Guidelines</div>
  <ul>
    <li>3–10 seconds of clear speech works best</li>
    <li>Speak naturally as you would in conversation</li>
    <li>Avoid loud background noise or music</li>
    <li>OmniVoice handles accent, language, and tone automatically</li>
    <li>Samples longer than 20s are truncated</li>
  </ul>
</div>
```

#### `static/voice.js`

Key additions:

1. **State tracking** — Add `customVoiceId` to settings:

```javascript
function defaultSettings() {
  return {
    voiceType: 'bootstrap',  // 'bootstrap' or 'custom:<voice_id>'
    gender: 'female',
    age: 'young adult',
    // ... existing fields ...
  };
}
```

2. **Custom voice management** — New functions:

```javascript
let customVoices = [];  // List of {voiceId, filename, duration}

async function loadCustomVoices() {
  try {
    const resp = await fetch('/api/voices');
    if (!resp.ok) return;
    const data = await resp.json();
    customVoices = data.voices || [];
    populateVoiceSelector();
  } catch (e) {
    // Silently fail — custom voices are optional
  }
}

function populateVoiceSelector() {
  const select = document.getElementById('voice-type');
  if (!select) return;

  // Preserve first option and optgroup
  const optgroup = select.querySelector('optgroup');
  if (!optgroup) return;

  // Clear existing custom options
  while (optgroup.firstChild) {
    optgroup.removeChild(optgroup.firstChild);
  }

  // Add "Upload new..." option
  const uploadOpt = document.createElement('option');
  uploadOpt.value = 'custom-upload';
  uploadOpt.textContent = 'Upload new voice...';
  optgroup.appendChild(uploadOpt);

  // Add existing custom voices
  customVoices.forEach(v => {
    const opt = document.createElement('option');
    opt.value = `custom:${v.voiceId}`;
    opt.textContent = `${v.filename} (${v.duration}s)`;
    opt.setAttribute('data-voice-id', v.voiceId);
    optgroup.appendChild(opt);
  });

  // Restore current selection
  if (settings.voiceType) {
    select.value = settings.voiceType;
  }
}

async function uploadVoiceFile(file) {
  const status = document.getElementById('voice-upload-status');
  if (!status) return;

  status.textContent = 'Uploading...';
  status.className = 'voice-upload-status uploading';

  const formData = new FormData();
  formData.append('file', file);

  try {
    const resp = await fetch('/api/voices', {
      method: 'POST',
      body: formData,
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Upload failed');
    }

    const data = await resp.json();
    status.textContent = `Voice "${data.filename}" uploaded successfully!`;
    status.className = 'voice-upload-status success';

    // Reload voice list
    await loadCustomVoices();

    // Auto-select the new voice
    const select = document.getElementById('voice-type');
    if (select) {
      select.value = `custom:${data.voiceId}`;
      syncSettingsFromForm();
    }

  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    status.className = 'voice-upload-status error';
    setTimeout(() => {
      status.textContent = '';
      status.className = 'voice-upload-status';
    }, 5000);
  }
}

async function deleteVoice(voiceId) {
  try {
    const resp = await fetch(`/api/voices/${voiceId}`, { method: 'DELETE' });
    if (resp.ok) {
      await loadCustomVoices();
    }
  } catch (e) {
    // Silently fail
  }
}

async function previewVoice(voiceId) {
  try {
    const audio = new Audio(`/api/voices/${voiceId}/audio`);
    await audio.play();
  } catch (e) {
    // Silently fail
  }
}

function confirmDeleteVoice(voiceId, displayName) {
  if (!confirm(`Delete "${displayName}"? This cannot be undone.`)) {
    return;
  }
  deleteVoice(voiceId);
}
```

3. **Microphone recording** — New functions:

```javascript
let mediaRecorder = null;
let audioChunks = [];
let recordingStartTime = 0;
let recordingTimer = null;

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      const file = new File([blob], 'recording.webm', { type: 'audio/webm' });
      await uploadVoiceFile(file);
    };

    mediaRecorder.start();
    recordingStartTime = Date.now();
    startRecordingTimer();
    updateRecordingUI(true);

  } catch (err) {
    console.error('Recording error:', err);
    alert('Could not access microphone. Please check permissions.');
  }
}

function stopRecording() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;

  const duration = (Date.now() - recordingStartTime) / 1000;
  if (duration < 2) {
    alert('Recording too short. Please record at least 2 seconds.');
    return;
  }
  if (duration > 30) {
    alert('Recording too long. Maximum is 30 seconds.');
    return;
  }

  mediaRecorder.stop();
  stopRecordingTimer();
  updateRecordingUI(false);
}

function startRecordingTimer() {
  const timerEl = document.getElementById('recording-timer');
  if (!timerEl) return;

  recordingTimer = setInterval(() => {
    const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
    const mins = String(Math.floor(elapsed / 60)).padStart(2, '0');
    const secs = String(elapsed % 60).padStart(2, '0');
    timerEl.textContent = `Recording... ${mins}:${secs}`;

    if (elapsed >= 30) {
      stopRecording();
    }
  }, 1000);
}

function stopRecordingTimer() {
  if (recordingTimer) {
    clearInterval(recordingTimer);
    recordingTimer = null;
  }
}

function updateRecordingUI(isRecording) {
  const recordBtn = document.getElementById('record-btn');
  const stopBtn = document.getElementById('stop-record-btn');

  if (recordBtn) recordBtn.style.display = isRecording ? 'none' : 'inline-flex';
  if (stopBtn) stopBtn.style.display = isRecording ? 'inline-flex' : 'none';
}
```

4. **Upload zone handlers**:

```javascript
function setupUploadZone() {
  const zone = document.getElementById('voice-upload-zone');
  const input = document.getElementById('voice-upload-input');
  const browseBtn = document.querySelector('.voice-upload-btn');

  if (!zone || !input) return;

  browseBtn?.addEventListener('click', () => input.click());
  input.addEventListener('change', (e) => {
    if (e.target.files?.[0]) {
      uploadVoiceFile(e.target.files[0]);
      e.target.value = '';
    }
  });

  // Drag and drop
  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => {
    zone.classList.remove('dragover');
  });
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const file = e.dataTransfer.files?.[0];
    if (file) uploadVoiceFile(file);
  });
}
```

4. **Voice type change handler** — Toggle bootstrap settings visibility:

```javascript
function handleVoiceTypeChange() {
  const select = document.getElementById('voice-type');
  const bootstrapSettings = document.getElementById('voice-bootstrap-settings');
  const uploadZone = document.getElementById('voice-upload-zone');

  if (!select || !bootstrapSettings) return;

  const value = select.value;
  const isBootstrap = value === 'bootstrap';
  const isUpload = value === 'custom-upload';

  // Toggle bootstrap controls
  bootstrapSettings.classList.toggle('disabled', !isBootstrap);
  bootstrapSettings.querySelectorAll('select, input').forEach(el => {
    el.disabled = !isBootstrap;
  });

  // Toggle upload zone
  if (uploadZone) {
    uploadZone.style.display = isUpload ? 'block' : 'none';
  }

  syncSettingsFromForm();
}

function syncSettingsFromForm() {
  const voiceTypeSelect = document.getElementById('voice-type');
  settings.voiceType = voiceTypeSelect?.value || 'bootstrap';

  // ... existing sync code ...

  saveSettings();
}
```

5. **Backend settings payload** — Include voice_id when custom voice selected:

```javascript
function getBackendSettings() {
  const result = {
    gender: settings.gender,
    age: settings.age,
    pitch: settings.pitch,
    accent: settings.accent,
    style: settings.style,
    speed: settings.speed,
    language: settings.language,
  };

  // Add voice_id for custom voices
  if (settings.voiceType && settings.voiceType.startsWith('custom:')) {
    result.voice_id = settings.voiceType.slice(7);  // Strip "custom:" prefix
  }

  return result;
}
```

6. **Load custom voices on account change**:

```javascript
function setAccount(acc) {
  account = acc;
  // ... existing code ...
  loadCustomVoices();
}
```

#### `static/styles.css`

Add styles for custom voice upload section:

```css
/* Custom voice section */
.voice-settings-divider {
  border: none;
  border-top: 1px solid var(--surface2);
  margin: 4px 0;
}

.voice-custom-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
}

.voice-custom-header span:first-child {
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
}

.voice-custom-help {
  font-size: 10px;
  color: var(--text-dim);
}

/* Upload zone */
.voice-upload-zone {
  border: 2px dashed var(--surface2);
  border-radius: 8px;
  padding: 16px;
  text-align: center;
  cursor: pointer;
  transition: all 0.2s;
  display: none;  /* Hidden by default, shown when "Upload new" selected */
}

.voice-upload-zone:hover,
.voice-upload-zone.dragover {
  border-color: var(--accent);
  background: rgba(0,0,0,0.05);
}

.voice-upload-prompt {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  color: var(--text-dim);
}

.voice-upload-prompt svg {
  opacity: 0.5;
}

.voice-upload-prompt span:first-child {
  font-size: 12px;
}

.voice-upload-btn {
  background: none;
  border: none;
  color: var(--accent);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  text-decoration: underline;
  padding: 0;
}

.voice-upload-hint {
  font-size: 10px;
  opacity: 0.6;
}

.voice-upload-status {
  margin-top: 8px;
  font-size: 11px;
  min-height: 16px;
}

.voice-upload-status.uploading {
  color: var(--detail);
}

.voice-upload-status.success {
  color: var(--success);
}

.voice-upload-status.error {
  color: var(--error);
}

/* Disabled bootstrap settings */
.voice-bootstrap-settings.disabled {
  opacity: 0.4;
  pointer-events: none;
}
```

## API Endpoint Specifications

### `POST /api/voices`

Upload a custom voice sample.

**Request:**
- `Content-Type: multipart/form-data`
- `file`: Audio file (required)
- `display_name`: Optional user-friendly name

**Response (200):**
```json
{
  "voiceId": "a1b2c3d4e5f6",
  "filename": "my_voice",
  "displayName": "My Voice",
  "duration": 5.2
}
```

**Errors:**
- `400`: Invalid file format, too large, too short, too long
- `401`: Not authenticated

### `GET /api/voices`

List all custom voices (shared across all profiles).

**Response (200):**
```json
{
  "voices": [
    {
      "voiceId": "a1b2c3d4e5f6",
      "filename": "my_voice",
      "displayName": "My Voice",
      "duration": 5.2,
      "uploadTime": 1785716896
    }
  ]
}
```

### `PATCH /api/voices/{voice_id}`

Update a voice sample's metadata (display name).

**Request:**
- `Content-Type: application/json`
- Body: `{"displayName": "New Name"}`

**Response (200):**
```json
{ "success": true }
```

**Errors:**
- `400`: No displayName provided
- `404`: Voice not found

### `DELETE /api/voices/{voice_id}`

Delete a custom voice sample.

**Response (200):**
```json
{ "success": true }
```

**Errors:**
- `404`: Voice not found

### `GET /api/voices/{voice_id}/audio`

Get the audio file for a voice sample (for preview playback).

**Response (200):**
- `Content-Type: audio/wav`
- Body: WAV file bytes

**Errors:**
- `404`: Voice not found

## File Format Requirements

### Supported Input Formats

| Format | Extension | Notes |
|--------|-----------|-------|
| WAV | `.wav` | Native, no conversion needed |
| MP3 | `.mp3` | Very common, requires decoding |
| FLAC | `.flac` | Lossless, good quality |
| M4A/AAC | `.m4a` | Common from iOS recordings |
| OGG | `.ogg` | Open format |
| WebM | `.webm` | Audio-only WebM |

All formats are decoded to **24kHz mono float32** internally, then normalized and stored as **16-bit PCM WAV at 24kHz** (OmniVoice native format).

### Audio Quality Guidelines

(Shown in UI via (?) help icon tooltip near upload zone)

- **Minimum duration**: 2 seconds (enough for voice characteristics)
- **Maximum duration**: 30 seconds (OmniVoice truncates at ~20 seconds internally)
- **Recommended**: 3–10 seconds of clear, natural speech
- **Sample rate**: Any input rate accepted; resampled to 24kHz
- **Channels**: Any input; converted to mono
- **Content**: Clear speech preferred; OmniVoice cleans up minor noise, but avoid loud background noise or music
- **Tip**: Speak naturally as you would in conversation; avoid reading from a script in a monotone way

### Validation

On upload:
1. Check file size ≤ 10MB
2. Check extension in allowed list
3. Decode audio and check duration (2–30 seconds)
4. Compute hash and check for duplicates

## Cache Key Algorithm

The cache key (`voice_id`) is a SHA-256 hash of the first 3 seconds of the normalized audio, converted to int16 PCM bytes:

```python
def compute_hash(waveform: np.ndarray) -> str:
    samples = int(3.0 * 24000)  # First 3 seconds at 24kHz
    segment = waveform[:samples]
    segment_int16 = np.clip(segment, -1.0, 1.0)
    segment_int16 = (segment_int16 * 32767).astype(np.int16)
    return hashlib.sha256(segment_int16.tobytes()).hexdigest()[:16]
```

**Rationale:**
- Uses the beginning of the sample (most representative, fast to hash)
- 3 seconds gives enough data for stable fingerprinting
- Normalized to int16 for consistent hashing regardless of float precision
- First 16 hex chars of SHA-256 is sufficient for uniqueness

**Deduplication:** If a user uploads the same file twice (or two files with identical first 3 seconds), they get the same `voice_id` and the existing cached voice is reused.

## UI Wireframe Description

### Voice Settings Dialog (Updated)

```
┌─────────────────────────────────────────────────────┐
│ Voice Settings                                [×]   │
├─────────────────────────────────────────────────────┤
│ Voice                                               │
│ ┌────────────────────────────────────────────────┐  │
│ │ Designed voice (adjust settings below)     ▼ │  │
│ │   ────────────────────────────────────────   │  │
│ │   My Voice (5.2s)                    [▶] [✕]│  │
│ │   Meeting Voice (3.8s)               [▶] [✕]│  │
│ │   Upload new voice...                    [?]│  │
│ └────────────────────────────────────────────────┘  │
│                                                     │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ Custom Voice Sample           Guidelines [?]        │
│                                                     │
│ ┌────────────────────────────────────────────────┐  │
│ │                                                │  │
│ │              Drop audio file here              │  │
│ │              or [browse files]                 │  │
│ │                                                │  │
│ │     WAV, MP3, FLAC, M4A, OGG · 2–30s · 10MB   │  │
│ │                                                │  │
│ └────────────────────────────────────────────────┘  │
│                                                     │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ Or record from microphone:                          │
│                                                     │
│              [🎤 Record]                             │
│                                                     │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ Name this voice: [My Voice________________]         │
│                                                     │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ Gender          [Female ▼]              (dimmed)    │
│ Age             [Young adult ▼]         (dimmed)    │
│ Pitch           [Moderate ▼]            (dimmed)    │
│ Accent          [American ▼]            (dimmed)    │
│ Style           [Normal ▼]              (dimmed)    │
│ Language        [English ▼]             (dimmed)    │
│ Speed           [0.8 ◄────► 1.25]       (dimmed)    │
│ Volume          [0 ◄────► 1]                          │
│                                                     │
│                                       [Reset] [Apply]│
└─────────────────────────────────────────────────────┘
```

### Recording UI (when "🎤 Record" is pressed)

```
┌────────────────────────────────────────────────┐
│                                                │
│        🎙️  Recording... 00:08                 │
│                                                │
│      ▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮          │
│                                                │
│              [⏹ Stop]                          │
│                                                │
│     Min 2s · Max 30s · Speak naturally         │
│                                                │
└────────────────────────────────────────────────┘
```

### Custom Voice List Item

Each custom voice in the selector shows:
- **Display name** (user-assigned, defaults to sanitized filename)
- **Duration** in seconds
- **Play button [▶]**: Preview the uploaded audio sample (fetches `/api/voices/{id}/audio`)
- **Delete button [✕]**: Delete the voice sample (shows confirmation dialog)

### Upload Zone

- Drag-and-drop zone with hover effect
- File picker button
- Format/duration/size hints inline
- **Guidelines (?) icon**: Tooltip with voice quality tips

### Recording Zone

- **🎤 Record button**: Starts microphone recording
- Shows elapsed timer while recording
- Simple waveform animation (optional)
- **⏹ Stop button**: Stops recording, auto-uploads
- Client-side duration validation (2-30s)
- Uses MediaRecorder API → WebM format → upload as `sample.webm`

### Name Input

- Appears below upload zone when "Upload new voice..." is selected
- Pre-filled with sanitized filename (for uploads) or empty (for recordings)
- Editable before/after upload
- Max 40 characters

### Voice Guidelines Tooltip (?)

```
┌─────────────────────────────────────────────┐
│ Voice Sample Guidelines                     │
│                                             │
│ • 3–10 seconds of clear speech works best   │
│ • Speak naturally as you would in           │
│   conversation                              │
│ • Avoid loud background noise or music      │
│ • OmniVoice handles accent, language, and   │
│   tone automatically from your sample       │
│ • Samples longer than 20s are truncated     │
└─────────────────────────────────────────────┘
```

### Delete Confirmation Dialog

```
┌─────────────────────────────────────────┐
│ Delete "My Voice"?                     │
│                                         │
│ This will permanently delete this       │
│ voice sample. This cannot be undone.    │
│                                         │
│                    [Cancel] [Delete]    │
└─────────────────────────────────────────┘
```

### Behavior

1. **Default**: "Designed voice" selected, all bootstrap controls enabled
2. **Select a custom voice**: bootstrap controls dim/disable, upload zone hidden, name input hidden
3. **Select "Upload new voice..."**: upload zone appears, recording zone appears, name input appears, bootstrap controls dim
4. **After upload**: new voice auto-selected in list, upload/recording zones hidden, name input hidden
5. **Play button**: Fetches WAV file from server, plays via Web Audio API
6. **Delete button**: Shows confirmation dialog, then calls DELETE endpoint
7. **Apply button**: prepares the selected voice (bootstrap or custom)
8. **Reset button**: Reverts to designed voice with current bootstrap settings
9. **Record button**: Requests mic permission, starts recording, shows timer
10. **Stop recording**: Validates duration client-side, uploads via existing endpoint

## Edge Cases and Error Handling

### Upload Errors

| Error | User Message |
|-------|--------------|
| File too large (>10MB) | "File too large. Maximum is 10MB." |
| Unsupported format | "Unsupported format. Please use WAV, MP3, FLAC, M4A, OGG, or WebM." |
| Audio too short (<2s) | "Audio too short (1.2s). Speak for at least 2 seconds." |
| Audio too long (>30s) | "Audio too long (45s). Use a shorter sample (max 30s)." |
| Corrupt file | "Failed to read audio file. Please try another file." |
| Duplicate upload | Silently returns existing voice (no error) |

### Voice Preparation Errors

| Error | User Message |
|-------|--------------|
| Voice file missing | "Voice sample not found. Please re-upload." |
| Model load failed | "Could not load voice model: [detail]" |
| OOM | "Not enough GPU memory for voice. Close other GPU apps or disable voice." |
| Invalid reference audio | "Voice sample is unusable. Try recording a clearer sample." |

### Runtime Errors

- If a custom voice fails to prepare, fall back to error state (same as bootstrap voice failure)
- Voice samples persist server-side; deleting them removes them from the list immediately
- If a user selects a custom voice that was deleted, show error on Apply

## Testing Plan (Test-Driven)

Follow TDD: write tests first, then implement to pass them.

### Test Fixtures

Use existing voice sample:
- `~/pi-chat/voices/voice-sample.m4a` — Real voice sample for integration/e2e tests

Generate synthetic test fixtures in `tests/fixtures/voices/`:
- `short.wav` — 1 second (too short, should fail)
- `valid.wav` — 5 seconds of speech (valid)
- `long.wav` — 35 seconds (too long, should fail)
- `valid.mp3` — 5 seconds (valid MP3)
- `valid.m4a` — 5 seconds (valid M4A)
- `valid.flac` — 5 seconds (valid FLAC)
- `valid.ogg` — 5 seconds (valid OGG)

Generate synthetic fixtures with a simple script using `uv run python` + numpy/soundfile.

**Important**: Tests should use synthetic fixtures for unit/integration tests. Use `voice-sample.m4a` only for manual/e2e voice quality verification.

### Unit Tests

#### 1. `tests/test_voice_store.py`

Test `VoiceStore` in isolation (no HTTP, no OmniVoice).

- **test_upload_valid_wav**: Upload valid WAV → returns VoiceSample with voice_id, display_name
- **test_upload_sets_display_name**: Upload with custom display_name → stored correctly
- **test_upload_defaults_display_name**: Upload without display_name → defaults to sanitized filename
- **test_upload_too_short**: Upload <2s audio → raises ValueError with duration
- **test_upload_too_long**: Upload >30s audio → raises ValueError with duration
- **test_upload_unsupported_format**: Upload .txt → raises ValueError
- **test_upload_too_large**: Upload >10MB → raises ValueError
- **test_upload_duplicate_returns_existing**: Upload same file twice → same voice_id, existing meta preserved
- **test_list_voices_empty**: List when no voices → returns []
- **test_list_voices**: Upload voices → list returns all with correct fields
- **test_get_voice_exists**: Get existing voice_id → returns VoiceSample
- **test_get_voice_missing**: Get non-existent voice_id → returns None
- **test_load_waveform**: Load waveform → returns (numpy array, 24000)
- **test_load_waveform_missing**: Load missing voice → raises ValueError
- **test_delete_voice**: Delete voice → files removed, get_voice returns None
- **test_delete_missing_voice**: Delete non-existent → returns False
- **test_update_display_name**: Update name → get_voice returns new name
- **test_update_display_name_missing**: Update missing voice → returns False
- **test_hash_stability**: Same audio content → same hash
- **test_hash_different_content**: Different audio → different hash

#### 2. `tests/test_tts_custom_voice.py`

Test custom voice path in TTSService with FakeOmniVoiceRuntime.

- **test_voice_key_custom**: VoiceSettings with voice_id → key starts with "custom:"
- **test_voice_key_bootstrap**: VoiceSettings without voice_id → key from design settings
- **test_prepare_custom_voice_calls_create_prompt**: prepare_voice with voice_id → calls create_voice_clone_prompt
- **test_prepare_custom_voice_no_instruct**: Custom voice prepared_voice has instruct=None
- **test_generate_custom_voice_no_instruct**: generate with custom voice → instruct not passed to model
- **test_generate_bootstrap_has_instruct**: generate with bootstrap voice → instruct passed to model

#### 3. `tests/test_voice_store_formats.py`

Test all supported upload formats decode correctly.

- **test_upload_mp3**: Upload valid.mp3 → stored as WAV, loadable
- **test_upload_m4a**: Upload valid.m4a → stored as WAV, loadable
- **test_upload_flac**: Upload valid.flac → stored as WAV, loadable
- **test_upload_ogg**: Upload valid.ogg → stored as WAV, loadable
- **test_upload_webm**: Upload valid.webm → stored as WAV, loadable
- **test_resample_different_sr**: Upload 44.1kHz WAV → stored at 24kHz
- **test_mono_conversion**: Upload stereo WAV → stored as mono

### Integration Tests

#### 4. `tests/test_voice_api.py`

Test API endpoints with FastAPI TestClient (no browser).

- **test_upload_voice_unauthenticated**: POST without auth → 401
- **test_upload_voice_valid**: POST valid WAV → 200 with voiceId, displayName, duration
- **test_upload_voice_with_name**: POST with display_name → 200 with correct displayName
- **test_upload_voice_too_short**: POST short audio → 400 with error message
- **test_upload_voice_unsupported_format**: POST .txt → 400
- **test_list_voices_empty**: GET with no voices → 200 with empty list
- **test_list_voices**: Upload then GET → returns uploaded voice
- **test_update_voice_name**: PATCH voice → 200, GET shows new name
- **test_update_voice_missing**: PATCH non-existent → 404
- **test_delete_voice**: Upload, DELETE → 200, GET no longer shows it
- **test_delete_voice_missing**: DELETE non-existent → 404
- **test_get_voice_audio**: GET /audio endpoint → 200 with WAV content
- **test_get_voice_audio_missing**: GET missing voice /audio → 404

#### 5. `tests/test_voice_e2e.py`

End-to-end with real TTSService (requires OmniVoice/torch). Mark as `@pytest.mark.slow` or skip by default.

- **test_full_flow_upload_prepare**: Upload voice → voice_prepare with voice_id → succeeds
- **test_full_flow_synthesize**: Upload voice → voice_prepare → synthesize → returns PCM
- **test_custom_voice_ignores_settings**: Prepare with voice_id + random settings → same result

### Fake Runtime Updates

#### 6. Extend `tests/fakes/voice.py`

- **FakeVoiceStore**: In-memory store for testing without disk I/O
- **FakeOmniVoiceRuntime**: Support custom voice path — when create_voice_clone_prompt called, track it

#### 7. `tests/test_voice_store_fake.py`

- Test FakeVoiceStore matches real VoiceStore interface

### Browser/Manual Tests

#### 8. Manual testing checklist

- Upload voice via drag-and-drop → shows success, appears in list
- Upload voice via file picker → same
- Record voice via microphone → shows timer, uploads on stop
- Enter custom name on upload → saved correctly
- Edit voice name after upload → saved correctly
- Play button on voice item → plays audio sample
- Delete button → shows confirmation → deletes on confirm
- Select custom voice → bootstrap controls dim
- Select designed voice → bootstrap controls enable
- Apply custom voice → voice mode uses cloned voice
- Switch between custom and bootstrap voices
- Upload same file twice → deduplicated (same voice_id)
- Guidelines (?) tooltip shows on hover
- All file formats: WAV, MP3, FLAC, M4A, OGG, WebM

## Design Decisions (Resolved)

1. **Shared voices**: All voices stored in `~/.pi/voices/`, shared across b and r profiles.
2. **No transcription**: `ref_text=None` for now. Will revisit when voice input is supported.
3. **User naming**: User can name each uploaded voice (defaults to sanitized filename). Editable after upload.
4. **Delete confirmation**: Confirmation dialog before deleting a voice.
5. **Voice preview**: Play button fetches WAV from server and plays via Web Audio API.
6. **Cache limit**: Custom voices share the same cache limit as bootstrap voices (8 total). LRU eviction applies.
7. **No cleanup**: All uploaded voices persist until user deletes them.
8. **No quality scoring**: OmniVoice handles cleanup/truncation. User educated via guidelines tooltip.
9. **Microphone recording**: In-browser recording via MediaRecorder API. Same upload flow as file upload.

## Remaining Open Questions

1. **Per-voice rename UI**: Should rename be inline (click name → edit) or via a separate dialog?
2. **Voice limit UX**: If cache is full (8 voices), should we auto-evict LRU or show an error asking user to delete first?
3. **Recording format**: WebM (native MediaRecorder) or WAV (requires conversion in browser)? WebM is simpler but adds a backend decode path.
