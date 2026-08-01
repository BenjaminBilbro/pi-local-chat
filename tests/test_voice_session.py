"""Tests for VoiceSession — settings, events, queue, binary protocol, cancellation.

Uses FakeTTSService; no torch, no GPU.
"""

import asyncio
import struct
import time

import pytest

from pi_chat.voice_session import (
    VoiceSession,
    build_pcm_frame,
    validate_settings,
    PCM_MAGIC,
    PCM_VERSION,
    PCM_HEADER_LEN,
    PCM_HEADER,
)
from tests.fakes.voice import FakeTTSService, VoiceSettings, fixture_one_sentence, fixture_retry


class FakeConfig:
    """Minimal config for VoiceSession tests."""
    TTS_MAX_QUEUE_CHUNKS = 12
    TTS_MAX_QUEUE_CHARS = 1800
    TTS_MAX_HOLD_MS = 100  # Short for tests


@pytest.fixture
def config():
    return FakeConfig()


@pytest.fixture
def captured_messages():
    return {"json": [], "bytes": []}


@pytest.fixture
async def voice_session(captured_messages):
    tts = FakeTTSService()
    await tts.load()
    await tts.prepare_voice(VoiceSettings())

    async def send_json(msg):
        captured_messages["json"].append(msg)

    async def send_bytes(data):
        captured_messages["bytes"].append(data)

    session = VoiceSession(tts, send_json, send_bytes, config_=FakeConfig())
    session.enabled = True
    session.ready = True
    session.voice_handle = await tts.prepare_voice(VoiceSettings())
    return session


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


class TestSettingsValidation:
    def test_default_settings_valid(self):
        result = validate_settings({})
        assert result is not None
        assert result.gender == "female"
        assert result.age == "young adult"

    def test_all_valid_fields(self):
        result = validate_settings({
            "gender": "male",
            "age": "child",
            "pitch": "high pitch",
            "accent": "british accent",
            "style": "whisper",
            "speed": 1.1,
        })
        assert result is not None
        assert result.gender == "male"
        assert result.style == "whisper"

    def test_invalid_gender(self):
        assert validate_settings({"gender": "robot"}) is None

    def test_invalid_age(self):
        assert validate_settings({"age": "ancient"}) is None

    def test_invalid_pitch(self):
        assert validate_settings({"pitch": "ultra high"}) is None

    def test_invalid_accent(self):
        assert validate_settings({"accent": "french accent"}) is None

    def test_invalid_style(self):
        assert validate_settings({"style": "shout"}) is None

    def test_speed_too_low(self):
        assert validate_settings({"speed": 0.5}) is None

    def test_speed_too_high(self):
        assert validate_settings({"speed": 2.0}) is None

    def test_unknown_key_rejected(self):
        assert validate_settings({"gender": "female", "volume": 0.5}) is None


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------


class TestEnableDisable:
    @pytest.mark.asyncio
    async def test_enable_sends_loading_then_ready(self, captured_messages):
        tts = FakeTTSService()
        await tts.load()
        handle = await tts.prepare_voice(VoiceSettings())

        async def send_json(msg):
            captured_messages["json"].append(msg)

        async def send_bytes(data):
            captured_messages["bytes"].append(data)

        session = VoiceSession(tts, send_json, send_bytes, config_=FakeConfig())
        await session.enable({})

        # Wait for preparation
        await asyncio.sleep(0.1)

        states = [m for m in captured_messages["json"] if m.get("type") == "voice_state"]
        assert any(s["state"] == "loading" for s in states)
        assert any(s["state"] == "ready" for s in states)
        assert session.ready
        assert session.enabled

    @pytest.mark.asyncio
    async def test_disable_stops_and_sends_disabled(self, voice_session, captured_messages):
        voice_session.active_run = True
        voice_session._stream_id = 5

        await voice_session.disable()

        assert not voice_session.enabled
        assert not voice_session.ready
        assert not voice_session.active_run

        states = [m for m in captured_messages["json"] if m.get("type") == "voice_state"]
        assert any(s["state"] == "disabled" for s in states)

    @pytest.mark.asyncio
    async def test_enable_invalid_settings_sends_error(self, captured_messages):
        tts = FakeTTSService()
        await tts.load()

        async def send_json(msg):
            captured_messages["json"].append(msg)

        async def send_bytes(data):
            pass

        session = VoiceSession(tts, send_json, send_bytes, config_=FakeConfig())
        await session.enable({"gender": "invalid"})

        errors = [m for m in captured_messages["json"] if m.get("type") == "voice_error"]
        assert len(errors) > 0
        assert errors[0]["code"] == "voice_invalid_settings"


