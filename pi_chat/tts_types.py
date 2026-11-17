"""The voice types that production and the fakes share.

These four lived in `tests/fakes/voice.py`, which meant `pi_chat` imported its own type definitions from
the test tree (`VOICE-06`): shipping the package required `tests/`, and `pyproject`'s
`packages = ["pi_chat"]` did not include it, so a wheel built from that would have been broken by
construction. The arrangement itself was right, one definition that the real engine and the fake cannot
drift apart. Only the direction was wrong, so now production owns the types and `tests/fakes/voice.py`
imports them from here.

`tests/test_import_layering.py` keeps the direction from flipping back.

Split note: `VoiceSample` and `FakeVoiceStore` stay in the fakes deliberately. They model *stored* voice
samples, whose real implementation is `pi_chat/voice_store.py` with its own shapes, and moving them here
would put a second, unused definition of that boundary in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class VoiceSettings:
    """Engine-neutral voice settings.

    OmniVoice reads the design fields (gender, age, pitch, accent, style, speed, language) and the
    `voice_id` of a cloned sample. Phonon ignores all of those and reads `phonon_voice` / `phonon_rate`,
    because its voices are named catalog entries rather than designs and its rate is one of five labeled
    steps rather than a float multiplier (see `pi_chat/phonon_voices.py`).

    One type for both engines is deliberate: it travels through `voice_session`, `tts_service`, the
    capabilities route and the browser payload without an engine branch at every hop, and
    `TTSService._voice_key` hashes it, so a field that means nothing to the active engine still has to
    participate in the cache key or a rate change reuses a cached voice.
    """

    gender: str = "female"
    age: str = "young adult"
    pitch: str = "moderate pitch"
    accent: str = "american accent"
    style: str | None = None
    speed: float = 1.0
    language: str = "English"
    voice_id: str | None = None  # Custom voice sample ID for cloning
    phonon_voice: str | None = None
    phonon_rate: str | None = None


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


@runtime_checkable
class TTSRuntime(Protocol):
    """The contract an engine adapter satisfies.

    `TTSService` depends on this, not on torch, `ptts` or omnivoice directly. It is deliberately minimal:
    the streaming additions (`generate(..., on_piece=...)`, `set_rate`) are engine capabilities the service
    probes with `getattr` rather than demands here, which is why `FakeOmniVoiceRuntime` can omit
    `supports_incremental_playback` and make the streaming path unreachable in fakes on purpose.
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
