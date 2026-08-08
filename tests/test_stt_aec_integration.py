"""Unit tests for STTSession AEC processing path."""

import numpy as np
import pytest
from unittest.mock import MagicMock, patch


def _make_packet(samples: np.ndarray):
    """Create an AudioPacket from numpy samples at 16kHz."""
    from pi_chat.stt_session import AudioPacket
    import json
    import struct

    metadata = json.dumps({
        "sampleRate": 16000,
        "channels": 1,
        "format": "pcm_s16le",
        "frames": len(samples),
    }).encode("utf-8")

    header = struct.pack("<I", len(metadata))
    audio = samples.tobytes()
    return AudioPacket(metadata={"sampleRate": 16000, "channels": 1, "format": "pcm_s16le", "frames": len(samples)}, audio=audio)


@pytest.mark.asyncio
async def test_aec_pass_through_when_not_speaking():
    """When agent is not speaking, audio passes through unchanged."""
    from pi_chat.stt_session import STTSession

    stt_service = MagicMock()
    send_json = MagicMock()
    session = STTSession(stt_service, send_json)

    # Link to a mock voice session that is NOT speaking
    voice = MagicMock()
    voice.is_speaking = False
    session.set_voice_session(voice)

    # Enable STT with a mock recorder
    session.enabled = True
    session._recorder = MagicMock()

    # Feed a packet
    samples = np.random.randint(-32768, 32767, size=1000, dtype=np.int16)
    packet = _make_packet(samples)
    session.ingest_audio_packet(packet)

    # Recorder should have received the original samples (no AEC)
    call_args = session._recorder.feed_audio.call_args
    fed_samples = call_args[0][0]
    np.testing.assert_array_equal(fed_samples, samples)


@pytest.mark.asyncio
async def test_aec_applied_when_speaking():
    """When agent is speaking, audio goes through AEC."""
    from pi_chat.stt_session import STTSession

    stt_service = MagicMock()
    send_json = MagicMock()
    session = STTSession(stt_service, send_json)

    # Link to a mock voice session that IS speaking, with reference audio
    voice = MagicMock()
    voice.is_speaking = True
    # Reference: same as input (degenerate case — AEC should attenuate)
    ref_samples = np.zeros(1000, dtype=np.int16)
    voice.drain_reference_bytes.return_value = ref_samples.tobytes()

    session.set_voice_session(voice)

    # Enable STT with a mock recorder
    session.enabled = True
    session._recorder = MagicMock()

    # Feed a packet of silence (same as reference)
    samples = np.zeros(1000, dtype=np.int16)
    packet = _make_packet(samples)
    session.ingest_audio_packet(packet)

    # Should have called drain_reference_bytes and feed_audio
    voice.drain_reference_bytes.assert_called_once()
    session._recorder.feed_audio.assert_called_once()

    # Output should be same length
    fed_samples = session._recorder.feed_audio.call_args[0][0]
    assert len(fed_samples) == len(samples)


@pytest.mark.asyncio
async def test_aec_underflow_zero_pads():
    """When reference buffer is shorter than mic packet, AEC zero-pads."""
    from pi_chat.stt_session import STTSession

    stt_service = MagicMock()
    send_json = MagicMock()
    session = STTSession(stt_service, send_json)

    # Voice returns only half the needed reference bytes
    voice = MagicMock()
    voice.is_speaking = True
    needed = 1000 * 2  # 1000 samples × 2 bytes
    voice.drain_reference_bytes.return_value = b"\x00" * (needed // 2)

    session.set_voice_session(voice)
    session.enabled = True
    session._recorder = MagicMock()

    samples = np.random.randint(-32768, 32767, size=1000, dtype=np.int16)
    packet = _make_packet(samples)

    # Should not raise — underflow is handled
    session.ingest_audio_packet(packet)

    fed_samples = session._recorder.feed_audio.call_args[0][0]
    assert len(fed_samples) == len(samples)


@pytest.mark.asyncio
async def test_aec_reset_on_speaking_transition():
    """AEC is reset when agent starts speaking (new stream)."""
    from pi_chat.stt_session import STTSession

    stt_service = MagicMock()
    send_json = MagicMock()
    session = STTSession(stt_service, send_json)

    voice = MagicMock()
    voice.is_speaking = True
    voice.drain_reference_bytes.return_value = b"\x00" * 2000

    session.set_voice_session(voice)
    session.enabled = True
    session._recorder = MagicMock()

    samples = np.zeros(1000, dtype=np.int16)
    packet = _make_packet(samples)

    # First packet while speaking — should create AEC and reset it
    session.ingest_audio_packet(packet)
    assert session._aec is not None
    assert session._aec_was_speaking is True

    # Second packet — no reset (already speaking)
    session.ingest_audio_packet(packet)

    # Agent stops speaking
    voice.is_speaking = False
    session.ingest_audio_packet(packet)
    assert session._aec_was_speaking is False

    # Agent starts speaking again — reset should happen
    voice.is_speaking = True
    session.ingest_audio_packet(packet)
    assert session._aec_was_speaking is True
