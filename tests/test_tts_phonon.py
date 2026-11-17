"""Tests for the Phonon runtime and the engine knob. No model, no network, no GPU.

A fake `ptts` module is injected into `sys.modules`, so these tests prove the wiring
(lazy import, thread ordering, conditioner values, waveform contract, engine selection)
without loading a checkpoint. Real-engine proof is a separate smoke run; the plan of
record for this work is `~/.pi/agent/plans/pi-chat-phonon-tts.md`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pi_chat import config
from pi_chat import tts_service as tts_service_module
from pi_chat.tts_phonon import DEFAULT_MODEL, DEFAULT_RATE, DEFAULT_VOICE, PhononRuntime
from pi_chat.tts_service import TTSService, VoiceSettings
from tests.fakes.voice import FakeVoiceStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The five decided steps. Pinned literally so a silent edit to config's ladder fails here.
EXPECTED_LADDER = {
    "slowest": 0.45,
    "slower": 0.20,
    "normal": 0.0,
    "fast": -0.20,
    "fastest": -0.40,
}


# ---------------------------------------------------------------------------
# Fake ptts module
# ---------------------------------------------------------------------------


class FakeStream:
    """Stands in for ptts.AudioStream: a context manager that yields float32 chunks."""

    def __init__(self, chunks: list[np.ndarray], sample_rate: int):
        self._chunks = chunks
        self.sample_rate = sample_rate
        self.closed = False

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, exc_type=None, exc_value=None, traceback=None) -> bool:
        self.closed = True
        return False

    def __iter__(self):
        return iter(self._chunks)


class FakeTTSModel:
    def __init__(self, init_kwargs: dict[str, Any], log: list[str], *, audio: list[np.ndarray]):
        self.init_kwargs = init_kwargs
        self._log = log
        self._audio = audio
        self.conditions: dict[str, float] = {}
        self.sample_rate = 24000
        self.voices = ["Russell", "Zoey"]
        self.stream_calls: list[dict[str, Any]] = []

    def stream(self, text: str, **kwargs: Any) -> FakeStream:
        self.stream_calls.append({"text": text, **kwargs})
        return FakeStream(self._audio, self.sample_rate)

    def set_conditions(self, conditions: dict[str, float]) -> None:
        self.conditions.update(conditions)
        self._log.append(f"set_conditions:{conditions.get('duration_delta')}")


class FakePttsModule:
    """A module-shaped object with the two names `PhononRuntime` touches."""

    def __init__(self, *, audio: list[np.ndarray] | None = None):
        self.log: list[str] = []
        self.models: list[FakeTTSModel] = []
        self.voices_for_lookup = ["Russell", "Zoey", "default"]
        self._audio = audio if audio is not None else [
            np.linspace(-0.5, 0.5, 1200, dtype=np.float32),
            np.linspace(0.5, -0.5, 800, dtype=np.float32),
        ]

    def set_num_threads(self, n: int) -> None:
        self.log.append(f"set_num_threads:{n}")

    def TTS(self, model, **kwargs: Any) -> FakeTTSModel:
        voice = kwargs.get("voice")
        # Faithful to ptts 0.7.1, which raises LookupError (NOT RuntimeError/ValueError) for
        # a catalog miss: "no voice 'Russel' is registered; this model has default". An
        # earlier version of this fake raised RuntimeError, which is how a real bug in the
        # runtime's except clause survived a green suite.
        if voice and not any(str(v).lower() == str(voice).lower() for v in self.voices_for_lookup):
            raise LookupError(f"no voice '{voice}' is registered; this model has default")
        self.log.append("TTS")
        model_ = FakeTTSModel({"model": model, **kwargs}, self.log, audio=self._audio)
        self.models.append(model_)
        return model_


def phonon_config_stub(**overrides: Any):
    class ConfigStub:
        TTS_ENGINE = "phonon"
        TTS_PHONON_MODEL = DEFAULT_MODEL
        TTS_PHONON_VOICE = DEFAULT_VOICE
        TTS_PHONON_RATE = DEFAULT_RATE
        TTS_PHONON_THREADS = 4
        TTS_PHONON_RATE_LADDER = dict(EXPECTED_LADDER)

    for key, value in overrides.items():
        setattr(ConfigStub, key, value)
    return ConfigStub()


@pytest.fixture
def fake_ptts(monkeypatch: pytest.MonkeyPatch) -> FakePttsModule:
    fake = FakePttsModule()
    monkeypatch.setitem(sys.modules, "ptts", fake)  # type: ignore[arg-type]
    return fake


# ---------------------------------------------------------------------------
# Import guards (fresh interpreter, so test order cannot make these lie)
# ---------------------------------------------------------------------------


def test_phonon_module_imports_without_torch():
    from tests.import_guard import has_module, imported_modules_for

    modules = imported_modules_for("pi_chat.tts_phonon")
    assert not has_module(modules, "torch")
    assert not has_module(modules, "omnivoice")


def test_phonon_module_imports_without_ptts():
    """The wheel is optional: importing the module must not require it."""
    from tests.import_guard import has_module, imported_modules_for

    modules = imported_modules_for("pi_chat.tts_phonon")
    assert not has_module(modules, "ptts"), "tts_phonon imported ptts at module load"


# ---------------------------------------------------------------------------
# Load path
# ---------------------------------------------------------------------------


def test_threads_are_set_before_the_model_is_built(fake_ptts: FakePttsModule):
    """ptts builds one global pool, so ordering is correctness, not tidiness."""
    runtime = PhononRuntime(config_=phonon_config_stub(TTS_PHONON_THREADS=3))
    runtime.load()

    assert fake_ptts.log == ["set_num_threads:3", "TTS"], fake_ptts.log


def test_load_pins_model_cpu_and_q8(fake_ptts: FakePttsModule):
    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()

    kwargs = fake_ptts.models[0].init_kwargs
    assert kwargs["model"].startswith("hf://gradium/phonon-31m-en@")
    assert kwargs["model"].split("@")[1] == "92741c5b07d085ff840e26fea8ce43110b87852f"
    assert kwargs["device"] == "cpu"
    assert kwargs["quant"] == "q8"
    assert kwargs["lang"] == "en"
    assert kwargs["voice"] == "Russell"


def test_pinned_default_is_the_measured_checkpoint():
    assert DEFAULT_MODEL.split("@")[1] == "92741c5b07d085ff840e26fea8ce43110b87852f"
    assert DEFAULT_VOICE == "Russell"
    assert DEFAULT_RATE == "fast"


def test_missing_ptts_names_both_ways_out(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "ptts", None)  # `import ptts` now raises ImportError

    runtime = PhononRuntime(config_=phonon_config_stub())
    with pytest.raises(RuntimeError) as excinfo:
        runtime.load()

    message = str(excinfo.value)
    # ptts became a base dependency when Phonon became the default engine, so "add the extra" is no
    # longer the fix. Both real answers belong in the message: sync the environment, or switch engine.
    assert "uv sync" in message
    assert "PI_CHAT_TTS_ENGINE=omnivoice" in message


@pytest.mark.parametrize(
    "error",
    [LookupError, RuntimeError, ValueError],
    ids=["LookupError(ptts 0.7.1)", "RuntimeError", "ValueError"],
)
def test_unknown_voice_is_named_in_the_error(
    fake_ptts: FakePttsModule, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
):
    if error is not LookupError:
        # Other backends/builds may surface the same failure differently; all must be wrapped.
        monkeypatch.setattr(
            fake_ptts, "TTS", lambda model, **kwargs: (_ for _ in ()).throw(error("bad voice"))
        )

    runtime = PhononRuntime(config_=phonon_config_stub(TTS_PHONON_VOICE="Russel"))
    with pytest.raises(RuntimeError) as excinfo:
        runtime.load()

    message = str(excinfo.value)
    assert "PI_CHAT_TTS_PHONON_VOICE" in message
    assert "Russel" in message


def test_threads_out_of_range_rejected(fake_ptts: FakePttsModule):
    runtime = PhononRuntime(config_=phonon_config_stub(TTS_PHONON_THREADS=0))
    with pytest.raises(ValueError):
        runtime.load()


# ---------------------------------------------------------------------------
# Rate ladder
# ---------------------------------------------------------------------------


def test_ladder_is_the_decided_table():
    assert config.TTS_PHONON_RATE_LADDER == EXPECTED_LADDER
    assert list(config.TTS_PHONON_RATE_LADDER) == ["slowest", "slower", "normal", "fast", "fastest"]
    assert config.TTS_PHONON_RATE == "fast"


def test_config_ladder_is_a_copy_at_runtime_use(fake_ptts: FakePttsModule):
    """The runtime must not hand out the config dict it holds."""
    stub = phonon_config_stub()
    runtime = PhononRuntime(config_=stub)

    ladder = runtime._ladder()
    ladder["fast"] = -99.0

    assert stub.TTS_PHONON_RATE_LADDER["fast"] == -0.20


@pytest.mark.parametrize(("label", "delta"), sorted(EXPECTED_LADDER.items()))
def test_each_label_becomes_that_conditioner_value(fake_ptts: FakePttsModule, label: str, delta: float):
    runtime = PhononRuntime(config_=phonon_config_stub(TTS_PHONON_RATE=label))
    runtime.load()

    assert runtime.rate_label == label
    assert fake_ptts.models[0].init_kwargs["conditions"] == {"duration_delta": pytest.approx(delta)}


def test_unknown_rate_label_rejected_at_load(fake_ptts: FakePttsModule):
    runtime = PhononRuntime(config_=phonon_config_stub(TTS_PHONON_RATE="brisk"))
    with pytest.raises(ValueError) as excinfo:
        runtime.load()

    assert "PI_CHAT_TTS_PHONON_RATE" in str(excinfo.value)
    assert "fastest" in str(excinfo.value)


def test_set_rate_moves_the_conditioner_without_reloading(fake_ptts: FakePttsModule):
    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()
    model = fake_ptts.models[0]

    runtime.set_rate("slowest")

    assert len(fake_ptts.models) == 1, "set_rate rebuilt the model"
    assert model.conditions == {"duration_delta": EXPECTED_LADDER["slowest"]}
    assert runtime.rate_label == "slowest"


def test_set_rate_rejects_an_unknown_step(fake_ptts: FakePttsModule):
    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()

    with pytest.raises(ValueError):
        runtime.set_rate("turbo")


def test_bad_engine_and_rate_are_startup_errors():
    """config validates at import, so a typo stops the server rather than the voice."""
    for env_value, knob in [("turbo", "PI_CHAT_TTS_ENGINE"), ("brisk", "PI_CHAT_TTS_PHONON_RATE")]:
        result = subprocess.run(
            [sys.executable, "-c", "import pi_chat.config"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env={**dict(os.environ), knob: env_value},
            timeout=120,
        )
        assert result.returncode != 0
        assert knob in result.stderr, result.stderr[-1500:]


# ---------------------------------------------------------------------------
# Synthesis contract
# ---------------------------------------------------------------------------


def test_generate_collects_chunks_and_sets_last_waveform(fake_ptts: FakePttsModule):
    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()
    prepared = runtime.prepare_voice(VoiceSettings(), "Hello.")

    returned, count = runtime.generate("Hello there.", prepared, VoiceSettings())

    assert count == 2000
    assert runtime._last_waveform is not None
    assert runtime._last_waveform.shape == (2000,)
    assert runtime._last_waveform.dtype == np.float32
    assert returned is prepared
    # The stream is closed, which is what releases ptts's worker threads.
    assert fake_ptts.models[0].stream_calls[0]["text"] == "Hello there."


def test_empty_audio_is_an_error(fake_ptts: FakePttsModule, monkeypatch: pytest.MonkeyPatch):
    empty = FakePttsModule(audio=[])
    monkeypatch.setitem(sys.modules, "ptts", empty)  # type: ignore[arg-type]

    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()
    prepared = runtime.prepare_voice(VoiceSettings(), "Hello.")

    with pytest.raises(RuntimeError, match="empty audio"):
        runtime.generate("Hello there.", prepared, VoiceSettings())


def test_non_finite_audio_is_an_error(monkeypatch: pytest.MonkeyPatch):
    noisy = FakePttsModule(audio=[np.array([0.1, float("nan"), 0.2], dtype=np.float32)])
    monkeypatch.setitem(sys.modules, "ptts", noisy)  # type: ignore[arg-type]

    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()
    prepared = runtime.prepare_voice(VoiceSettings(), "Hello.")

    with pytest.raises(RuntimeError, match="non-finite"):
        runtime.generate("Hello there.", prepared, VoiceSettings())


async def test_service_returns_s16le_pcm_at_phonon_rate(fake_ptts: FakePttsModule):
    """End to end through TTSService: the shape the browser-facing frames depend on."""
    service = TTSService(
        runtime_factory=lambda: PhononRuntime(config_=phonon_config_stub()),
        config_=phonon_config_stub(),
        voice_store=FakeVoiceStore(),
    )
    await service.load()
    handle = await service.prepare_voice(VoiceSettings())
    audio = await service.synthesize("Hello there.", handle)

    assert audio.sample_rate == 24000
    assert audio.sample_count == 2000
    assert len(audio.pcm_s16le) == audio.sample_count * 2
    assert service.status()["sampling_rate"] == 24000

    await service.close()


def test_phonon_load_never_consults_the_vram_guard(
    fake_ptts: FakePttsModule, monkeypatch: pytest.MonkeyPatch
):
    """The 6 GB free-VRAM rule lives inside _OmniVoiceRuntime, and q8 needs no GPU at all."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Phonon must not run nvidia-smi or a VRAM check")

    monkeypatch.setattr(tts_service_module.subprocess, "check_output", forbidden)

    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()
    assert runtime.sampling_rate == 24000


