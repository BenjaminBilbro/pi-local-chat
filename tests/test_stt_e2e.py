"""End-to-end STT tests with fake runtime.

Tests full STT flow: enable → audio packets → synthetic transcript → final events.
No RealtimeSTT dependency.
"""

import asyncio
import json
import struct

import pytest

from pi_chat.stt_session import AudioPacket, AudioPacketError, decode_audio_packet, packet_to_server_samples
from tests.fakes.stt import (
    FakeSTTService,
    FakeSTTSession,
    AudioPacket as FakeAudioPacket,
    build_audio_packet,
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def generate_silent_pcm(num_samples):
    """Generate silent PCM samples (int16)."""
    return b"\x00" * (num_samples * 2)


def generate_sine_pcm(num_samples, sample_rate=16000, frequency=440.0, amplitude=0.5):
    """Generate a sine wave PCM sample (int16)."""
    import math
    samples = bytearray()
    for i in range(num_samples):
        t = i / sample_rate
        value = int(amplitude * 32767 * math.sin(2 * math.pi * frequency * t))
        samples.extend(struct.pack("<h", value))
    return bytes(samples)


# ---------------------------------------------------------------------------
# Audio packet decoding tests
# ---------------------------------------------------------------------------

class TestDecodeAudioPacket:
    """Test audio packet decoding."""

    def test_decode_valid_packet(self):
        """Test decoding a valid audio packet."""
        audio = generate_silent_pcm(100)
        packet = build_audio_packet(16000, 1, audio)

        decoded = decode_audio_packet(packet)
        assert decoded.metadata["sampleRate"] == 16000
        assert decoded.metadata["channels"] == 1
        assert decoded.metadata["format"] == "pcm_s16le"
        assert decoded.metadata["frames"] == 100
        assert decoded.audio == audio

    def test_decode_stereo_packet(self):
        """Test decoding a stereo audio packet."""
        audio = generate_silent_pcm(200)  # 100 frames stereo
        packet = build_audio_packet(48000, 2, audio)

        decoded = decode_audio_packet(packet)
        assert decoded.metadata["sampleRate"] == 48000
        assert decoded.metadata["channels"] == 2
        assert decoded.metadata["frames"] == 100

    def test_decode_empty_audio(self):
        """Test decoding a packet with empty audio."""
        packet = build_audio_packet(16000, 1, b"")

        decoded = decode_audio_packet(packet)
        assert decoded.audio == b""
        assert decoded.metadata["frames"] == 0

    def test_decode_non_binary_raises(self):
        """Test decoding non-binary input raises AudioPacketError."""
        with pytest.raises(AudioPacketError, match="must be binary"):
            decode_audio_packet("not bytes")

    def test_decode_too_short_raises(self):
        """Test decoding packet too short raises AudioPacketError."""
        with pytest.raises(AudioPacketError, match="missing metadata length"):
            decode_audio_packet(b"\x00\x00")

    def test_decode_metadata_too_large_raises(self):
        """Test decoding packet with metadata too large raises."""
        # Metadata length exceeds MAX_METADATA_BYTES
        packet = struct.pack("<I", 100 * 1024 * 1024) + b"garbage"
        with pytest.raises(AudioPacketError, match="metadata is too large"):
            decode_audio_packet(packet)

    def test_decode_incomplete_metadata_raises(self):
        """Test decoding packet with incomplete metadata raises."""
        # Say metadata is 100 bytes but only provide 50
        packet = struct.pack("<I", 100) + b"x" * 50
        with pytest.raises(AudioPacketError, match="metadata is incomplete"):
            decode_audio_packet(packet)

    def test_decode_invalid_json_metadata_raises(self):
        """Test decoding packet with invalid JSON metadata raises."""
        metadata = b"not json"
        packet = struct.pack("<I", len(metadata)) + metadata + b"audio"
        with pytest.raises(AudioPacketError, match="metadata is invalid JSON"):
            decode_audio_packet(packet)

    def test_decode_non_object_metadata_raises(self):
        """Test decoding packet with non-object JSON metadata raises."""
        metadata = json.dumps([1, 2, 3]).encode()
        packet = struct.pack("<I", len(metadata)) + metadata + b"audio"
        with pytest.raises(AudioPacketError, match="metadata must be a JSON object"):
            decode_audio_packet(packet)


# ---------------------------------------------------------------------------
# Packet to server samples tests
# ---------------------------------------------------------------------------

class TestPacketToServerSamples:
    """Test packet_to_server_samples conversion."""

    def test_same_sample_rate_no_resample(self):
        """Test packet at server sample rate (16000) returns same samples."""
        audio = generate_silent_pcm(100)
        raw_packet = build_audio_packet(16000, 1, audio)
        decoded = decode_audio_packet(raw_packet)

        samples = packet_to_server_samples(decoded)
        assert len(samples) == 100
        assert samples.dtype == "int16"

    def test_resample_up(self):
        """Test resampling from lower to higher rate."""
        audio = generate_sine_pcm(8000, sample_rate=8000)  # 1 second
        raw_packet = build_audio_packet(8000, 1, audio)
        decoded = decode_audio_packet(raw_packet)

        samples = packet_to_server_samples(decoded)
        # Should be ~16000 samples (1 second at 16000Hz)
        assert 15000 <= len(samples) <= 17000

    def test_resample_down(self):
        """Test resampling from higher to lower rate."""
        audio = generate_sine_pcm(48000, sample_rate=48000)  # 1 second
        raw_packet = build_audio_packet(48000, 1, audio)
        decoded = decode_audio_packet(raw_packet)

        samples = packet_to_server_samples(decoded)
        # Should be ~16000 samples (1 second at 16000Hz)
        assert 15000 <= len(samples) <= 17000

    def test_stereo_downmix(self):
        """Test stereo packet is downmixed to mono."""
        audio = generate_silent_pcm(200)  # 100 frames stereo
        raw_packet = build_audio_packet(16000, 2, audio)
        decoded = decode_audio_packet(raw_packet)

        samples = packet_to_server_samples(decoded)
        assert len(samples) == 100  # 100 frames mono after downmix

    def test_invalid_sample_rate_raises(self):
        """Test packet with missing sampleRate raises."""
        metadata = json.dumps({"channels": 1, "format": "pcm_s16le"}).encode()
        packet = struct.pack("<I", len(metadata)) + metadata + b"\x00\x00"
        decoded = decode_audio_packet(packet)
        with pytest.raises(AudioPacketError, match="sampleRate"):
            packet_to_server_samples(decoded)

    def test_invalid_format_raises(self):
        """Test packet with unsupported format raises."""
        metadata = json.dumps({"sampleRate": 16000, "channels": 1, "format": "pcm_f32le"}).encode()
        packet = struct.pack("<I", len(metadata)) + metadata + b"\x00\x00"
        decoded = decode_audio_packet(packet)
        with pytest.raises(AudioPacketError, match="pcm_s16le"):
            packet_to_server_samples(decoded)

    def test_unaligned_audio_raises(self):
        """Test packet with unaligned audio raises."""
        metadata = json.dumps({"sampleRate": 16000, "channels": 1, "format": "pcm_s16le"}).encode()
        packet = struct.pack("<I", len(metadata)) + metadata + b"\x00"  # Odd byte count
        decoded = decode_audio_packet(packet)
        with pytest.raises(AudioPacketError, match="not aligned"):
            packet_to_server_samples(decoded)


# ---------------------------------------------------------------------------
# FakeSTTSession end-to-end tests
# ---------------------------------------------------------------------------

class TestFakeSTTSessionE2E:
    """Test FakeSTTSession end-to-end flow."""

    @pytest.mark.asyncio
    async def test_enable_sends_ready(self):
        """Test enable sends stt_state:ready."""
        service = FakeSTTService()
        events = []

        async def send_json(msg):
            events.append(msg)

        session = FakeSTTSession(service, send_json)
        await session.enable()

        assert session.enabled is True
        assert any(e["type"] == "stt_state" and e["state"] == "ready" for e in events)

    @pytest.mark.asyncio
    async def test_disable_sends_disabled(self):
        """Test disable sends stt_state:disabled."""
        service = FakeSTTService()
        events = []

        async def send_json(msg):
            events.append(msg)

        session = FakeSTTSession(service, send_json)
        await session.enable()
        await session.disable()

        assert session.enabled is False
        assert any(e["type"] == "stt_state" and e["state"] == "disabled" for e in events)

    @pytest.mark.asyncio
    async def test_ingest_audio_tracks_packets(self):
        """Test ingest_audio_packet tracks packet count."""
        service = FakeSTTService()

        async def send_json(msg):
            pass

        session = FakeSTTSession(service, send_json)
        await session.enable()

        audio = generate_silent_pcm(100)
        packet = build_audio_packet(16000, 1, audio)
        decoded = decode_audio_packet(packet)

        session.ingest_audio_packet(decoded)
        assert session.ingested_packets == 1

    @pytest.mark.asyncio
    async def test_synthetic_transcript_emitted(self):
        """Test synthetic transcript is emitted after enable."""
        service = FakeSTTService()
        events = []

        async def send_json(msg):
            events.append(msg)

        session = FakeSTTSession(
            service,
            send_json,
            synthetic_transcripts=["Hello world", "Second transcript"],
        )
        await session.enable()

        # Wait for synthetic transcripts
        await asyncio.sleep(0.2)

        final_events = [e for e in events if e["type"] == "stt_final"]
        assert len(final_events) >= 1
        assert final_events[0]["text"] == "Hello world"

    @pytest.mark.asyncio
    async def test_ingest_when_disabled_is_noop(self):
        """Test ingest_audio_packet is noop when disabled."""
        service = FakeSTTService()

        async def send_json(msg):
            pass

        session = FakeSTTSession(service, send_json)
        # Don't enable

        audio = generate_silent_pcm(100)
        packet = build_audio_packet(16000, 1, audio)
        decoded = decode_audio_packet(packet)

        session.ingest_audio_packet(decoded)
        assert session.ingested_packets == 0

    @pytest.mark.asyncio
    async def test_close_cleans_up(self):
        """Test close cleans up resources."""
        service = FakeSTTService()

        async def send_json(msg):
            pass

        session = FakeSTTSession(service, send_json)
        await session.enable()
        await session.close()

        assert session.closed is True
        assert session.enabled is False

    @pytest.mark.asyncio
    async def test_fail_on_enable_raises(self):
        """Test fail_on_enable raises RuntimeError."""
        service = FakeSTTService()

        async def send_json(msg):
            pass

        session = FakeSTTSession(service, send_json, fail_on_enable=True)
        with pytest.raises(RuntimeError, match="simulated enable failure"):
            await session.enable()


# ---------------------------------------------------------------------------
# Full flow simulation
# ---------------------------------------------------------------------------

class TestFullSTTFlow:
    """Test full STT flow simulation."""

    @pytest.mark.asyncio
    async def test_enable_audio_transcript(self):
        """Test full flow: enable → audio packets → transcript."""
        service = FakeSTTService()
        events = []

        async def send_json(msg):
            events.append(msg)

        session = FakeSTTSession(
            service,
            send_json,
            synthetic_transcripts=["Hello there."],
        )

        # Enable
        await session.enable()
        assert session.enabled is True

        # Send some audio packets
        for _ in range(5):
            audio = generate_sine_pcm(100, sample_rate=16000)
            packet = build_audio_packet(16000, 1, audio)
            decoded = decode_audio_packet(packet)
            session.ingest_audio_packet(decoded)

        # Wait for transcript
        await asyncio.sleep(0.15)

        # Verify events
        assert session.ingested_packets == 5
        final_events = [e for e in events if e["type"] == "stt_final"]
        assert len(final_events) >= 1
        assert final_events[0]["text"] == "Hello there."

        # Disable
        await session.disable()
        assert session.enabled is False
