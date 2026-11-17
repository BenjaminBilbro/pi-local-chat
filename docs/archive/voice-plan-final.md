> **Status, archived 2026-11-17:** the OmniVoice-era voice plan that called itself final. Superseded by `docs/phonon-tts-engine.md`. Useful as a record of what was considered and why it was rejected.

# Custom Voice Cloning — Final Implementation Plan

**Version:** 2.0 (review-integrated)  
**Date:** 2026-06-01  
**Based on:** custom-voice-cloning.md + adversarial review (52 issues addressed)  

This is the authoritative implementation plan. All critical and high-priority review findings have been incorporated as concrete tasks with corrected code.

---

## 1. Overview & User Story

Allow pi-chat users to upload their own voice sample files and use them for OmniVoice cloning, bypassing the existing voice design parameters (gender/age/pitch/accent). OmniVoice already supports reference-audio-based cloning; this feature exposes that capability through the pi-chat UI.

### User Story

As a user, I want to upload a short audio clip of my voice, optionally name it, and have pi-chat speak in my voice instead of the default bootstrapped voices. Custom voices are shared across all pi-chat profiles (b and r), persist across sessions, and are selectable from the voice settings menu.

### Review Findings

- **MED-1:** Voices shared across profiles is intentional but may be a privacy concern if profiles represent different people.
- **MED-2:** No ethical documentation for voice cloning.

### Implementation Tasks

[MEDIUM] T1: Add voice cloning ethical usage note to guidelines tooltip
- File: static/index.html
- Fix: Add "Only clone voices you have permission to use" to guidelines
- Addresses: MED-2
- Acceptance: Guidelines tooltip includes consent reminder

---

## 2. Architecture & Data Flow

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

### WebSocket Integration Flow

**Frontend → Backend (voice_prepare with custom voice):**

1. User selects custom voice in settings dialog, clicks Apply
2. voice.js calls `getBackendSettings()` → returns `{voice_id: "abc123...", ...}`
3. voice.js sends WebSocket command:
   ```json
   {"type": "voice_prepare", "settings": {"voice_id": "abc123...", "language": "English", ...}}
   ```
4. websocket.py validates via `validate_settings()` (must accept voice_id)
5. VoiceSession calls `TTSService.prepare_voice(settings)`
6. TTSService detects `settings.voice_id` → calls `_prepare_custom_voice()`
7. `_prepare_custom_voice()` uses **injected** VoiceStore to load waveform
8. OmniVoice `create_voice_clone_prompt()` called with reference audio
9. VoiceHandle returned, cached with key `custom:abc123...`

**Frontend → Backend (synthesis with custom voice):**

