"""Tests for TTSService with fake OmniVoice runtime — no torch, no GPU.

These tests verify:
- Lazy imports (no torch at module load)
- Idempotent concurrent load
- Load failure handling
- OOM normalization
- One executor thread (max_active_calls == 1)
- Voice handle cache hit and LRU eviction
- PCM format validation
- close() prevents new work
"""

import asyncio
import struct
import threading
import time

import pytest

from pi_chat.tts_service import TTSService, VoiceHandle, VoiceSettings, SynthesizedAudio
from tests.fakes.voice import FakeOmniVoiceRuntime


# ---------------------------------------------------------------------------
# Import guards
# ---------------------------------------------------------------------------


class Test0ImportGuards:  # Runs first (Test0) before torch is imported
    """Prove TTSService imports without torch (fresh interpreter, so test order can't lie)."""

    def test_no_torch_imported(self):
        from tests.import_guard import has_module, imported_modules_for

        modules = imported_modules_for("pi_chat.tts_service")
        assert not has_module(modules, "torch"), "tts_service pulled torch into the interpreter"

    def test_no_omnivoice_imported(self):
        from tests.import_guard import has_module, imported_modules_for

        modules = imported_modules_for("pi_chat.tts_service")
        assert not has_module(modules, "omnivoice"), "tts_service pulled omnivoice into the interpreter"


# ---------------------------------------------------------------------------
# Fake runtime factory helper
# ---------------------------------------------------------------------------


def make_service(**fake_kwargs):
    """Create a TTSService with a FakeOmniVoiceRuntime."""
    runtime = FakeOmniVoiceRuntime(**fake_kwargs)

    class ConfigStub:
        TTS_MODEL = "fake-model"
        TTS_DEVICE = "cpu"
        TTS_DTYPE = "float32"
        TTS_NUM_STEPS = 4
        TTS_FLASHINFER = False
        TTS_CUDA_GRAPH = False
        TTS_CPU_THREADS = 2

    return TTSService(
        runtime_factory=lambda: runtime,
        config_=ConfigStub(),
    ), runtime


# ---------------------------------------------------------------------------
# Load behavior
# ---------------------------------------------------------------------------


class TestLoadBehavior:
    async def test_load_succeeds(self):
        service, runtime = make_service()
        await service.load()
        assert service._loaded
        assert runtime._loaded

    async def test_load_idempotent(self):
        service, runtime = make_service(load_delay=0.05)
        await service.load()
        await service.load()
        # Should only call load once
        assert service._loaded

    async def test_concurrent_load_deduplicates(self):
        """Concurrent load calls should share one load operation."""
        service, runtime = make_service(load_delay=0.1)
        results = await asyncio.gather(
            service.load(),
            service.load(),
            service.load(),
        )
        assert all(r is None for r in results)
        assert service._loaded

    async def test_load_failure_raises(self):
        service, runtime = make_service(fail_on_load=True)
        with pytest.raises(RuntimeError, match="load failure"):
            await service.load()
        assert not service._loaded

    async def test_cancelled_load_waiter_does_not_cancel_shared_load(self):
        """Cancelling one waiter should not cancel the shared load."""
        service, runtime = make_service(load_delay=0.2)

        async def load_then_cancel():
            return await service.load()

        task1 = asyncio.create_task(load_then_cancel())
        task2 = asyncio.create_task(load_then_cancel())

        # Let them start
        await asyncio.sleep(0.05)

        # Cancel one waiter
        task2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task2

        # First should complete
        await task1
        assert service._loaded


# ---------------------------------------------------------------------------
# Voice preparation
# ---------------------------------------------------------------------------


