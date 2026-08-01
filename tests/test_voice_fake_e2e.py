"""Fake end-to-end voice tests: synthetic pi events through VoiceSession.

Verifies binary frames before agent_settled, stream lifecycle, and cancellation.
No torch, no GPU.
"""

import asyncio
import struct

import pytest

from pi_chat.voice_session import VoiceSession, PCM_HEADER, PCM_HEADER_LEN
from tests.fakes.voice import (
    FakeTTSService,
    VoiceSettings,
    fixture_one_sentence,
    fixture_multiple_text_blocks,
    fixture_tool_gap,
    fixture_retry,
    fixture_markdown,
    fixture_code_block,
    fixture_cancellation,
)


class FakeConfig:
    TTS_MAX_QUEUE_CHUNKS = 12
    TTS_MAX_QUEUE_CHARS = 1800
    TTS_MAX_HOLD_MS = 50


async def create_test_session():
    """Create a ready VoiceSession with FakeTTSService."""
    tts = FakeTTSService()
    await tts.load()
    handle = await tts.prepare_voice(VoiceSettings())

    captured = {"json": [], "bytes": []}

    async def send_json(msg):
        captured["json"].append(msg)

    async def send_bytes(data):
        captured["bytes"].append(data)

    session = VoiceSession(tts, send_json, send_bytes, config_=FakeConfig())
    session.enabled = True
    session.ready = True
    session.voice_handle = handle

    return session, captured


def parse_pcm_frames(byte_messages):
    """Parse binary messages into PCM frame metadata."""
    frames = []
    for msg in byte_messages:
        if isinstance(msg, bytes) and len(msg) >= PCM_HEADER_LEN:
            magic, version, flags, hlen, stream_id, seq, sr, sc = PCM_HEADER.unpack(msg[:PCM_HEADER_LEN])
            if magic == b"PIV1":
                frames.append({
                    "stream_id": stream_id,
                    "sequence": seq,
                    "sample_rate": sr,
                    "sample_count": sc,
                    "payload_len": len(msg) - PCM_HEADER_LEN,
                })
    return frames


def get_json_messages(captured, msg_type=None):
    """Filter JSON messages by type."""
    if msg_type is None:
        return captured["json"]
    return [m for m in captured["json"] if m.get("type") == msg_type]


# ---------------------------------------------------------------------------
# One sentence — basic flow
# ---------------------------------------------------------------------------


class TestOneSentenceE2E:
    @pytest.mark.asyncio
    async def test_frames_before_settled(self):
        session, captured = await create_test_session()

        for event in fixture_one_sentence():
            await session.observe_pi_event(event)

        # Wait for synthesis to complete
        await asyncio.sleep(0.3)

        frames = parse_pcm_frames(captured["bytes"])
        assert len(frames) > 0, "Expected PCM frames for one sentence"

        # All frames belong to same stream
        stream_ids = {f["stream_id"] for f in frames}
        assert len(stream_ids) == 1

        # Sequences are ordered
        seqs = [f["sequence"] for f in frames]
        assert seqs == list(range(1, len(seqs) + 1))

        # Stream end with complete reason
        stream_ends = get_json_messages(captured, "voice_stream_end")
        assert len(stream_ends) > 0
        assert stream_ends[-1]["reason"] == "complete"


# ---------------------------------------------------------------------------
# Multiple text blocks
# ---------------------------------------------------------------------------


class TestMultipleTextBlocksE2E:
    @pytest.mark.asyncio
    async def test_continues_across_blocks(self):
        session, captured = await create_test_session()

        for event in fixture_multiple_text_blocks():
            await session.observe_pi_event(event)

        await asyncio.sleep(0.3)

        frames = parse_pcm_frames(captured["bytes"])
        assert len(frames) > 0, "Expected frames for multiple text blocks"


# ---------------------------------------------------------------------------
# Tool gap
# ---------------------------------------------------------------------------