1. agent_start received, VoiceSession begins streaming
2. VoiceSession calls `TTSService.synthesize(chunk, prepared_voice)`
3. `prepared_voice.instruct` is None → instruct NOT passed to model
4. `language` is ALWAYS passed (user's language preference)
5. PCM frames streamed to browser via WebSocket binary messages

### Key Insight

Custom voice cloning skips voice design entirely:
1. Load user's reference audio file
2. Create `VoiceClonePrompt` directly via `model.create_voice_clone_prompt(ref_audio=(waveform, sr), ref_text=None)`
3. Use that prompt for all synthesis

### Review Findings

- **CRIT-2:** VoiceStore was double-instantiated — must be injected into TTSService
- **CRIT-3:** language=None for custom voices breaks OmniVoice — always pass language
- **HIGH-4:** VoiceSettings defined in tests/fakes/voice.py — all consumers must be updated
- **HIGH-5:** WebSocket command flow was undocumented

### Implementation Tasks

[CRITICAL] T2: Inject VoiceStore into TTSService constructor
- File: pi_chat/tts_service.py
- Fix: Accept VoiceStore as constructor parameter
- Addresses: CRIT-2
- Acceptance: TTSService uses injected VoiceStore, not a new instance

[CRITICAL] T3: Always pass language to OmniVoice.generate()
- File: pi_chat/tts_service.py
- Fix: Use `language=settings.language` always, never None
- Addresses: CRIT-3
- Acceptance: Custom voice synthesis passes language parameter

[HIGH] T4: Update all VoiceSettings consumers
- Files: tests/fakes/voice.py, pi_chat/tts_service.py, pi_chat/voice_session.py
- Fix: Add voice_id field to all VoiceSettings definitions
- Addresses: HIGH-4
- Acceptance: No VoiceSettings instance missing voice_id field

[HIGH] T5: Document WebSocket command flow
- File: This document (section 2)
- Fix: Added WebSocket Integration Flow subsection
- Addresses: HIGH-5
- Acceptance: Flow from frontend voice_prepare to backend synthesis is fully documented

---

## 3. Backend Implementation

### 3.1 `pi_chat/voice_store.py` (NEW)

Manages custom voice sample storage, validation, and metadata.

#### Review Findings (addressed in code below)

- **CRIT-5:** No MIME/waveform validation — only extension checked
- **CRIT-6:** TOCTOU race condition on duplicate upload
- **CRIT-7:** No atomic file writes — corruption on crash
- **CRIT-9:** Voice file deleted externally — zombie voices
- **HIGH-1:** No storage quota — disk exhaustion possible
- **HIGH-17:** uploadTime uses monotonic() — meaningless timestamp
- **MED-4:** No display_name length limit
- **MED-5:** Glob pattern could match unexpected files

#### Corrected Code

```python
"""Custom voice sample storage and management."""

import hashlib
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

# Supported input formats (handled by librosa or soundfile)
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".webm"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_TOTAL_STORAGE = 500 * 1024 * 1024  # 500MB total limit (HIGH-1)
MIN_DURATION = 2.0   # seconds
MAX_DURATION = 30.0  # seconds
TARGET_SAMPLE_RATE = 24000  # OmniVoice native rate
MAX_DISPLAY_NAME_LENGTH = 40  # (MED-4)

# Number of seconds from start to hash for cache key
HASH_WINDOW_SECONDS = 3.0

# Valid voice_id pattern: 16 hex chars (MED-5)
VOICE_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True)
class VoiceSample:
    """Metadata for a stored voice sample."""
    voice_id: str          # SHA-256 hash of audio content
    filename: str          # Original filename (sanitized)
    display_name: str      # User-assigned name (defaults to sanitized filename)
    duration: float        # Duration in seconds
    upload_time: float     # Unix timestamp (wall clock)
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
        self._lock = threading.Lock()  # (CRIT-6) Serialize uploads

    def upload(self, file_bytes: bytes, original_filename: str, display_name: str | None = None) -> VoiceSample:
        """Validate, normalize, and store an uploaded voice sample."""
        # Validate file size
        if len(file_bytes) > MAX_FILE_SIZE:
            raise ValueError(f"File too large. Maximum is 10MB.")

        # Validate extension
        ext = Path(original_filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Unsupported format '{ext}'. Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        # Default display name
        if display_name is None:
            display_name = self._sanitize_filename(original_filename)
        
        # Enforce display name length (MED-4)
        display_name = display_name[:MAX_DISPLAY_NAME_LENGTH]

        # Load and validate audio
        waveform, duration = self._load_audio(file_bytes, ext)

        # Validate waveform is plausible (CRIT-5)
        self._validate_waveform(waveform)

        # Validate duration
        if duration < MIN_DURATION:
            raise ValueError(
                f"Audio too short ({duration:.1f}s). Speak for at least {MIN_DURATION}s."
            )
        if duration > MAX_DURATION:
            raise ValueError(
                f"Audio too long ({duration:.1f}s). Use a shorter sample (max {MAX_DURATION}s)."
            )

        # Compute cache key
        voice_id = self._compute_hash(waveform)

        # Atomic upload with lock (CRIT-6)
        with self._lock:
            # Check if this voice already exists
            existing_meta = self._meta_path(voice_id)
            if existing_meta.exists():
                logger.info("Voice %s already exists (duplicate upload), returning existing", voice_id[:8])
                return self._load_meta(voice_id)

            # Check total storage quota (HIGH-1)
            self._check_storage_quota()

            # Normalize and store atomically (CRIT-7)
            normalized = self._normalize(waveform)
            wav_path = self._wav_path(voice_id)
            self._save_wav_atomic(wav_path, normalized, TARGET_SAMPLE_RATE)

            # Store metadata atomically
            sanitized_filename = self._sanitize_filename(original_filename)
            sample = VoiceSample(
                voice_id=voice_id,
                filename=sanitized_filename,
                display_name=display_name,
                duration=duration,
                upload_time=time.time(),  # (HIGH-17) Wall clock, not monotonic
                waveform=None,
            )
            self._save_meta_atomic(voice_id, sample)

        logger.info("Stored voice %s (%.1fs, %d bytes wav)", voice_id[:8], duration, wav_path.stat().st_size)
        return sample

    def list_voices(self) -> list[VoiceSample]:
        """List all stored voice samples. Skips voices with missing WAV files (CRIT-9)."""
        samples = []
        for meta_file in Path(self._voice_dir).glob("*.meta.json"):
            voice_id = meta_file.stem
            # (MED-5) Validate voice_id format
            if not VOICE_ID_PATTERN.match(voice_id):
                continue
            try:
                sample = self._load_meta(voice_id)
                # (CRIT-9) Integrity check: skip if WAV missing
                if not self._wav_path(voice_id).exists():
                    logger.warning("Voice %s has missing WAV file, skipping", voice_id[:8])
                    continue
                samples.append(sample)
            except Exception as e:
                logger.warning("Failed to load meta for %s: %s", voice_id, e)
        return samples

    def get_voice(self, voice_id: str) -> VoiceSample | None:
        """Get a voice sample by ID."""
        if not VOICE_ID_PATTERN.match(voice_id):
            return None
        meta_path = self._meta_path(voice_id)
        if not meta_path.exists():
            return None
        # Also check WAV exists (CRIT-9)
        if not self._wav_path(voice_id).exists():
            return None
        return self._load_meta(voice_id)

    def load_waveform(self, voice_id: str) -> tuple[Any, int]:
        """Load the waveform for a voice sample."""
        sample = self.get_voice(voice_id)
        if sample is None:
            raise ValueError(f"Voice {voice_id} not found")

        wav_path = self._wav_path(voice_id)
        if not wav_path.exists():
            raise ValueError(f"WAV file missing for voice {voice_id}")

        import soundfile as sf
        waveform, sr = sf.read(str(wav_path), dtype="float32")
        return waveform, sr

    def delete_voice(self, voice_id: str) -> bool:
        """Delete a voice sample and its files."""
        if not VOICE_ID_PATTERN.match(voice_id):
            return False
        deleted = False
        for path in [self._wav_path(voice_id), self._meta_path(voice_id)]:
            if path.exists():
                path.unlink()
                deleted = True
        return deleted

    def update_display_name(self, voice_id: str, display_name: str) -> bool:
        """Update the user-friendly display name for a voice sample."""
        if not VOICE_ID_PATTERN.match(voice_id):
            return False
        sample = self.get_voice(voice_id)
        if sample is None:
            return False
        # Enforce length (MED-4)
        display_name = display_name[:MAX_DISPLAY_NAME_LENGTH]
        new_sample = VoiceSample(
            voice_id=sample.voice_id,
            filename=sample.filename,
            display_name=display_name,
            duration=sample.duration,
            upload_time=sample.upload_time,
            waveform=None,
        )
        self._save_meta_atomic(voice_id, new_sample)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_waveform(self, waveform: Any) -> None:
        """Validate waveform is plausible audio (CRIT-5)."""
        import numpy as np
        if len(waveform) == 0:
            raise ValueError("Audio file is empty or unreadable.")
        if not np.all(np.isfinite(waveform)):
            raise ValueError("Audio contains invalid values.")
        max_val = np.max(np.abs(waveform))
        if max_val == 0:
            raise ValueError("Audio is silent. Please speak into the recording.")
        if max_val > 2.0:
            raise ValueError("Audio has abnormal amplitude. Try another file.")

    def _check_storage_quota(self) -> None:
        """Check total storage hasn't exceeded MAX_TOTAL_STORAGE (HIGH-1)."""
        total = sum(p.stat().st_size for p in Path(self._voice_dir).iterdir() if p.is_file())
        if total > MAX_TOTAL_STORAGE:
            raise ValueError(
                f"Voice storage quota exceeded ({total / (1024*1024):.0f}MB / {MAX_TOTAL_STORAGE / (1024*1024):.0f}MB). "
                "Delete some voices and try again."
            )

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
            if sr != TARGET_SAMPLE_RATE:
                import librosa
                waveform = librosa.resample(waveform, orig_sr=sr, target_sr=TARGET_SAMPLE_RATE)
                sr = TARGET_SAMPLE_RATE
        except Exception as e:
            raise ValueError(f"Failed to read audio file: {e}")

        duration = len(waveform) / sr
        return waveform, duration

    def _compute_hash(self, waveform: Any) -> str:
        """Compute a stable hash from the first HASH_WINDOW_SECONDS of audio."""
        import numpy as np
        samples = int(HASH_WINDOW_SECONDS * TARGET_SAMPLE_RATE)
        segment = waveform[:samples]
        segment_int16 = np.clip(segment, -1.0, 1.0)
        segment_int16 = (segment_int16 * 32767).astype(np.int16)
        return hashlib.sha256(segment_int16.tobytes()).hexdigest()[:16]

    def _normalize(self, waveform: Any) -> Any:
        """Normalize waveform to [-1, 1] range."""
        import numpy as np
        max_val = np.max(np.abs(waveform))
        if max_val > 0:
            waveform = waveform / max_val * 0.95
        return waveform

    def _save_wav_atomic(self, path: Path, waveform: Any, sr: int) -> None:
        """Save waveform as 16-bit mono WAV using atomic write (CRIT-7)."""
        import numpy as np
        import soundfile as sf
        samples = (np.clip(waveform, -1.0, 1.0) * 32767).astype(np.int16)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".wav.tmp")
        try:
            with os.fdopen(fd, 'wb') as f:
                sf.write(f, samples, sr, format="WAV", subtype="PCM_16")
            os.replace(tmp_path, path)  # atomic
        except:
            os.close(fd) if not fd else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _save_meta_atomic(self, voice_id: str, sample: VoiceSample) -> None:
        """Save metadata JSON using atomic write (CRIT-7)."""
        meta_path = self._meta_path(voice_id)
        meta = {
            "voice_id": sample.voice_id,
            "filename": sample.filename,
            "display_name": sample.display_name,
            "duration": sample.duration,
            "upload_time": sample.upload_time,
        }
        fd, tmp_path = tempfile.mkstemp(dir=str(meta_path.parent), suffix=".json.tmp")
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp_path, meta_path)
        except:
            os.close(fd) if not fd else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _load_meta(self, voice_id: str) -> VoiceSample:
        """Load metadata JSON."""
        meta_path = self._meta_path(voice_id)
        meta = json.loads(meta_path.read_text())
        return VoiceSample(
            voice_id=meta["voice_id"],
            filename=meta["filename"],
            display_name=meta.get("display_name", meta["filename"]),
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

### Implementation Tasks (voice_store.py)

[CRITICAL] T6: Add waveform validation after decode
- File: pi_chat/voice_store.py
- Fix: Add `_validate_waveform()` checking emptiness, finiteness, amplitude
- Addresses: CRIT-5
- Acceptance: Corrupt/empty audio files rejected with clear error

[CRITICAL] T7: Add threading lock for uploads
- File: pi_chat/voice_store.py
- Fix: Wrap check-then-write in `self._lock`
- Addresses: CRIT-6
- Acceptance: Concurrent duplicate uploads don't corrupt files

[CRITICAL] T8: Use atomic file writes
- File: pi_chat/voice_store.py
- Fix: Write to temp file, then `os.replace()`
- Addresses: CRIT-7
- Acceptance: Crash mid-write leaves no corrupt files

[CRITICAL] T9: Integrity check in list_voices
- File: pi_chat/voice_store.py
- Fix: Skip voices where WAV file is missing
- Addresses: CRIT-9
- Acceptance: Externally deleted voices don't appear in list

[HIGH] T10: Add storage quota
- File: pi_chat/voice_store.py
- Fix: Check total size before accepting new upload
- Addresses: HIGH-1
- Acceptance: Upload rejected when total exceeds 500MB

[HIGH] T11: Use time.time() instead of monotonic()
- File: pi_chat/voice_store.py
- Fix: Change `__import__("time").monotonic()` to `time.time()`
- Addresses: HIGH-17
- Acceptance: uploadTime is a meaningful Unix timestamp

---

### 3.2 `pi_chat/config.py` (MODIFIED)

Add voice sample storage configuration.

```python
# Custom voice sample storage
VOICE_SAMPLES_DIR = os.environ.get(
    "PI_CHAT_VOICE_SAMPLES_DIR",
    os.path.join(os.path.expanduser("~/.pi"), "voices"),
)

# Feature flag for custom voices (HIGH-18)
TTS_CUSTOM_VOICES_ENABLED = os.environ.get("PI_CHAT_TTS_CUSTOM_VOICES_ENABLED", "1").lower() in ("1", "true", "yes")
```

#### Review Findings

- **HIGH-18:** No feature flag to disable custom voices
- **HIGH-19:** VOICE_SAMPLES_DIR not validated at import time

### Implementation Tasks (config.py)

[HIGH] T12: Add feature flag for custom voices
- File: pi_chat/config.py
- Fix: Add `TTS_CUSTOM_VOICES_ENABLED` env var
- Addresses: HIGH-18
- Acceptance: Custom voices can be disabled via environment variable

[HIGH] T13: Validate VOICE_SAMPLES_DIR at startup
- File: pi_chat/config.py or app.py
- Fix: Check directory is writable at import/startup
- Addresses: HIGH-19
- Acceptance: Startup fails fast if voice directory is unwritable

---

### 3.3 `pi_chat/tts_service.py` (MODIFIED)

#### Review Findings

- **CRIT-2:** VoiceStore was double-instantiated
- **CRIT-3:** language=None breaks OmniVoice
- **HIGH-6:** generate() language=None may break OmniVoice contract
- **HIGH-8:** prepare_voice() not designed for VoiceStore integration

#### Corrected Code Changes

1. **VoiceSettings with voice_id:**

```python
@dataclass(frozen=True)
class VoiceSettings:
    gender: str = "female"
    age: str = "young adult"
    pitch: str = "moderate pitch"
    accent: str = "american accent"
    style: str | None = None
    speed: float = 1.0
    language: str = "English"
    voice_id: str | None = None  # Custom voice sample ID
```

2. **TTSService constructor with injected VoiceStore (CRIT-2):**

```python
def __init__(self, config_: Any | None = None, voice_store: Any | None = None):
    self._config = config_ or config
    self._voice_store = voice_store or VoiceStore(self._config)  # Injected, not new
    # ... rest unchanged
```

3. **_voice_key with custom voice support:**

```python
def _voice_key(self, settings: VoiceSettings) -> str:
    if settings.voice_id:
        return f"custom:{settings.voice_id}"
    raw = f"{settings.gender}|{settings.age}|{settings.pitch}|{settings.accent}|{settings.style}|{settings.speed}|{settings.language}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

4. **prepare_voice with custom voice path (using injected store):**

```python
def prepare_voice(self, settings: VoiceSettings, bootstrap_text: str) -> object:
    if settings.voice_id:
        return self._prepare_custom_voice(settings)
    return self._prepare_bootstrap_voice(settings, bootstrap_text)

def _prepare_custom_voice(self, settings: VoiceSettings) -> object:
    """Prepare a voice clone prompt from a user-uploaded reference audio."""
    import torch

    # Use injected VoiceStore (CRIT-2)
    waveform, sr = self._voice_store.load_waveform(settings.voice_id)

    logger.info("TTS: _prepare_custom_voice() voice_id=%s waveform_len=%d sr=%d",
                settings.voice_id[:8], len(waveform), sr)

    voice_clone_prompt = self._model.create_voice_clone_prompt(
        ref_audio=(torch.from_numpy(waveform), sr),
        ref_text=None,
    )

    return {
        "voice_clone_prompt": voice_clone_prompt,
        "instruct": None,
        "voice_id": settings.voice_id,
    }
```

5. **generate() always passes language (CRIT-3):**

```python
def generate(self, text: str, prepared_voice: object, settings: VoiceSettings) -> tuple[object, int]:
    voice_clone_prompt = prepared_voice.get("voice_clone_prompt")
    instruct = prepared_voice.get("instruct")

    kw = {
        "text": text,
        "voice_clone_prompt": voice_clone_prompt,
        "language": settings.language,  # ALWAYS pass language (CRIT-3)
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

### Implementation Tasks (tts_service.py)

[CRITICAL] T14: Inject VoiceStore into TTSService
- File: pi_chat/tts_service.py
- Fix: Accept voice_store parameter in __init__, use self._voice_store
- Addresses: CRIT-2, HIGH-8
- Acceptance: No VoiceStore() calls inside _prepare_custom_voice

[CRITICAL] T15: Always pass language to generate()
- File: pi_chat/tts_service.py
- Fix: Use `language=settings.language` unconditionally
- Addresses: CRIT-3, HIGH-6
- Acceptance: generate() never passes language=None

[HIGH] T16: Update FakeOmniVoiceRuntime for custom voices
- File: tests/fakes/voice.py
- Fix: Support create_voice_clone_prompt in fake runtime
- Addresses: HIGH-4
- Acceptance: Fake runtime tracks custom voice preparation

---

### 3.4 `pi_chat/app.py` (MODIFIED)

#### Review Findings

- **CRIT-4:** No FastAPI-level file size limit — DoS vulnerability
- **HIGH-15:** Upload decode blocks event loop

#### Corrected Code

```python
from .voice_store import VoiceStore, ALLOWED_EXTENSIONS, MAX_FILE_SIZE

# In create_app():
voice_store = VoiceStore()

@app.post("/api/voices")
async def upload_voice(request: Request, file: UploadFile, display_name: str | None = None):
    """Upload a custom voice sample."""
    _require_account(request, auth)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # (CRIT-4) Check Content-Length before reading
    content_length = file.size if hasattr(file, 'size') else None
    if content_length and content_length > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail="File too large. Maximum is 10MB."
        )

    content = await file.read()
    
    # (HIGH-15) Run upload in thread pool to avoid blocking event loop
    loop = asyncio.get_running_loop()
    try:
        sample = await loop.run_in_executor(
            None,
            lambda: voice_store.upload(content, file.filename, display_name)
        )
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
                "uploadTime": int(s.upload_time),  # (HIGH-17) Integer timestamp
            }
            for s in samples
        ]
    }

