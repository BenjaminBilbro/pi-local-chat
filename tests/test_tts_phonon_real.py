"""Real-engine Phonon tests. Marked `audio_cpu`: they run the actual ptts runtime on the
CPU, so they are excluded from the default run by
    pytest -m "not audio_gpu and not audio_cpu"
and selected by
    pytest -m audio_cpu

Deliberate: the fake-ptts tests in tests/test_tts_phonon.py cover the seam in milliseconds,
and these cover the thing a fake cannot - that the pinned checkpoint loads, that the rate
conditioner really changes the audio, and that it stays fast enough to speak over a live
stream on this box while other work holds the machine.

Weights come from the Hugging Face cache (first use downloads ~65 MB). Nothing here touches
a GPU: q8 is a CPU-only path in ptts, and the runtime passes device="cpu" explicitly.
"""

from __future__ import annotations

import importlib.util
import logging
import time
from types import SimpleNamespace

import pytest

from pi_chat.tts_phonon import DEFAULT_MODEL, PhononRuntime

pytestmark = pytest.mark.audio_cpu

TEXT = "The tune finished at ten thirty eight, and the winner uses four cpu threads."

# Sustained throughput needs enough audio to amortize per-call setup. pi-chat synthesizes one
# speech chunk per call, so both numbers matter: this one is the engine's throughput, the
# short one above is what a user waits through per sentence.
LONG_TEXT = (
    "Sure thing. The tune finished at ten thirty eight this morning, and the winner is search "
    "twenty twenty six one one zero four one zero three eight five seven: thirty six hundred "
    "prompt tokens with about forty five tokens per second. I would leave the drafter alone and "
    "rerun the I Q three pass once, then compare the two. The notes are in the strata inference "
    "topic, and the full table is at https colon slash slash example dot com slash tune slash report."
)

# ptts sizes one global pool at first use, so a test that sets this cannot be undone in-process.
# Two threads keeps the run polite next to whatever else holds the box.
THREADS = 2


def _require_ptts() -> None:
    if importlib.util.find_spec("ptts") is None:
        pytest.skip("ptts is not installed; uv sync")


@pytest.fixture(scope="module")
def runtime() -> object:
    _require_ptts()
    cfg = SimpleNamespace(
        TTS_PHONON_MODEL=DEFAULT_MODEL,
        TTS_PHONON_VOICE="Russell",
        TTS_PHONON_RATE="normal",
        TTS_PHONON_THREADS=THREADS,
        TTS_PHONON_RATE_LADDER={
            "slowest": 0.45,
            "slower": 0.20,
            "normal": 0.0,
            "fast": -0.20,
            "fastest": -0.40,
        },
    )
    rt = PhononRuntime(config_=cfg)
    rt.load()
    try:
        yield rt
    finally:
        rt.close()


def _synth(rt: PhononRuntime, text: str) -> tuple[int, float]:
    """Return (sample_count, wall seconds) for one streamed synthesis."""
    voice = rt.prepare_voice(SimpleNamespace(), "bootstrap")
    started = time.perf_counter()
    _, samples = rt.generate(text, voice, SimpleNamespace())
    return samples, time.perf_counter() - started


def test_pinned_checkpoint_loads_on_cpu(runtime: PhononRuntime) -> None:
    assert runtime.sampling_rate == 24_000
    assert any(v.lower() == "russell" for v in (runtime._model.voices or []))
    # q8 on cpu: the whole point of choosing this engine is that it never asks for a GPU.
    assert runtime._model.device == "cpu"
    assert "q8" in str(runtime._model.quant)


def test_generate_returns_a_real_mono_waveform(runtime: PhononRuntime) -> None:
    samples, wall = _synth(runtime, TEXT)
    waveform = runtime._last_waveform

    assert samples > 0, "no audio"
    assert waveform.ndim == 1
    assert len(waveform) == samples
    assert float(waveform.min()) > -1.5 and float(waveform.max()) < 1.5

    seconds = samples / runtime.sampling_rate
    assert 0.5 < seconds < 30.0
    # Per-call overhead is real and this is the production shape (one call per speech chunk).
    # Measured 4.56s of audio in 0.58-0.66s = 6.9x-7.9x at 2 threads with strata live, and the
    # duration is stable to the frame across repeats, so the floor only catches a regression.
    rtf = seconds / wall
    print(f"[phonon] short sentence: {seconds:.2f}s audio in {wall:.2f}s = {rtf:.2f}x")
    assert rtf > 3.0, f"per-call real-time factor {rtf:.2f}x is too slow for live voice"


def test_sustained_throughput_stays_comfortably_realtime(runtime: PhononRuntime) -> None:
    """The engine's throughput, separate from per-call overhead."""
    samples, wall = _synth(runtime, LONG_TEXT)
    seconds = samples / runtime.sampling_rate
    rtf = seconds / wall
    print(f"[phonon] sustained: {seconds:.2f}s audio in {wall:.2f}s = {rtf:.2f}x")
    # tools/phonon_probe.py measured 7.3x at 2 threads and 9.0x at 4 with strata live.
    assert rtf > 4.0, f"sustained real-time factor {rtf:.2f}x is too slow"


