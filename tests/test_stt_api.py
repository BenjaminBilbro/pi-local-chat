"""Integration tests for STT WebSocket commands.

Tests stt_enable/stt_disable commands and binary audio packet handling
using FakeSTTService/FakeSTTSession (no RealtimeSTT dependency).
"""

import asyncio
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_chat.stt_session import decode_audio_packet
from pi_chat.websocket import handle_websocket, _dispatch_command
from tests.fakes.stt import FakeSTTService, FakeSTTSession, build_audio_packet


def build_test_audio_packet(sample_rate=16000, channels=1, num_frames=100):
    """Build a simple test audio packet with silent PCM data."""
    audio_bytes = b"\x00" * (num_frames * channels * 2)
    return build_audio_packet(sample_rate, channels, audio_bytes)


class FakeSTTSessionFactory:
    """Factory that returns FakeSTTSession instances for testing."""

    def __init__(self, synthetic_transcripts=None):
        self._synthetic_transcripts = synthetic_transcripts or ["Hello there."]

    def __call__(self, stt_service, pi, config_):
        return FakeSTTSession(
            stt_service=stt_service,
            send_json=pi.send_browser_json,
            config_=config_,
            synthetic_transcripts=self._synthetic_transcripts,
        )


class MockPiProcess:
    """Mock PiProcess for WebSocket testing."""

    def __init__(self):
        self.ws = None
        self.account = None
        self.proc = None
        self.session_id = "test-session"
        self.event_observer = None
        self._sent_json = []
        self._sent_bytes = []
        self._sent_commands = []

    async def send_browser_json(self, msg):
        self._sent_json.append(msg)

    async def send_browser_bytes(self, data):
        self._sent_bytes.append(data)

    async def send(self, cmd):
        self._sent_commands.append(cmd)

    async def kill(self):
        pass

    async def spawn(self):
        self.proc = MagicMock()

    def get_sent_json(self, msg_type=None):
        if msg_type is None:
            return self._sent_json
        return [m for m in self._sent_json if m.get("type") == msg_type]


class MockWebSocket:
    """Mock WebSocket for testing."""

    def __init__(self, received_messages=None):
        self._received = received_messages or []
        self._index = 0
        self._sent_json = []
        self._sent_bytes = []
        self.accepted = False
        self.closed = False
        self.cookies = {}
        self.query_params = {}

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if self._index >= len(self._received):
            raise Exception("No more messages")
        msg = self._received[self._index]
        self._index += 1
        return msg

    async def receive_text(self):
        if self._index >= len(self._received):
            raise Exception("No more messages")
        msg = self._received[self._index]
        self._index += 1
        if isinstance(msg, str):
            return msg
        return msg.get("text", "")

    async def receive_json(self):
        if self._index >= len(self._received):
            raise Exception("No more messages")
        msg = self._received[self._index]
        self._index += 1
        if isinstance(msg, dict):
            return msg
        return json.loads(msg)

    async def send_json(self, msg):
        self._sent_json.append(msg)

    async def send_bytes(self, data):
        self._sent_bytes.append(data)

    async def close(self, code=1000):
        self.closed = True

    def get_sent_json(self, msg_type=None):
        if msg_type is None:
            return self._sent_json
        return [m for m in self._sent_json if m.get("type") == msg_type]


# ---------------------------------------------------------------------------
# STT command dispatch tests
# ---------------------------------------------------------------------------

class TestSTTCommandDispatch:
    """Test STT command dispatch in _dispatch_command."""

    @pytest.mark.asyncio
    async def test_stt_enable_sends_ready(self):
        """Test stt_enable command triggers stt_state:ready."""
        websocket = MockWebSocket()
        pi = MockPiProcess()
        stt_service = FakeSTTService()
        stt = FakeSTTSession(stt_service, pi.send_browser_json)

        await _dispatch_command(
            websocket, pi, "b",
            {"type": "stt_enable"},
            voice=None, stt=stt,
        )

        # Wait for async enable to complete
        await asyncio.sleep(0.05)

        assert stt.enabled is True
        ready_events = pi.get_sent_json("stt_state")
        assert any(e["state"] == "ready" for e in ready_events)

    @pytest.mark.asyncio
    async def test_stt_disable_sends_disabled(self):
        """Test stt_disable command triggers stt_state:disabled."""
        websocket = MockWebSocket()
        pi = MockPiProcess()
        stt_service = FakeSTTService()
        stt = FakeSTTSession(stt_service, pi.send_browser_json)

        # Enable first
        await _dispatch_command(
            websocket, pi, "b",
            {"type": "stt_enable"},
            voice=None, stt=stt,
        )
        await asyncio.sleep(0.05)

        # Then disable
        await _dispatch_command(
            websocket, pi, "b",
            {"type": "stt_disable"},
            voice=None, stt=stt,
        )
        await asyncio.sleep(0.05)

        assert stt.enabled is False
        disabled_events = pi.get_sent_json("stt_state")
        assert any(e["state"] == "disabled" for e in disabled_events)

    @pytest.mark.asyncio
    async def test_stt_enable_without_stt_sends_error(self):
        """Test stt_enable sends error when STT is not configured."""
        websocket = MockWebSocket()
        pi = MockPiProcess()

        await _dispatch_command(
            websocket, pi, "b",
            {"type": "stt_enable"},
            voice=None, stt=None,
        )

        error_events = websocket.get_sent_json("stt_error")
        assert len(error_events) > 0
        assert error_events[-1]["code"] == "stt_not_configured"

    @pytest.mark.asyncio
    async def test_stt_disable_without_stt_is_noop(self):
        """Test stt_disable is noop when STT is not configured."""
        websocket = MockWebSocket()
        pi = MockPiProcess()

        # Should not raise
        await _dispatch_command(
            websocket, pi, "b",
            {"type": "stt_disable"},
            voice=None, stt=None,
        )

        # No errors sent
        assert len(websocket.get_sent_json("stt_error")) == 0


