"""Tests for STTSession audio packet handling and fake session — no RealtimeSTT, no GPU.

These tests verify:
- Audio packet decoding (valid/invalid packets)
- Audio packet building for test fixtures
- Resample behavior (same rate returns copy)
- FakeSTTSession enable/disable/ingest/close
- Callback bridge sends events to event loop
"""

import asyncio
import struct

import pytest

from pi_chat.stt_session import (
    AudioPacket,
    AudioPacketError,
    decode_audio_packet,
    packet_to_server_samples,
    resample_int16,
    SERVER_SAMPLE_RATE,
)
from tests.fakes.stt import FakeSTTService, FakeSTTSession, build_audio_packet


# ---------------------------------------------------------------------------
# Audio packet decoding tests
# ---------------------------------------------------------------------------


class TestDecodeAudioPacket:
    def test_decode_valid_packet(self):
        """Decode a valid audio packet with mono PCM at 16000Hz."""
        audio = b"\x00\x01" * 100  # 100 samples of PCM
        packet = build_audio_packet(16000, 1, audio)

        result = decode_audio_packet(packet)
        assert isinstance(result, AudioPacket)
        assert result.metadata["sampleRate"] == 16000
        assert result.metadata["channels"] == 1
        assert result.metadata["format"] == "pcm_s16le"
        assert result.metadata["frames"] == 100
        assert result.audio == audio

    def test_decode_stereo_packet(self):
        """Decode a valid stereo audio packet."""
        audio = b"\x00\x01\x00\x02" * 50  # 50 stereo frames
        packet = build_audio_packet(48000, 2, audio)

        result = decode_audio_packet(packet)
        assert result.metadata["sampleRate"] == 48000
        assert result.metadata["channels"] == 2
        assert result.metadata["frames"] == 50

    def test_decode_empty_audio(self):
        """Decode a packet with zero audio bytes."""
        packet = build_audio_packet(16000, 1, b"")

        result = decode_audio_packet(packet)
        assert result.audio == b""
        assert result.metadata["frames"] == 0

    def test_decode_bytearray_input(self):
        """Decode works with bytearray input."""
        audio = b"\x00\x01" * 10
        packet = bytearray(build_audio_packet(16000, 1, audio))

        result = decode_audio_packet(packet)
        assert result.audio == audio

    def test_decode_memoryview_input(self):
        """Decode works with memoryview input."""
        audio = b"\x00\x01" * 10
        packet = memoryview(build_audio_packet(16000, 1, audio))

        result = decode_audio_packet(packet)
        assert result.audio == audio


class TestDecodeAudioPacketErrors:
    def test_error_non_binary_input(self):
        """Raise error for non-binary input."""
        with pytest.raises(AudioPacketError, match="must be binary"):
            decode_audio_packet("not bytes")  # type: ignore

    def test_error_too_short(self):
        """Raise error for packet shorter than 4 bytes."""
        with pytest.raises(AudioPacketError, match="metadata length"):
            decode_audio_packet(b"\x00\x01\x02")

    def test_error_metadata_too_large(self):
        """Raise error for metadata length exceeding max."""
        packet = struct.pack("<I", 100 * 1024 * 1024) + b"garbage"
        with pytest.raises(AudioPacketError, match="metadata is too large"):
            decode_audio_packet(packet)

    def test_error_metadata_incomplete(self):
        """Raise error when metadata is truncated."""
        metadata = b'{"sampleRate": 16000}'
        packet = struct.pack("<I", len(metadata)) + metadata[:5]
        with pytest.raises(AudioPacketError, match="metadata is incomplete"):
            decode_audio_packet(packet)

    def test_error_invalid_json_metadata(self):
        """Raise error for invalid JSON in metadata."""
        metadata = b"not json"
        packet = struct.pack("<I", len(metadata)) + metadata
        with pytest.raises(AudioPacketError, match="invalid JSON"):
            decode_audio_packet(packet)

    def test_error_metadata_not_object(self):
        """Raise error when metadata is not a JSON object."""
        metadata = b"[1,2,3]"
        packet = struct.pack("<I", len(metadata)) + metadata
        with pytest.raises(AudioPacketError, match="must be a JSON object"):
            decode_audio_packet(packet)


