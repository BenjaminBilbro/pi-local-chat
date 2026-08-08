"""Deterministic fake STT service and session for testing without RealtimeSTT.

This module provides fake implementations that:
- Never import RealtimeSTT at module load time
- Produce deterministic, machine-verifiable transcription output
- Support configurable failures, delays, and state tracking
- Match the real STTService and STTSession public API
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


# ---------------------------------------------------------------------------
# Audio packet types (mirrors pi_chat.stt_session)
# ---------------------------------------------------------------------------

SERVER_SAMPLE_RATE = 16000
MAX_METADATA_BYTES = 64 * 1024


class AudioPacketError(ValueError):
    """Raised when audio packet is malformed."""
    pass


@dataclass(frozen=True)
class AudioPacket:
    """Decoded audio packet with metadata and PCM payload."""
    metadata: Dict[str, Any]
    audio: bytes


def decode_audio_packet(message: bytes) -> AudioPacket:
    """Decode a binary audio packet from the browser.

    Format: [4 bytes: metadata length (uint32 LE)][metadata JSON][PCM audio bytes]
    """
    if not isinstance(message, (bytes, bytearray, memoryview)):
        raise AudioPacketError("audio packet must be binary")

    data = bytes(message)
    if len(data) < 4:
        raise AudioPacketError("audio packet is missing metadata length")

    metadata_length = struct.unpack("<I", data[:4])[0]
    if metadata_length > MAX_METADATA_BYTES:
        raise AudioPacketError("audio packet metadata is too large")
    if len(data) < 4 + metadata_length:
        raise AudioPacketError("audio packet metadata is incomplete")

    metadata_bytes = data[4:4 + metadata_length]
    audio = data[4 + metadata_length:]
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AudioPacketError("audio packet metadata is invalid JSON") from exc
    if not isinstance(metadata, dict):
        raise AudioPacketError("audio packet metadata must be a JSON object")

    return AudioPacket(metadata=metadata, audio=audio)


def build_audio_packet(sample_rate: int, channels: int, audio_bytes: bytes) -> bytes:
    """Build a binary audio packet for testing.

    Format: [4 bytes: metadata length (uint32 LE)][metadata JSON][PCM audio bytes]
    """
    metadata = {
        "sampleRate": sample_rate,
        "channels": channels,
        "format": "pcm_s16le",
        "frames": len(audio_bytes) // (channels * 2),
    }
    metadata_bytes = json.dumps(metadata).encode("utf-8")
    packet = struct.pack("<I", len(metadata_bytes)) + metadata_bytes + audio_bytes
    return packet


# ---------------------------------------------------------------------------
# Fake STT service
# ---------------------------------------------------------------------------


class FakeSTTService:
    """Fake STTService for testing without RealtimeSTT.

    Matches the real STTService API but does not import RealtimeSTT.
    Supports configurable failures and delays.
    """

    def __init__(
        self,
        *,
        load_delay: float = 0.0,
        fail_on_load: bool = False,
        fail_with_import_error: bool = False,
        config_: Any | None = None,
    ):
        self._config = config_
        self._load_delay = load_delay
        self._fail_on_load = fail_on_load
        self._fail_with_import_error = fail_with_import_error
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._load_future = None
        self._load_count = 0
        self._close_count = 0

    async def load(self) -> None:
        """Load the fake STT service. Idempotent and deduplicated."""
        async with self._load_lock:
            if self._loaded:
                return
            if self._load_future is not None:
                await asyncio.shield(self._load_future)
                return

            self._load_future = asyncio.get_event_loop().create_future()

        try:
            self._load_count += 1
            if self._load_delay > 0:
                await asyncio.sleep(self._load_delay)

            if self._fail_on_load:
                if self._fail_with_import_error:
                    raise ImportError("No module named 'RealtimeSTT'")
                raise RuntimeError("FakeSTTService: simulated load failure")

            self._loaded = True
            if self._load_future and not self._load_future.done():
                self._load_future.set_result(None)

        except Exception as e:
            if self._load_future and not self._load_future.done():
                self._load_future.set_exception(e)
            raise
        finally:
            self._load_future = None

    async def close(self) -> None:
        """Close the service and release resources."""
        self._close_count += 1
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        """Return True if the service is loaded."""
        return self._loaded

    def status(self) -> dict:
        """Return service status for diagnostics."""
        return {
            "loaded": self._loaded,
            "load_count": self._load_count,
            "close_count": self._close_count,
        }


# ---------------------------------------------------------------------------
# Fake STT session
# ---------------------------------------------------------------------------


class FakeSTTSession:
    """Fake STTSession for testing without RealtimeSTT.

    Matches the real STTSession API but does not import RealtimeSTT.
    Emits synthetic transcription events for testing.
    """

    def __init__(
        self,
        stt_service: FakeSTTService,
        send_json: Callable,
        config_: Any | None = None,
        *,
        enable_delay: float = 0.0,
        fail_on_enable: bool = False,
        synthetic_transcripts: list[str] | None = None,
        fail_on_ingest: bool = False,
    ):
        self.stt_service = stt_service
        self._send_json = send_json
        self._config = config_

        # State
        self.enabled = False
        self.active = False
        self.closed = False
        self._activation_id = 0

        # Fake config
        self._enable_delay = enable_delay
        self._fail_on_enable = fail_on_enable
        self._synthetic_transcripts = synthetic_transcripts or ["Hello there."]
        self._fail_on_ingest = fail_on_ingest
        self._transcript_index = 0
        self._ingested_packets = 0
        self._events_sent = []

        # Fake recorder state
        self._recorder = None
        self._text_thread = None
        self._text_thread_active = threading.Event()
        self._loop = asyncio.get_running_loop()

    async def enable(self, settings: Optional[dict] = None) -> None:
        """Enable STT: create fake recorder, start text worker thread."""
        self._activation_id += 1
        await self.stt_service.load()

        if self._enable_delay > 0:
            await asyncio.sleep(self._enable_delay)

        if self._fail_on_enable:
            raise RuntimeError("FakeSTTSession: simulated enable failure")

        # Create fake recorder
        self._recorder = self._FakeRecorder()

        # Start text worker thread
        self._text_thread_active.set()
        self._text_thread = threading.Thread(
            target=self._text_worker_loop, daemon=True
        )
        self._text_thread.start()

        self.enabled = True
        await self._send_json({"type": "stt_state", "state": "ready"})
        self._events_sent.append({"type": "stt_state", "state": "ready"})

    async def disable(self) -> None:
        """Disable STT: stop text worker, shutdown recorder."""
        # Stop text worker
        self._text_thread_active.clear()
        if self._text_thread:
            self._text_thread.join(timeout=3)
            self._text_thread = None

        # Shutdown fake recorder
        if self._recorder:
            self._recorder.shutdown()
            self._recorder = None

        self.enabled = False
        self.active = False
        await self._send_json({"type": "stt_state", "state": "disabled"})
        self._events_sent.append({"type": "stt_state", "state": "disabled"})

    def ingest_audio_packet(self, packet: AudioPacket) -> None:
        """Ingest an audio packet (fake: just tracks count)."""
        if not self.enabled or self._recorder is None:
            return

        self._ingested_packets += 1

        if self._fail_on_ingest:
            self._safe_send({
                "type": "stt_error",
                "code": "audio_feed_error",
                "message": "FakeSTTSession: simulated ingest failure",
                "recoverable": True,
            })
            self._events_sent.append({
                "type": "stt_error",
                "code": "audio_feed_error",
                "message": "FakeSTTSession: simulated ingest failure",
                "recoverable": True,
            })
            return

        # Feed to fake recorder
        self._recorder.feed_audio(packet)

    def _text_worker_loop(self) -> None:
        """Background thread: emit synthetic transcriptions."""
        while self._text_thread_active.is_set():
            try:
                # Wait a bit, then emit a synthetic transcription
                time.sleep(0.05)
                if self._transcript_index < len(self._synthetic_transcripts):
                    text = self._synthetic_transcripts[self._transcript_index]
                    self._transcript_index += 1
                    self._safe_send({"type": "stt_final", "text": text})
                    self._events_sent.append({"type": "stt_final", "text": text})
            except Exception:
                if self._text_thread_active.is_set():
                    self._safe_send({
                        "type": "stt_error",
                        "code": "transcription_error",
                        "message": "FakeSTTSession: text worker error",
                        "recoverable": True,
                    })

    def _safe_send(self, event: dict) -> None:
        """Thread-safe send to the main event loop."""
        try:
            if self._loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(self._send_json(event), self._loop)
        except RuntimeError:
            # Event loop closed or not running — silently ignore in tests
            pass

    async def close(self) -> None:
        """Close the STT session and clean up all resources."""
        self.closed = True
        try:
            await self.disable()
        except RuntimeError:
            # No event loop — do synchronous cleanup
            self._text_thread_active.clear()
            if self._text_thread:
                self._text_thread.join(timeout=3)
                self._text_thread = None
            if self._recorder:
                try:
                    self._recorder.shutdown()
                except Exception:
                    pass
                self._recorder = None
            self.enabled = False
            self.active = False

    # Public inspection helpers for tests
    @property
    def ingested_packets(self) -> int:
        return self._ingested_packets

    @property
    def events_sent(self) -> list[dict]:
        return list(self._events_sent)

    class _FakeRecorder:
        """Minimal fake recorder for ingest_audio_packet."""

        def __init__(self):
            self._fed_audio_count = 0

        def feed_audio(self, packet: AudioPacket) -> None:
            self._fed_audio_count += 1

        def shutdown(self) -> None:
            pass

        @property
        def fed_audio_count(self) -> int:
            return self._fed_audio_count
