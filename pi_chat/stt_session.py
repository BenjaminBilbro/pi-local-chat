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
import struct
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from pi_chat import config
from pi_chat.audio_utils import resample_int16  # re-exported for compatibility

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
        # Guard against concurrent/duplicate enable() calls (each create a
        # full RealtimeSTT model; leaks would multiply model memory).
        self._enable_lock = asyncio.Lock()

        # AEC: echo cancellation during TTS playback
        self._voice_session: Any | None = None  # VoiceSession reference
        self._aec: Any | None = None  # pywebrtc_audio.AudioProcessor (lazy init)
        self._aec_unavailable: bool = False  # pywebrtc_audio import failed
        self._aec_was_speaking: bool = False  # track speaking transitions for reset

        # Closed flag
        self.closed = False

    def set_voice_session(self, voice_session: Any) -> None:
        """Link to the VoiceSession for AEC reference audio.

        Called by the WebSocket handler after both sessions are created.
        """
        self._voice_session = voice_session

    def _ensure_aec(self) -> None:
        """Lazily create the AEC processor on first use.

        Imports pywebrtc_audio here to keep it out of the critical path when
        STT is enabled but TTS is not in use. If the package is unavailable,
        server-side AEC is disabled and mic audio passes through unmodified
        (the browser's built-in AEC remains the primary cancellation path).
        """
        if self._aec is not None or self._aec_unavailable:
            return
        try:
            from pywebrtc_audio import AudioProcessor
        except ImportError as e:
            self._aec_unavailable = True
            logger.warning("STT: pywebrtc_audio unavailable, server AEC disabled: %s", e)
            return
        self._aec = AudioProcessor(
            sample_rate=SERVER_SAMPLE_RATE,
            echo_cancellation=True,
            noise_suppression=True,
        )
        logger.info("STT: AEC processor created")

    def _run_aec(self, mic_samples: "np.ndarray") -> "np.ndarray":
        """Run acoustic echo cancellation on a mic packet.

        Pulls matching reference audio from the VoiceSession buffer.
        Returns cleaned samples (same length as input). Passes the mic
        through unchanged when AEC is unavailable (e.g. pywebrtc_audio
        missing) or no voice session is linked.
        """
        import numpy as np

        aec = self._aec
        voice = self._voice_session
        if aec is None or voice is None:
            return mic_samples

        # Drain reference bytes matching the mic packet size
        needed_bytes = len(mic_samples) * 2  # int16 = 2 bytes/sample
        ref_bytes = voice.drain_reference_bytes(needed_bytes)

        # If reference is shorter, zero-pad (underflow guardrail)
        if len(ref_bytes) < needed_bytes:
            ref_bytes = ref_bytes + b"\x00" * (needed_bytes - len(ref_bytes))

        # Convert to numpy arrays
        near = np.frombuffer(mic_samples.tobytes(), dtype=np.int16)
        far = np.frombuffer(ref_bytes[:needed_bytes], dtype=np.int16)

        # Run AEC
        clean = aec.process(near, far)
        return clean

    async def enable(self, settings: Optional[dict] = None) -> None:
        """Enable STT: create recorder, start text worker thread.

        Re-entrant safe: a duplicate enable (double-click, reconnect race)
        tears down any existing recorder first so RealtimeSTT models and
        worker threads are never leaked. If a disable races with this enable,
        the stale activation is detected and the fresh recorder is torn down.

        Args:
            settings: Optional STT settings override dict (not yet used).
        """
        async with self._enable_lock:
            if self._recorder is not None or self._text_thread is not None:
                # Re-enable path: tear down the previous recorder first.
                await self._teardown_recorder()

            self._activation_id += 1
            activation_id = self._activation_id

            logger.debug("STT: loading the shared speech-input service")
            await self.stt_service.load()

            # Import RealtimeSTT (lazy, after service load)
            logger.debug("STT: importing AudioToTextRecorder")
            from RealtimeSTT import AudioToTextRecorder

            # Create recorder with external audio mode
            logger.debug("STT: creating recorder model=%s device=%s",
                         self._config.STT_MODEL, self._config.STT_DEVICE)
            recorder = AudioToTextRecorder(
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
            logger.debug("STT: starting the text worker thread")
            self._recorder = recorder
            self._text_thread_active.set()
            self._text_thread = threading.Thread(
                target=self._text_worker_loop, daemon=True
            )
            self._text_thread.start()

            # A disable() may have raced with the (slow) recorder creation;
            # tear the fresh recorder down instead of reviving a disabled session.
            if self.closed or activation_id != self._activation_id:
                logger.info("STT: enable() finished for STALE activation %d (current=%d), tearing down",
                            activation_id, self._activation_id)
                self._text_thread_active.clear()
                thread, self._text_thread = self._text_thread, None
                await asyncio.to_thread(thread.join, 5)
                recorder_ref, self._recorder = self._recorder, None
                await asyncio.to_thread(self._shutdown_recorder, recorder_ref)
                return

            self.enabled = True
            logger.debug("STT: session enabled, sending stt_state=ready")
            await self._send_json({"type": "stt_state", "state": "ready"})

    async def disable(self) -> None:
        """Disable STT: stop text worker, shutdown recorder.

        Runs all blocking RealtimeSTT teardown off the event loop. shutdown()
        happens BEFORE joining the worker thread because RealtimeSTT's
        text() only unblocks on shutdown(); joining first would stall the
        event loop for the full join timeout on every disable.
        """
        logger.info("STT: disabling session")
        self.enabled = False
        self.active = False
        self._activation_id += 1
        await self._teardown_recorder()
        await self._send_json({"type": "stt_state", "state": "disabled"})

    async def _teardown_recorder(self) -> None:
        """Tear down recorder + text worker thread without blocking the loop."""
        self._text_thread_active.clear()
        if self._recorder is not None:
            recorder, self._recorder = self._recorder, None
            await asyncio.to_thread(self._shutdown_recorder, recorder)
        thread = self._text_thread
        if thread is not None:
            self._text_thread = None
            await asyncio.to_thread(thread.join, 5)
            if thread.is_alive():
                logger.warning(
                    "STT: text worker thread did not exit within 5s of "
                    "recorder shutdown (daemon; dies at process exit)"
                )

    @staticmethod
    def _shutdown_recorder(recorder) -> None:
        """Shutdown a RealtimeSTT recorder (runs in a worker thread)."""
        try:
            recorder.shutdown()
        except Exception as e:
            # `VOICE-15`: this was `debug`, so a recorder that refused to shut down - and the model thread
            # that then refuses to exit, holding its weights resident for the rest of the process - was
            # invisible at the default log level. Still non-fatal: the caller is tearing the session down
            # either way. But a leak you cannot see is not non-fatal, it is unfound.
            logger.warning("STT: recorder.shutdown() failed (non-fatal, model may stay resident): %s", e)

    def ingest_audio_packet(self, packet: AudioPacket) -> None:
        """Ingest an audio packet: decode, resample, optionally AEC, feed to recorder.

        Called directly from the WebSocket handler (event loop thread).
        No queue — recorder manages its own internal audio buffer.

        When the agent is speaking (TTS playing), runs acoustic echo
        cancellation using the TTS reference audio before feeding to STT.
        """
        if not self.enabled or self._recorder is None:
            return

        try:
            samples = packet_to_server_samples(packet)

            # AEC: cancel echo when agent is speaking
            voice = self._voice_session
            if voice is not None and voice.is_speaking:
                # Reset AEC on new stream (speaking transition)
                if not self._aec_was_speaking:
                    self._ensure_aec()
                    if self._aec is not None:
                        self._aec.reset()
                    self._aec_was_speaking = True
                samples = self._run_aec(samples)
            else:
                self._aec_was_speaking = False

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

        recorder.text() blocks until a final transcription is ready or the
        recorder is shut down. This matches the RealtimeSTT example server
        pattern.
        """
        import time as _time

        while self._text_thread_active.is_set():
            recorder = self._recorder
            if recorder is None:
                # Torn down (disable/close) — exit.
                _time.sleep(0.05)
                continue
            try:
                # Blocks until final transcription ready or shutdown
                text = recorder.text()
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
                    # Backoff so a wedged recorder can't hot-loop error spam.
                    _time.sleep(0.2)

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
        """Thread-safe send to the main event loop.

        Safe to call from recorder callback threads after the WebSocket is
        gone: a closed loop is ignored, and send failures never propagate
        back into RealtimeSTT's internal threads.
        """
        if self.closed:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._send_json(event), self._loop)
        except (RuntimeError, TypeError):
            # Event loop already closed (disconnect race), or a test double
            # that is not a coroutine function.
            return

        def _swallow(fut):
            try:
                fut.result()
            except Exception as error:
                # Deliberate swallow, deliberate log level: this is the done-callback on an event we were
                # already trying to deliver, and if the socket is gone there is nobody left to tell. Debug, not
                # warning. But it used to be silent, and "the microphone died and the log says nothing" is a
                # question this subsystem has been asked before, so the reason now exists somewhere.
                logger.debug(
                    "STT: could not deliver %s: %s", event.get("type", "event"), error
                )

        future.add_done_callback(_swallow)

    async def close(self) -> None:
        """Close the STT session and clean up all resources (off-loop)."""
        if self.closed:
            return
        self.closed = True
        logger.info("STT: closing session")
        try:
            await self.disable()
        except Exception:
            logger.debug("STT: close cleanup error", exc_info=True)