@app.patch("/api/voices/{voice_id}")
async def update_voice(request: Request, voice_id: str):
    """Update a voice sample's metadata (display name)."""
    _require_account(request, auth)

    # (HIGH-7) Validate JSON body properly
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    display_name = body.get("displayName")
    if not isinstance(display_name, str) or not display_name:
        raise HTTPException(status_code=400, detail="displayName must be a non-empty string")

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

    wav_path = voice_store._wav_path(voice_id)
    if not wav_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    from fastapi.responses import FileResponse
    return FileResponse(wav_path, media_type="audio/wav")
```

### Implementation Tasks (app.py)

[CRITICAL] T17: Add FastAPI-level file size limit
- File: pi_chat/app.py
- Fix: Check Content-Length before await file.read()
- Addresses: CRIT-4
- Acceptance: Large uploads rejected before reading into memory

[HIGH] T18: Run upload in thread pool
- File: pi_chat/app.py
- Fix: Use loop.run_in_executor for voice_store.upload()
- Addresses: HIGH-15
- Acceptance: Upload decode doesn't block event loop

[HIGH] T19: Validate PATCH body properly
- File: pi_chat/app.py
- Fix: Check JSON parsing and displayName type
- Addresses: HIGH-7
- Acceptance: Invalid PATCH body returns 400 with clear message

---

### 3.5 `pi_chat/voice_session.py` (MODIFIED)

#### Review Findings

- **CRIT-1:** validate_settings rejects voice_id — custom voices will never work
- **CRIT-8:** No timeout on voice preparation — can hang indefinitely

#### Corrected Code

```python
def validate_settings(raw: dict) -> VoiceSettings | None:
    try:
        # (CRIT-1) voice_id MUST be in allowed_keys
        allowed_keys = {"gender", "age", "pitch", "accent", "style", "speed", "language", "voice_id"}
        if not set(raw.keys()).issubset(allowed_keys):
            return None

        voice_id = raw.get("voice_id")
        if voice_id is not None and not isinstance(voice_id, str):
            return None
        # Validate voice_id format if present
        if voice_id is not None and not re.match(r"^[0-9a-f]{16}$", voice_id):
            return None

        return VoiceSettings(
            gender=raw.get("gender", "female"),
            age=raw.get("age", "young adult"),
            pitch=raw.get("pitch", "moderate pitch"),
            accent=raw.get("accent", "american accent"),
            style=raw.get("style"),
            speed=float(raw.get("speed", 1.0)),
            language=raw.get("language", "English"),
            voice_id=voice_id,  # (CRIT-1) Pass voice_id to VoiceSettings
        )
    except (TypeError, ValueError):
        return None