class TestVoicePreparation:
    async def test_prepare_creates_handle(self):
        service, runtime = make_service()
        await service.load()
        settings = VoiceSettings()
        handle = await service.prepare_voice(settings)
        assert isinstance(handle, VoiceHandle)
        assert handle.settings == settings

    async def test_prepare_caches_by_settings(self):
        service, runtime = make_service()
        await service.load()
        settings = VoiceSettings()
        h1 = await service.prepare_voice(settings)
        h2 = await service.prepare_voice(settings)
        assert h1 is h2

    async def test_prepare_different_settings_different_handles(self):
        service, runtime = make_service()
        await service.load()
        s1 = VoiceSettings(gender="female")
        s2 = VoiceSettings(gender="male")
        h1 = await service.prepare_voice(s1)
        h2 = await service.prepare_voice(s2)
        assert h1.key != h2.key

    async def test_prepare_lru_eviction(self):
        service, runtime = make_service()
        await service.load()

        # Fill cache beyond MAX_VOICE_CACHE_ENTRIES (16)
        for i in range(20):
            settings = VoiceSettings(speed=0.8 + i * 0.01)
            await service.prepare_voice(settings)

        assert len(service._voice_cache) == TTSService.MAX_VOICE_CACHE_ENTRIES

    async def test_prepare_failure_raises(self):
        service, runtime = make_service(fail_on_prepare=True)
        await service.load()
        with pytest.raises(RuntimeError, match="prepare failure"):
            await service.prepare_voice(VoiceSettings())

    async def test_concurrent_prepare_deduplicates(self):
        """Concurrent prepare for same key should share one operation."""
        service, runtime = make_service(prepare_delay=0.1)
        await service.load()
        settings = VoiceSettings()

        handles = await asyncio.gather(
            service.prepare_voice(settings),
            service.prepare_voice(settings),
            service.prepare_voice(settings),
        )
        # All should get the same handle
        assert all(h is handles[0] for h in handles)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


class TestSynthesis:
    async def test_synthesize_produces_audio(self):
        service, runtime = make_service()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        audio = await service.synthesize("Hello world", handle)

        assert isinstance(audio, SynthesizedAudio)
        assert audio.sample_rate > 0
        assert len(audio.pcm_s16le) > 0
        assert audio.sample_count > 0
        assert audio.generation_seconds >= 0

    async def test_synthesize_pcm_format(self):
        """PCM should be valid s16le."""
        service, runtime = make_service()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        audio = await service.synthesize("Test", handle)

        # Check byte length matches sample count
        assert len(audio.pcm_s16le) == audio.sample_count * 2

        # Check a few samples are in valid range
        for i in range(min(10, audio.sample_count)):
            sample = struct.unpack("<h", audio.pcm_s16le[i * 2 : i * 2 + 2])[0]
            assert -32768 <= sample <= 32767

    async def test_synthesize_empty_text_raises(self):
        service, runtime = make_service()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        with pytest.raises(ValueError, match="empty text"):
            await service.synthesize("", handle)

    async def test_synthesize_not_loaded_raises(self):
        service, runtime = make_service()
        handle = VoiceHandle(key="fake", settings=VoiceSettings())
        with pytest.raises(RuntimeError, match="not loaded"):
            await service.synthesize("test", handle)

    async def test_synthesize_timing_metrics(self):
        service, runtime = make_service(generate_delay=0.05)
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())

        audio = await service.synthesize("Hello", handle)
        assert audio.generation_seconds >= 0.04  # Allow some slack


# ---------------------------------------------------------------------------
# Concurrency and serialization
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_one_executor_thread(self):
        """Prove all syntheses run in one thread."""
        service, runtime = make_service(generate_delay=0.05)
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())

        # Run many concurrent syntheses
        tasks = [
            service.synthesize(f"Text {i}", handle)
            for i in range(10)
        ]
        await asyncio.gather(*tasks)

        # Runtime should never have had more than 1 active call
        assert runtime.max_active_calls == 1

    async def test_cancellation_does_not_allow_overlap(self):
        """Cancelling a synthesis should not allow a second to overlap."""
        block_event = threading.Event()
        service, runtime = make_service(block_generate_event=block_event)
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())

        # Start synthesis that will block
        task1 = asyncio.create_task(service.synthesize("First", handle))
        await asyncio.sleep(0.1)  # Let it enter the executor

        # Cancel it
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1

        # Wait a bit
        await asyncio.sleep(0.05)

        # Second synthesis should wait for first to finish
        task2 = asyncio.create_task(service.synthesize("Second", handle))

        # Release the block
        block_event.set()

        await task2
        assert runtime.max_active_calls == 1


# ---------------------------------------------------------------------------
# Status and close
# ---------------------------------------------------------------------------


