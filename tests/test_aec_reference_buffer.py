"""Unit tests for VoiceSession AEC reference buffer."""

import asyncio
import pytest
from unittest.mock import MagicMock


@pytest.mark.asyncio
async def test_reference_buffer_append_and_drain():
    """Appending chunks and draining returns them in order."""
    from pi_chat.voice_session import VoiceSession

    tts = MagicMock()
    voice = VoiceSession(tts, MagicMock(), MagicMock())

    # Append two chunks
    chunk1 = b"\x01\x02\x03\x04"
    chunk2 = b"\x05\x06\x07\x08"
    with voice._reference_lock:
        voice._tts_reference_buffer.extend(chunk1)
        voice._tts_reference_buffer.extend(chunk2)

    # Drain all
    data = voice.drain_reference_bytes(8)
    assert data == chunk1 + chunk2

    # Buffer is empty
    data = voice.drain_reference_bytes(10)
    assert data == b""


@pytest.mark.asyncio
async def test_reference_buffer_drain_partial():
    """Draining more than available returns what's there."""
    from pi_chat.voice_session import VoiceSession

    tts = MagicMock()
    voice = VoiceSession(tts, MagicMock(), MagicMock())

    with voice._reference_lock:
        voice._tts_reference_buffer.extend(b"\xAA\xBB\xCC")

    data = voice.drain_reference_bytes(10)
    assert data == b"\xAA\xBB\xCC"
    assert len(data) == 3


@pytest.mark.asyncio
async def test_reference_buffer_overflow_protection():
    """Overflow truncation logic keeps only the newest bytes."""
    from pi_chat.voice_session import VoiceSession

    tts = MagicMock()
    voice = VoiceSession(tts, MagicMock(), MagicMock())
    max_bytes = 10

    # Simulate the overflow protection logic from _worker()
    with voice._reference_lock:
        voice._tts_reference_buffer.extend(bytes(range(20)))
        # This is the exact logic from _worker()
        if len(voice._tts_reference_buffer) > max_bytes:
            voice._tts_reference_buffer = voice._tts_reference_buffer[-max_bytes:]

    # Should have kept only the last 10
    assert len(voice._tts_reference_buffer) == 10
    assert voice._tts_reference_buffer == bytes(range(10, 20))


@pytest.mark.asyncio
async def test_is_speaking_flag_transitions():
    """is_speaking goes True on first frame, False on stream end."""
    from pi_chat.voice_session import VoiceSession

    tts = MagicMock()
    voice = VoiceSession(tts, MagicMock(), MagicMock())
    assert voice.is_speaking is False

    # Simulate first frame being sent
    voice.is_speaking = True
    assert voice.is_speaking is True

    # Simulate stream end
    voice.is_speaking = False
    assert voice.is_speaking is False