# ---------------------------------------------------------------------------
# Binary audio packet tests
# ---------------------------------------------------------------------------

class TestBinaryAudioPackets:
    """Test binary audio packet handling."""

    @pytest.mark.asyncio
    async def test_binary_packet_accepted_when_enabled(self):
        """Test binary audio packets are accepted when STT is enabled."""
        websocket = MockWebSocket()
        pi = MockPiProcess()
        stt_service = FakeSTTService()
        stt = FakeSTTSession(stt_service, pi.send_browser_json)

        # Enable STT
        await stt.enable()

        # Send binary packet via decode + ingest
        audio_packet = build_test_audio_packet()
        decoded = decode_audio_packet(audio_packet)
        stt.ingest_audio_packet(decoded)

        assert stt.ingested_packets == 1

    @pytest.mark.asyncio
    async def test_binary_packet_ignored_when_disabled(self):
        """Test binary audio packets are ignored when STT is disabled."""
        websocket = MockWebSocket()
        pi = MockPiProcess()
        stt_service = FakeSTTService()
        stt = FakeSTTSession(stt_service, pi.send_browser_json)

        # Don't enable STT
        audio_packet = build_test_audio_packet()
        decoded = decode_audio_packet(audio_packet)
        stt.ingest_audio_packet(decoded)

        assert stt.ingested_packets == 0

    @pytest.mark.asyncio
    async def test_invalid_binary_packet_returns_error(self):
        """Test invalid binary packet returns stt_error."""
        from pi_chat.stt_session import AudioPacketError

        websocket = MockWebSocket()
        pi = MockPiProcess()
        stt_service = FakeSTTService()
        stt = FakeSTTSession(stt_service, pi.send_browser_json)

        await stt.enable()

        # Simulate invalid packet handling (as in websocket.py)
        try:
            decoded = decode_audio_packet(b"\x00\x00")  # Too short
        except AudioPacketError as e:
            await websocket.send_json({
                "type": "stt_error",
                "code": "invalid_packet",
                "message": str(e),
                "recoverable": True,
            })

        error_events = websocket.get_sent_json("stt_error")
        assert len(error_events) > 0
        assert error_events[-1]["code"] == "invalid_packet"


# ---------------------------------------------------------------------------
# FakeSTTSession integration tests
# ---------------------------------------------------------------------------

class TestFakeSTTSessionIntegration:
    """Test FakeSTTSession integration with WebSocket pattern."""

    @pytest.mark.asyncio
    async def test_enable_disable_cycle(self):
        """Test multiple enable/disable cycles."""
        stt_service = FakeSTTService()
        pi = MockPiProcess()

        for _ in range(3):
            stt = FakeSTTSession(stt_service, pi.send_browser_json)

            await stt.enable()
            assert stt.enabled is True
            assert any(e["type"] == "stt_state" and e["state"] == "ready"
                       for e in pi.get_sent_json())

            await stt.disable()
            assert stt.enabled is False
            assert any(e["type"] == "stt_state" and e["state"] == "disabled"
                       for e in pi.get_sent_json())

    @pytest.mark.asyncio
    async def test_synthetic_transcript_emitted(self):
        """Test FakeSTTSession emits synthetic transcription."""
        stt_service = FakeSTTService()
        pi = MockPiProcess()

        stt = FakeSTTSession(
            stt_service,
            pi.send_browser_json,
            synthetic_transcripts=["Test transcript"],
        )

        await stt.enable()
        await asyncio.sleep(0.15)

        final_events = pi.get_sent_json("stt_final")
        assert len(final_events) >= 1
        assert final_events[0]["text"] == "Test transcript"

    @pytest.mark.asyncio
    async def test_close_on_disconnect(self):
        """Test STT session is closed on disconnect."""
        stt_service = FakeSTTService()
        pi = MockPiProcess()

        stt = FakeSTTSession(stt_service, pi.send_browser_json)
        await stt.enable()
        assert stt.enabled is True

        await stt.close()
        assert stt.closed is True
        assert stt.enabled is False
