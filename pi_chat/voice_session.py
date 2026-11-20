"""Per-WebSocket voice session state and orchestration.

Manages voice enable/disable, settings, stream IDs, queue, framing,
and cancellation for one browser connection.

Never imports torch/omnivoice directly — depends only on TTSService.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import threading
import time
from dataclasses import dataclass, field

from pi_chat import config
from pi_chat import phonon_voices
from pi_chat.audio_utils import resample_pcm_bytes
from pi_chat.mode_marker import is_mode_marker_message
from pi_chat.tts_chunking import StreamingSpeechChunker
from pi_chat.tts_service import TTSService, VoiceHandle, VoiceSettings

logger = logging.getLogger("pi-chat.voice")

# ---------------------------------------------------------------------------
# Binary PCM frame format (24-byte header)
# ---------------------------------------------------------------------------

PCM_MAGIC = b"PIV1"
PCM_VERSION = 1
PCM_HEADER_LEN = 24
PCM_HEADER = struct.Struct("<4sBBHIIII")  # magic, version, flags, header_len, stream_id, seq, sample_rate, sample_count


def build_pcm_frame(
    stream_id: int,
    sequence: int,
    sample_rate: int,
    pcm_s16le: bytes,
) -> bytes:
    """Build a versioned binary PCM frame."""
    sample_count = len(pcm_s16le) // 2
    header = PCM_HEADER.pack(
        PCM_MAGIC,
        PCM_VERSION,
        0,  # flags
        PCM_HEADER_LEN,
        stream_id,
        sequence,
        sample_rate,
        sample_count,
    )
    return header + pcm_s16le


# ---------------------------------------------------------------------------
# Allowed OmniVoice settings values
# ---------------------------------------------------------------------------

ALLOWED_GENDERS = {"male", "female"}
ALLOWED_AGES = {"child", "teenager", "young adult", "middle-aged", "elderly"}
ALLOWED_PITCHES = {"very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"}
ALLOWED_ACCENTS = {
    "american accent", "british accent", "australian accent",
    "canadian accent", "indian accent", "chinese accent",
    "korean accent", "japanese accent", "portuguese accent", "russian accent",
}
ALLOWED_STYLES = {None, "whisper"}
ALLOWED_LANGUAGES = {"English", "Spanish", "French", "German"}
MIN_SPEED = 0.8
MAX_SPEED = 1.25

# Pieces may be queued toward the browser before the model thread has to wait. Generation runs
# several times faster than real time, so without a cap a long reply would buffer its entire
# remaining audio in RAM; with one, a slow browser stalls the model, which is exactly the
# backpressure ptts's bounded stream buffers expect.
PIECES_IN_FLIGHT = 4

# Ceiling on how long the model thread will wait for a queued piece to reach the browser. It only
# trips if the event loop has stopped (shutdown, or a bug), and existing without it the model
# thread would sit there forever and the process could not be interrupted.
PIECE_SEND_TIMEOUT = 10.0


class StreamSuperseded(RuntimeError):
    """The stream this piece belongs to was replaced while the model was still generating.

    Raised on the model thread from the piece callback, so generation stops instead of finishing
    audio nobody will hear.
    """

# Phonon voice names come from gradium/phonon-voices and are handed to ptts, which resolves
# them into cached file names, so the shape is checked here even though existence is not.
# Letters (accented included), digits, and the few punctuation marks real names use
# ("Elodie-Rose", "O'Brien", "Captain Silas Blackwater") and nothing that could read as a
# path, a URL scheme, or a flag.
PHONON_VOICE_PATTERN = re.compile(
    r"(?!.*--)(?!.*\.\.)^[A-Za-z\u00c0-\u024f0-9][A-Za-z\u00c0-\u024f0-9 .'-]{0,63}$"
)


def _validate_phonon_voice(value: object) -> str | None:
    """Sanity-check a Phonon voice name and normalize it to the catalog's spelling.

    Existence is deliberately NOT checked here: the catalog is a committed snapshot, so a
    voice Gradium publishes after we snapshot it is still a valid request, and ptts gives its
    own clear error for a name it cannot resolve. What this does reject is anything shaped
    like a path or a flag, and it rewrites "russel"-style case differences to the canonical
    catalog spelling so the engine (and the voice cache key) sees one stable string.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not PHONON_VOICE_PATTERN.match(name):
        logger.info("Voice: phonon_voice rejected by shape check: %r", name[:80])
        return None

    canonical = phonon_voices.describe(name)
    if canonical and canonical.get("name"):
        return str(canonical["name"])
    logger.info("Voice: phonon_voice %r is not in the shipped catalog snapshot; ptts will resolve it", name)
    return name


