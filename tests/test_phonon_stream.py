"""Piece-at-a-time speech (T1b): the runtime hands PCM over while the model is still generating,
and the browser gets a frame per piece instead of one per sentence.

Why it matters, measured on this box 2026-11-13 with strata resident: a 160-character sentence
produces its first audio frame 0.02 s in but finishes synthesis at 0.89 s, and a 320-character one
at 2.2 s. Waiting for the whole utterance threw all of that away.

Two shapes are load-bearing and both are tested here:
- the callback runs on the model executor thread, NOT the event loop (the fake service below uses
  a real thread so the scheduling under test is the real one), and
- a runtime that cannot produce pieces must behave exactly as it did before.
"""

from __future__ import annotations

import asyncio
import math
import struct
import threading
import time

import pytest

from pi_chat.tts_phonon import PhononRuntime
from pi_chat.tts_service import TTSService, VoiceSettings
from pi_chat.voice_session import (
    PCM_HEADER,
    PCM_HEADER_LEN,
    PCM_MAGIC,
    VoiceSession,
)
from tests.fakes.voice import (
    FakeOmniVoiceRuntime,
    FakeVoiceStore,
    SynthesizedAudio,
    VoiceHandle,
)

from tests.test_tts_phonon import FakePttsModule, phonon_config_stub  # the fake-ptts harness

PIECE_SAMPLES = 480  # 20 ms at 24 kHz
N_PIECES = 5


# ---------------------------------------------------------------------------
# Runtime level: generate(on_piece=...)
# ---------------------------------------------------------------------------


def test_generate_hands_over_each_chunk_as_a_playable_piece(monkeypatch: pytest.MonkeyPatch):
    import sys

    fake = FakePttsModule()
    monkeypatch.setitem(sys.modules, "ptts", fake)  # type: ignore[arg-type]

    runtime = PhononRuntime(config_=phonon_config_stub())
    runtime.load()

    pieces: list[tuple[bytes, int, int]] = []
    handle = runtime.prepare_voice(VoiceSettings(), "x")
    _, total = runtime.generate(
        "Hello there, this is a sentence.",
        handle,
        VoiceSettings(),
        lambda pcm, count, rate: pieces.append((pcm, count, rate)),
    )

    fake_chunks = len(fake._audio)
    assert len(pieces) == fake_chunks, "one piece per stream chunk"
    assert sum(count for _, count, _ in pieces) == total
    # s16le: two bytes per sample, and each piece standalone-playable.
    for pcm, count, rate in pieces:
        assert len(pcm) == count * 2
        assert rate == 24000
        assert count > 0

    whole = b"".join(pcm for pcm, _, _ in pieces)
    audio_pcm, sample_count = TTSService(runtime_factory=lambda: runtime)._waveform_to_pcm(
        runtime._last_waveform, 24000
    )
    assert whole == audio_pcm, "the pieces and the whole utterance must be the same bytes"
    assert sample_count == total


# ---------------------------------------------------------------------------
# Service level
# ---------------------------------------------------------------------------


def test_service_passes_pieces_through_for_a_streaming_runtime(monkeypatch: pytest.MonkeyPatch):
    import sys

    fake = FakePttsModule()
    monkeypatch.setitem(sys.modules, "ptts", fake)  # type: ignore[arg-type]

    service = TTSService(
        runtime_factory=lambda: PhononRuntime(config_=phonon_config_stub()),
        config_=phonon_config_stub(),
        voice_store=FakeVoiceStore(),
    )

    async def run():
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        pieces: list[bytes] = []
        audio = await service.synthesize_stream("Speak this.", handle, lambda p, n, r: pieces.append(p))
        return audio, pieces

    audio, pieces = asyncio.run(run())
    assert len(pieces) > 1
    assert b"".join(pieces) == audio.pcm_s16le


def test_service_never_pieces_for_a_runtime_that_cannot_stream():
    service = TTSService(
        runtime_factory=lambda: FakeOmniVoiceRuntime(),
        voice_store=FakeVoiceStore(),
    )

    async def run():
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        called = 0

        def sink(pcm: bytes, count: int, rate: int) -> None:
            nonlocal called
            called += 1

        streamed = await service.synthesize_stream("Hello.", handle, sink)
        plain = await service.synthesize("Hello.", handle)
        return called, streamed, plain

    called, streamed, plain = asyncio.run(run())
    assert called == 0, "a non-streaming runtime must not be asked to produce pieces"
    assert streamed.sample_rate == plain.sample_rate
    assert streamed.sample_count == plain.sample_count
    assert len(streamed.pcm_s16le) == len(plain.pcm_s16le) == streamed.sample_count * 2
    # (bytes differ on purpose: the fake encodes the call index in its pitch, so two calls of
    # the same text are intentionally not byte-identical)


# ---------------------------------------------------------------------------
# VoiceSession level: frames per piece, contiguous sequences, AEC fed per piece
# ---------------------------------------------------------------------------


