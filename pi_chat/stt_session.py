"""Per-WebSocket STT session state and audio ingestion.

Manages STT enable/disable, direct audio feed, text worker thread,
and callback bridge for one browser connection.

Never imports RealtimeSTT directly at module load time — imports happen
inside enable() after STTService.load() completes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from pi_chat import config

logger = logging.getLogger("pi-chat.stt")

# ---------------------------------------------------------------------------
# Audio packet constants
# ---------------------------------------------------------------------------

SERVER_SAMPLE_RATE = 16000
MAX_METADATA_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Audio packet types
# ---------------------------------------------------------------------------


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


def require_positive_int(metadata: dict, key: str) -> int:
    """Extract and validate a positive integer from packet metadata."""
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AudioPacketError(
            f"audio packet metadata field '{key}' must be a positive integer"
        )
    return value


def resample_int16(samples, source_rate: int, target_rate: int):
    """Resample int16 PCM audio from source_rate to target_rate.

    Uses scipy.signal.resample_poly when available, falls back to linear interp.
    Returns numpy int16 array.
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
    except Exception:
        # Fallback: linear interpolation
        duration = samples.size / float(source_rate)
        target_size = max(1, int(round(duration * target_rate)))
        source_positions = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
        target_positions = np.linspace(0.0, 1.0, num=target_size, endpoint=False)
        resampled = np.interp(target_positions, source_positions, samples.astype(np.float32))

    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def packet_to_server_samples(packet: AudioPacket):
    """Decode packet audio and resample to SERVER_SAMPLE_RATE (16000Hz).

    Returns numpy int16 array at 16000Hz.
    """
    import numpy as np

    sample_rate = require_positive_int(packet.metadata, "sampleRate")
    channels = packet.metadata.get("channels", 1)
    if isinstance(channels, bool) or not isinstance(channels, int) or channels <= 0:
        raise AudioPacketError("channels must be a positive integer")
    if channels > 8:
        raise AudioPacketError("channels must be at most 8")

    audio_format = packet.metadata.get("format", "pcm_s16le")
    if audio_format != "pcm_s16le":
        raise AudioPacketError("only pcm_s16le audio packets are supported")

    frame_width = channels * 2
    if len(packet.audio) % frame_width:
        raise AudioPacketError("pcm_s16le audio packet is not aligned to whole frames")

    samples = np.frombuffer(packet.audio, dtype=np.int16)

    # Downmix stereo to mono if needed
    if channels > 1:
        usable = len(samples) - (len(samples) % channels)
        if usable <= 0:
            return np.array([], dtype=np.int16)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1).astype(np.int16)

    # Resample to server sample rate
    return resample_int16(samples, sample_rate, SERVER_SAMPLE_RATE)


# ---------------------------------------------------------------------------
# STT session
# ---------------------------------------------------------------------------


