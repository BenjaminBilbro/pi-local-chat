"""Per-WebSocket voice session state and orchestration.

Manages voice enable/disable, settings, stream IDs, queue, framing,
and cancellation for one browser connection.

Never imports torch/omnivoice directly — depends only on TTSService.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field

from pi_chat import config
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
        allowed_keys = {"gender", "age", "pitch", "accent", "style", "speed", "language", "voice_id"}
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

        return VoiceSettings(
            gender=gender,
            age=age,
            pitch=pitch,
            accent=accent,
            style=style,
            speed=speed,
            language=language,
            voice_id=voice_id,
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
    ):
        self._tts = tts_service
        self._send_json = send_json
        self._send_bytes = send_bytes
        self._config = config_ or config

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

        # Chunker
        self.chunker: StreamingSpeechChunker = StreamingSpeechChunker()

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

        # Closed flag
        self.closed: bool = False

    # ------------------------------------------------------------------
    # Public control methods
    # ------------------------------------------------------------------

    async def enable(self, raw_settings: dict) -> None:
        """Enable voice mode with the given settings."""
        logger.info("Voice: [DEBUG] enable() called with settings=%s", raw_settings)
        settings = validate_settings(raw_settings)
        if settings is None:
            logger.info("Voice: [DEBUG] enable() FAILED - invalid settings")
            await self._send_error(
                "voice_invalid_settings",
                "Invalid voice settings provided.",
                recoverable=True,
            )
            return

        self.enabled = True
        self._current_activation_id = self._activation_id = self._activation_id + 1
        self.settings = settings
        logger.info("Voice: [DEBUG] enable() activation_id=%d", self._activation_id)

        await self._send_json({"type": "voice_state", "state": "loading", "available": True})

        # Start preparation in background
        self._prepare_task = asyncio.create_task(self._prepare_voice())

    async def disable(self) -> None:
        """Disable voice mode."""
        logger.info("Voice: [DEBUG] disable() called")
        self.enabled = False
        self.ready = False
        self._invalidate_activation()
        await self._stop_current_internal(reason="disabled")

        await self._send_json({"type": "voice_state", "state": "disabled", "available": False})

    async def update_settings(self, raw_settings: dict) -> None:
        """Update voice settings for the next response."""
        logger.info("Voice: [DEBUG] update_settings() called with settings=%s", raw_settings)
        settings = validate_settings(raw_settings)
        if settings is None:
            logger.info("Voice: [DEBUG] update_settings() FAILED - invalid settings")
            await self._send_error(
                "voice_invalid_settings",
                "Invalid voice settings provided.",
                recoverable=True,
            )
            return

        self._current_activation_id = self._activation_id = self._activation_id + 1
        self.settings = settings
        logger.info("Voice: [DEBUG] update_settings() activation_id=%d", self._activation_id)

        # Start preparation in background
        self._prepare_task = asyncio.create_task(self._prepare_voice())

    async def stop_current(self, reason: str = "stopped") -> None:
        """Stop the current response's voice but keep voice mode enabled."""
        await self._stop_current_internal(reason=reason)

    async def prepare(self, raw_settings: dict) -> None:
        """Prepare voice for given settings without changing enabled state.

        Sends voice_prepared or voice_error response.
        Used by frontend when user applies settings changes.
        """
        logger.info("Voice: [DEBUG] prepare() called with settings=%s", raw_settings)
        settings = validate_settings(raw_settings)
        if settings is None:
            logger.info("Voice: [DEBUG] prepare() FAILED - invalid settings")
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
            logger.info("Voice: [DEBUG] prepare() activation_id=%d", self._activation_id)

            # Load TTS and prepare voice
            logger.info("Voice: [DEBUG] prepare() calling tts.load()...")
            await self._tts.load()
            logger.info("Voice: [DEBUG] prepare() calling tts.prepare_voice()...")
            handle = await self._tts.prepare_voice(settings)
            logger.info("Voice: [DEBUG] prepare() tts.prepare_voice() returned handle=%s", handle.key)

            # Cache the handle if voice is enabled
            if self.enabled:
                self.voice_handle = handle
                self.ready = True
                logger.info("Voice: [DEBUG] prepare() cached handle (voice enabled)")

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
            logger.info("Voice: [DEBUG] prepare() COMPLETE")

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
        logger.info("Voice: [DEBUG] observe_pi_event() event_type=%s", event_type)

        if event_type == "agent_start":
            await self._on_agent_start()
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
        logger.info("Voice: [DEBUG] _prepare_voice() START activation_id=%d", activation_id)
        try:
            logger.info("Voice: [DEBUG] _prepare_voice() calling tts.load()...")
            await self._tts.load()
            logger.info("Voice: [DEBUG] _prepare_voice() calling tts.prepare_voice()...")
            handle = await self._tts.prepare_voice(self.settings)
            logger.info("Voice: [DEBUG] _prepare_voice() tts.prepare_voice() returned handle=%s", handle.key)

            # Install only if this activation is still current
            if (
                not self.closed
                and self.enabled
                and activation_id == self._activation_id
            ):
                self.voice_handle = handle
                self.ready = True
                logger.info("Voice: [DEBUG] _prepare_voice() installed handle, ready=True")
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
                logger.info("Voice: [DEBUG] _prepare_voice() completed for STALE activation %d (current=%d)", activation_id, self._activation_id)

        except asyncio.CancelledError:
            logger.info("Voice: [DEBUG] _prepare_voice() CANCELLED activation_id=%d", activation_id)
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

    async def _on_agent_start(self) -> None:
        """Handle agent_start: start a new voice stream if enabled and ready."""
        logger.info("Voice: [DEBUG] _on_agent_start() ready=%s, has_handle=%s", self.ready, self.voice_handle is not None)
        # Cancel old stream if active
        if self.active_run:
            logger.info("Voice: [DEBUG] _on_agent_start() cancelling old active_run")
            await self._stop_current_internal(reason="superseded")

        if not self.ready or not self.voice_handle:
            logger.info("Voice: [DEBUG] _on_agent_start() returning early - not ready or no handle")
            return

        self.active_run = True
        self.suppress_current_run = False
        self._stream_id += 1
        self._sequence = 0
        self.chunker.reset()
        self._queued_characters = 0
        self._synthesizing = False

        # Create bounded queue (max_data_chunks + 1 for sentinel)
        max_chunks = self._config.TTS_MAX_QUEUE_CHUNKS
        self._queue = asyncio.Queue(maxsize=max_chunks + 1)
        self._pending_characters = 0

        # Cancel old hold timer
        if self._hold_timer_task and not self._hold_timer_task.done():
            self._hold_timer_task.cancel()
            self._hold_timer_task = None

        # Start worker
        self._worker_task = asyncio.create_task(self._worker())

        logger.info("Voice: [DEBUG] _on_agent_start() stream_id=%d, voice_handle=%s", self._stream_id, self.voice_handle.key)
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
            logger.info("Voice: [DEBUG] _on_message_update() text_start")
        elif sub_type == "text_delta":
            delta = am_event.get("delta", "")
            if delta:
                logger.info("Voice: [DEBUG] _on_message_update() text_delta len=%d text=%s", len(delta), repr(delta[:60]) + ("..." if len(delta) > 60 else ""))
                await self._feed_delta(delta)
        elif sub_type == "text_end":
            logger.info("Voice: [DEBUG] _on_message_update() text_end")
            # Flush current text block
            await self._flush_text_block()

    async def _feed_delta(self, delta: str) -> None:
        """Feed a text delta through the chunker and enqueue speech chunks."""
        now = time.monotonic()
        chunks = self.chunker.feed(delta, now=now)
        logger.info("Voice: [DEBUG] _feed_delta() produced %d chunks: %s", len(chunks), [repr(c[:30]) + "..." if len(c) > 30 else repr(c) for c in chunks])

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
        logger.info("Voice: [DEBUG] _flush_text_block() produced %d chunks: %s", len(chunks), [repr(c[:30]) + "..." if len(c) > 30 else repr(c) for c in chunks])
        for chunk in chunks:
            await self._enqueue_chunk(chunk)

        # Cancel hold timer on block end
        if self._hold_timer_task and not self._hold_timer_task.done():
            self._hold_timer_task.cancel()
            self._hold_timer_task = None

    async def _on_agent_settled(self) -> None:
        """Handle agent_settled: finish the response."""
        logger.info("Voice: [DEBUG] _on_agent_settled() active_run=%s", self.active_run)
        if not self.active_run:
            return

        await self._flush_text_block()

        # Finish chunker and enqueue remaining
        now = time.monotonic()
        chunks = self.chunker.finish()
        logger.info("Voice: [DEBUG] _on_agent_settled() chunker.finish() produced %d chunks: %s", len(chunks), [repr(c[:30]) + "..." if len(c) > 30 else repr(c) for c in chunks])
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

    async def _stop_current_internal(self, reason: str) -> None:
        """Stop the current voice stream internally."""
        logger.info("Voice: [DEBUG] _stop_current_internal() reason=%s stream_id=%d", reason, self._stream_id)
        stream_id = self._stream_id
        self.active_run = False

        # Cancel hold timer
        if self._hold_timer_task and not self._hold_timer_task.done():
            self._hold_timer_task.cancel()
            self._hold_timer_task = None

        # Reset chunker
        self.chunker.reset()
        self._queued_characters = 0

        # If we have an active queue, enqueue sentinel to drain worker
        if self._queue is not None and not self._queue.full():
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        # Send stream end if we had an active stream
        if stream_id > 0:
            await self._send_json({
                "type": "voice_stream_end",
                "streamId": stream_id,
                "reason": reason,
            })

        self._queue = None

    async def _worker(self) -> None:
        """Consume queue and synthesize speech chunks."""
        stream_id = self._stream_id
        handle = self.voice_handle

        if handle is None or self._queue is None:
            logger.info("Voice: [DEBUG] _worker() exiting early - no handle or queue")
            return

        logger.info("Voice: [DEBUG] _worker() START stream_id=%d handle=%s", stream_id, handle.key)

        try:
            while True:
                item = await self._queue.get()

                # Sentinel — end of stream
                if item is None:
                    logger.info("Voice: [DEBUG] _worker() received sentinel, stream_id=%d", stream_id)
                    # Send stream end if this stream is still active
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
                    logger.info("Voice: [DEBUG] _worker() stream superseded (was %d, now %d), exiting", stream_id, self._stream_id)
                    return

                self._synthesizing = True
                self._queued_characters -= len(text)
                # pending_characters stays until synthesis completes

                logger.info("Voice: [DEBUG] _worker() synthesizing chunk len=%d text=%s", len(text), repr(text[:60]) + ("..." if len(text) > 60 else ""))

                try:
                    audio = await self._tts.synthesize(text, handle)

                    # Double-check stream is still active after synthesis
                    if self._stream_id != stream_id:
                        return

                    # Build and send binary frame
                    self._sequence += 1
                    frame = build_pcm_frame(
                        stream_id=stream_id,
                        sequence=self._sequence,
                        sample_rate=audio.sample_rate,
                        pcm_s16le=audio.pcm_s16le,
                    )
                    logger.info("Voice: [DEBUG] _worker() sending frame seq=%d stream=%d samples=%d bytes=%d", self._sequence, stream_id, audio.sample_count, len(frame))
                    await self._send_bytes(frame)

                except MemoryError as e:
                    logger.error("Synthesis OOM: %s", e)
                    if self._stream_id == stream_id:
                        await self._send_error("voice_oom", str(e), recoverable=True)
                    return
                except Exception as e:
                    logger.error("Synthesis failed: %s", e)
                    if self._stream_id == stream_id:
                        await self._send_error("voice_generation_failed", str(e), recoverable=True)
                    return
                finally:
                    self._synthesizing = False
                    self._pending_characters -= len(text)

        except asyncio.CancelledError:
            logger.info("Voice: [DEBUG] _worker() CANCELLED")
            return
        except Exception as e:
            logger.error("Voice worker error: %s", e)

    async def _send_error(self, code: str, message: str, recoverable: bool = False) -> None:
        """Send a voice error to the browser."""
        await self._send_json({
            "type": "voice_error",
            "code": code,
            "message": message,
            "recoverable": recoverable,
        })