class TestStatusAndClose:
    async def test_status_before_load(self):
        service, runtime = make_service()
        status = service.status()
        assert status["loaded"] is False

    async def test_status_after_load(self):
        service, runtime = make_service()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        await service.synthesize("Test", handle)

        status = service.status()
        assert status["loaded"] is True
        assert status["voice_handles"] >= 1
        assert status["total_synthesizes"] >= 1

    async def test_close_prevents_new_work(self):
        service, runtime = make_service()
        await service.load()
        await service.close()

        with pytest.raises(Exception):  # Executor shutdown
            await service.synthesize("Test", VoiceHandle(key="x", settings=VoiceSettings()))

    async def test_close_releases_resources(self):
        service, runtime = make_service()
        await service.load()
        await service.prepare_voice(VoiceSettings())
        await service.close()

        assert not service._loaded
        assert len(service._voice_cache) == 0


# ---------------------------------------------------------------------------
# OOM handling
# ---------------------------------------------------------------------------


class TestOOMHandling:
    async def test_oom_on_load(self):
        service, runtime = make_service(fail_on_load=True)
        with pytest.raises(RuntimeError):
            await service.load()
        assert not service._loaded

    async def test_oom_on_generate(self):
        service, runtime = make_service(
            fail_on_generate_index=0,
            fail_with_oom=True,
        )
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())

        with pytest.raises((MemoryError, RuntimeError)):
            await service.synthesize("Test", handle)


# ---------------------------------------------------------------------------
# VOICE-03: the waveform handoff
# ---------------------------------------------------------------------------


class ThreadAffineRuntime(FakeOmniVoiceRuntime):
    """A runtime whose waveform may only be read from the thread that wrote it.

    `generate` hands the waveform to `TTSService` by parking it on this attribute rather than returning
    it, so a read from any other thread is the race: two profiles talking at once, and each gets handed
    the other's audio. Here that read raises instead of succeeding quietly.
    """

    def __init__(self, **kwargs):
        # Before super().__init__: the parent assigns `_last_waveform = None` and that goes through the
        # setter below.
        self._writer = None
        self._waveform_value = None
        super().__init__(**kwargs)

    @property
    def _last_waveform(self):
        value = self._waveform_value
        if value is None:
            return None
        if threading.current_thread() is not self._writer:
            raise RuntimeError(
                "the waveform was read off the thread that produced it; that is how one profile's "
                "audio gets delivered to the other"
            )
        return value

    @_last_waveform.setter
    def _last_waveform(self, value):
        self._writer = threading.current_thread()
        self._waveform_value = value


def make_thread_affine_service(**fake_kwargs):
    runtime = ThreadAffineRuntime(**fake_kwargs)

    class ConfigStub:
        TTS_MODEL = "fake-model"
        TTS_DEVICE = "cpu"
        TTS_DTYPE = "float32"
        TTS_NUM_STEPS = 4
        TTS_FLASHINFER = False
        TTS_CUDA_GRAPH = False
        TTS_CPU_THREADS = 2

    return TTSService(runtime_factory=lambda: runtime, config_=ConfigStub()), runtime


class TestWaveformHandoff:
    async def test_the_waveform_is_read_on_the_thread_that_produced_it(self):
        service, runtime = make_thread_affine_service()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())

        audio = await service.synthesize("hello, read me on my own thread", handle)

        assert audio.sample_count > 0

    async def test_two_conversation_syntheses_do_not_cross_their_waveforms(self):
        """The window this closes: the model worker finishes one task and can start the next before the
        event loop resumes, so the second synthesis writes before the first has read."""
        service, runtime = make_thread_affine_service(generate_delay=0.01)
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())

        first, second = await asyncio.gather(
            service.synthesize("first profile is speaking", handle),
            service.synthesize("second profile is speaking", handle),
        )

        assert first.sample_count > 0 and second.sample_count > 0



# ---------------------------------------------------------------------------
# VOICE-10: the cache cannot drop a voice that is speaking, and a waiter gets a handle
# ---------------------------------------------------------------------------


class PinRecordingRuntime(FakeOmniVoiceRuntime):
    """Records what the service's voice pins looked like while the model was actually running.

    The obvious version, holding a real synthesis in flight with `block_generate_event`, cannot work:
    model work and voice preparation share the one executor, so a blocked generate blocks the prepare
    the test then waits for. That serialization is the executor's contract, not a bug, so the pin is
    observed from inside the call instead of raced from outside.
    """

    def __init__(self, service, **kwargs):
        super().__init__(**kwargs)
        self._service = service
        self.pins_during_generate: list[dict[str, int]] = []

    def generate(self, text, prepared_voice, settings):
        self.pins_during_generate.append(dict(self._service._voice_pins))
        return super().generate(text, prepared_voice, settings)


