"""Tests for the VoiceSession AEC reference window.

Covers the server-side echo-cancellation state machine that keeps STT from
transcribing TTS audio: 16kHz reference resampling, per-stream resets,
browser-confirmed playback end, tail-timeout fallback, phantom-reference
guard, and stream cleanup on synthesis errors.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from pi_chat.voice_session import VoiceSession


def make_voice() -> VoiceSession:
    tts = MagicMock()
    return VoiceSession(tts, AsyncMock(), AsyncMock())


def make_24k_pcm(seconds: float = 0.5) -> bytes:
    """Synthetic 24kHz mono int16 PCM (a 440Hz tone)."""
    t = np.arange(int(seconds * 24000)) / 24000.0
    samples = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    return samples.tobytes()


# ---------------------------------------------------------------------------
# Reference append + resampling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reference_resampled_to_16k():
    voice = make_voice()
    pcm24k = make_24k_pcm(0.5)  # 12000 samples at 24kHz = 24000 bytes
    voice._append_reference(pcm24k, 24000)

    # 0.5s at 16kHz = 8000 samples = 16000 bytes (± resampler rounding)
    with voice._reference_lock:
        buf_len = len(voice._tts_reference_buffer)
    expected_bytes = int(0.5 * 16000) * 2
    assert abs(buf_len - expected_bytes) <= 8
    assert voice.is_speaking is True


@pytest.mark.asyncio
async def test_reference_passthrough_when_already_16k():
    voice = make_voice()
    pcm16k = (np.arange(1600, dtype=np.int16) % 1000).tobytes()
    voice._append_reference(pcm16k, 16000)
    with voice._reference_lock:
        buf = bytes(voice._tts_reference_buffer)
    assert buf == pcm16k


@pytest.mark.asyncio
async def test_reference_drain_matches_mic_rate():
    """Draining at the 16kHz mic rate consumes exactly real-time reference.

    Regression guard for the old bug where 24kHz reference bytes were drained
    as 16kHz, leaving the reference ~33% behind actual playback.
    """
    voice = make_voice()
    pcm24k = make_24k_pcm(1.0)  # 1 second of audio
    voice._append_reference(pcm24k, 24000)

    # A 1-second mic stream drains 16000 samples = 32000 bytes.
    drained = voice.drain_reference_bytes(16000 * 2)
    assert len(drained) == 32000  # buffer held exactly 1s of 16k reference

    # Nothing left: the reference tracked playback 1:1.
    assert voice.drain_reference_bytes(1000) == b""


# ---------------------------------------------------------------------------
# Per-stream resets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_start_clears_stale_reference():
    voice = make_voice()
    voice.ready = True
    voice.voice_handle = MagicMock(key="v1")

    # Simulate a leftover reference from a previous (dropped) stream.
    voice._append_reference(make_24k_pcm(0.5), 24000)
    assert voice.is_speaking is True

    await voice._on_agent_start()
    try:
        with voice._reference_lock:
            assert len(voice._tts_reference_buffer) == 0
        assert voice.is_speaking is False
        assert voice._playback_ended is False
    finally:
        if voice._worker_task:
            voice._worker_task.cancel()


# ---------------------------------------------------------------------------
# Playback end (browser-confirmed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playback_end_closes_window():
    voice = make_voice()
    voice._stream_id = 7
    voice._append_reference(make_24k_pcm(0.5), 24000)
    assert voice.is_speaking is True

    voice.on_playback_end(7)
    assert voice.is_speaking is False
    with voice._reference_lock:
        assert len(voice._tts_reference_buffer) == 0
    assert voice._playback_ended is True


@pytest.mark.asyncio
async def test_playback_end_ignores_stale_stream():
    voice = make_voice()
    voice._stream_id = 8
    voice._append_reference(make_24k_pcm(0.5), 24000)

    voice.on_playback_end(7)  # old stream — must be ignored
    assert voice.is_speaking is True
    with voice._reference_lock:
        assert len(voice._tts_reference_buffer) > 0


@pytest.mark.asyncio
async def test_phantom_reference_guard():
    """After the browser confirms playback stopped, late frames (e.g. user
    typed a prompt mid-stream) must not add stale reference audio."""
    voice = make_voice()
    voice._stream_id = 3
    voice.on_playback_end(3)

    voice._append_reference(make_24k_pcm(0.5), 24000)
    with voice._reference_lock:
        assert len(voice._tts_reference_buffer) == 0
    assert voice.is_speaking is False


# ---------------------------------------------------------------------------
# Tail-timeout fallback (stale clients / lost playback_end commands)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tail_timeout_closes_window():
    voice = make_voice()
    voice._stream_id = 1
    voice._append_reference(make_24k_pcm(0.5), 24000)

    # Drain the buffer (mic consumed the reference at real time)...
    while voice.drain_reference_bytes(64000) != b"":
        pass
    assert voice.is_speaking is True

    # ...and simulate time passing well past the tail with no playback_end.
    voice._last_ref_append = time.monotonic() - (voice._aec_tail_seconds + 1)
    voice.drain_reference_bytes(0)  # drain acts as the AEC window tick
    assert voice.is_speaking is False


@pytest.mark.asyncio
async def test_tail_timeout_does_not_fire_recent_append():
    voice = make_voice()
    voice._stream_id = 1
    voice._append_reference(make_24k_pcm(0.5), 24000)
    while voice.drain_reference_bytes(64000) != b"":
        pass
    # Reference was appended just now → window stays open.
    voice.drain_reference_bytes(0)
    assert voice.is_speaking is True


# ---------------------------------------------------------------------------
# Stream cleanup on synthesis errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_error_ends_stream_and_closes_speaking():
    """A synthesis error must end the stream (browser reset + AEC close)
    instead of leaving active_run/is_speaking stuck True."""
    voice = make_voice()
    voice.ready = True
    voice.voice_handle = MagicMock(key="v1")
    voice._tts.synthesize = AsyncMock(side_effect=RuntimeError("GPU blew up"))

    await voice._on_agent_start()
    await voice._enqueue_chunk("hello world, this is a test")

    # Worker (started by _on_agent_start) processes the chunk and errors.
    for _ in range(20):
        if voice._worker_task.done():
            break
        await asyncio.sleep(0)

    sent = [c.args[0] for c in voice._send_json.await_args_list]
    assert any(m.get("type") == "voice_stream_end" for m in sent)
    assert any(m.get("type") == "voice_error" for m in sent)
    assert voice.active_run is False
    assert voice.is_speaking is False
    assert voice._queue is None