# ---------------------------------------------------------------------------
# pi event mapping
# ---------------------------------------------------------------------------


class TestPiEventMapping:
    @pytest.mark.asyncio
    async def test_agent_start_sends_stream_start(self, voice_session, captured_messages):
        await voice_session.observe_pi_event({"type": "agent_start"})

        stream_starts = [m for m in captured_messages["json"] if m.get("type") == "voice_stream_start"]
        assert len(stream_starts) == 1
        assert stream_starts[0]["encoding"] == "pcm_s16le"
        assert stream_starts[0]["channels"] == 1
        assert voice_session.active_run

    @pytest.mark.asyncio
    async def test_text_delta_produces_frames_before_settled(self, voice_session, captured_messages):
        await voice_session.observe_pi_event({"type": "agent_start"})

        # Feed text deltas
        await voice_session.observe_pi_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_start"},
        })
        await voice_session.observe_pi_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Hello there. How are you?"},
        })
        await voice_session.observe_pi_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "content": "Hello there. How are you?"},
        })

        # Wait for synthesis
        await asyncio.sleep(0.2)

        # Should have binary frames
        assert len(captured_messages["bytes"]) > 0, "Expected binary PCM frames"

    @pytest.mark.asyncio
    async def test_agent_settled_sends_stream_end(self, voice_session, captured_messages):
        await voice_session.observe_pi_event({"type": "agent_start"})
        await voice_session.observe_pi_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_start"},
        })
        await voice_session.observe_pi_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Done."},
        })
        await voice_session.observe_pi_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "content": "Done."},
        })
        await voice_session.observe_pi_event({"type": "agent_settled"})

        # Wait for worker to drain
        await asyncio.sleep(0.3)

        stream_ends = [m for m in captured_messages["json"] if m.get("type") == "voice_stream_end"]
        assert len(stream_ends) > 0
        assert stream_ends[-1]["reason"] == "complete"

    @pytest.mark.asyncio
    async def test_retry_creates_new_stream_id(self, voice_session, captured_messages):
        # First agent_start
        await voice_session.observe_pi_event({"type": "agent_start"})
        first_starts = [m for m in captured_messages["json"] if m.get("type") == "voice_stream_start"]
        first_id = first_starts[-1]["streamId"]

        # agent_end without settled (retry)
        await voice_session.observe_pi_event({"type": "agent_end", "messages": []})

        # Second agent_start
        await voice_session.observe_pi_event({"type": "agent_start"})
        second_starts = [m for m in captured_messages["json"] if m.get("type") == "voice_stream_start"]
        second_id = second_starts[-1]["streamId"]

        assert second_id > first_id

    @pytest.mark.asyncio
    async def test_failed_response_ends_stream(self, voice_session, captured_messages):
        await voice_session.observe_pi_event({"type": "agent_start"})
        await voice_session.observe_pi_event({
            "type": "response",
            "command": "prompt",
            "success": False,
        })

        stream_ends = [m for m in captured_messages["json"] if m.get("type") == "voice_stream_end"]
        assert len(stream_ends) > 0
        assert stream_ends[-1]["reason"] == "error"


# ---------------------------------------------------------------------------
# Stop / suppress
# ---------------------------------------------------------------------------