# ---------------------------------------------------------------------------
# Packet to server samples tests
# ---------------------------------------------------------------------------


class TestPacketToServerSamples:
    def test_same_rate_no_resample(self):
        """Packet at server sample rate returns samples without resampling."""
        import numpy as np
        samples = np.array([100, 200, 300], dtype=np.int16)
        packet = AudioPacket(
            metadata={"sampleRate": SERVER_SAMPLE_RATE, "channels": 1, "format": "pcm_s16le"},
            audio=samples.tobytes(),
        )

        result = packet_to_server_samples(packet)
        np.testing.assert_array_equal(result, samples)

    def test_resample_higher_rate(self):
        """Packet at higher sample rate is resampled down."""
        import numpy as np
        # 48000Hz samples -> 16000Hz (3x reduction)
        samples = np.array([100, 200, 300, 100, 200, 300], dtype=np.int16)
        packet = AudioPacket(
            metadata={"sampleRate": 48000, "channels": 1, "format": "pcm_s16le"},
            audio=samples.tobytes(),
        )

        result = packet_to_server_samples(packet)
        assert len(result) < len(samples)  # Resampled down
        assert result.dtype == np.int16

    def test_resample_lower_rate(self):
        """Packet at lower sample rate is resampled up."""
        import numpy as np
        # 8000Hz samples -> 16000Hz (2x increase)
        samples = np.array([100, 200, 300], dtype=np.int16)
        packet = AudioPacket(
            metadata={"sampleRate": 8000, "channels": 1, "format": "pcm_s16le"},
            audio=samples.tobytes(),
        )

        result = packet_to_server_samples(packet)
        assert len(result) > len(samples)  # Resampled up
        assert result.dtype == np.int16

    def test_stereo_downmix(self):
        """Stereo packet is downmixed to mono."""
        import numpy as np
        # Stereo: [L1, R1, L2, R2] -> [avg1, avg2]
        samples = np.array([200, 100, 400, 200], dtype=np.int16)  # avg: 150, 300
        packet = AudioPacket(
            metadata={"sampleRate": SERVER_SAMPLE_RATE, "channels": 2, "format": "pcm_s16le"},
            audio=samples.tobytes(),
        )

        result = packet_to_server_samples(packet)
        assert len(result) == 2
        assert result[0] == 150
        assert result[1] == 300

    def test_error_invalid_sample_rate(self):
        """Raise error for missing sampleRate."""
        packet = AudioPacket(
            metadata={"channels": 1, "format": "pcm_s16le"},
            audio=b"\x00\x01",
        )
        with pytest.raises(AudioPacketError, match="sampleRate"):
            packet_to_server_samples(packet)

    def test_error_invalid_channels(self):
        """Raise error for invalid channels value."""
        packet = AudioPacket(
            metadata={"sampleRate": 16000, "channels": 0, "format": "pcm_s16le"},
            audio=b"\x00\x01",
        )
        with pytest.raises(AudioPacketError, match="channels"):
            packet_to_server_samples(packet)

    def test_error_wrong_format(self):
        """Raise error for non-pcm_s16le format."""
        packet = AudioPacket(
            metadata={"sampleRate": 16000, "channels": 1, "format": "pcm_f32le"},
            audio=b"\x00\x01\x00\x01",
        )
        with pytest.raises(AudioPacketError, match="pcm_s16le"):
            packet_to_server_samples(packet)

    def test_error_unaligned_audio(self):
        """Raise error for audio not aligned to whole frames."""
        packet = AudioPacket(
            metadata={"sampleRate": 16000, "channels": 1, "format": "pcm_s16le"},
            audio=b"\x00\x01\x02",  # 3 bytes, not aligned to 2
        )
        with pytest.raises(AudioPacketError, match="not aligned"):
            packet_to_server_samples(packet)


# ---------------------------------------------------------------------------
# Resample tests
# ---------------------------------------------------------------------------