def validate_settings(raw: dict) -> VoiceSettings | None:
    """Validate and construct VoiceSettings from raw dict.

    Returns None if settings are invalid.
    Now accepts voice_id for custom voice cloning (CRIT-1).
    """
    try:
        gender = raw.get("gender", "female")
        age = raw.get("age", "young adult")
        pitch = raw.get("pitch", "moderate pitch")
        accent = raw.get("accent", "american accent")
        style = raw.get("style")
        speed = float(raw.get("speed", 1.0))

        # (CRIT-1) voice_id MUST be in allowed_keys
        allowed_keys = {
            "gender", "age", "pitch", "accent", "style", "speed", "language", "voice_id",
            "phonon_voice", "phonon_rate",
        }
        if not set(raw.keys()).issubset(allowed_keys):
            return None

        if gender not in ALLOWED_GENDERS:
            return None
        if age not in ALLOWED_AGES:
            return None
        if pitch not in ALLOWED_PITCHES:
            return None
        if accent not in ALLOWED_ACCENTS:
            return None
        if style not in ALLOWED_STYLES:
            return None
        if speed < MIN_SPEED or speed > MAX_SPEED:
            return None

        language = raw.get("language", "English")
        if language not in ALLOWED_LANGUAGES:
            return None

        # (CRIT-1) Validate voice_id if present
        voice_id = raw.get("voice_id")
        if voice_id is not None:
            if not isinstance(voice_id, str):
                return None
            # Validate hex format (16 lowercase hex chars)
            if len(voice_id) != 16 or not all(c in "0123456789abcdef" for c in voice_id):
                return None

        phonon_voice = None
        if raw.get("phonon_voice") is not None:
            phonon_voice = _validate_phonon_voice(raw["phonon_voice"])
            if phonon_voice is None:
                return None

        phonon_rate = raw.get("phonon_rate")
        if phonon_rate is not None and phonon_rate not in config.TTS_PHONON_RATE_LABELS:
            return None

        return VoiceSettings(
            gender=gender,
            age=age,
            pitch=pitch,
            accent=accent,
            style=style,
            speed=speed,
            language=language,
            voice_id=voice_id,
            phonon_voice=phonon_voice,
            phonon_rate=phonon_rate,
        )
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Voice session
# ---------------------------------------------------------------------------

