"""Tests for fake voice runtime and service — no torch, no GPU.

These tests verify that the fake implementations are deterministic,
support all required failure modes, and prove concurrency constraints.
"""

import asyncio
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.fakes.voice import (
    FakeOmniVoiceRuntime,
    FakeTTSService,
    SynthesizedAudio,
    TTSRuntime,
    VoiceHandle,
    VoiceSettings,
    fixture_all_fixtures,
    fixture_cancellation,
    fixture_code_block,
    fixture_long_unpunctuated,
    fixture_markdown,
    fixture_multiple_text_blocks,
    fixture_one_sentence,
    fixture_retry,
    fixture_tool_gap,
)


# ---------------------------------------------------------------------------
# Import guards
# ---------------------------------------------------------------------------


class TestImportGuards:
    """Prove fake suite imports without torch or omnivoice."""

    def test_no_torch_imported(self):
        import sys
        assert "torch" not in sys.modules, "torch should not be imported by fake voice module"

    def test_no_omnivoice_imported(self):
        import sys
        assert "omnivoice" not in sys.modules, "omnivoice should not be imported by fake voice module"

    def test_no_cuda_available(self):
        import sys
        assert "torch.cuda" not in sys.modules, "torch.cuda should not be imported"


# ---------------------------------------------------------------------------
# TTSRuntime protocol
# ---------------------------------------------------------------------------


class TestTTSRuntimeProtocol:
    def test_protocol_defined(self):
        assert hasattr(TTSRuntime, "__protocol_attrs__") or hasattr(TTSRuntime, "__annotations__")

    def test_fake_implements_protocol(self):
        runtime = FakeOmniVoiceRuntime()
        assert callable(getattr(runtime, "load", None))
        assert callable(getattr(runtime, "prepare_voice", None))
        assert callable(getattr(runtime, "generate", None))
        assert callable(getattr(runtime, "close", None))


# ---------------------------------------------------------------------------
# FakeOmniVoiceRuntime basic behavior
# ---------------------------------------------------------------------------


class TestFakeOmniVoiceRuntimeBasic:
    def test_load_succeeds(self):
        runtime = FakeOmniVoiceRuntime()
        runtime.load()
        # Should not raise

    def test_prepare_requires_load(self):
        runtime = FakeOmniVoiceRuntime()
        with pytest.raises(RuntimeError, match="not loaded"):
            runtime.prepare_voice(VoiceSettings(), "test")

    def test_prepare_creates_voice(self):
        runtime = FakeOmniVoiceRuntime()
        runtime.load()
        settings = VoiceSettings()
        voice = runtime.prepare_voice(settings, "Hello")
        assert isinstance(voice, dict)
        assert voice["settings"] == settings
        assert voice["bootstrap_text"] == "Hello"

    def test_prepare_caches_by_settings(self):
        runtime = FakeOmniVoiceRuntime()
        runtime.load()
        settings = VoiceSettings()
        v1 = runtime.prepare_voice(settings, "Hello")
        v2 = runtime.prepare_voice(settings, "Different text")
        assert v1 is v2

    def test_generate_produces_samples(self):
        runtime = FakeOmniVoiceRuntime()
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        prepared, sample_count = runtime.generate("test text", voice, VoiceSettings())
        assert sample_count > 0

    def test_generate_deterministic(self):
        runtime = FakeOmniVoiceRuntime()
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        _, count1 = runtime.generate("same text", voice, VoiceSettings())
        _, count2 = runtime.generate("same text", voice, VoiceSettings())
        assert count1 == count2

    def test_distinct_frequencies_per_call(self):
        runtime = FakeOmniVoiceRuntime()
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        runtime.generate("call one", voice, VoiceSettings())
        runtime.generate("call two", voice, VoiceSettings())
        calls = runtime.generate_calls
        assert len(calls) == 2
        assert calls[0]["index"] == 0
        assert calls[1]["index"] == 1

    def test_close_clears_state(self):
        runtime = FakeOmniVoiceRuntime()
        runtime.load()
        runtime.prepare_voice(VoiceSettings(), "Hello")
        runtime.close()
        assert runtime.prepared_voice_keys == []
        assert runtime.generate_calls == []


# ---------------------------------------------------------------------------
# FakeOmniVoiceRuntime concurrency
# ---------------------------------------------------------------------------