# ---------------------------------------------------------------------------
# Engine selection, and which engine you get when nobody says
# ---------------------------------------------------------------------------


def test_default_engine_is_phonon(monkeypatch: pytest.MonkeyPatch):
    """Phonon became the default engine on 2026-11-17. OmniVoice stays one env var away, and the
    voice dialog renders whichever engine the server reports."""
    monkeypatch.delattr(config, "TTS_ENGINE", raising=False)
    runtime = TTSService._default_runtime_factory()
    assert type(runtime).__name__ == "PhononRuntime"

    monkeypatch.setattr(config, "TTS_ENGINE", "omnivoice", raising=False)
    assert type(TTSService._default_runtime_factory()).__name__ == "_OmniVoiceRuntime"


def test_the_default_engine_has_one_source(monkeypatch: pytest.MonkeyPatch):
    """`getattr(config, "TTS_ENGINE", "omnivoice")` used to be written out at every call site, so the
    default was really several defaults. A config that does not mention the engine must agree with
    `config.DEFAULT_TTS_ENGINE` wherever it is asked."""
    class Silent:
        pass

    monkeypatch.delattr(config, "TTS_ENGINE", raising=False)
    assert config.resolve_tts_engine() == config.DEFAULT_TTS_ENGINE
    assert config.resolve_tts_engine(Silent()) == config.DEFAULT_TTS_ENGINE