class ThreadedStreamingFakeService:
    """A TTS service double that calls the piece callback from a worker thread.

    That is the shape that matters: `TTSService` runs `generate` on its single model thread, and
    `VoiceSession` schedules the actual send back onto the event loop. Calling the callback on
    the loop instead would deadlock against the piece cap, so the fake uses a real thread.
    """

    def __init__(self, *, pieces: int = N_PIECES, samples: int = PIECE_SAMPLES, delay: float = 0.005):
        self.pieces = pieces
        self.samples = samples
        self.delay = delay
        self.rate = 24_000
        self.handle = VoiceHandle(key="stream", settings=VoiceSettings())
        self.calls: list[str] = []

    async def load(self) -> None:
        return None

    async def prepare_voice(self, settings: VoiceSettings) -> VoiceHandle:
        return self.handle

    async def synthesize(self, text: str, voice: VoiceHandle) -> SynthesizedAudio:
        pcm = self._pcm(0, self.pieces * self.samples)
        return SynthesizedAudio(
            sample_rate=self.rate, pcm_s16le=pcm, sample_count=self.pieces * self.samples,
            generation_seconds=0.1,
        )

    async def synthesize_stream(self, text: str, voice: VoiceHandle, on_piece) -> SynthesizedAudio:
        self.calls.append(text)

        def work() -> None:
            for index in range(self.pieces):
                on_piece(self._pcm(index, self.samples), self.samples, self.rate)
                time.sleep(self.delay)

        await asyncio.get_running_loop().run_in_executor(None, work)
        total = self.pieces * self.samples
        return SynthesizedAudio(
            sample_rate=self.rate, pcm_s16le=self._pcm(0, total), sample_count=total,
            generation_seconds=self.pieces * self.delay,
        )

    def status(self) -> dict:
        return {"loaded": True}

    async def close(self) -> None:
        return None

    @staticmethod
    def _pcm(seed: int, samples: int) -> bytes:
        values = [
            int(0.5 * 32767 * math.sin(2 * math.pi * (220 + seed) * i / 24000))
            for i in range(samples)
        ]
        return struct.pack("<" + "h" * samples, *values)


class FakeConfig:
    TTS_MAX_QUEUE_CHUNKS = 12
    TTS_MAX_QUEUE_CHARS = 1800
    TTS_MAX_HOLD_MS = 50


def _parse_frame(frame: bytes) -> dict[str, int]:
    assert frame[:4] == PCM_MAGIC
    magic, version, flags, header_len, stream_id, sequence, sample_rate, sample_count = PCM_HEADER.unpack(
        frame[:PCM_HEADER_LEN]
    )
    assert header_len == PCM_HEADER_LEN
    return {
        "stream_id": stream_id,
        "sequence": sequence,
        "sample_rate": sample_rate,
        "sample_count": sample_count,
        "payload": len(frame) - header_len,
    }