```

**Timeout wrapper in websocket.py (CRIT-8):**

```python
async def handle_voice_prepare(ws, voice_session, settings):
    """Prepare voice with timeout protection (CRIT-8)."""
    try:
        prepared = await asyncio.wait_for(
            voice_session.prepare_voice(settings),
            timeout=60.0  # 60 second timeout
        )
        return prepared
    except asyncio.TimeoutError:
        raise RuntimeError("Voice preparation timed out. Try again or use a shorter sample.")
```

### Implementation Tasks (voice_session.py)

[CRITICAL] T20: Add voice_id to validate_settings allowed_keys
- File: pi_chat/voice_session.py
- Fix: Add "voice_id" to allowed_keys set
- Addresses: CRIT-1
- Acceptance: validate_settings({"voice_id": "abc123def456"}) returns valid VoiceSettings

[CRITICAL] T21: Add timeout on voice preparation
- File: pi_chat/websocket.py
- Fix: Wrap prepare_voice in asyncio.wait_for with 60s timeout
- Addresses: CRIT-8
- Acceptance: Hanging voice preparation doesn't block indefinitely

[HIGH] T22: Validate voice_id format in validate_settings
- File: pi_chat/voice_session.py
- Fix: Check voice_id matches hex pattern if present
- Addresses: CRIT-1 (extension)
- Acceptance: Invalid voice_id format rejected

---

## 4. Frontend Implementation

### 4.1 `static/index.html` (MODIFIED)

Add custom voice upload section to the voice settings dialog.

#### Review Findings

- **CRIT-13:** Upload zone not keyboard-accessible

#### Corrected Code

```html
<!-- Inside voice-settings-dialog, after existing controls, before actions -->
<div class="voice-settings-divider"></div>
<div class="voice-custom-section">
  <div class="voice-custom-header">
    <span>Custom Voice Sample</span>
    <div class="voice-guidelines-tooltip">
      <button type="button" class="voice-help-icon" title="Voice sample guidelines" aria-label="Voice sample guidelines">?</button>
    </div>
  </div>

  <!-- Voice selector: bootstrapped OR custom -->
  <label class="voice-setting-label">
    <span>Voice</span>
    <select id="voice-type" aria-label="Select voice type">
      <option value="bootstrap">Designed voice (adjust settings below)</option>
      <optgroup label="Custom voices" id="voice-custom-optgroup">
        <!-- Populated dynamically -->
      </optgroup>
    </select>
  </label>

  <!-- Custom voice actions row -->
  <div class="voice-custom-actions" id="voice-custom-actions" style="display:none">
    <button type="button" class="voice-preview-btn" title="Preview this voice sample" aria-label="Play voice preview">▶ Play</button>
    <button type="button" class="voice-delete-btn" title="Delete this voice sample" aria-label="Delete voice">✕ Delete</button>
    <button type="button" class="voice-rename-btn" title="Rename this voice" aria-label="Rename voice">✎ Rename</button>
  </div>

  <!-- Upload zone (CRIT-13: keyboard accessible) -->
  <div class="voice-upload-zone" id="voice-upload-zone" 
       style="display:none" 
       tabindex="0" 
       role="button" 
       aria-label="Upload audio file. Drop file here or press Enter to browse.">
    <div class="voice-upload-prompt">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <span>Drop audio file here or <button class="voice-upload-btn" type="button">browse</button></span>
      <span class="voice-upload-hint">WAV, MP3, FLAC, M4A, OGG, WebM · 2–30 seconds · max 10MB</span>
    </div>
    <input type="file" id="voice-upload-input" accept=".wav,.mp3,.flac,.m4a,.ogg,.webm" aria-label="Select audio file to upload" style="display:none">
    <div class="voice-upload-status" id="voice-upload-status" aria-live="polite"></div>
  </div>

  <!-- Recording zone -->
  <div class="voice-recording-zone" id="voice-recording-zone" style="display:none">
    <div class="voice-recording-prompt">
      <span>Or record from microphone:</span>
    </div>
    <div class="voice-recording-controls">
      <button type="button" id="record-btn" class="voice-record-btn" aria-label="Start recording">🎤 Record</button>
      <div class="voice-recording-active" id="voice-recording-active" style="display:none">
        <span id="recording-timer" aria-live="polite">Recording... 00:00</span>
        <button type="button" id="stop-record-btn" class="voice-stop-btn" aria-label="Stop recording">⏹ Stop</button>
      </div>
    </div>
  </div>

  <!-- Name input -->
  <div class="voice-name-input" id="voice-name-input" style="display:none">
    <label class="voice-setting-label">
      <span>Name this voice</span>
      <input type="text" id="voice-display-name" maxlength="40" placeholder="My Voice" aria-label="Voice display name">
    </label>
  </div>

  <!-- Bootstrap settings (disabled when custom voice selected) -->
  <div class="voice-bootstrap-settings" id="voice-bootstrap-settings">
    <!-- Existing gender/age/pitch/accent/style/language/speed controls -->
  </div>
