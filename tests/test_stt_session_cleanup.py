"""Tests for STTSession lifecycle/cleanup behavior.

Covers: off-loop disable with shutdown-before-join ordering (no event-loop
stall), loop-closed safety of _safe_send, re-entrant enable (no recorder
leaks), and graceful degradation when pywebrtc_audio is unavailable.
"""

import asyncio
import sys
import threading
import time
import types
from unittest import mock

import numpy as np
import pytest

from pi_chat.stt_session import STTSession, AudioPacket


def make_packet(samples: np.ndarray) -> AudioPacket:
    metadata = {"sampleRate": 16000, "channels": 1, "format": "pcm_s16le", "frames": len(samples)}
    return AudioPacket(metadata=metadata, audio=samples.tobytes())


def make_session(send_json=None) -> STTSession:
    stt_service = mock.MagicMock()
    stt_service.load = mock.AsyncMock()
    return STTSession(stt_service, send_json or mock.AsyncMock())


# ---------------------------------------------------------------------------
# Disable: shutdown before join, off the event loop
# ---------------------------------------------------------------------------


class BlockedRecorder:
    """Recorder whose worker thread only unblocks on shutdown().

    Mirrors RealtimeSTT: text() (our worker loop) blocks until shutdown()
    sets is_shut_down + wakeup events. Joining the worker BEFORE shutting
    down the recorder therefore always eats the full join timeout.
    """

    def __init__(self):
        self.shutdown_called = False
        self._shutdown_event = threading.Event()

    def shutdown(self):
        self.shutdown_called = True
        self._shutdown_event.set()

    def wait_shutdown(self, timeout):
        return self._shutdown_event.wait(timeout)


@pytest.mark.asyncio
async def test_disable_is_fast_when_worker_blocks_until_shutdown():
    """Regression: old disable() joined the worker (blocked on text()) for
    the full 3s timeout BEFORE calling shutdown() — stalling the event loop
    on every stt_disable and WS disconnect."""
    session = make_session()
    session.enabled = True

    recorder = BlockedRecorder()
    worker_alive = threading.Event()

    def fake_worker():
        worker_alive.set()
        recorder.wait_shutdown(10)  # stand in for recorder.text() blocking

    thread = threading.Thread(target=fake_worker, daemon=True)
    thread.start()
    worker_alive.wait(2)
    session._recorder = recorder
    session._text_thread = thread

    loop_start = time.monotonic()
    # If the loop were blocked by a synchronous join, this await would not
    # even start until the join timed out (>=3s old behavior).
    await asyncio.sleep(0)
    await session.disable()
    elapsed = time.monotonic() - loop_start

    assert recorder.shutdown_called
    assert not thread.is_alive()
    assert elapsed < 2.0, f"disable() took {elapsed:.1f}s — event loop likely blocked"


@pytest.mark.asyncio
async def test_disable_sends_state_and_clears_flags():
    sent = []

    async def send_json(event):
        sent.append(event)

    session = make_session(send_json)
    session.enabled = True
    session.active = True
    await session.disable()
    assert session.enabled is False
    assert session.active is False
    assert {"type": "stt_state", "state": "disabled"} in sent


# ---------------------------------------------------------------------------
# _safe_send: recorder callbacks after the event loop is closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_send_survives_closed_loop():
    """RealtimeSTT callbacks can fire from its threads after the WebSocket
    disconnects (loop closed). _safe_send must swallow that, not raise into
    the recorder's thread."""
    session = make_session()
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    session._loop = dead_loop

    session._safe_send({"type": "stt_final", "text": "hello"})  # must not raise
    session._safe_send({"type": "stt_error", "message": "x"})


@pytest.mark.asyncio
async def test_safe_send_ignores_after_close():
    session = make_session()
    session.closed = True
    session._safe_send({"type": "stt_final", "text": "x"})  # no-op, no raise


# ---------------------------------------------------------------------------
# Re-entrant / concurrent enable
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_realtimestt(monkeypatch):
    """Stub RealtimeSTT so enable() never loads a real whisper model."""
    created = []

    class FakeRecorder:
        def __init__(self, **kwargs):
            created.append(self)
            self.shutdown_called = False
            self._stop = threading.Event()

        def text(self):
            # Stand in for RealtimeSTT's blocking text(): unblocks on shutdown.
            self._stop.wait(5)
            return ""

        def shutdown(self):
            self.shutdown_called = True
            self._stop.set()

    module = types.ModuleType("RealtimeSTT")
    module.AudioToTextRecorder = FakeRecorder
    monkeypatch.setitem(sys.modules, "RealtimeSTT", module)
    return created


@pytest.mark.asyncio
async def test_enable_twice_does_not_leak_recorders(fake_realtimestt):
    session = make_session()
    await session.enable()
    first = session._recorder
    await session.enable()

    assert len(fake_realtimestt) == 2
    assert session._recorder is not first
    assert first.shutdown_called, "old recorder must be shut down on re-enable"
    assert not session._recorder.shutdown_called
    assert session.enabled is True