class TestStopSuppress:
    @pytest.mark.asyncio
    async def test_stop_current_sends_stopped(self, voice_session, captured_messages):
        await voice_session.observe_pi_event({"type": "agent_start"})
        stream_id = voice_session._stream_id

        await voice_session.stop_current()

        stream_ends = [m for m in captured_messages["json"] if m.get("type") == "voice_stream_end"]
        stopped_ends = [e for e in stream_ends if e["reason"] == "stopped"]
        assert len(stopped_ends) > 0
        assert stopped_ends[-1]["streamId"] == stream_id

    @pytest.mark.asyncio
    async def test_stop_suppresses_current_run(self, voice_session, captured_messages):
        await voice_session.observe_pi_event({"type": "agent_start"})
        await voice_session.stop_current()

        # Feed more text — should be suppressed
        await voice_session.observe_pi_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "This should not be spoken."},
        })
        await asyncio.sleep(0.1)

        # No new frames after stop
        assert len(captured_messages["bytes"]) == 0


# ---------------------------------------------------------------------------
# Binary frame format
# ---------------------------------------------------------------------------


class TestBinaryFrameFormat:
    def test_build_frame_header(self):
        pcm = b"\x00\x01" * 100  # 100 samples
        frame = build_pcm_frame(stream_id=7, sequence=3, sample_rate=24000, pcm_s16le=pcm)

        header = frame[:PCM_HEADER_LEN]
        magic, version, flags, hlen, sid, seq, sr, sc = PCM_HEADER.unpack(header)

        assert magic == PCM_MAGIC
        assert version == PCM_VERSION
        assert flags == 0
        assert hlen == PCM_HEADER_LEN
        assert sid == 7
        assert seq == 3
        assert sr == 24000
        assert sc == 100

        assert len(frame) == PCM_HEADER_LEN + len(pcm)

    def test_frame_payload_length_matches_sample_count(self):
        pcm = b"\x00\x01" * 50
        frame = build_pcm_frame(1, 1, 24000, pcm)
        _, _, _, _, _, _, _, sample_count = PCM_HEADER.unpack(frame[:PCM_HEADER_LEN])
        assert sample_count * 2 == len(frame) - PCM_HEADER_LEN


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_stops_and_disables(self, voice_session, captured_messages):
        await voice_session.observe_pi_event({"type": "agent_start"})
        await voice_session.close()

        assert voice_session.closed
        assert not voice_session.enabled
        assert not voice_session.active_run


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_backlog_stops_on_overflow(self):
        """When queue limits are exceeded, voice stops with backlog reason."""
        tts = FakeTTSService(synthesize_delay=2.0)  # Very slow synthesis
        await tts.load()
        handle = await tts.prepare_voice(VoiceSettings())

        captured = {"json": [], "bytes": []}

        async def send_json(msg):
            captured["json"].append(msg)

        async def send_bytes(data):
            captured["bytes"].append(data)

        # Very small queue limits - each chunk is ~50 chars
        tight_config = FakeConfig()
        tight_config.TTS_MAX_QUEUE_CHUNKS = 10
        tight_config.TTS_MAX_QUEUE_CHARS = 100

        session = VoiceSession(tts, send_json, send_bytes, config_=tight_config)
        session.enabled = True
        session.ready = True
        session.voice_handle = handle

        await session.observe_pi_event({"type": "agent_start"})

        # Directly enqueue chunks to bypass chunker buffering
        # Each chunk is ~50 chars, limit is 100, so 3rd should trigger backlog
        chunk = "This is a speech chunk that is about fifty characters long. "
        assert len(chunk) >= 40  # Ensure chunk is substantial

        await session._enqueue_chunk(chunk)  # pending = ~50
        await asyncio.sleep(0)
        await session._enqueue_chunk(chunk)  # pending = ~100
        await asyncio.sleep(0)
        await session._enqueue_chunk(chunk)  # pending would be ~150 > 100, backlog!

        await asyncio.sleep(0.1)

        # Should have backlog error
        errors = [m for m in captured["json"] if m.get("type") == "voice_error"]
        backlog_errors = [e for e in errors if e.get("code") == "voice_backlog"]
        assert len(backlog_errors) > 0, f"Expected backlog error, got errors: {[e.get('code') for e in errors]}"