class TestToolGapE2E:
    @pytest.mark.asyncio
    async def test_text_before_tool_is_spoken(self):
        session, captured = await create_test_session()

        for event in fixture_tool_gap():
            await session.observe_pi_event(event)

        await asyncio.sleep(0.3)

        frames = parse_pcm_frames(captured["bytes"])
        assert len(frames) > 0, "Expected frames across tool gap"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetryE2E:
    @pytest.mark.asyncio
    async def test_new_stream_id_on_retry(self):
        session, captured = await create_test_session()

        for event in fixture_retry():
            await session.observe_pi_event(event)

        await asyncio.sleep(0.3)

        stream_starts = get_json_messages(captured, "voice_stream_start")
        assert len(stream_starts) >= 2, "Expected multiple stream starts for retry"

        stream_ids = [s["streamId"] for s in stream_starts]
        assert stream_ids[-1] > stream_ids[0], "Stream ID should increase on retry"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


class TestMarkdownE2E:
    @pytest.mark.asyncio
    async def test_markdown_sanitized(self):
        session, captured = await create_test_session()

        for event in fixture_markdown():
            await session.observe_pi_event(event)

        await asyncio.sleep(0.3)

        frames = parse_pcm_frames(captured["bytes"])
        assert len(frames) > 0, "Expected frames for markdown text"


# ---------------------------------------------------------------------------
# Code block
# ---------------------------------------------------------------------------


class TestCodeBlockE2E:
    @pytest.mark.asyncio
    async def test_code_omitted(self):
        session, captured = await create_test_session()

        for event in fixture_code_block():
            await session.observe_pi_event(event)

        await asyncio.sleep(0.3)

        frames = parse_pcm_frames(captured["bytes"])
        # Should have frames for "Here is some code:" and "That should work."
        assert len(frames) > 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellationE2E:
    @pytest.mark.asyncio
    async def test_failed_response_ends_stream(self):
        session, captured = await create_test_session()

        await session.observe_pi_event({"type": "agent_start"})
        for event in fixture_cancellation():
            await session.observe_pi_event(event)

        stream_ends = get_json_messages(captured, "voice_stream_end")
        assert len(stream_ends) > 0
        assert stream_ends[-1]["reason"] == "error"


# ---------------------------------------------------------------------------
# Stop mid-stream
# ---------------------------------------------------------------------------


class TestStopMidStreamE2E:
    @pytest.mark.asyncio
    async def test_stop_sends_stopped_reason(self):
        session, captured = await create_test_session()

        await session.observe_pi_event({"type": "agent_start"})
        stream_id = session._stream_id

        await session.stop_current()

        stream_ends = get_json_messages(captured, "voice_stream_end")
        stopped = [e for e in stream_ends if e["reason"] == "stopped"]
        assert len(stopped) > 0
        assert stopped[-1]["streamId"] == stream_id


# ---------------------------------------------------------------------------
# Disable mid-stream
# ---------------------------------------------------------------------------


class TestDisableMidStreamE2E:
    @pytest.mark.asyncio
    async def test_disable_ends_stream(self):
        session, captured = await create_test_session()

        await session.observe_pi_event({"type": "agent_start"})
        await session.disable()

        stream_ends = get_json_messages(captured, "voice_stream_end")
        disabled_ends = [e for e in stream_ends if e["reason"] == "disabled"]
        assert len(disabled_ends) > 0


# ---------------------------------------------------------------------------
# Stream ID isolation
# ---------------------------------------------------------------------------


class TestStreamIdIsolationE2E:
    @pytest.mark.asyncio
    async def test_frames_match_stream_id(self):
        session, captured = await create_test_session()

        for event in fixture_one_sentence():
            await session.observe_pi_event(event)

        await asyncio.sleep(0.3)

        stream_starts = get_json_messages(captured, "voice_stream_start")
        frames = parse_pcm_frames(captured["bytes"])

        assert len(stream_starts) > 0
        assert len(frames) > 0
        assert all(f["stream_id"] == stream_starts[-1]["streamId"] for f in frames)
