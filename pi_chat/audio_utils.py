"""Shared PCM audio helpers for TTS/STT (resampling, PCM conversion).

Kept dependency-light so both voice_session (TTS) and stt_session (STT)
can use the same resampling code without importing each other.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# `VOICE-14`: the polyphase path can fail on a machine that has scipy, and the fallback below used to
# swallow that silently on every call, forever. One warning per process is the difference between a
# machine quietly resampling badly and a machine nobody can diagnose from a log.
_linear_fallback_warned = False


def _warn_linear_fallback(exc: Exception, source_rate: int, target_rate: int) -> None:
    global _linear_fallback_warned
    if _linear_fallback_warned:
        return
    _linear_fallback_warned = True
    logger.warning(
        "audio: scipy resample_poly failed (%s: %s), falling back to linear interpolation for %d -> %d Hz "
        "and for every later call in this process. Audio quality is lower; expect aliasing on downsampling.",
        type(exc).__name__,
        exc,
        source_rate,
        target_rate,
    )


def float32_to_s16le(waveform: Any) -> bytes:
    """Clip a float32 waveform to [-1, 1] and encode it as s16le PCM bytes.

    Shared by the whole-utterance path in `TTSService._waveform_to_pcm` and the piece-at-a-time
    path in `PhononRuntime.generate`, so the two cannot drift into different audio.
    """
    import numpy as np

    samples = np.asarray(waveform)
    if samples.dtype != np.float32 and samples.dtype != np.float64:
        samples = samples.astype(np.float32)
    samples = np.clip(samples, -1.0, 1.0)
    return (samples * 32767).astype("<i2", copy=False).tobytes()


def resample_int16(samples: Any, source_rate: int, target_rate: int):
    """Resample int16 PCM samples from source_rate to target_rate.

    Uses `scipy.signal.resample_poly` (polyphase, anti-aliased) when available, and falls back to linear
    interpolation (`np.interp`) when scipy is missing **or** the call raises; that fallback is logged once
    per process rather than swallowed, because a permanently degraded path looks identical to a healthy one
    otherwise. Returns a numpy int16 array.
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
    except Exception as exc:
        # Fallback: linear interpolation. Deliberately broad (missing scipy, bad rates, empty edges), and
        # deliberately loud once: see `_warn_linear_fallback`.
        _warn_linear_fallback(exc, source_rate, target_rate)
        duration = samples.size / float(source_rate)
        target_size = max(1, int(round(duration * target_rate)))
        source_positions = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
        target_positions = np.linspace(0.0, 1.0, num=target_size, endpoint=False)
        resampled = np.interp(
            target_positions, source_positions, samples.astype(np.float32)
        )

    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def resample_pcm_bytes(pcm_s16le: bytes, source_rate: int, target_rate: int) -> bytes:
    """Resample little-endian int16 PCM bytes between sample rates.

    Returns little-endian int16 PCM bytes at target_rate.
    """
    import numpy as np

    if not pcm_s16le or source_rate == target_rate:
        return bytes(pcm_s16le)

    samples = np.frombuffer(pcm_s16le, dtype=np.int16)
    resampled = resample_int16(samples, source_rate, target_rate)
    return resampled.tobytes()