class TestResampleInt16:
    def test_same_rate_returns_copy(self):
        """Resampling at same rate returns a copy."""
        import numpy as np
        samples = np.array([100, 200, 300], dtype=np.int16)
        result = resample_int16(samples, 16000, 16000)
        np.testing.assert_array_equal(result, samples)
        assert result is not samples  # Should be a copy

    def test_empty_input(self):
        """Resampling empty input returns empty array."""
        import numpy as np
        samples = np.array([], dtype=np.int16)
        result = resample_int16(samples, 48000, 16000)
        assert len(result) == 0

    def test_clips_to_int16_range(self):
        """Resampled values are clipped to int16 range."""
        import numpy as np
        # Very large values that might overflow during resampling
        samples = np.array([32767, -32768], dtype=np.int16)
        result = resample_int16(samples, 16000, 8000)
        assert np.all(result >= -32768)
        assert np.all(result <= 32767)


# ---------------------------------------------------------------------------
# Fake STTSession tests
# ---------------------------------------------------------------------------


class TestFakeSTTSession:
    async def test_enable_sends_ready_event(self):
        """Enable creates session and sends stt_state:ready."""
        events = []

        async def send_json(event):
            events.append(event)

        service = FakeSTTService()
        session = FakeSTTSession(service, send_json)
        await session.enable()

        assert session.enabled
        assert {"type": "stt_state", "state": "ready"} in events

    async def test_disable_sends_disabled_event(self):
        """Disable stops session and sends stt_state:disabled."""
        events = []

        async def send_json(event):
            events.append(event)

        service = FakeSTTService()
        session = FakeSTTSession(service, send_json)
        await session.enable()
        await session.disable()

        assert not session.enabled
        assert {"type": "stt_state", "state": "disabled"} in events

    async def test_ingest_audio_packet_tracks_count(self):
        """Ingest audio packets are tracked."""
        events = []

        async def send_json(event):
            events.append(event)

        service = FakeSTTService()
        session = FakeSTTSession(service, send_json)
        await session.enable()

        audio = b"\x00\x01" * 100
        packet = build_audio_packet(16000, 1, audio)
        decoded = decode_audio_packet(packet)

        session.ingest_audio_packet(decoded)
        session.ingest_audio_packet(decoded)

        assert session.ingested_packets == 2

    async def test_ingest_ignored_when_disabled(self):
        """Ingest audio is ignored when session is not enabled."""
        events = []

        async def send_json(event):
            events.append(event)

        service = FakeSTTService()
        session = FakeSTTSession(service, send_json)
        # Not enabled

        audio = b"\x00\x01" * 100
        packet = build_audio_packet(16000, 1, audio)
        decoded = decode_audio_packet(packet)

        session.ingest_audio_packet(decoded)

        assert session.ingested_packets == 0

    async def test_fail_on_ingest_sends_error(self):
        """Fail on ingest sends stt_error event."""
        events = []

        async def send_json(event):
            events.append(event)

        service = FakeSTTService()
        session = FakeSTTSession(service, send_json, fail_on_ingest=True)
        await session.enable()

        audio = b"\x00\x01" * 100
        packet = build_audio_packet(16000, 1, audio)
        decoded = decode_audio_packet(packet)

        session.ingest_audio_packet(decoded)

        # Give event loop time to process the scheduled coroutine
        await asyncio.sleep(0.05)

        error_events = [e for e in events if e["type"] == "stt_error"]
        assert len(error_events) == 1
        assert error_events[0]["code"] == "audio_feed_error"

    async def test_close_cleans_up(self):
        """Close disables session and cleans up."""
        events = []

        async def send_json(event):
            events.append(event)

        service = FakeSTTService()
        session = FakeSTTSession(service, send_json)
        await session.enable()
        await session.close()

        assert session.closed
        assert not session.enabled

    async def test_emits_synthetic_transcription(self):
        """Fake session emits synthetic transcription events."""
        events = []

        async def send_json(event):
            events.append(event)

        service = FakeSTTService()
        session = FakeSTTSession(
            service,
            send_json,
            synthetic_transcripts=["Hello world.", "How are you?"],
        )
        await session.enable()

        # Wait for synthetic transcriptions to be emitted
        await asyncio.sleep(0.2)

        final_events = [e for e in events if e["type"] == "stt_final"]
        assert len(final_events) == 2
        assert final_events[0]["text"] == "Hello world."
        assert final_events[1]["text"] == "How are you?"