@pytest.mark.asyncio
async def test_concurrent_enable_single_active_recorder(fake_realtimestt):
    session = make_session()
    await asyncio.gather(session.enable(), session.enable())
    assert session.enabled is True
    active = session._recorder
    assert active is not None and not active.shutdown_called
    # Any earlier recorder from a duplicate enable must be torn down.
    for rec in fake_realtimestt:
        if rec is not active:
            assert rec.shutdown_called


@pytest.mark.asyncio
async def test_enable_torn_down_if_disable_races_it(fake_realtimestt):
    """disable() landing while the (slow) recorder is being created must
    win: no ready state, no leaked recorder."""
    sent = []

    async def send_json(event):
        sent.append(event)

    session = make_session(send_json)

    # Hold enable() inside stt_service.load() so disable() can land first.
    load_entered = asyncio.Event()
    load_release = asyncio.Event()

    async def slow_load():
        load_entered.set()
        await load_release.wait()

    session.stt_service.load = slow_load

    enable_task = asyncio.create_task(session.enable())
    await load_entered.wait()  # enable() is now parked inside load()
    await session.disable()
    load_release.set()
    await enable_task

    assert session.enabled is False
    assert session._recorder is None
    assert session._text_thread is None
    assert all(e.get("state") != "ready" for e in sent if e.get("type") == "stt_state")


# ---------------------------------------------------------------------------
# AEC degradation when pywebrtc_audio is missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aec_unavailable_passes_mic_through(monkeypatch):
    """If pywebrtc_audio can't be imported, STT must keep working (raw mic
    passthrough) instead of dropping every packet with an error."""
    session = make_session()
    session.enabled = True
    recorder = mock.MagicMock()
    session._recorder = recorder
    voice = mock.MagicMock()
    voice.is_speaking = True
    voice.drain_reference_bytes = mock.MagicMock(return_value=b"\x00" * 1600)
    session.set_voice_session(voice)

    # Force ImportError from pywebrtc_audio
    monkeypatch.setitem(sys.modules, "pywebrtc_audio", None)

    samples = np.random.randint(-1000, 1000, size=800, dtype=np.int16)
    session.ingest_audio_packet(make_packet(samples))

    assert session._aec_unavailable is True
    recorder.feed_audio.assert_called_once()
    fed = recorder.feed_audio.call_args[0][0]
    np.testing.assert_array_equal(fed, samples)


# ---------------------------------------------------------------------------
# What the recorder gets built with (S7: measured by tools/stt_probe.py)
# ---------------------------------------------------------------------------


def test_shipped_stt_defaults_are_the_measured_ones():
    """A measured default is worth exactly as much as the test that stops it drifting back.

    `tools/stt_probe.py` produced these on 2026-11-18: with the library defaults (float32 on CPU because
    `default` widens a float16 checkpoint, beam 5) the wait from last spoken word to text was 3080 ms; at
    int8 plus greedy beam it is 2510 ms with the same words. `small.en` stays the final model because tiny.en
    does not mishear the noisy fixture, it hears a different sentence: "weather in Concord tomorrow morning"
    came back as "weather in contrast to all of them", and a lost place name is not recoverable from context.
    """
    from pi_chat import config

    assert config.STT_COMPUTE_TYPE == "int8"
    assert config.STT_BEAM_SIZE == 1
    assert config.STT_MODEL == "small.en"
    # The one knob the measurements say to leave alone. Below 0.6 the multi-sentence fixture loses 40-80% of
    # its words to a closed recording, which is not a latency improvement at any price.
    assert config.STT_SILENCE_DURATION == 0.6


@pytest.mark.asyncio
async def test_recorder_is_built_with_the_latency_knobs(monkeypatch):
    """A config value nobody passes to the constructor is a knob that changes nothing.

    This is the shape `tests/test_config_knob_liveness.py` cannot see: the constant is read by application
    code, so it passes the grep, and the recorder still uses its own default because the keyword never left.
    """
    from pi_chat import config

    seen: dict = {}

    class KwargsRecorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def text(self):
            threading.Event().wait(0.05)
            return ""

        def shutdown(self):
            pass

    module = types.ModuleType("RealtimeSTT")
    module.AudioToTextRecorder = KwargsRecorder
    monkeypatch.setitem(sys.modules, "RealtimeSTT", module)

    session = make_session()
    await session.enable()
    try:
        assert seen["compute_type"] == config.STT_COMPUTE_TYPE
        assert seen["beam_size"] == config.STT_BEAM_SIZE
        assert seen["post_speech_silence_duration"] == config.STT_SILENCE_DURATION
        assert seen["realtime_model_type"] == config.STT_REALTIME_MODEL
    finally:
        await session.disable()