class TestFakeOmniVoiceRuntimeConcurrency:
    def test_active_calls_tracking(self):
        runtime = FakeOmniVoiceRuntime(generate_delay=0.05)
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")

        def do_generate():
            runtime.generate("text", voice, VoiceSettings())

        threads = [threading.Thread(target=do_generate) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert runtime.active_calls == 0
        assert runtime.max_active_calls >= 1

    def test_block_generate_proves_serial_execution(self):
        """Use a blocking event to prove only one call runs at a time."""
        block_event = threading.Event()  # Starts cleared = blocked
        runtime = FakeOmniVoiceRuntime(block_generate_event=block_event)
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")

        results = []
        lock = threading.Lock()
        first_thread_blocked = threading.Event()

        def do_generate(idx):
            if idx == 0:
                first_thread_blocked.set()  # Signal we're in the block
            runtime.generate(f"text {idx}", voice, VoiceSettings())
            with lock:
                results.append(idx)

        # Launch 3 threads; first one blocks, others queue behind
        threads = [threading.Thread(target=do_generate, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()

        # Wait for first thread to be blocked in generate
        first_thread_blocked.wait(timeout=2)
        time.sleep(0.1)
        # First thread is blocked but still counts as active
        assert runtime.active_calls == 1

        # Release the block - all threads will complete sequentially
        block_event.set()

        for t in threads:
            t.join(timeout=5)

        assert runtime.active_calls == 0
        assert len(results) == 3

    def test_call_counter_increments(self):
        runtime = FakeOmniVoiceRuntime()
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        runtime.generate("a", voice, VoiceSettings())
        runtime.generate("b", voice, VoiceSettings())
        runtime.generate("c", voice, VoiceSettings())
        assert runtime.call_counter == 3


# ---------------------------------------------------------------------------
# FakeOmniVoiceRuntime failure modes
# ---------------------------------------------------------------------------


class TestFakeOmniVoiceRuntimeFailures:
    def test_fail_on_load(self):
        runtime = FakeOmniVoiceRuntime(fail_on_load=True)
        with pytest.raises(RuntimeError, match="load failure"):
            runtime.load()

    def test_fail_on_prepare(self):
        runtime = FakeOmniVoiceRuntime(fail_on_prepare=True)
        runtime.load()
        with pytest.raises(RuntimeError, match="prepare failure"):
            runtime.prepare_voice(VoiceSettings(), "Hello")

    def test_fail_on_generate_index(self):
        runtime = FakeOmniVoiceRuntime(fail_on_generate_index=1)
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        runtime.generate("first", voice, VoiceSettings())
        with pytest.raises(RuntimeError, match="generate failure"):
            runtime.generate("second", voice, VoiceSettings())

    def test_fail_with_oom(self):
        runtime = FakeOmniVoiceRuntime(
            fail_on_generate_index=0,
            fail_with_oom=True,
        )
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        with pytest.raises(MemoryError, match="OOM"):
            runtime.generate("text", voice, VoiceSettings())

    def test_generate_empty(self):
        runtime = FakeOmniVoiceRuntime(generate_empty=True)
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        _, count = runtime.generate("text", voice, VoiceSettings())
        assert count == 0

    def test_generate_nans(self):
        runtime = FakeOmniVoiceRuntime(generate_nans=True)
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        runtime.generate("text", voice, VoiceSettings())
        # Should not raise, but samples would be NaN

    def test_generate_infs(self):
        runtime = FakeOmniVoiceRuntime(generate_infs=True)
        runtime.load()
        voice = runtime.prepare_voice(VoiceSettings(), "Hello")
        runtime.generate("text", voice, VoiceSettings())
        # Should not raise, but samples would be Inf

    def test_alternate_sample_rate(self):
        runtime = FakeOmniVoiceRuntime(alternate_sample_rate=16000)
        assert runtime.sampling_rate == 16000


# ---------------------------------------------------------------------------
# FakeTTSService
# ---------------------------------------------------------------------------


class TestFakeTTSService:
    @pytest.mark.asyncio
    async def test_load_idempotent(self):
        service = FakeTTSService()
        await service.load()
        await service.load()
        assert service.status()["loaded"] is True

    @pytest.mark.asyncio
    async def test_prepare_voice_returns_handle(self):
        service = FakeTTSService()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        assert isinstance(handle, VoiceHandle)
        assert handle.key is not None
        assert handle.settings == VoiceSettings()

    @pytest.mark.asyncio
    async def test_prepare_voice_caches(self):
        service = FakeTTSService()
        await service.load()
        h1 = await service.prepare_voice(VoiceSettings())
        h2 = await service.prepare_voice(VoiceSettings())
        assert h1.key == h2.key

    @pytest.mark.asyncio
    async def test_synthesize_returns_audio(self):
        service = FakeTTSService()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        audio = await service.synthesize("test text", handle)
        assert isinstance(audio, SynthesizedAudio)
        assert audio.sample_rate == 24000
        assert len(audio.pcm_s16le) == audio.sample_count * 2
        assert audio.generation_seconds > 0

    @pytest.mark.asyncio
    async def test_synthesize_pcm_format(self):
        service = FakeTTSService()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        audio = await service.synthesize("test", handle)
        # Verify little-endian 16-bit
        value = struct.unpack("<h", audio.pcm_s16le[:2])[0]
        assert -32768 <= value <= 32767

    @pytest.mark.asyncio
    async def test_synthesize_distinct_tones(self):
        service = FakeTTSService()
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        a1 = await service.synthesize("first", handle)
        a2 = await service.synthesize("second", handle)
        # Different tones means different first few samples
        assert a1.pcm_s16le[:10] != a2.pcm_s16le[:10]

    @pytest.mark.asyncio
    async def test_synthesize_with_delay(self):
        service = FakeTTSService(synthesize_delay=0.05)
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        start = time.monotonic()
        await service.synthesize("text", handle)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.04

    @pytest.mark.asyncio
    async def test_synthesize_failure(self):
        service = FakeTTSService(fail_on_synthesize_index=0)
        await service.load()
        handle = await service.prepare_voice(VoiceSettings())
        with pytest.raises(RuntimeError, match="synthesis failure"):
            await service.synthesize("text", handle)

    @pytest.mark.asyncio
    async def test_close(self):
        service = FakeTTSService()
        await service.load()
        await service.close()
        assert service.status()["loaded"] is False


# ---------------------------------------------------------------------------
# Synthetic pi event fixtures
# ---------------------------------------------------------------------------


class TestPiEventFixtures:
    def test_fixture_one_sentence(self):
        events = fixture_one_sentence()
        assert any(e["type"] == "agent_start" for e in events)
        assert any(e["type"] == "agent_settled" for e in events)
        deltas = [e for e in events if e.get("type") == "message_update"]
        assert len(deltas) > 0

    def test_fixture_multiple_text_blocks(self):
        events = fixture_multiple_text_blocks()
        text_starts = [
            e for e in events
            if e.get("type") == "message_update"
            and e.get("assistantMessageEvent", {}).get("type") == "text_start"
        ]
        assert len(text_starts) >= 2

    def test_fixture_tool_gap(self):
        events = fixture_tool_gap()
        assert any(e["type"] == "tool_execution_start" for e in events)
        assert any(e["type"] == "tool_execution_end" for e in events)

    def test_fixture_retry(self):
        events = fixture_retry()
        agent_starts = [e for e in events if e["type"] == "agent_start"]
        assert len(agent_starts) >= 2

    def test_fixture_long_unpunctuated(self):
        events = fixture_long_unpunctuated()
        deltas = [
            e for e in events
            if e.get("type") == "message_update"
            and e.get("assistantMessageEvent", {}).get("type") == "text_delta"
        ]
        total_text = "".join(
            d["assistantMessageEvent"]["delta"] for d in deltas
        )
        assert "." not in total_text

    def test_fixture_markdown(self):
        events = fixture_markdown()
        deltas = [
            e for e in events
            if e.get("type") == "message_update"
            and e.get("assistantMessageEvent", {}).get("type") == "text_delta"
        ]
        total_text = "".join(
            d["assistantMessageEvent"]["delta"] for d in deltas
        )
        assert "**" in total_text
        assert "[Link label]" in total_text

    def test_fixture_code_block(self):
        events = fixture_code_block()
        deltas = [
            e for e in events
            if e.get("type") == "message_update"
            and e.get("assistantMessageEvent", {}).get("type") == "text_delta"
        ]
        total_text = "".join(
            d["assistantMessageEvent"]["delta"] for d in deltas
        )
        assert "```python" in total_text

    def test_fixture_cancellation(self):
        events = fixture_cancellation()
        assert events[0]["type"] == "response"
        assert events[0]["success"] is False

    def test_fixture_all_fixtures(self):
        fixtures = fixture_all_fixtures()
        assert "one_sentence" in fixtures
        assert "tool_gap" in fixtures
        assert "retry" in fixtures
        assert len(fixtures) >= 8


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class TestDataTypes:
    def test_voice_settings_defaults(self):
        s = VoiceSettings()
        assert s.gender == "female"
        assert s.age == "young adult"
        assert s.pitch == "moderate pitch"
        assert s.accent == "american accent"
        assert s.style is None
        assert s.speed == 1.0

    def test_voice_settings_custom(self):
        s = VoiceSettings(gender="male", speed=1.1)
        assert s.gender == "male"
        assert s.speed == 1.1

    def test_voice_handle(self):
        h = VoiceHandle(key="test", settings=VoiceSettings())
        assert h.key == "test"

    def test_synthesized_audio(self):
        a = SynthesizedAudio(
            sample_rate=24000,
            pcm_s16le=b"\x00\x00",
            sample_count=1,
            generation_seconds=0.1,
        )
        assert a.sample_rate == 24000
        assert len(a.pcm_s16le) == 2
