"""Custom voice sample storage and management."""

import hashlib
import io
import json
import logging
import math
import os
import re
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

# Supported input formats (handled by soundfile)
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
            # Extract voice_id: filename is <voice_id>.meta.json, stem gives <voice_id>.meta
            voice_id = meta_file.stem.replace(".meta", "")
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
        if len(waveform) == 0:
            raise ValueError("Audio file is empty or unreadable.")
        
        # Check for NaN/Inf
        for sample in waveform:
            if sample != sample:  # NaN check
                raise ValueError("Audio contains invalid values.")
            if sample == float('inf') or sample == float('-inf'):
                raise ValueError("Audio contains invalid values.")
        
        max_val = max(abs(s) for s in waveform)
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
        """Load audio from bytes using soundfile, with ffmpeg fallback."""
        import soundfile as sf
        
        try:
            waveform, sr = sf.read(io.BytesIO(file_bytes), dtype="float32")
        except Exception:
            # soundfile failed (e.g., M4A not supported) - use ffmpeg
            waveform, sr = self._load_audio_via_ffmpeg(file_bytes)

        # Convert stereo to mono
        if len(waveform.shape) > 1 and waveform.shape[1] > 1:
            waveform = waveform.mean(axis=1)

        # Resample if needed
        if sr != TARGET_SAMPLE_RATE:
            waveform = self._resample(waveform, sr, TARGET_SAMPLE_RATE)
            sr = TARGET_SAMPLE_RATE

        duration = len(waveform) / sr
        return waveform, duration

    def _load_audio_via_ffmpeg(self, file_bytes: bytes) -> tuple[Any, float]:
        """Load audio via ffmpeg by converting to PCM in memory."""
        import subprocess
        import tempfile
        import numpy as np
        
        # Write to temp file (ffmpeg pipe:0 input is unreliable with subprocess)
        fd, tmp_path = tempfile.mkstemp(suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(file_bytes)

            try:
                result = subprocess.run(
                    [
                        'ffmpeg', '-i', tmp_path,
                        '-f', 's16le', '-acodec', 'pcm_s16le',
                        '-ac', '1', '-ar', str(TARGET_SAMPLE_RATE),
                        '-'
                    ],
                    capture_output=True,
                    timeout=30
                )
            except FileNotFoundError:
                raise ValueError(
                    "ffmpeg not found. Install ffmpeg to support this audio format "
                    "(apt install ffmpeg)."
                )
            except subprocess.TimeoutExpired:
                raise ValueError("Audio decoding timed out. File may be too large.")

            if result.returncode != 0:
                stderr = result.stderr.decode('utf-8', errors='replace').strip()
                raise ValueError(f"Failed to decode audio with ffmpeg: {stderr}")

            if len(result.stdout) == 0:
                raise ValueError("ffmpeg produced no output. File may be empty or corrupted.")

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        pcm_data = np.frombuffer(result.stdout, dtype=np.int16)
        waveform = pcm_data.astype(np.float32) / 32768.0
        return waveform, TARGET_SAMPLE_RATE

    def _resample(self, waveform: Any, orig_sr: int, target_sr: int) -> Any:
        """Simple resampling using sinc interpolation."""
        import numpy as np
        if orig_sr == target_sr:
            return waveform
        
        length = len(waveform)
        new_length = int(length * target_sr / orig_sr)
        
        # Create new time axis
        new_indices = np.linspace(0, length - 1, new_length)
        
        # Linear interpolation (good enough for voice cloning reference)
        return np.interp(new_indices, np.arange(length), waveform)

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
            if not (fd is None or fd < 0):
                try:
                    os.close(fd)
                except:
                    pass
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
            if not (fd is None or fd < 0):
                try:
                    os.close(fd)
                except:
                    pass
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