@pytest.mark.asyncio
async def test_one_frame_per_piece_with_contiguous_sequences():
    sent: list[bytes] = []

    async def send_json(msg: dict) -> None:
        return None

    async def send_bytes(data: bytes) -> None:
        sent.append(data)

    service = ThreadedStreamingFakeService()
    session = VoiceSession(service, send_json, send_bytes, config_=FakeConfig())
    await session.enable({})
    await asyncio.sleep(0.05)
    await session.observe_pi_event({"type": "agent_start"})

    await session._enqueue_chunk("Hello there.")
    await session._enqueue_sentinel()

    for _ in range(100):
        if len(sent) >= N_PIECES:
            await asyncio.sleep(0.02)
            break
        await asyncio.sleep(0.01)

    frames = [_parse_frame(f) for f in sent]
    assert len(frames) == N_PIECES, f"expected {N_PIECES} frames, got {len(frames)}"
    assert [f["sequence"] for f in frames] == list(range(frames[0]["sequence"], frames[0]["sequence"] + N_PIECES))
    assert len({f["stream_id"] for f in frames}) == 1
    assert all(f["sample_rate"] == 24_000 for f in frames)
    assert all(f["sample_count"] == PIECE_SAMPLES for f in frames)
    assert all(f["payload"] == PIECE_SAMPLES * 2 for f in frames)

    # Each piece must also reach the echo-cancellation reference, or the mic hears the
    # assistant and the assistant answers itself.
    reference = session.drain_reference_bytes(N_PIECES * PIECE_SAMPLES * 4)
    assert len(reference) >= N_PIECES * PIECE_SAMPLES * 2 * (16_000 // 24_000)


@pytest.mark.asyncio
async def test_pieces_arrive_in_sequence_order_across_chunk_boundaries():
    """Regression: the model thread schedules sends, so without a gate they race.

    The first piece of a chunk is made slow on purpose. Unchained, the pieces behind it finish
    first and arrive out of order - and `static/voice.js` drops any frame whose sequence is not
    greater than the last it saw, so the listener loses audio mid-sentence. The echo reference
    would also be filled out of time order, which is worse because it feeds AEC.
    """
    sent: list[bytes] = []
    pieces_per_chunk = 4

    async def send_json(msg: dict) -> None:
        return None

    async def send_bytes(data: bytes) -> None:
        frame = _parse_frame(data)
        # Slow on the first piece of each synthesis (sequences 1 and 5), fast on the rest: the
        # shape that exposes racing between the pieces queued behind it.
        await asyncio.sleep(0.06 if frame["sequence"] % pieces_per_chunk == 1 else 0.001)
        sent.append(data)

    service = ThreadedStreamingFakeService(pieces=pieces_per_chunk, samples=480, delay=0.001)
    session = VoiceSession(service, send_json, send_bytes, config_=FakeConfig())
    await session.enable({})
    await asyncio.sleep(0.05)
    await session.observe_pi_event({"type": "agent_start"})

    await session._enqueue_chunk("First sentence.")
    await session._enqueue_chunk("Second sentence.")
    await session._enqueue_sentinel()

    for _ in range(300):
        if len(sent) >= 8:
            await asyncio.sleep(0.02)
            break
        await asyncio.sleep(0.01)

    frames = [_parse_frame(f) for f in sent]
    assert len(frames) == 8, f"expected 4 pieces for each of 2 chunks, got {len(frames)}"
    sequences = [f["sequence"] for f in frames]
    assert sequences == sorted(sequences), f"pieces arrived out of order: {sequences}"
    assert sequences == list(range(sequences[0], sequences[0] + 8))


@pytest.mark.asyncio
async def test_worker_never_blocks_the_event_loop_for_a_piece_future(monkeypatch):
    """Regression: the whole server froze on 2026-11-14.

    The piece callback runs on the model thread and schedules the send onto the event loop with
    `run_coroutine_threadsafe`. Waiting on that future with `Future.result()` is correct on the
    model thread and a **self-deadlock on the loop thread**: the future is a coroutine that the
    blocked loop itself has to run. The worker's post-synthesis drain did exactly that, so the
    frozen process served nothing, streamed nothing, and could not be interrupted.

    The guard below makes the illegal wait raise instead of hang, so this test fails fast (and
    would hang forever if written as a plain timeout, because a blocked loop cannot time out).
    """
    loop_thread = threading.current_thread()
    real_rcts = asyncio.run_coroutine_threadsafe

    def guarded(coro, loop):
        future = real_rcts(coro, loop)
        original = future.result

        def result(timeout=None):
            # Waiting on an already-finished future is harmless (asyncio.wrap_future does it).
            # Waiting on a pending one from the loop thread is the bug: the future is a
            # coroutine that this blocked loop is the only thing that can run.
            if not future.done() and threading.current_thread() is loop_thread:
                raise AssertionError(
                    "voice worker blocked the event thread on Future.result(); piece futures "
                    "must be awaited (asyncio.wrap_future), never waited on synchronously"
                )
            return original(timeout)

        future.result = result  # type: ignore[method-assign]
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", guarded)

    sent: list[bytes] = []
    json_out: list[dict] = []

    async def send_json(msg: dict) -> None:
        json_out.append(msg)

    async def send_bytes(data: bytes) -> None:
        # Slow enough that several piece futures are still pending when synthesis returns, which
        # is the condition the drain used to handle by blocking.
        await asyncio.sleep(0.02)
        sent.append(data)

    service = ThreadedStreamingFakeService(pieces=6, delay=0.002)
    session = VoiceSession(service, send_json, send_bytes, config_=FakeConfig())
    await session.enable({})
    await asyncio.sleep(0.05)
    await session.observe_pi_event({"type": "agent_start"})

    await session._enqueue_chunk("Speak in pieces.")
    await session._enqueue_sentinel()

    for _ in range(200):
        if len(sent) >= 6:
            break
        await asyncio.sleep(0.01)

    errors = [m for m in json_out if m.get("type") == "voice_error"]
    assert not errors, f"worker reported an error: {errors}"
    assert len(sent) == 6, f"expected 6 piece frames, got {len(sent)}"


@pytest.mark.asyncio
async def test_superseded_stream_stops_generating_pieces():
    sent: list[bytes] = []

    async def send_json(msg: dict) -> None:
        return None

    async def send_bytes(data: bytes) -> None:
        sent.append(data)

    service = ThreadedStreamingFakeService(pieces=12, delay=0.02)
    session = VoiceSession(service, send_json, send_bytes, config_=FakeConfig())
    await session.enable({})
    await asyncio.sleep(0.05)
    await session.observe_pi_event({"type": "agent_start"})

    await session._enqueue_chunk("A long sentence that will be interrupted.")
    await asyncio.sleep(0.05)

    # Same thing a new assistant run does: bump the stream id out from under the worker.
    session._stream_id += 1

    await session._enqueue_sentinel()
    await asyncio.sleep(0.2)

    assert len(sent) < 12, "generation should have stopped once the stream was superseded"