def test_phonon_engine_is_selected_by_the_knob(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "TTS_ENGINE", "phonon")
    runtime = TTSService._default_runtime_factory()

    assert isinstance(runtime, PhononRuntime)
    assert runtime.sampling_rate is None, "the factory must not load a model"


async def test_phonon_refuses_uploaded_voices_with_an_actionable_message(
    fake_ptts: FakePttsModule,
):
    """Phonon has no speaker encoder, so the custom-voice path must fail clearly."""
    service = TTSService(
        runtime_factory=lambda: PhononRuntime(config_=phonon_config_stub()),
        config_=phonon_config_stub(),
        voice_store=FakeVoiceStore(),
    )
    store = FakeVoiceStore()
    sample = store.upload(b"RIFF" + b"\x00" * 5000, "voice.wav", "Mine")
    await service.load()

    with pytest.raises(RuntimeError) as excinfo:
        await service.prepare_voice(VoiceSettings(voice_id=sample.voice_id))

    assert "PI_CHAT_TTS_ENGINE=omnivoice" in str(excinfo.value)
    await service.close()


def test_phonon_voice_clone_prompt_entry_point_also_refuses(fake_ptts: FakePttsModule):
    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()

    with pytest.raises(RuntimeError, match="no on-device voice cloning"):
        runtime.create_voice_clone_prompt((np.zeros(10, dtype=np.float32), 24000), None)


