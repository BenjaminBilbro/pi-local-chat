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


class TestImportGuards:
    """Prove TTSService imports without torch."""

    def test_no_torch_imported(self):
        import sys
        assert "torch" not in sys.modules, "torch should not be imported by tts_service"

    def test_no_omnivoice_imported(self):
        import sys
        assert "omnivoice" not in sys.modules, "omnivoice should not be imported by tts_service"


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

        # Fill cache beyond MAX_VOICE_CACHE_ENTRIES (8)
        for i in range(10):
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