</div>

<!-- Guidelines tooltip popup -->
<div class="voice-guidelines-popup" id="voice-guidelines-popup" role="dialog" aria-label="Voice sample guidelines" style="display:none">
  <div class="voice-guidelines-title">Voice Sample Guidelines</div>
  <ul>
    <li>3–10 seconds of clear speech works best</li>
    <li>Speak naturally as you would in conversation</li>
    <li>Avoid loud background noise or music</li>
    <li>OmniVoice handles accent, language, and tone automatically</li>
    <li>Samples longer than 20s are truncated</li>
    <li>Only clone voices you have permission to use</li>
  </ul>
</div>
```

### Implementation Tasks (index.html)

[CRITICAL] T23: Make upload zone keyboard-accessible
- File: static/index.html
- Fix: Add tabindex="0", role="button", aria-label to upload zone
- Addresses: CRIT-13
- Acceptance: Keyboard-only users can activate upload zone with Tab+Enter

[MEDIUM] T24: Add ethical usage note to guidelines
- File: static/index.html
- Fix: Add "Only clone voices you have permission to use" to tooltip
- Addresses: MED-2
- Acceptance: Guidelines include consent reminder

---

### 4.2 `static/voice.js` (MODIFIED)

#### Review Findings

- **CRIT-10:** voiceType not persisted — custom voice selection lost on reload
- **CRIT-11:** No debounce on upload — concurrent uploads corrupt state
- **CRIT-12:** Microphone recording cleanup failure
- **HIGH-9:** previewVoice() creates Audio objects without cleanup
- **HIGH-10:** recordingTimer not cleared on page unload
- **HIGH-11:** Drag-and-drop accepts non-file drops
- **HIGH-12:** No client-side file validation

#### Corrected Code

1. **State tracking with persistence (CRIT-10):**

```javascript
function defaultSettings() {
  return {
    voiceType: 'bootstrap',  // 'bootstrap' or 'custom:<voice_id>'
    gender: 'female',
    age: 'young adult',
    pitch: 'moderate pitch',
    accent: 'american accent',
    style: null,
    speed: 1.0,
    language: 'English',
  };
}

function loadSettings() {
  try {
    const saved = localStorage.getItem('voice-settings');
    if (saved) {
      const parsed = JSON.parse(saved);
      // (CRIT-10) Validate voiceType on load
      if (!parsed.voiceType || !['bootstrap', 'custom-upload'].includes(parsed.voiceType) && !parsed.voiceType.startsWith('custom:')) {
        parsed.voiceType = 'bootstrap';
      }
      return { ...defaultSettings(), ...parsed };
    }
  } catch (e) {
    console.warn('Failed to load voice settings:', e);
  }
  return defaultSettings();
}

function saveSettings() {
  try {
    localStorage.setItem('voice-settings', JSON.stringify(settings));
  } catch (e) {
    console.warn('Failed to save voice settings:', e);
  }
}
```

2. **Upload with debounce guard (CRIT-11) and client-side validation (HIGH-12):**

```javascript
let uploadInProgress = false;  // (CRIT-11) Debounce flag

async function uploadVoiceFile(file) {
  // (CRIT-11) Reject concurrent uploads
  if (uploadInProgress) {
    showUploadStatus('Upload already in progress. Please wait.', 'error');
    return;
  }

  // (HIGH-12) Client-side file validation
  if (file.size > 10 * 1024 * 1024) {
    showUploadStatus('File too large. Maximum is 10MB.', 'error');
    return;
  }
  
  const allowedTypes = ['audio/wav', 'audio/mpeg', 'audio/flac', 'audio/mp4', 'audio/ogg', 'audio/webm', 'video/webm'];
  if (!allowedTypes.includes(file.type) && !file.name.match(/\.(wav|mp3|flac|m4a|ogg|webm)$/i)) {
    showUploadStatus('Unsupported file type.', 'error');
    return;
  }

  uploadInProgress = true;
  showUploadStatus('Uploading...', 'uploading');

  const formData = new FormData();
  formData.append('file', file);
  const displayName = document.getElementById('voice-display-name')?.value?.trim();
  if (displayName) {
    formData.append('display_name', displayName);
  }

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
    showUploadStatus(`Voice "${data.displayName}" uploaded successfully!`, 'success');

    await loadCustomVoices();

    const select = document.getElementById('voice-type');
    if (select) {
      select.value = `custom:${data.voiceId}`;
      syncSettingsFromForm();
    }

  } catch (err) {
    showUploadStatus(`Error: ${err.message}`, 'error');
    setTimeout(() => {
      showUploadStatus('', '');
    }, 5000);
  } finally {
    uploadInProgress = false;  // (CRIT-11) Always clear flag
  }
}

