"""Deterministic fake OmniVoice runtime and TTS service for testing without GPU.

This module provides fake implementations that:
- Never import torch, numpy, or omnivoice at module load time
- Produce deterministic, machine-verifiable audio output
- Support configurable failures, delays, and concurrency proofs
- Match the real TTSRuntime protocol and TTSService public API
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import struct
import threading
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Public data types (from plan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceSettings:
    """OmniVoice voice design settings."""

    gender: str = "female"
    age: str = "young adult"
    pitch: str = "moderate pitch"
    accent: str = "american accent"
    style: str | None = None
    speed: float = 1.0


@dataclass(frozen=True)
class VoiceHandle:
    """Opaque handle to a prepared voice."""

    key: str
    settings: VoiceSettings


@dataclass(frozen=True)
class SynthesizedAudio:
    """Result of a synthesis call."""

    sample_rate: int
    pcm_s16le: bytes
    sample_count: int
    generation_seconds: float


# ---------------------------------------------------------------------------
# Internal runtime seam
# ---------------------------------------------------------------------------


@runtime_checkable
class TTSRuntime(Protocol):
    """Internal protocol for model-specific runtime operations.

    TTSService depends on this, not on torch/omnivoice directly.
    """

    def load(self) -> None: ...

    def prepare_voice(
        self,
        settings: VoiceSettings,
        bootstrap_text: str,
    ) -> object: ...

    def generate(
        self,
        text: str,
        prepared_voice: object,
        settings: VoiceSettings,
    ) -> tuple[object, int]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Fake OmniVoice runtime
# ---------------------------------------------------------------------------


class FakeOmniVoiceRuntime:
    """Deterministic fake OmniVoice runtime with full failure/delay control.

    Produces a sine wave whose frequency encodes the call index so ordering
    is both audible and machine-verifiable.
    """

    BASE_SAMPLE_RATE = 24_000
    # Base frequency (Hz) for the first call; each subsequent call adds 50 Hz
    BASE_FREQUENCY = 440.0
    FREQUENCY_STEP = 50.0
    # Approximate duration per character of text
    DURATION_PER_CHAR = 0.08

    def __init__(
        self,
        *,
        sampling_rate: int = BASE_SAMPLE_RATE,
        load_delay: float = 0.0,
        prepare_delay: float = 0.0,
        generate_delay: float = 0.0,
        fail_on_load: bool = False,
        fail_on_prepare: bool = False,
        fail_on_generate_index: int | None = None,
        fail_with_oom: bool = False,
        generate_empty: bool = False,
        generate_nans: bool = False,
        generate_infs: bool = False,
        clip_output: bool = False,
        alternate_sample_rate: int | None = None,
        block_generate_event: threading.Event | None = None,
    ):
        # alternate_sample_rate takes precedence over sampling_rate
        self.sampling_rate = alternate_sample_rate if alternate_sample_rate else sampling_rate
        self.load_delay = load_delay
        self.prepare_delay = prepare_delay
        self.generate_delay = generate_delay
        self.fail_on_load = fail_on_load
        self.fail_on_prepare = fail_on_prepare
        self.fail_on_generate_index = fail_on_generate_index
        self.fail_with_oom = fail_with_oom
        self.generate_empty = generate_empty
        self.generate_nans = generate_nans
        self.generate_infs = generate_infs
        self.clip_output = clip_output
        self.alternate_sample_rate = alternate_sample_rate

        # Concurrency tracking
        self._lock = threading.Lock()
        self.active_calls: int = 0
        self.max_active_calls: int = 0
        self._call_counter: int = 0

        # State
        self._loaded: bool = False
        self._prepared_voices: dict[str, Any] = {}
        self._prepare_keys: list[str] = []
        self._generate_calls: list[dict[str, Any]] = []
        self._block_generate_event = block_generate_event
        self._last_waveform: list[float] | None = None  # For TTSService extraction

    def load(self) -> None:
        if self.load_delay > 0:
            time.sleep(self.load_delay)
        if self.fail_on_load:
            raise RuntimeError("FakeOmniVoiceRuntime: simulated load failure")
        self._loaded = True

    def prepare_voice(
        self,
        settings: VoiceSettings,
        bootstrap_text: str,
    ) -> object:
        if not self._loaded:
            raise RuntimeError("FakeOmniVoiceRuntime: not loaded")
        if self.prepare_delay > 0:
            time.sleep(self.prepare_delay)
        if self.fail_on_prepare:
            raise RuntimeError("FakeOmniVoiceRuntime: simulated prepare failure")

        key = self._voice_key(settings)
        with self._lock:
            if key not in self._prepared_voices:
                self._prepared_voices[key] = {
                    "settings": settings,
                    "bootstrap_text": bootstrap_text,
                    "created_at": time.monotonic(),
                }
                self._prepare_keys.append(key)
        return self._prepared_voices[key]

    def generate(
        self,
        text: str,
        prepared_voice: object,
        settings: VoiceSettings,
    ) -> tuple[object, int]:
        if not self._loaded:
            raise RuntimeError("FakeOmniVoiceRuntime: not loaded")

        # Serialize all generate calls so only one is active at a time.
        # This matches the TTSService single-thread executor behavior.
        with self._lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            call_index = self._call_counter
            self._call_counter += 1

            # Check for Nth-call failure
            if (
                self.fail_on_generate_index is not None
                and call_index == self.fail_on_generate_index
            ):
                self.active_calls -= 1
                if self.fail_with_oom:
                    raise MemoryError("FakeOmniVoiceRuntime: simulated OOM")
                raise RuntimeError(
                    f"FakeOmniVoiceRuntime: simulated generate failure at index {call_index}"
                )

            if self.generate_delay > 0:
                time.sleep(self.generate_delay)

            # Block until event is set (event starts cleared = blocked)
            # Lock is held during wait so other threads queue behind
            if self._block_generate_event is not None:
                self._block_generate_event.wait()

            # Record call
            self._generate_calls.append({
                "index": call_index,
                "text": text,
                "settings": settings,
                "prepared_voice_key": (
                    prepared_voice.get("key")
                    if isinstance(prepared_voice, dict)
                    else None
                ),
            })

            # Generate deterministic audio
            if self.generate_empty:
                self.active_calls -= 1
                return prepared_voice, 0

            sr = self.alternate_sample_rate if self.alternate_sample_rate else self.sampling_rate
            frequency = self.BASE_FREQUENCY + (call_index * self.FREQUENCY_STEP)
            duration = max(0.1, len(text) * self.DURATION_PER_CHAR)
            num_samples = int(duration * sr)

            samples = []
            for i in range(num_samples):
                t = i / sr
                value = 0.7 * math.sin(2 * math.pi * frequency * t)
                if self.generate_nans:
                    value = float("nan")
                elif self.generate_infs:
                    value = float("inf")
                samples.append(value)

            # Store waveform for TTSService extraction (matches _OmniVoiceRuntime)
            self._last_waveform = samples

            self.active_calls -= 1
            return prepared_voice, len(samples)

    def close(self) -> None:
        self._loaded = False
        self._prepared_voices.clear()
        self._prepare_keys.clear()
        self._generate_calls.clear()

    # Public inspection helpers for tests
    @property
    def prepared_voice_keys(self) -> list[str]:
        return list(self._prepare_keys)

    @property
    def generate_calls(self) -> list[dict[str, Any]]:
        return list(self._generate_calls)

    @property
    def call_counter(self) -> int:
        return self._call_counter

    def _voice_key(self, settings: VoiceSettings) -> str:
        raw = f"{settings.gender}|{settings.age}|{settings.pitch}|{settings.accent}|{settings.style}|{settings.speed}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fake TTS service (for VoiceSession testing)
# ---------------------------------------------------------------------------


class FakeTTSService:
    """Fake TTSService that wraps FakeOmniVoiceRuntime for VoiceSession testing.

    Returns valid SynthesizedAudio immediately or under controlled delay.
    Used when testing VoiceSession/WebSocket behavior in isolation.
    """

    def __init__(
        self,
        *,
        runtime: FakeOmniVoiceRuntime | None = None,
        synthesize_delay: float = 0.0,
        fail_on_synthesize_index: int | None = None,
    ):
        self._runtime = runtime or FakeOmniVoiceRuntime()
        self._synthesize_delay = synthesize_delay
        self._fail_on_synthesize_index = fail_on_synthesize_index
        self._loaded = False
        self._voice_handles: dict[str, VoiceHandle] = {}
        self._synth_counter = 0
        self._lock = threading.Lock()

    async def load(self) -> None:
        if self._loaded:
            return
        self._runtime.load()
        self._loaded = True

    async def prepare_voice(self, settings: VoiceSettings) -> VoiceHandle:
        key = self._voice_key(settings)
        if key in self._voice_handles:
            return self._voice_handles[key]

        prepared = self._runtime.prepare_voice(settings, "Hello. I'm ready to help.")
        handle = VoiceHandle(key=key, settings=settings)
        self._voice_handles[key] = handle
        return handle

    async def synthesize(
        self,
        text: str,
        voice: VoiceHandle,
    ) -> SynthesizedAudio:
        if self._synthesize_delay > 0:
            await asyncio.sleep(self._synthesize_delay)

        with self._lock:
            idx = self._synth_counter
            self._synth_counter += 1

        if (
            self._fail_on_synthesize_index is not None
            and idx == self._fail_on_synthesize_index
        ):
            raise RuntimeError("FakeTTSService: simulated synthesis failure")

        # Generate deterministic PCM
        sr = self._runtime.sampling_rate
        frequency = FakeOmniVoiceRuntime.BASE_FREQUENCY + (idx * FakeOmniVoiceRuntime.FREQUENCY_STEP)
        duration = max(0.1, len(text) * FakeOmniVoiceRuntime.DURATION_PER_CHAR)
        num_samples = int(duration * sr)

        pcm = bytearray()
        for i in range(num_samples):
            t = i / sr
            value = int(0.7 * 32767 * math.sin(2 * math.pi * frequency * t))
            pcm.extend(struct.pack("<h", value))

        return SynthesizedAudio(
            sample_rate=sr,
            pcm_s16le=bytes(pcm),
            sample_count=num_samples,
            generation_seconds=duration,
        )

    def status(self) -> dict:
        return {
            "loaded": self._loaded,
            "voice_handles": len(self._voice_handles),
            "synthesizes": self._synth_counter,
        }

    async def close(self) -> None:
        self._runtime.close()
        self._loaded = False
        self._voice_handles.clear()

    def _voice_key(self, settings: VoiceSettings) -> str:
        raw = f"{settings.gender}|{settings.age}|{settings.pitch}|{settings.accent}|{settings.style}|{settings.speed}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Synthetic pi event fixtures
# ---------------------------------------------------------------------------

def fixture_one_sentence() -> list[dict]:
    """Single text_delta producing one sentence."""
    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Hello there. "}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "How can I help you today?"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "Hello there. How can I help you today?"}},
        {"type": "message_end", "message": {"role": "assistant", "content": "Hello there. How can I help you today?"}},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]


def fixture_multiple_text_blocks() -> list[dict]:
    """Multiple text_start/text_end blocks in one run."""
    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "First block. "}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "First block."}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Second block here. "}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "Second block here."}},
        {"type": "message_end", "message": {"role": "assistant", "content": "First block. Second block here."}},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]


def fixture_tool_gap() -> list[dict]:
    """Text, then tool call, then more text."""
    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Let me check that for you. "}},
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_start", "toolCall": {"id": "call_1"}}},
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_end", "toolCall": {"id": "call_1", "name": "read", "arguments": "{}"}}},
        {"type": "message_end", "message": {"role": "assistant", "content": "Let me check that for you."}},
        {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "read"},
        {"type": "tool_execution_end", "toolCallId": "call_1", "result": {"content": "file content"}},
        {"type": "message_start", "message": {"role": "toolResult", "content": "file content"}},
        {"type": "message_end", "message": {"role": "toolResult", "content": "file content"}},
        {"type": "turn_end"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Here's what I found. It looks good."}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "Here's what I found. It looks good."}},
        {"type": "message_end", "message": {"role": "assistant", "content": "Here's what I found. It looks good."}},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]


def fixture_retry() -> list[dict]:
    """agent_start, agent_end, then another agent_start before settled."""
    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "First attempt. "}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "First attempt."}},
        {"type": "message_end", "message": {"role": "assistant", "content": "First attempt."}},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Retry attempt. Done."}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "Retry attempt. Done."}},
        {"type": "message_end", "message": {"role": "assistant", "content": "Retry attempt. Done."}},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]


def fixture_long_unpunctuated() -> list[dict]:
    """Long unpunctuated text that should trigger max-hold chunking."""
    text = "this is a very long response without any punctuation at all and it keeps going and going and going without stopping to take a breath or pause for effect because the model just keeps generating tokens without any sentence boundaries"
    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": text}},
        {"type": "message_end", "message": {"role": "assistant", "content": text}},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]


def fixture_markdown() -> list[dict]:
    """Text with Markdown formatting."""
    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "# Heading\n\n"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "**Bold text** and *italic text*. "}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "[Link label](https://example.com). "}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Here is some `inline code` too."}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "# Heading\n\n**Bold text** and *italic text*. [Link label](https://example.com). Here is some `inline code` too."}},
        {"type": "message_end", "message": {"role": "assistant", "content": "# Heading\n\n**Bold text** and *italic text*. [Link label](https://example.com). Here is some `inline code` too."}},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]


def fixture_code_block() -> list[dict]:
    """Text with fenced code block that should be omitted."""
    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Here is some code:\n\n```python\n"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "def hello():\n    print('world')\n```"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "\n\nThat should work."}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "Here is some code:\n\n```python\ndef hello():\n    print('world')\n```\n\nThat should work."}},
        {"type": "message_end", "message": {"role": "assistant", "content": "Here is some code:\n\n```python\ndef hello():\n    print('world')\n```\n\nThat should work."}},
        {"type": "turn_end"},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]


def fixture_cancellation() -> list[dict]:
    """Events that should trigger cancellation (response failure)."""
    return [
        {"type": "response", "command": "prompt", "success": False},
    ]


def fixture_all_fixtures() -> dict[str, list[dict]]:
    """All fixtures by name."""
    return {
        "one_sentence": fixture_one_sentence(),
        "multiple_text_blocks": fixture_multiple_text_blocks(),
        "tool_gap": fixture_tool_gap(),
        "retry": fixture_retry(),
        "long_unpunctuated": fixture_long_unpunctuated(),
        "markdown": fixture_markdown(),
        "code_block": fixture_code_block(),
        "cancellation": fixture_cancellation(),
    }