class VoiceSession:
    """Per-WebSocket voice state and orchestration."""

    def __init__(
        self,
        tts_service: TTSService,
        send_json: callable,
        send_bytes: callable,
        config_: config | None = None,
        audit: callable | None = None,
    ):
        self._tts = tts_service
        self._send_json = send_json
        self._send_bytes = send_bytes
        self._config = config_ or config
        # Debug-mode audit hook (`pi_chat/audit.py`). The AEC speaking window is the one piece of voice state
        # with no message attached to it: when it sticks open, nothing crosses the wire to say so, and the
        # symptom is a call that goes deaf part way through. Defaulted to a no-op so a session built without a
        # recorder - every test, and every run without the flag - pays nothing.
        self._audit = audit or (lambda kind, **data: None)

        # State
        self.enabled: bool = False
        self.ready: bool = False
        self.settings: VoiceSettings = VoiceSettings()
        self.voice_handle: VoiceHandle | None = None

        # Stream tracking
        self._stream_id: int = 0
        self._sequence: int = 0

        # Run state
        self.active_run: bool = False
        self.suppress_current_run: bool = False
        # A voice-mode marker turn is answered, but never spoken. Two flags, because the two facts arrive at
        # different moments: `_marker_armed` is set when the browser *submits* a marker (it is the only party
        # that knows, and `agent_start` carries no text, so without the hint the stream would already be open
        # and the browser would believe the assistant is talking); `_marker_turn` is the delivered truth, taken
        # from the user message as pi delivers it, and it is what decides an assistant message. See
        # `pi_chat/mode_marker.py`.
        self._marker_armed: bool = False
        self._marker_turn: bool = False

        # Chunker. `VOICE-07`: the hold timer used to read `TTS_MAX_HOLD_MS` while the chunker kept its own
        # 450 ms default, so the knob only worked upward - set it to 200 and nothing changed, because the
        # flush the timer triggers was gated by the chunker's larger value. One value, passed in.
        self.chunker: StreamingSpeechChunker = StreamingSpeechChunker(
            max_hold_ms=self._config.TTS_MAX_HOLD_MS
        )

        # Queue
        self._queue: asyncio.Queue[str | None] | None = None
        self._queued_characters: int = 0
        self._pending_characters: int = 0  # includes queued + in-synthesis
        self._synthesizing: bool = False

        # Tasks
        self._worker_task: asyncio.Task | None = None
        self._hold_timer_task: asyncio.Task | None = None
        self._prepare_task: asyncio.Task | None = None

        # Activation tracking for async races
        self._activation_id: int = 0
        self._current_activation_id: int = 0

        # AEC reference: TTS audio buffer for echo cancellation.
        # The buffer stores 16kHz mono int16 reference audio matching what the
        # browser is (or may still be) playing out of the speakers. STTSession
        # drains it at the real-time mic rate to feed WebRTC AEC.
        self.is_speaking: bool = False
        self._tts_reference_buffer: bytearray = bytearray()
        self._aec_max_buffer_bytes: int = 2_000_000  # ~100s at 16kHz mono int16
        self._last_ref_append: float = 0.0  # monotonic time of last reference append
        self._playback_ended: bool = False  # browser confirmed playback stopped
        self._reference_lock: threading.Lock = threading.Lock()

        # Fallback close for the AEC window: if the reference buffer has been
        # empty this long (and the browser never sent voice_playback_end), stop
        # running AEC. Covers stale clients and lost commands.
        self._aec_tail_seconds: float = 10.0

        # Closed flag
        self.closed: bool = False

    # ------------------------------------------------------------------
    # Public control methods
    # ------------------------------------------------------------------

    async def enable(self, raw_settings: dict) -> None:
        """Enable voice mode with the given settings."""
        logger.debug("Voice: [DEBUG] enable() called with settings=%s", raw_settings)
        settings = validate_settings(raw_settings)
        if settings is None:
            logger.debug("Voice: [DEBUG] enable() FAILED - invalid settings")
            await self._send_error(
                "voice_invalid_settings",
                "Invalid voice settings provided.",
                recoverable=True,
            )
            return

        self.enabled = True
        self._current_activation_id = self._activation_id = self._activation_id + 1
        self.settings = settings
        logger.debug("Voice: [DEBUG] enable() activation_id=%d", self._activation_id)

        await self._send_json({"type": "voice_state", "state": "loading", "available": True})

        # Start preparation in background
        self._prepare_task = asyncio.create_task(self._prepare_voice())

    async def disable(self) -> None:
        """Disable voice mode."""
        logger.debug("Voice: [DEBUG] disable() called")
        self.enabled = False
        self.ready = False
        self._invalidate_activation()
        await self._stop_current_internal(reason="disabled")

        await self._send_json({"type": "voice_state", "state": "disabled", "available": False})

    async def update_settings(self, raw_settings: dict) -> None:
        """Update voice settings for the next response."""
        logger.debug("Voice: [DEBUG] update_settings() called with settings=%s", raw_settings)
        settings = validate_settings(raw_settings)
        if settings is None:
            logger.debug("Voice: [DEBUG] update_settings() FAILED - invalid settings")
            await self._send_error(
                "voice_invalid_settings",
                "Invalid voice settings provided.",
                recoverable=True,
            )
            return

        self._current_activation_id = self._activation_id = self._activation_id + 1
        self.settings = settings
        logger.debug("Voice: [DEBUG] update_settings() activation_id=%d", self._activation_id)

        # Start preparation in background
        self._prepare_task = asyncio.create_task(self._prepare_voice())

    async def stop_current(self, reason: str = "stopped") -> None:
        """Stop the current response's voice but keep voice mode enabled."""
        await self._stop_current_internal(reason=reason)

    def drain_reference_bytes(self, n: int) -> bytes:
        """Drain up to n bytes from the TTS reference buffer for AEC.

        Returns available bytes (may be shorter than n if buffer is low).
        Thread-safe: called from STT ingest (event loop thread).

        Also acts as a tick for the AEC window fallback: when the buffer has
        been empty long enough, close the speaking window even if the browser
        never sent voice_playback_end.
        """
        with self._reference_lock:
            if self.is_speaking and len(self._tts_reference_buffer) == 0:
                if (
                    not self._playback_ended
                    and self._last_ref_append > 0
                    and (time.monotonic() - self._last_ref_append) > self._aec_tail_seconds
                ):
                    self.is_speaking = False
                    self._audit("aec_window", open=False, why="tail_timeout")
                    logger.info("Voice: AEC window closed via tail timeout")
            if len(self._tts_reference_buffer) == 0:
                return b""
            available = min(n, len(self._tts_reference_buffer))
            data = bytes(self._tts_reference_buffer[:available])
            del self._tts_reference_buffer[:available]
            return data

    def on_playback_end(self, stream_id: int) -> None:
        """Browser confirmed that all TTS playback for a stream has ended.

        Sent by the browser when scheduled audio sources have fully drained
        (natural end) or when local playback is stopped (user stop, new prompt,
        new stream, disconnect). Closes the AEC window and drops any stale
        reference so the next stream starts clean.
        """
        if stream_id != self._stream_id:
            return
        with self._reference_lock:
            self._tts_reference_buffer.clear()
            self._playback_ended = True
            self._last_ref_append = 0.0
        if self.is_speaking:
            self._audit("aec_window", open=False, why="playback_end", streamId=stream_id)
        self.is_speaking = False

    def _reset_aec_for_stream(self) -> None:
        """Reset AEC state for a new voice stream (called on agent_start).

        The browser stops the previous stream's sources when a new stream
        starts, so any remaining reference audio is stale and must be dropped.
        """
        with self._reference_lock:
            self._tts_reference_buffer.clear()
            self._last_ref_append = 0.0
            self._playback_ended = False
        self.is_speaking = False

    async def _deliver_frame(
        self,
        frame: bytes,
        pcm_s16le: bytes,
        sample_rate: int,
        previous: "object | None" = None,
    ) -> None:
        """Send one synthesized piece, then feed it to the AEC reference buffer.

        Runs on the event loop (the model thread schedules it). Same ordering rule as the
        whole-utterance path: audio that never reached the browser must not pollute the echo
        reference.

        `previous` is the future for the piece scheduled before this one. Waiting on it is what
        keeps pieces in order on the wire and in the echo reference; an unchained set of these
        coroutines races, and a late frame is silently dropped by the browser.
        """
        if previous is not None:
            # Await, never `.result()`: we are on the loop thread, and `previous` is a coroutine
            # this same loop has to run.
            await asyncio.wrap_future(previous)
        await self._send_bytes(frame)
        # AEC: buffer 16kHz reference audio for echo cancellation. is_speaking is set inside;
        # closes later via browser voice_playback_end or the tail timeout in
        # drain_reference_bytes.
        self._append_reference(pcm_s16le, sample_rate)

    def _append_reference(self, pcm_s16le: bytes, sample_rate: int) -> None:
        """Append synthesized audio (at its native rate) to the AEC reference.

        Resamples to 16kHz so the reference matches the real-time drain rate
        used by STTSession. Skipped after the browser confirmed playback ended
        (e.g. user typed a prompt mid-stream) to avoid phantom reference.
        """
        if self._playback_ended:
            return
        ref = resample_pcm_bytes(pcm_s16le, sample_rate, 16000)
        if not ref:
            return
        # Recorded on the transition only. This runs once per synthesized chunk, so an unguarded line here
        # would write hundreds of identical window events and bury the one that matters, which is the absence
        # of the matching close.
        if not self.is_speaking:
            self._audit("aec_window", open=True, why="reference_buffered", streamId=self._stream_id)
        self.is_speaking = True
        now = time.monotonic()
        with self._reference_lock:
            self._tts_reference_buffer.extend(ref)
            if len(self._tts_reference_buffer) > self._aec_max_buffer_bytes:
                self._tts_reference_buffer = self._tts_reference_buffer[-self._aec_max_buffer_bytes:]
            self._last_ref_append = now

    async def prepare(self, raw_settings: dict) -> None:
        """Prepare voice for given settings without changing enabled state.

        Sends voice_prepared or voice_error response.
        Used by frontend when user applies settings changes.
        """
        logger.debug("Voice: [DEBUG] prepare() called with settings=%s", raw_settings)
        settings = validate_settings(raw_settings)
        if settings is None:
            logger.debug("Voice: [DEBUG] prepare() FAILED - invalid settings")
            await self._send_error(
                "voice_invalid_settings",
                "Invalid voice settings provided.",
                recoverable=True,
            )
            return

        try:
            # Update settings and activation (so any pending prep is invalidated)
            self._activation_id += 1
            self.settings = settings
            logger.debug("Voice: [DEBUG] prepare() activation_id=%d", self._activation_id)

            # Load TTS and prepare voice
            logger.debug("Voice: [DEBUG] prepare() calling tts.load()...")
            await self._tts.load()
            logger.debug("Voice: [DEBUG] prepare() calling tts.prepare_voice()...")
            handle = await self._tts.prepare_voice(settings)
            logger.debug("Voice: [DEBUG] prepare() tts.prepare_voice() returned handle=%s", handle.key)

            # Cache the handle if voice is enabled
            if self.enabled:
                self.voice_handle = handle
                self.ready = True
                logger.debug("Voice: [DEBUG] prepare() cached handle (voice enabled)")

            await self._send_json({
                "type": "voice_prepared",
                "settings": {
                    "gender": settings.gender,
                    "age": settings.age,
                    "pitch": settings.pitch,
                    "accent": settings.accent,
                    "style": settings.style,
                    "speed": settings.speed,
                    "language": settings.language,
                },
            })
            logger.debug("Voice: [DEBUG] prepare() COMPLETE")

        except MemoryError as e:
            logger.error("Voice prepare OOM: %s", e)
            await self._send_error("voice_oom", str(e), recoverable=True)
        except Exception as e:
            logger.error("Voice prepare failed: %s", e)
            await self._send_error("voice_prepare_failed", str(e), recoverable=True)

    async def observe_pi_event(self, event: dict) -> None:
        """Handle a pi RPC event for voice processing."""
        if not self.enabled or self.closed:
            return

        event_type = event.get("type")
        logger.debug("Voice: [DEBUG] observe_pi_event() event_type=%s", event_type)

        if event_type == "agent_start":
            await self._on_agent_start()
        elif event_type == "message_start":
            await self._on_message_start(event)
        elif event_type == "message_update":
            await self._on_message_update(event)
        elif event_type == "agent_settled":
            await self._on_agent_settled()
        elif event_type == "response":
            await self._on_response(event)

    async def close(self) -> None:
        """Close the voice session and clean up all resources."""
        self.closed = True
        self.enabled = False
        self.ready = False
        self._invalidate_activation()
        await self._stop_current_internal(reason="disabled")

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    async def _prepare_voice(self) -> None:
        """Prepare voice in background. Safe to cancel."""
        activation_id = self._activation_id
        logger.debug("Voice: [DEBUG] _prepare_voice() START activation_id=%d", activation_id)
        try:
            logger.debug("Voice: [DEBUG] _prepare_voice() calling tts.load()...")
            await self._tts.load()
            logger.debug("Voice: [DEBUG] _prepare_voice() calling tts.prepare_voice()...")
            handle = await self._tts.prepare_voice(self.settings)
            logger.debug("Voice: [DEBUG] _prepare_voice() tts.prepare_voice() returned handle=%s", handle.key)

            # Install only if this activation is still current
            if (
                not self.closed
                and self.enabled
                and activation_id == self._activation_id
            ):
                self.voice_handle = handle
                self.ready = True
                logger.debug("Voice: [DEBUG] _prepare_voice() installed handle, ready=True")
                await self._send_json({
                    "type": "voice_state",
                    "state": "ready",
                    "available": True,
                    "settings": {
                        "gender": self.settings.gender,
                        "age": self.settings.age,
                        "pitch": self.settings.pitch,
                        "accent": self.settings.accent,
                        "style": self.settings.style,
                        "speed": self.settings.speed,
                    },
                })
            else:
                logger.debug("Voice: [DEBUG] _prepare_voice() completed for STALE activation %d (current=%d)", activation_id, self._activation_id)

        except asyncio.CancelledError:
            logger.debug("Voice: [DEBUG] _prepare_voice() CANCELLED activation_id=%d", activation_id)
            raise
        except MemoryError as e:
            logger.error("Voice preparation OOM: %s", e)
            if activation_id == self._activation_id and not self.closed:
                self.ready = False
                await self._send_error("voice_oom", str(e), recoverable=True)
        except Exception as e:
            logger.error("Voice preparation failed: %s", e)
            if activation_id == self._activation_id and not self.closed:
                self.ready = False
                await self._send_error("voice_model_load_failed", str(e), recoverable=True)

    def _invalidate_activation(self) -> None:
        """Invalidate the current activation so pending prep is ignored."""
        self._activation_id += 1

    def arm_mode_marker_turn(self) -> None:
        """The next run to start is an answer to a mode marker, so it gets no audio.

        Arming is a hint, not an authority: pi's `agent_start` event carries no text, so this is the only way
        to know a turn is a handshake *before* opening its stream. The delivered message
        (`_on_message_start`) is what is believed once it arrives, which is why a marker queued behind a
        running answer cannot silence that answer.
        """
        self._marker_armed = True
        logger.debug("Voice: mode marker armed for the next run (nothing will be synthesized)")

    async def _on_message_start(self, event: dict) -> None:
        """Track which message the assistant is about to answer.

        pi replays each prompt as a `message_start` with role `user` at the moment it delivers it, including
        one it pulls out of its queue mid-run, then announces the reply as another `message_start` with role
        `assistant`. Delivering, not submitting, is the distinction that matters here: an OFF marker queued
        behind a live answer is delivered while that answer is still streaming its text, and silencing *it*
        would be the exact opposite of the point. So a user delivery only records what comes next, and only an
        assistant delivery decides whether this text is spoken.
        """
        message = event.get("message")
        if not isinstance(message, dict):
            return
        role = message.get("role")
        if role == "user":
            self._marker_turn = is_mode_marker_message(message)
            if self._marker_turn and self.active_run and self._sequence == 0:
                # Safety net for an order this file does not otherwise see: a marker whose stream was opened
                # before anyone knew. Nothing has been synthesized on it yet (`_sequence == 0`), so closing it
                # now costs nothing, and an empty stream is not a sentence the browser should sit and wait for.
                # Once any audio has gone out the answer is being heard, and it is left alone.
                self.suppress_current_run = True
            return
        if role == "assistant":
            self.suppress_current_run = self._marker_turn

    async def _on_agent_start(self) -> None:
        """Handle agent_start: start a new voice stream if enabled and ready."""
        logger.debug("Voice: [DEBUG] _on_agent_start() ready=%s, has_handle=%s", self.ready, self.voice_handle is not None)
        # Cancel old stream if active
        if self.active_run:
            logger.debug("Voice: [DEBUG] _on_agent_start() cancelling old active_run")
            await self._stop_current_internal(reason="superseded")

        # Only the submit-time hint can suppress a run that has not delivered its prompt yet, because this
        # event arrives *before* the message the run is answering: inheriting `_marker_turn` here would make
        # the person's own question a silent turn, since the flag still describes the handshake. Recomputed for
        # every run, and before the readiness return below, so a flag can never outlive the run it was set for.
        self.suppress_current_run = self._marker_armed
        self._marker_armed = False
        if self.suppress_current_run:
            # No stream at all, not a stream with nothing in it: `voice_stream_start` alone sets the browser's
            # `speaking`, and a surface claiming to talk with nothing to hear is what used to make it throw
            # away the first thing the person said (`static/voice.js`, `handleVoiceStreamStart`).
            logger.debug("Voice: [DEBUG] _on_agent_start() mode marker turn - no stream opened")
            return

        if not self.ready or not self.voice_handle:
            logger.debug("Voice: [DEBUG] _on_agent_start() returning early - not ready or no handle")
            return

        self.active_run = True
        self._stream_id += 1
        self._sequence = 0
        self.chunker.reset()
        self._queued_characters = 0
        self._synthesizing = False

        # AEC: start each stream with a clean reference window
        self._reset_aec_for_stream()

        # Create bounded queue (max_data_chunks + 1 for sentinel)
        max_chunks = self._config.TTS_MAX_QUEUE_CHUNKS
        self._queue = asyncio.Queue(maxsize=max_chunks + 1)
        self._pending_characters = 0

        # Cancel old hold timer
        if self._hold_timer_task and not self._hold_timer_task.done():
            self._hold_timer_task.cancel()
            self._hold_timer_task = None

        # Start worker. Nothing should still be running here, but `_stop_current_internal` is only
        # reached when `active_run` was set, and an over-written task is a task that never gets cancelled.
        await self._cancel_worker()
        self._worker_task = asyncio.create_task(self._worker())

        logger.debug("Voice: [DEBUG] _on_agent_start() stream_id=%d, voice_handle=%s", self._stream_id, self.voice_handle.key)
        # Send stream start
        await self._send_json({
            "type": "voice_stream_start",
            "streamId": self._stream_id,
            "encoding": "pcm_s16le",
            "channels": 1,
        })

    async def _on_message_update(self, event: dict) -> None:
        """Handle message_update events."""
        if not self.active_run or self.suppress_current_run:
            return

        am_event = event.get("assistantMessageEvent", {})
        sub_type = am_event.get("type")

        if sub_type == "text_start":
            # Start/continue text block — don't reset whole response
            logger.debug("Voice: [DEBUG] _on_message_update() text_start")
        elif sub_type == "text_delta":
            delta = am_event.get("delta", "")
            if delta:
                logger.debug("Voice: [DEBUG] _on_message_update() text_delta len=%d text=%s", len(delta), repr(delta[:60]) + ("..." if len(delta) > 60 else ""))
                await self._feed_delta(delta)
        elif sub_type == "text_end":
            logger.debug("Voice: [DEBUG] _on_message_update() text_end")
            # Flush current text block
            await self._flush_text_block()

    async def _feed_delta(self, delta: str) -> None:
        """Feed a text delta through the chunker and enqueue speech chunks."""
        now = time.monotonic()
        chunks = self.chunker.feed(delta, now=now)
        logger.debug("Voice: [DEBUG] _feed_delta() produced %d chunks: %s", len(chunks), [repr(c[:30]) + "..." if len(c) > 30 else repr(c) for c in chunks])

        for chunk in chunks:
            await self._enqueue_chunk(chunk)

        # Arm hold timer if enough pending text
        if self.chunker.pending_characters >= 72:
            if self._hold_timer_task is None or self._hold_timer_task.done():
                self._arm_hold_timer()

    def _arm_hold_timer(self) -> None:
        """Arm a timer to flush pending text after max hold."""
        hold_ms = self._config.TTS_MAX_HOLD_MS
        self._hold_timer_task = asyncio.create_task(self._hold_timer(hold_ms))

    async def _hold_timer(self, hold_ms: float) -> None:
        """Wait and then flush pending text if still buffered."""
        try:
            await asyncio.sleep(hold_ms / 1000.0)
            if self.active_run and not self.suppress_current_run:
                await self._flush_pending_on_hold()
        except asyncio.CancelledError:
            pass

    async def _flush_pending_on_hold(self) -> None:
        """Flush pending text at a safe boundary after hold timeout."""
        # Feed empty string to trigger max-hold flush
        now = time.monotonic()
        chunks = self.chunker.feed("", now=now)
        for chunk in chunks:
            await self._enqueue_chunk(chunk)

    async def _flush_text_block(self) -> None:
        """Flush the current text block."""
        if not self.active_run or self.suppress_current_run:
            return

        now = time.monotonic()
        chunks = self.chunker.flush_text_block(now=now)
        logger.debug("Voice: [DEBUG] _flush_text_block() produced %d chunks: %s", len(chunks), [repr(c[:30]) + "..." if len(c) > 30 else repr(c) for c in chunks])
        for chunk in chunks:
            await self._enqueue_chunk(chunk)

        # Cancel hold timer on block end
        if self._hold_timer_task and not self._hold_timer_task.done():
            self._hold_timer_task.cancel()
            self._hold_timer_task = None

    async def _on_agent_settled(self) -> None:
        """Handle agent_settled: finish the response."""
        logger.debug("Voice: [DEBUG] _on_agent_settled() active_run=%s", self.active_run)
        if not self.active_run:
            return

        await self._flush_text_block()

        # Finish chunker and enqueue remaining
        now = time.monotonic()
        chunks = self.chunker.finish()
        logger.debug("Voice: [DEBUG] _on_agent_settled() chunker.finish() produced %d chunks: %s", len(chunks), [repr(c[:30]) + "..." if len(c) > 30 else repr(c) for c in chunks])
        for chunk in chunks:
            await self._enqueue_chunk(chunk)

        # Enqueue sentinel to drain worker
        await self._enqueue_sentinel()

    async def _on_response(self, event: dict) -> None:
        """Handle response event (e.g., failed prompt)."""
        if not event.get("success", True):
            await self._stop_current_internal(reason="error")

    async def _enqueue_chunk(self, chunk: str) -> None:
        """Enqueue a speech chunk, enforcing backpressure limits."""
        if not self.active_run or self.suppress_current_run or self._queue is None:
            return

        queue = self._queue
        max_chunks = self._config.TTS_MAX_QUEUE_CHUNKS
        max_chars = self._config.TTS_MAX_QUEUE_CHARS

        chunk_chars = len(chunk)

        # Check limits including chunks being synthesized
        if self._pending_characters + chunk_chars > max_chars:
            await self._stop_current_internal(reason="backlog")
            await self._send_error(
                "voice_backlog",
                "Voice synthesis backlog exceeded. Text chat continues.",
                recoverable=True,
            )
            return

        try:
            queue.put_nowait(chunk)
            self._queued_characters += chunk_chars
            self._pending_characters += chunk_chars
        except asyncio.QueueFull:
            await self._stop_current_internal(reason="backlog")
            await self._send_error(
                "voice_backlog",
                "Voice synthesis backlog exceeded. Text chat continues.",
                recoverable=True,
            )

    async def _enqueue_sentinel(self) -> None:
        """Enqueue the end sentinel."""
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(None)  # sentinel
        except asyncio.QueueFull:
            # Queue is full with data chunks; worker will handle finish
            pass

    async def _cancel_worker(self) -> None:
        """Stop this stream's worker and wait until it is gone.

        Cancellation rather than a sentinel. The queue is bounded, so on a backlog it is full and the
        sentinel cannot be enqueued at all (the `QueueFull: pass` branch that used to be the whole
        shutdown path), which left the worker parked forever on a queue nothing would write to again.
        It then either died on a nulled `self._queue` or, worse, dequeued chunks off the *next* stream's
        queue and dropped them at the stream-id check. Both read to the user as speech that simply
        never arrived.

        Returns immediately when the worker calls it itself: `_worker`'s error paths reach
        `_stop_current_internal`, and a task cannot await its own cancellation.
        """
        task = self._worker_task
        if task is None:
            return
        if task is asyncio.current_task():
            # The worker stopping its own stream (a synthesis error). It is on its way out, and the
            # reference stays so the *next* caller still awaits it instead of starting a second worker
            # beside a finishing one.
            return
        self._worker_task = None
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _stop_current_internal(self, reason: str) -> None:
        """Stop the current voice stream internally."""
        logger.debug("Voice: [DEBUG] _stop_current_internal() reason=%s stream_id=%d", reason, self._stream_id)
        stream_id = self._stream_id
        self.active_run = False
        # `is_speaking` is deliberately not cleared here, so record what the window is doing at the moment a
        # stop is requested. A `voice_stopped` with `open=True` and no matching `aec_window open=False` is the
        # stuck state, and this line is the only place it becomes visible.
        self._audit("voice_stopped", reason=reason, streamId=stream_id, window_open=self.is_speaking)

        # Cancel hold timer
        if self._hold_timer_task and not self._hold_timer_task.done():
            self._hold_timer_task.cancel()
            self._hold_timer_task = None

        # Reset chunker
        self.chunker.reset()
        self._queued_characters = 0

        # Before the queue is dropped: a worker that outlives its own stream is the bug, not a detail.
        await self._cancel_worker()

        # Send stream end if we had an active stream.
        # NOTE: is_speaking intentionally stays True — the browser may still
        # be playing buffered audio. The AEC window closes when the browser
        # sends voice_playback_end (or via the tail timeout fallback).
        if stream_id > 0:
            await self._send_json({
                "type": "voice_stream_end",
                "streamId": stream_id,
                "reason": reason,
            })

        self._queue = None

    async def _worker(self) -> None:
        """Consume this stream's queue and synthesize its speech chunks.

        The queue is captured at entry, not re-read from `self._queue`: that attribute is rebound by
        `_on_agent_start` and set to None by `_stop_current_internal`, so a worker that reads it can end
        up draining the next stream's chunks (and dropping them at the stream-id check, which is silent
        missing speech) or dereferencing None.
        """
        stream_id = self._stream_id
        handle = self.voice_handle
        queue = self._queue

        if handle is None or queue is None:
            logger.debug("Voice: [DEBUG] _worker() exiting early - no handle or queue")
            return

        logger.debug("Voice: [DEBUG] _worker() START stream_id=%d handle=%s", stream_id, handle.key)

        # The piece callback runs on the model thread and schedules each send onto this loop, so
        # without a gate the sends race. This chain makes each piece wait for the one before it,
        # which keeps the wire in sequence order (static/voice.js DROPS any frame whose sequence is
        # not greater than the last it saw) and keeps the AEC reference in time order. It spans
        # chunks on purpose: the previous chunk's last piece must land first.
        previous_piece: "object | None" = None

        try:
            while True:
                item = await queue.get()

                # Sentinel — end of stream
                if item is None:
                    logger.debug("Voice: [DEBUG] _worker() received sentinel, stream_id=%d", stream_id)
                    # Send stream end if this stream is still active.
                    # NOTE: is_speaking stays True here — the browser may still
                    # be playing buffered audio. The AEC window closes when the
                    # browser sends voice_playback_end (or via tail timeout).
                    if self._stream_id == stream_id:
                        await self._send_json({
                            "type": "voice_stream_end",
                            "streamId": stream_id,
                            "reason": "complete",
                        })
                    return

                text: str = item

                # Check if this stream is still active
                if self._stream_id != stream_id:
                    # Stream was superseded — discard and exit
                    logger.debug("Voice: [DEBUG] _worker() stream superseded (was %d, now %d), exiting", stream_id, self._stream_id)
                    return

                self._synthesizing = True
                if self._stream_id == stream_id:
                    self._queued_characters -= len(text)
                # pending_characters stays until synthesis completes

                logger.debug("Voice: [DEBUG] _worker() synthesizing chunk len=%d text=%s", len(text), repr(text[:60]) + ("..." if len(text) > 60 else ""))

                try:
                    # Piece-at-a-time when the engine can produce pieces (Phonon streams frame
                    # by frame: first audio 0.02 s in, while the whole 160-character sentence
                    # takes 0.89 s). Otherwise the callback is never called and the single frame
                    # is sent below, exactly as before.
                    loop = asyncio.get_running_loop()
                    inflight: list = []
                    pieces_sent = 0

                    def deliver_piece(pcm_s16le: bytes, samples: int, rate: int) -> None:
                        nonlocal pieces_sent, previous_piece
                        if self._stream_id != stream_id:
                            raise StreamSuperseded()
                        self._sequence += 1
                        frame = build_pcm_frame(
                            stream_id=stream_id,
                            sequence=self._sequence,
                            sample_rate=rate,
                            pcm_s16le=pcm_s16le,
                        )
                        logger.debug(
                            "Voice: [DEBUG] _worker() sending piece seq=%d stream=%d samples=%d bytes=%d",
                            self._sequence, stream_id, samples, len(frame),
                        )
                        future = asyncio.run_coroutine_threadsafe(
                            self._deliver_frame(frame, pcm_s16le, rate, previous_piece), loop
                        )
                        previous_piece = future
                        inflight.append(future)
                        if len(inflight) >= PIECES_IN_FLIGHT:
                            # Blocks the MODEL thread until the oldest piece is on the wire. This
                            # callback is only ever called from that thread; waiting on a future
                            # for a coroutine the calling thread itself has to run is a deadlock,
                            # which is what froze the whole server on 2026-11-14.
                            future.result(timeout=PIECE_SEND_TIMEOUT)
                            while inflight and inflight[0].done():
                                inflight.pop(0)
                        pieces_sent += 1

                    audio = await self._tts.synthesize_stream(text, handle, deliver_piece)

                    # Surface any send failure in this task, not on the model thread. Await these,
                    # never `future.result()`: we are running on the event loop, and the futures
                    # are waiting for coroutines this very loop has to execute.
                    for future in inflight:
                        await asyncio.wrap_future(future)

                    # Double-check stream is still active after synthesis
                    if self._stream_id != stream_id:
                        return

                    if pieces_sent == 0:
                        # Non-streaming runtime: one frame for the whole utterance, as before.
                        self._sequence += 1
                        frame = build_pcm_frame(
                            stream_id=stream_id,
                            sequence=self._sequence,
                            sample_rate=audio.sample_rate,
                            pcm_s16le=audio.pcm_s16le,
                        )
                        logger.debug("Voice: [DEBUG] _worker() sending frame seq=%d stream=%d samples=%d bytes=%d", self._sequence, stream_id, audio.sample_count, len(frame))

                        # Deliver the frame first; only audio actually sent to the
                        # browser counts as AEC reference (a failed send must not
                        # pollute the reference buffer).
                        await self._send_bytes(frame)
                        self._append_reference(audio.pcm_s16le, audio.sample_rate)

                except StreamSuperseded:
                    # Pieces already scheduled still go out; the browser drops frames whose stream
                    # id is no longer the live one. Do not wait for them here: generation was
                    # abandoned on purpose, and the frames are harmless.
                    logger.debug("Voice: [DEBUG] _worker() generation abandoned, stream %d superseded", stream_id)
                    return
                except MemoryError as e:
                    logger.error("Synthesis OOM: %s", e)
                    if self._stream_id == stream_id:
                        await self._send_error("voice_oom", str(e), recoverable=True)
                        # End the stream so the browser resets and the AEC
                        # window can close (otherwise is_speaking and
                        # active_run stay stuck True).
                        await self._stop_current_internal(reason="error")
                    return
                except Exception as e:
                    logger.error("Synthesis failed: %s", e)
                    if self._stream_id == stream_id:
                        await self._send_error("voice_generation_failed", str(e), recoverable=True)
                        await self._stop_current_internal(reason="error")
                    return
                finally:
                    self._synthesizing = False
                    if self._stream_id == stream_id:
                        self._pending_characters -= len(text)

        except asyncio.CancelledError:
            logger.debug("Voice: [DEBUG] _worker() CANCELLED")
            raise
        except Exception as e:
            # With no traceback this line was the only evidence of a worker that had outlived its
            # stream, and it read as a one-line mystery rather than the None dereference it was.
            logger.error("Voice worker error: %s", e, exc_info=True)

    async def _send_error(self, code: str, message: str, recoverable: bool = False) -> None:
        """Send a voice error to the browser."""
        await self._send_json({
            "type": "voice_error",
            "code": code,
            "message": message,
            "recoverable": recoverable,
        })