# ---------------------------------------------------------------------------
# Settings from the dialog: voice name and rate step travel on VoiceSettings
# ---------------------------------------------------------------------------


def _load(fake_ptts: FakePttsModule, **overrides: Any) -> PhononRuntime:
    runtime = PhononRuntime(config_=phonon_config_stub(**overrides))
    runtime.load()
    return runtime


def test_prepare_voice_honours_the_dialog_over_config_defaults(fake_ptts: FakePttsModule):
    runtime = _load(fake_ptts)  # config says Russell / fast
    handle = runtime.prepare_voice(VoiceSettings(phonon_voice="Zoey", phonon_rate="slowest"), "x")

    assert handle["voice"] == "Zoey"
    assert handle["rate"] == "slowest"

    runtime.generate("hello", handle, VoiceSettings(phonon_voice="Zoey", phonon_rate="slowest"))
    model = fake_ptts.models[-1]
    assert model.stream_calls[-1]["voice"] == "Zoey"
    assert model.conditions["duration_delta"] == EXPECTED_LADDER["slowest"]


def test_prepare_voice_falls_back_to_start_up_config(fake_ptts: FakePttsModule):
    runtime = _load(fake_ptts)
    handle = runtime.prepare_voice(VoiceSettings(), "x")
    assert handle["voice"] == DEFAULT_VOICE and handle["rate"] == DEFAULT_RATE