function showUploadStatus(text, className) {
  const status = document.getElementById('voice-upload-status');
  if (status) {
    status.textContent = text;
    status.className = className ? `voice-upload-status ${className}` : 'voice-upload-status';
  }
}
```

3. **Microphone recording with proper cleanup (CRIT-12, HIGH-10):**

```javascript
let mediaRecorder = null;
let audioChunks = [];
let recordingStartTime = 0;
let recordingTimer = null;
let currentStream = null;  // Track for cleanup

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    currentStream = stream;  // Track for cleanup
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      // Stop all tracks
      if (currentStream) {
        currentStream.getTracks().forEach(t => t.stop());
        currentStream = null;
      }
      
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      
      // (CRIT-12) Check for data before upload
      if (blob.size === 0) {
        showUploadStatus('Recording failed: no audio captured.', 'error');
        return;
      }
      
      const file = new File([blob], 'recording.webm', { type: 'audio/webm' });
      await uploadVoiceFile(file);
    };

    mediaRecorder.onerror = (e) => {  // (CRIT-12) Error handler
      console.error('MediaRecorder error:', e);
      if (currentStream) {
        currentStream.getTracks().forEach(t => t.stop());
        currentStream = null;
      }
      showUploadStatus('Recording error. Please try again.', 'error');
    };

    mediaRecorder.start();
    recordingStartTime = Date.now();
    startRecordingTimer();
    updateRecordingUI(true);

  } catch (err) {
    console.error('Recording error:', err);
    showUploadStatus('Could not access microphone. Please check permissions.', 'error');
  }
}

function stopRecording() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;

  const duration = (Date.now() - recordingStartTime) / 1000;
  if (duration < 2) {
    showUploadStatus('Recording too short. Please record at least 2 seconds.', 'error');
    // Don't stop — let user continue recording
    return;
  }
  if (duration > 30) {
    showUploadStatus('Recording too long. Maximum is 30 seconds.', 'error');
  }

  mediaRecorder.stop();
  stopRecordingTimer();
  updateRecordingUI(false);
}

// (HIGH-10) Cleanup on page unload
window.addEventListener('beforeunload', () => {
  stopRecordingTimer();
  if (currentStream) {
    currentStream.getTracks().forEach(t => t.stop());
  }
});
```

4. **Preview with cleanup (HIGH-9):**

```javascript
let previewAudio = null;  // Reuse single Audio element

async function previewVoice(voiceId) {
  // Stop any current playback
  if (previewAudio) {
    previewAudio.pause();
    previewAudio.src = '';
  }
  
  try {
    previewAudio = new Audio(`/api/voices/${voiceId}/audio`);
    await previewAudio.play();
    previewAudio.onended = () => {
      previewAudio.src = '';
      previewAudio = null;
    };
  } catch (e) {
    console.warn('Preview playback failed:', e);
  }
}
```

5. **Drag-and-drop with file validation (HIGH-11):**

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

  // (CRIT-13) Keyboard activation
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      input.click();
    }
  });

  // Drag and drop with file validation (HIGH-11)
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
    const files = e.dataTransfer.files;
    // (HIGH-11) Check files exist
    if (files && files.length > 0) {
      uploadVoiceFile(files[0]);
    }
  });
}
```

6. **Backend settings payload:**

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
    result.voice_id = settings.voiceType.slice(7);
  }

  return result;
}
```

### Implementation Tasks (voice.js)

[CRITICAL] T25: Persist voiceType in localStorage
- File: static/voice.js
- Fix: Include voiceType in saveSettings/loadSettings
- Addresses: CRIT-10
- Acceptance: Custom voice selection survives page reload

[CRITICAL] T26: Add uploadInProgress debounce
- File: static/voice.js
- Fix: Guard uploadVoiceFile with flag
- Addresses: CRIT-11
- Acceptance: Concurrent uploads rejected

[CRITICAL] T27: Fix microphone recording cleanup
- File: static/voice.js
- Fix: Track currentStream, stop tracks on all paths, add error handler
- Addresses: CRIT-12
- Acceptance: Recording resources cleaned up on error/failure

[HIGH] T28: Reuse Audio element for previews
- File: static/voice.js
- Fix: Use single previewAudio, clean up after playback
- Addresses: HIGH-9
- Acceptance: No memory leak from repeated previews

[HIGH] T29: Clear recordingTimer on page unload
- File: static/voice.js
- Fix: Add beforeunload handler
- Addresses: HIGH-10
- Acceptance: Timer cleared when page closes during recording

[HIGH] T30: Validate dropped files
- File: static/voice.js
- Fix: Check dataTransfer.files.length > 0
- Addresses: HIGH-11
- Acceptance: Non-file drops ignored

[HIGH] T31: Add client-side file validation
- File: static/voice.js
- Fix: Check size and type before upload
- Addresses: HIGH-12
- Acceptance: Invalid files rejected before network request

---

### 4.3 `static/styles.css` (MODIFIED)

Add styles for custom voice upload section.

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

/* Upload zone */
.voice-upload-zone {
  border: 2px dashed var(--surface2);
  border-radius: 8px;
  padding: 16px;
  text-align: center;
  cursor: pointer;
  transition: all 0.2s;
  display: none;
}

.voice-upload-zone:hover,
.voice-upload-zone.dragover,
.voice-upload-zone:focus {
  border-color: var(--accent);
  background: rgba(0,0,0,0.05);
  outline: none;
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

.voice-upload-status.uploading { color: var(--detail); }
.voice-upload-status.success { color: var(--success); }
.voice-upload-status.error { color: var(--error); }

/* Disabled bootstrap settings */
.voice-bootstrap-settings.disabled {
  opacity: 0.4;
  pointer-events: none;
}

/* Recording zone */
.voice-recording-zone {
  padding: 8px 0;
}

.voice-recording-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 4px;
}

.voice-record-btn,
.voice-stop-btn {
  padding: 6px 12px;
  border-radius: 4px;
  border: 1px solid var(--surface2);
  background: var(--surface1);
  color: var(--text);
  cursor: pointer;
  font-size: 12px;
}

.voice-stop-btn {
  background: var(--error);
  color: white;
  border-color: var(--error);
}

/* Custom voice actions */
.voice-custom-actions {
  display: flex;
  gap: 8px;
  padding: 4px 0;
}

.voice-preview-btn,
.voice-delete-btn,
.voice-rename-btn {
  padding: 4px 8px;
  border-radius: 4px;
  border: 1px solid var(--surface2);
  background: var(--surface1);
  color: var(--text);
  cursor: pointer;
  font-size: 11px;
}

.voice-delete-btn:hover {
  background: var(--error);
  color: white;
}

/* Guidelines popup */
.voice-guidelines-popup {
  position: absolute;
  background: var(--surface1);
  border: 1px solid var(--surface2);
  border-radius: 8px;
  padding: 12px;
  font-size: 11px;
  max-width: 280px;
  z-index: 100;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}

.voice-guidelines-popup ul {
  margin: 8px 0 0;
  padding-left: 16px;
}

.voice-guidelines-popup li {
  margin-bottom: 4px;
}
```

---

## 5. API Endpoint Specifications

### `POST /api/voices`

Upload a custom voice sample.

**Request:**
- `Content-Type: multipart/form-data`
- `file`: Audio file (required) — max 10MB
- `display_name`: Optional user-friendly name (form field, max 40 chars)

**Response (200):**
```json
{
  "voiceId": "a1b2c3d4e5f6a1b2",
  "filename": "my_voice",
  "displayName": "My Voice",
  "duration": 5.2
}
```

**Errors:**
- `400`: Invalid file format, too large, too short, too long, corrupt, quota exceeded
- `401`: Not authenticated

### `GET /api/voices`

List all custom voices (shared across all profiles).