class STTSession:
    """Per-WebSocket STT session with direct audio ingestion and callback bridge.

    Audio packets arrive from the WebSocket handler and are fed directly to the
    recorder (no background queue thread). A text worker thread calls recorder.text()
    to retrieve final transcriptions.
    """

    def __init__(
        self,
        stt_service: "STTService",
        send_json: Callable,
        config_: Any | None = None,
    ):
        """Create an STT session.

        Args:
            stt_service: Shared STTService instance.
            send_json: Async callable to send JSON events to the browser.
            config_: Configuration module. Defaults to pi_chat.config.
        """
        self.stt_service = stt_service
        self._send_json = send_json
        self._config = config_ or config

        # State
        self.enabled = False
        self.active = False  # Currently recording speech

        # Recorder instance (created on enable)
        self._recorder = None

        # Text worker thread (retrieves final transcriptions)
        self._text_thread = None
        self._text_thread_active = threading.Event()

        # Main event loop reference for thread-safe sends
        self._loop = asyncio.get_running_loop()

        # Activation ID for race safety (same pattern as VoiceSession)
        self._activation_id = 0

        # Closed flag
        self.closed = False

    async def enable(self, settings: Optional[dict] = None) -> None:
        """Enable STT: create recorder, start text worker thread.

        Args:
            settings: Optional STT settings override dict (not yet used).
        """
        logger.info("[STT DEBUG] enable() called, settings=%s", settings)
        self._activation_id += 1
        activation_id = self._activation_id

        logger.info("[STT DEBUG] loading stt_service")
        await self.stt_service.load()

        logger.info("[STT DEBUG] stt_service loaded, enabling session activation_id=%d", activation_id)

        # Import RealtimeSTT (lazy, after service load)
        logger.info("[STT DEBUG] importing AudioToTextRecorder")
        from RealtimeSTT import AudioToTextRecorder

        # Create recorder with external audio mode
        logger.info("[STT DEBUG] creating AudioToTextRecorder with model=%s device=%s",
                    self._config.STT_MODEL, self._config.STT_DEVICE)
        self._recorder = AudioToTextRecorder(
            spinner=False,
            use_microphone=False,
            model=self._config.STT_MODEL,
            realtime_model_type=self._config.STT_REALTIME_MODEL,
            device=self._config.STT_DEVICE,
            compute_type=self._config.STT_COMPUTE_TYPE,
            language=self._config.STT_LANGUAGE,
            post_speech_silence_duration=self._config.STT_SILENCE_DURATION,
            min_length_of_recording=self._config.STT_MIN_RECORDING_LENGTH,
            min_gap_between_recordings=self._config.STT_MIN_GAP_BETWEEN_RECORDINGS,
            enable_realtime_transcription=True,
            realtime_processing_pause=self._config.STT_REALTIME_PROCESSING_PAUSE,
            on_recording_start=self._on_recording_start,
            on_recording_stop=self._on_recording_stop,
            on_realtime_transcription_update=self._on_realtime,
        )

        # Start text worker thread (retrieves final transcriptions)
        logger.info("[STT DEBUG] starting text worker thread")
        self._text_thread_active.set()
        self._text_thread = threading.Thread(
            target=self._text_worker_loop, daemon=True
        )
        self._text_thread.start()

        self.enabled = True
        logger.info("[STT DEBUG] session enabled, sending stt_state=ready")
        await self._send_json({"type": "stt_state", "state": "ready"})

    async def disable(self) -> None:
        """Disable STT: stop text worker, shutdown recorder."""
        logger.info("STT: disabling session")

        # Stop text worker
        self._text_thread_active.clear()
        if self._text_thread:
            self._text_thread.join(timeout=3)
            self._text_thread = None

        # Shutdown recorder: stop() first, then shutdown()
        if self._recorder:
            try:
                self._recorder.stop()
            except Exception:
                logger.debug("STT: recorder.stop() error (non-fatal)")
            try:
                self._recorder.shutdown()
            except Exception:
                logger.debug("STT: recorder.shutdown() error (non-fatal)")
            self._recorder = None

        self.enabled = False
        self.active = False
        await self._send_json({"type": "stt_state", "state": "disabled"})

    def ingest_audio_packet(self, packet: AudioPacket) -> None:
        """Ingest an audio packet: decode, resample, feed to recorder.

        Called directly from the WebSocket handler (event loop thread).
        No queue — recorder manages its own internal audio buffer.
        """
        if not self.enabled or self._recorder is None:
            return

        try:
            samples = packet_to_server_samples(packet)
            self._recorder.feed_audio(samples, original_sample_rate=SERVER_SAMPLE_RATE)
        except AudioPacketError as e:
            logger.warning("STT: invalid audio packet: %s", e)
        except Exception as e:
            logger.error("STT: failed to feed audio: %s", e)
            self._safe_send({
                "type": "stt_error",
                "code": "audio_feed_error",
                "message": str(e),
                "recoverable": True,
            })

    def _text_worker_loop(self) -> None:
        """Background thread: call recorder.text() to get final transcriptions.

        recorder.text() blocks until a final transcription is ready.
        This matches the RealtimeSTT example server pattern.
        """
        while self._text_thread_active.is_set():
            try:
                # Blocks until final transcription ready or shutdown
                text = self._recorder.text()
                if text and text.strip():
                    self._safe_send({"type": "stt_final", "text": text.strip()})
            except Exception as e:
                if self._text_thread_active.is_set():
                    logger.error("STT: text worker error: %s", e)
                    self._safe_send({
                        "type": "stt_error",
                        "code": "transcription_error",
                        "message": str(e),
                        "recoverable": True,
                    })

    def _on_recording_start(self) -> None:
        """Called by recorder thread when speech detected."""
        self.active = True
        logger.debug("STT: recording started")
        self._safe_send({"type": "stt_recording_start"})

    def _on_recording_stop(self) -> None:
        """Called by recorder thread when speech ends.

        Note: final transcription comes from _text_worker_loop via recorder.text(),
        not from this callback.
        """
        self.active = False
        logger.debug("STT: recording stopped")
        self._safe_send({"type": "stt_recording_stop"})

    def _on_realtime(self, text: str) -> None:
        """Called by recorder thread with interim transcription."""
        self._safe_send({"type": "stt_realtime", "text": text})

    def _safe_send(self, event: dict) -> None:
        """Thread-safe send to the main event loop."""
        asyncio.run_coroutine_threadsafe(self._send_json(event), self._loop)

    async def close(self) -> None:
        """Close the STT session and clean up all resources."""
        self.closed = True
        logger.info("STT: closing session")
        # Disable is async but we're potentially called from sync context
        # during WebSocket disconnect, so we need to handle both cases
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
                    self._recorder.stop()
                except Exception:
                    pass
                try:
                    self._recorder.shutdown()
                except Exception:
                    pass
                self._recorder = None
            self.enabled = False
            self.active = False