def test_rate_conditioner_actually_changes_pace(runtime: PhononRuntime) -> None:
    samples_normal, _ = _synth(runtime, TEXT)
    runtime.set_rate("fastest")
    try:
        samples_fast, _ = _synth(runtime, TEXT)
    finally:
        runtime.set_rate("normal")

    ratio = samples_fast / samples_normal
    # Measured on this box: -0.40 reaches about 0.76 of the normal duration (1.31x faster).
    # Bounds are wide because the model is stochastic, but the direction is not negotiable.
    assert 0.5 < ratio < 0.92, f"fastest should speak clearly faster, ratio was {ratio:.3f}"


def test_slowest_step_paces_down(runtime: PhononRuntime) -> None:
    samples_normal, _ = _synth(runtime, TEXT)
    runtime.set_rate("slowest")
    try:
        samples_slow, _ = _synth(runtime, TEXT)
    finally:
        runtime.set_rate("normal")

    ratio = samples_slow / samples_normal
    assert 1.05 < ratio < 1.6, f"slowest should speak slower, ratio was {ratio:.3f}"


def test_pieces_arrive_while_generation_is_still_running(runtime: PhononRuntime) -> None:
    """The reason the piece callback exists: the browser must hear something before the
    sentence is finished being computed.

    Measured on this box 2026-11-13 with strata resident: first chunk 0.02 s in for a
    160-character sentence that takes 0.89 s overall, and 0.018 s for a 320-character one that
    takes 2.20 s. The bound below is loose (0.5 s and half the wall time) so it catches a
    regression to whole-utterance delivery without being flaky on a busy machine.
    """
    voice = runtime.prepare_voice(SimpleNamespace(), "bootstrap")
    started = time.perf_counter()
    first_piece_at: list[float] = []
    pieces: list[tuple[int, int]] = []

    def sink(pcm: bytes, count: int, rate: int) -> None:
        first_piece_at.append(time.perf_counter() - started)
        pieces.append((count, len(pcm)))

    _, samples = runtime.generate(LONG_TEXT, voice, SimpleNamespace(), sink)
    wall = time.perf_counter() - started

    assert len(pieces) >= 4, f"expected several pieces, got {len(pieces)}"
    assert sum(count for count, _ in pieces) == samples
    assert all(byte_len == count * 2 for count, byte_len in pieces), "s16le per piece"

    ttfa = first_piece_at[0]
    print(f"[phonon] streaming: first piece {ttfa:.3f}s, wall {wall:.2f}s, {len(pieces)} pieces")
    assert ttfa < 0.5, f"first piece took {ttfa:.2f}s; delivery has regressed to whole-utterance"
    assert ttfa < wall * 0.5, f"first piece at {ttfa:.2f}s of {wall:.2f}s total is not streaming"


def test_unknown_voice_fails_with_an_actionable_message(caplog: pytest.LogCaptureFixture) -> None:
    """A typo'd catalog name must not surface as an opaque model error.

    This one loads a model, so it belongs with the real-engine file rather than with the
    fake-ptts tests.
    """
    _require_ptts()
    cfg = SimpleNamespace(
        TTS_PHONON_MODEL=DEFAULT_MODEL,
        TTS_PHONON_VOICE="Russel",  # the misspelling that starts a support thread
        TTS_PHONON_RATE="fast",
        TTS_PHONON_THREADS=THREADS,
        TTS_PHONON_RATE_LADDER={"fast": -0.20},
    )
    rt = PhononRuntime(config_=cfg)
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError) as exc:
            rt.load()
    assert "PI_CHAT_TTS_PHONON_VOICE" in str(exc.value)
    rt.close()


def test_a_superseded_stream_propagates_and_leaves_the_model_usable(
    runtime: PhononRuntime,
) -> None:
    """Barge-in depends on this, and until 2026-11-17 it was an assumption about a compiled extension.

    `voice_session.py` stops a synthesis by raising `StreamSuperseded` from the piece callback. That callback
    runs inside `with self._model.stream(...)` in `tts_phonon.generate`, and `AudioStream` is ptts's compiled
    class with no Python source to read, so whether its `__exit__` propagates or swallows was genuinely
    unknown. Sweep-2 listed it as unverified and it stayed that way because nothing on the box had ptts with a
    model loaded.

    If it swallowed, the except clause in `voice_session` would be decoration: generation would run to
    completion for audio nobody wants, and a stopped reply would keep arriving afterwards. Measured on this box
    on 2026-11-17 at 2 threads with strata live: the exception escaped after one chunk, leaving the `with` took
    0.05 s against 0.58 s to read that sentence to the end, and the next stream produced audio normally.

    Runs through `PhononRuntime.generate` rather than raw ptts, because the claim that matters is about our
    call site, not theirs.
    """

    class Superseded(RuntimeError):
        """Stands in for `voice_session.StreamSuperseded`, raised from the same place it is raised."""

    voice = runtime.prepare_voice(SimpleNamespace(), "bootstrap")
    seen = 0

    def on_piece(_pcm: bytes, _samples: int, _rate: int) -> None:
        nonlocal seen
        seen += 1
        raise Superseded("the browser stopped wanting this stream")

    with pytest.raises(Superseded):
        runtime.generate(TEXT, voice, SimpleNamespace(), on_piece=on_piece)

    assert seen == 1, "the callback should fire once per piece and the raise should stop the loop at once"

    # An abandoned stream must not cost the next one. ptts stops the generator's workers on close, and
    # "closed" here happens through an exception rather than an orderly exit, which is the case worth pinning.
    samples, _wall = _synth(runtime, "Still working?")
    assert samples > 0, "an aborted stream left the model unable to speak again"