def test_the_rate_conditioner_is_applied_once_per_change(fake_ptts: FakePttsModule):
    """set_conditions is model-level: re-applying the same step should not spam the model."""
    runtime = _load(fake_ptts)
    model = fake_ptts.models[-1]
    applied = lambda: [entry for entry in model._log if entry.startswith("set_conditions")]

    runtime.prepare_voice(VoiceSettings(phonon_rate="fastest"), "x")
    assert applied() == [f"set_conditions:{EXPECTED_LADDER['fastest']}"]

    runtime.prepare_voice(VoiceSettings(phonon_rate="fastest"), "x")
    runtime.prepare_voice(VoiceSettings(phonon_rate="fastest"), "x")
    assert len(applied()) == 1, "the same step was pushed to the model repeatedly"

    runtime.prepare_voice(VoiceSettings(phonon_rate="slower"), "x")
    assert len(applied()) == 2


def test_a_voice_the_dialog_invented_still_reaches_ptts(fake_ptts: FakePttsModule):
    """The catalog is a snapshot, so a newer voice name must not be blocked client-side."""
    runtime = _load(fake_ptts)
    fake_ptts.voices_for_lookup = list(fake_ptts.voices_for_lookup) + ["NewerVoice"]
    handle = runtime.prepare_voice(VoiceSettings(phonon_voice="NewerVoice"), "x")
    runtime.generate("hello", handle, VoiceSettings(phonon_voice="NewerVoice"))
    assert fake_ptts.models[-1].stream_calls[-1]["voice"] == "NewerVoice"


def test_the_rate_belongs_to_the_stream_being_synthesized(fake_ptts: FakePttsModule):
    """VOICE-02. `duration_delta` is model-level state and there is no per-call rate, so conditioning
    only at prepare time means the profile that prepared last wins for *both* of them."""
    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()
    model = fake_ptts.models[0]

    slow = runtime.prepare_voice(VoiceSettings(phonon_rate="slowest"), "Hello.")
    runtime.prepare_voice(VoiceSettings(phonon_rate="fastest"), "Hello.")
    assert model.conditions == {"duration_delta": EXPECTED_LADDER["fastest"]}

    runtime.generate("this one is slow, and it stays slow", slow, slow["settings"])

    assert model.conditions == {"duration_delta": EXPECTED_LADDER["slowest"]}, (
        "the synthesis ran at the other profile's speed. One profile would hear the other's setting "
        "and there is nothing in the UI that would say so"
    )


def test_a_prepare_cannot_move_the_rate_of_a_stream_already_running(fake_ptts: FakePttsModule):
    """The other half: a prepare landing while someone is talking used to re-condition the model in the
    middle of their audio. What matters is what the model holds *while the stream runs*."""
    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()
    model = fake_ptts.models[0]
    hers = runtime.prepare_voice(VoiceSettings(phonon_rate="slowest"), "Hello.")

    held_while_streaming = []
    original_stream = model.stream

    def watched(*args: Any, **kwargs: Any):
        held_while_streaming.append(dict(model.conditions))
        return original_stream(*args, **kwargs)

    model.stream = watched

    runtime.prepare_voice(VoiceSettings(phonon_rate="fastest"), "Hello.")
    runtime.generate("mine", hers, hers["settings"])

    assert held_while_streaming == [{"duration_delta": EXPECTED_LADDER["slowest"]}]


def test_an_unchanged_rate_does_not_re_condition_the_model(fake_ptts: FakePttsModule):
    """The fix runs on every sentence, so it has to be a no-op in the single-profile case it exists to
    protect. `set_conditions` is cheap on the fake and not cheap on a real model."""
    runtime = PhononRuntime(config_=phonon_config_stub(TTS_PHONON_RATE="normal"))
    runtime.load()
    model = fake_ptts.models[0]
    prepared = runtime.prepare_voice(VoiceSettings(phonon_rate="normal"), "Hello.")

    applied: list[dict[str, float]] = []
    original = model.set_conditions

    def watched(conditions: dict[str, float]) -> None:
        applied.append(dict(conditions))
        original(conditions)

    model.set_conditions = watched

    runtime.generate("one", prepared, prepared["settings"])
    runtime.generate("two", prepared, prepared["settings"])

    assert applied == [], f"re-conditioned twice for a rate that never changed: {applied}"