**Response (200):**
```json
{
  "voices": [
    {
      "voiceId": "a1b2c3d4e5f6a1b2",
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
- `400`: No displayName provided, invalid JSON, displayName too long
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

### Review Findings (addressed above)

- **HIGH-16:** display_name parameter ambiguity — now explicitly a form field
- **HIGH-17:** uploadTime uses monotonic() — now uses time.time()
- **MED-13:** DELETE returns 200 with body — acceptable for consistency with other endpoints
- **MED-14:** Error responses expose internal messages — addressed with user-friendly messages

---

## 6. File Format Requirements

### Supported Input Formats

| Format | Extension | Notes |
|--------|-----------|-------|
| WAV | `.wav` | Native, no conversion needed |
| MP3 | `.mp3` | Very common, requires decoding |
| FLAC | `.flac` | Lossless, good quality |
| M4A/AAC | `.m4a` | Common from iOS recordings |
| OGG | `.ogg` | Open format |
| WebM | `.webm` | Audio-only WebM (from browser recording) |

All formats are decoded to **24kHz mono float32** internally, then normalized and stored as **16-bit PCM WAV at 24kHz** (OmniVoice native format).

### Audio Quality Guidelines

- **Minimum duration**: 2 seconds
- **Maximum duration**: 30 seconds
- **Recommended**: 3–10 seconds of clear, natural speech
- **Sample rate**: Any input rate accepted; resampled to 24kHz
- **Channels**: Any input; converted to mono
- **Content**: Clear speech preferred; avoid loud background noise or music
- **Tip**: Speak naturally as you would in conversation

### Validation (on upload)

1. Check file size ≤ 10MB (FastAPI-level + VoiceStore-level)
2. Check extension in allowed list
3. Decode audio and validate waveform (non-empty, finite values, reasonable amplitude)
4. Check duration (2–30 seconds)
5. Compute hash and check for duplicates
6. Check total storage quota (≤ 500MB)

---

## 7. Cache Key Algorithm

The cache key (`voice_id`) is a SHA-256 hash of the first 3 seconds of the normalized audio, converted to int16 PCM bytes:

```python
def compute_hash(waveform: np.ndarray) -> str:
    samples = int(3.0 * 24000)
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

**Deduplication:** If a user uploads the same file twice, they get the same `voice_id` and the existing cached voice is reused.

### Review Findings

- **HIGH-13:** Cache eviction uses insertion order, not LRU
- **HIGH-14:** Cache limit of 8 is too low

### Implementation Tasks

[HIGH] T32: Implement LRU cache eviction
- File: pi_chat/tts_service.py
- Fix: Use OrderedDict with move_to_end() on cache hits
- Addresses: HIGH-13
- Acceptance: Frequently used voices stay cached longer

[HIGH] T33: Increase cache limit
- File: pi_chat/tts_service.py
- Fix: Increase max cache size to 16-32 voices
- Addresses: HIGH-14
- Acceptance: More voices can be cached simultaneously

---

## 8. UI Wireframe Description

(See original plan for detailed ASCII wireframes — unchanged.)

Key behaviors:

1. **Default**: "Designed voice" selected, all bootstrap controls enabled
2. **Select a custom voice**: bootstrap controls dim/disable, upload zone hidden
3. **Select "Upload new voice..."**: upload zone appears, recording zone appears, bootstrap controls dim
4. **After upload**: new voice auto-selected in list, upload/recording zones hidden
5. **Play button**: Fetches WAV file from server, plays via Web Audio API
6. **Delete button**: Shows confirmation dialog, then calls DELETE endpoint
7. **Apply button**: prepares the selected voice (bootstrap or custom)
8. **Reset button**: Reverts to designed voice with current bootstrap settings
9. **Record button**: Requests mic permission, starts recording, shows timer
10. **Stop recording**: Validates duration client-side, uploads via existing endpoint

---

## 9. Edge Cases and Error Handling

### Upload Errors

| Error | User Message |
|-------|--------------|
| File too large (>10MB) | "File too large. Maximum is 10MB." |
| Unsupported format | "Unsupported format. Please use WAV, MP3, FLAC, M4A, OGG, or WebM." |
| Audio too short (<2s) | "Audio too short (1.2s). Speak for at least 2 seconds." |
| Audio too long (>30s) | "Audio too long (45s). Use a shorter sample (max 30s)." |
| Corrupt file | "Failed to read audio file. Please try another file." |
| Silent audio | "Audio is silent. Please speak into the recording." |
| Storage quota exceeded | "Voice storage full. Delete some voices and try again." |
| Duplicate upload | Silently returns existing voice (no error) |

### Voice Preparation Errors

| Error | User Message |
|-------|--------------|
| Voice file missing | "Voice sample not found. Please re-upload." |
| Model load failed | "Could not load voice model: [detail]" |
| OOM | "Not enough GPU memory for voice. Close other GPU apps or disable voice." |
| Invalid reference audio | "Voice sample is unusable. Try recording a clearer sample." |
| Preparation timeout | "Voice preparation timed out. Try again or use a shorter sample." |

### Runtime Errors

- If a custom voice fails to prepare, fall back to error state (same as bootstrap voice failure)
- Voice samples persist server-side; deleting them removes them from the list immediately
- If a user selects a custom voice that was deleted, show error on Apply

### Review Findings (additional edge cases)

- **MED-3:** Potential model crash from malformed audio — addressed by waveform validation
- **MED-8:** Voice deleted while in active use — should invalidate cache entry
- **MED-9:** Corrupt meta.json handling — existing try/except is OK but add validation

### Implementation Tasks

[MEDIUM] T34: Invalidate cache on voice deletion
- File: pi_chat/app.py or tts_service.py
- Fix: On DELETE, remove voice from TTSService cache
- Addresses: MED-8
- Acceptance: Deleted voices don't remain usable from cache

[MEDIUM] T35: Add waveform validation before OmniVoice
- File: pi_chat/tts_service.py
- Fix: Validate loaded waveform before create_voice_clone_prompt
- Addresses: MED-3
- Acceptance: Malformed audio doesn't crash the model

---

## 10. Testing Plan (Test-Driven)

Follow TDD: write tests first, then implement to pass them.

### Test Fixtures

Generate synthetic test fixtures in `tests/fixtures/voices/`:
- `short.wav` — 1 second (too short, should fail)
- `valid.wav` — 5 seconds of speech (valid)
- `long.wav` — 35 seconds (too long, should fail)
- `valid.mp3` — 5 seconds (valid MP3)
- `valid.m4a` — 5 seconds (valid M4A)
- `valid.flac` — 5 seconds (valid FLAC)
- `valid.ogg` — 5 seconds (valid OGG)
- `silence.wav` — 5 seconds of silence (should fail validation)
- `stereo.wav` — stereo WAV (should be converted to mono)
- `44100hz.wav` — 44.1kHz WAV (should be resampled to 24kHz)

### Unit Tests

#### 1. `tests/test_voice_store.py`

Original tests plus review-added tests:

- All original tests (upload, list, get, load_waveform, delete, update, hash)
- **test_upload_waveform_validation_rejects_empty** (CRIT-5)
- **test_upload_waveform_validation_rejects_silence** (CRIT-5)
- **test_upload_atomic_write_no_corruption_on_crash** (CRIT-7)
- **test_list_voices_skips_missing_wav** (CRIT-9)
- **test_upload_respects_storage_quota** (HIGH-1)
- **test_upload_concurrent_duplicate_safe** (CRIT-6)