def make_pin_recording_service():
    holder: dict = {}

    class ConfigStub:
        TTS_MODEL = "fake-model"
        TTS_DEVICE = "cpu"
        TTS_DTYPE = "float32"
        TTS_NUM_STEPS = 4
        TTS_FLASHINFER = False
        TTS_CUDA_GRAPH = False
        TTS_CPU_THREADS = 2

    def factory():
        runtime = PinRecordingRuntime(holder["service"])
        holder["runtime"] = runtime
        return runtime

    holder["service"] = TTSService(runtime_factory=factory, config_=ConfigStub())
    return holder["service"], holder


class TestVoiceCacheLifecycle:
    async def test_a_synthesis_pins_the_voice_it_is_using(self):
        service, holder = make_pin_recording_service()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings(phonon_voice="catalog-a"))

        audio = await service.synthesize("the model is busy with this sentence", handle)

        assert audio.sample_count > 0
        assert holder["runtime"].pins_during_generate == [{handle.key: 1}]
        assert service._voice_pins == {}, "the synthesis left a pin behind"

    async def test_a_waiter_never_hands_back_none(self):
        """The preparing caller finished and its handle is already gone: the voice was deleted, or the
        cache was cleared, while both callers were parked. This used to `return
        self._voice_cache.get(key)`, which is None, and the first thing the caller did with it was
        `voice.key` inside `_synthesize`."""
        service, runtime = make_service()
        await service.load()
        settings = VoiceSettings(phonon_voice="catalog-a")
        key = service._voice_key(settings)

        # The state the waiter branch sees: an in-flight entry that has completed, and no handle left
        # in the cache to hand over.
        done = asyncio.get_event_loop().create_future()
        done.set_result(None)
        service._prepare_futures[key] = done

        handle = await service.prepare_voice(settings)

        assert isinstance(handle, VoiceHandle)
        assert handle.key == key
        assert service._prepared_objects.get(key) is not None
        assert service.status()["voice_handles"] == 1

    async def test_eviction_skips_a_voice_that_is_spoken_in(self):
        """LRU here means "least recently *prepared*", not "safe to forget": one handle serves every
        sentence of a conversation. Evicting it also drops its prepared object, so the next sentence
        died with `no prepared object for voice` after the model had already done the work."""
        service, runtime = make_service()
        service.MAX_VOICE_CACHE_ENTRIES = 1
        await service.load()

        speaking = await service.prepare_voice(VoiceSettings(phonon_voice="catalog-a"))
        service._pin_voice(speaking.key)  # stands in for a synthesis in flight on this voice
        try:
            await service.prepare_voice(VoiceSettings(phonon_voice="catalog-b"))
            assert speaking.key in service._voice_cache, "a voice was evicted while it was in use"
            assert service._prepared_objects.get(speaking.key) is not None
        finally:
            service._unpin_voice(speaking.key)

        await service.prepare_voice(VoiceSettings(phonon_voice="catalog-c"))
        assert speaking.key not in service._voice_cache, "an unpinned voice must be evictable again"

    async def test_a_failed_synthesis_still_releases_its_pin(self):
        """A pin left behind on the error path means that voice can never be evicted again, which is a
        slow leak of prepared voices rather than something anyone would notice."""
        service, runtime = make_service(fail_on_generate_index=0)
        service.MAX_VOICE_CACHE_ENTRIES = 1
        await service.load()

        first = await service.prepare_voice(VoiceSettings(phonon_voice="catalog-a"))
        with pytest.raises(RuntimeError):
            await service.synthesize("this one fails", first)

        assert service._voice_pins == {}

        await service.prepare_voice(VoiceSettings(phonon_voice="catalog-b"))
        assert first.key not in service._voice_cache

    async def test_the_cache_stays_bounded_when_nothing_is_in_use(self):
        """The guard must not quietly turn the cache into an unbounded map."""
        service, runtime = make_service()
        service.MAX_VOICE_CACHE_ENTRIES = 2
        await service.load()

        for name in ("catalog-a", "catalog-b", "catalog-c"):
            await service.prepare_voice(VoiceSettings(phonon_voice=name))

        assert len(service._voice_cache) == 2
        assert len(service._prepared_objects) == 2