#### 2. `tests/test_tts_custom_voice.py`

Original tests plus review-added tests:

- All original tests (voice_key, prepare_custom_voice, generate paths)
- **test_prepare_custom_voice_uses_injected_store** (CRIT-2)
- **test_generate_always_passes_language** (CRIT-3)

#### 3. `tests/test_voice_session_validate.py` (NEW)

- **test_accepts_voice_id** (CRIT-14)
- **test_rejects_invalid_voice_id_format** (CRIT-14)
- **test_rejects_unknown_keys** (CRIT-1)
- **test_accepts_voice_id_with_other_keys**

#### 4. `tests/test_voice_store_formats.py`

Test all supported upload formats decode correctly (original tests).

#### 5. `tests/test_voice_api.py`

Integration tests with FastAPI TestClient (original tests).

#### 6. `tests/test_voice_e2e.py`

End-to-end with real TTSService (original tests).

### Fake Runtime Updates

#### Extend `tests/fakes/voice.py`

- **FakeVoiceStore**: In-memory store for testing without disk I/O
- **FakeOmniVoiceRuntime**: Support custom voice path — track create_voice_clone_prompt calls
- **FakeTTSService with injected VoiceStore**: Verify injection pattern

### Review Findings (testing gaps)

- **CRIT-14:** No test for validate_settings accepting voice_id
- **MED-10:** No test fixtures for edge case audio (silence, stereo, etc.)
- **MED-11:** No test for voice preparation failure
- **MED-12:** FakeVoiceStore implementation not shown

### Implementation Tasks

[CRITICAL] T36: Add validate_settings tests for voice_id
- File: tests/test_voice_session_validate.py
- Fix: Test voice_id accepted, invalid format rejected
- Addresses: CRIT-14
- Acceptance: Tests verify CRIT-1 fix

[MEDIUM] T37: Add edge case audio fixtures
- File: tests/fixtures/voices/
- Fix: Generate silence.wav, stereo.wav, 44100hz.wav
- Addresses: MED-10
- Acceptance: Edge case fixtures exist and are used in tests

[MEDIUM] T38: Add test for voice preparation failure
- File: tests/test_tts_custom_voice.py
- Fix: Fake runtime that fails on prepare
- Addresses: MED-11
- Acceptance: Error handling tested

[MEDIUM] T39: Show FakeVoiceStore implementation in plan
- File: tests/fakes/voice.py
- Fix: Document FakeVoiceStore interface
- Addresses: MED-12
- Acceptance: Fake implementation matches real VoiceStore

---

## 11. Design Decisions (Updated)

1. **Shared voices**: All voices stored in `~/.pi/voices/`, shared across b and r profiles. Intentional for local use; documented as a design choice.

2. **No transcription**: `ref_text=None` for now. Will revisit when voice input is supported.

3. **User naming**: User can name each uploaded voice (defaults to sanitized filename). Editable after upload. Max 40 chars.

4. **Delete confirmation**: Confirmation dialog before deleting a voice.

5. **Voice preview**: Play button fetches WAV from server and plays via Web Audio API.

6. **Cache limit**: Custom voices share the same cache limit as bootstrap voices. Increased to 16-32 total. LRU eviction.

7. **Storage quota**: 500MB total limit on voice storage. Upload rejected when exceeded.

8. **No quality scoring**: OmniVoice handles cleanup/truncation. User educated via guidelines tooltip.

9. **Microphone recording**: In-browser recording via MediaRecorder API. Same upload flow as file upload. Uses WebM format.

10. **Feature flag**: Custom voices can be disabled via `PI_CHAT_TTS_CUSTOM_VOICES_ENABLED=0`.

11. **Atomic writes**: All file writes use temp file + os.replace for crash safety.

12. **Upload timeout**: Voice preparation has 60-second timeout to prevent hangs.

---

## 12. Open Questions (with Recommendations)

1. **Per-voice rename UI**: Should rename be inline (click name → edit) or via a separate dialog?
   - **Recommendation**: Start with prompt() for simplicity. Can upgrade to inline edit later.

2. **Voice limit UX**: If cache is full (16-32 voices), should we auto-evict LRU or show an error asking user to delete first?
   - **Recommendation**: Auto-evict LRU. User shouldn't have to manually manage cache.

3. **Recording format**: WebM (native MediaRecorder) or WAV (requires conversion in browser)?
   - **Recommendation**: WebM. Simpler, backend handles decode.

4. **Ethical guidelines**: Should we add a consent warning or usage guidelines for voice cloning?
   - **Recommendation**: Yes. Added "Only clone voices you have permission to use" to guidelines tooltip.

5. **Multi-user deployment**: Is this app intended to be strictly single-user/local, or could it be deployed multi-user?
   - **Recommendation**: Single-user/local for now. Document this assumption.

6. **Voice sample ownership**: Can users export/download their voice samples? Should there be a bulk delete?
   - **Recommendation**: Export via existing /audio endpoint. Bulk delete can be added later.

7. **Voice preview in voice mode**: Should the play button preview the original sample, or a short TTS synthesis using that voice?
   - **Recommendation**: Original sample for now. TTS preview is a nice-to-have enhancement.

8. **Recording duration feedback**: Should we warn the user when approaching the 30-second limit during recording?
   - **Recommendation**: Yes. Add visual warning at 25 seconds (red timer).

---

## Task Summary by Priority

### CRITICAL (Must complete before implementation works)
T1-T27: 27 tasks covering validate_settings, VoiceStore injection, language parameter, file size limits, waveform validation, atomic writes, integrity checks, timeouts, persistence, debouncing, recording cleanup, accessibility.

### HIGH (Should complete before implementation)
T28-T33: 6 tasks covering cache eviction, cache limit, thread pool execution, body validation, feature flag, directory validation, fake runtime updates.

### MEDIUM (Can complete during implementation)
T34-T39: 6 tasks covering cache invalidation, waveform validation, test fixtures, test coverage.

### LOW (Nice-to-have)
- Custom modal for delete confirmation (vs native confirm())
- Audio visualization during recording
- Backup strategy documentation
- Multi-user isolation documentation

---

## New Dependencies

Add to `pyproject.toml` (under voice extra):

```toml
[project.optional-dependencies]
voice = [
    "torch",
    "omnivoice",
    "librosa",        # Audio loading with resampling
    "soundfile",      # WAV read/write
]
```

---

## Environment Variables

```
PI_CHAT_VOICE_SAMPLES_DIR=~/.pi/voices          # Voice sample storage directory
PI_CHAT_TTS_CUSTOM_VOICES_ENABLED=1             # Feature flag (1=true, 0=false)
```

---

## Checklist for Implementation

- [ ] All CRITICAL tasks completed
- [ ] All HIGH tasks completed  
- [ ] All unit tests passing
- [ ] All integration tests passing
- [ ] Manual testing checklist completed
- [ ] Documentation updated (AGENTS.md, README if applicable)
- [ ] Feature flag tested (enable/disable)
- [ ] Storage quota tested
- [ ] Concurrent upload tested
- [ ] Atomic write crash recovery tested
