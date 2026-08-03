"""End-to-end tests for custom voice cloning with fake runtimes.

Tests the full flow: upload → prepare → synthesize using FakeVoiceStore
and FakeOmniVoiceRuntime. No GPU required.
"""

import asyncio
import pytest
from tests.fakes.voice import FakeOmniVoiceRuntime, FakeVoiceStore, VoiceSettings


@pytest.fixture
def fake_runtime():
    return FakeOmniVoiceRuntime()


@pytest.fixture
def fake_store():
    return FakeVoiceStore()


@pytest.fixture
def tts_service(fake_runtime, fake_store):
    from pi_chat.tts_service import TTSService
    service = TTSService(
        runtime_factory=lambda: fake_runtime,
        voice_store=fake_store,
    )
    return service


class TestVoiceUploadPrepareSynthesizeFlow:
    """Full flow: upload → prepare → synthesize."""

    @pytest.mark.asyncio
    async def test_full_custom_voice_flow(self, tts_service, fake_store, fake_runtime):
        """Upload a voice, prepare it, synthesize speech."""
        # 1. Upload voice sample
        sample = fake_store.upload(b"test audio data" * 2000, "my_voice.wav", "My Voice")
        assert sample.voice_id is not None
        assert sample.display_name == "My Voice"

        # 2. Load TTS service
        await tts_service.load()
        assert tts_service.status()["loaded"]

        # 3. Prepare custom voice
        settings = VoiceSettings(voice_id=sample.voice_id, language="English")
        handle = await tts_service.prepare_voice(settings)
        assert handle.key == f"custom:{sample.voice_id}"

        # 4. Verify voice clone prompt was created
        assert len(fake_runtime.voice_clone_prompts) == 1

        # 5. Synthesize speech
        audio = await tts_service.synthesize("Hello, this is my cloned voice.", handle)
        assert audio is not None
        assert audio.sample_count > 0
        assert audio.sample_rate == 24000

    @pytest.mark.asyncio
    async def test_multiple_custom_voices(self, tts_service, fake_store, fake_runtime):
        """Upload and use multiple custom voices."""
        # Upload two voices
        sample1 = fake_store.upload(b"voice one data" * 2000, "voice1.wav", "Voice One")
        sample2 = fake_store.upload(b"voice two data" * 2000, "voice2.wav", "Voice Two")

        await tts_service.load()

        # Prepare both
        settings1 = VoiceSettings(voice_id=sample1.voice_id, language="English")
        settings2 = VoiceSettings(voice_id=sample2.voice_id, language="English")
        handle1 = await tts_service.prepare_voice(settings1)
        handle2 = await tts_service.prepare_voice(settings2)

        assert handle1.key != handle2.key

        # Synthesize with both
        audio1 = await tts_service.synthesize("Speaking with voice one.", handle1)
        audio2 = await tts_service.synthesize("Speaking with voice two.", handle2)

        assert audio1.sample_count > 0
        assert audio2.sample_count > 0

    @pytest.mark.asyncio
    async def test_custom_voice_with_different_languages(self, tts_service, fake_store, fake_runtime):
        """Custom voice works with different language settings."""
        sample = fake_store.upload(b"test audio" * 2000, "test.wav", "Test")

        await tts_service.load()

        for lang in ["English", "Spanish", "French", "German"]:
            settings = VoiceSettings(voice_id=sample.voice_id, language=lang)
            handle = await tts_service.prepare_voice(settings)
            audio = await tts_service.synthesize("Test", handle)
            assert audio.sample_count > 0


class TestVoiceCacheInvalidation:
    """Test cache invalidation on voice deletion (T34/MED-8)."""

    @pytest.mark.asyncio
    async def test_delete_voice_invalidates_cache(self, tts_service, fake_store, fake_runtime):
        """Deleting a voice removes it from the cache."""
        sample = fake_store.upload(b"test audio" * 2000, "test.wav", "Test")

        await tts_service.load()

        settings = VoiceSettings(voice_id=sample.voice_id, language="English")
        handle = await tts_service.prepare_voice(settings)

        # Verify cached
        assert handle.key in tts_service._voice_cache

        # Delete voice and invalidate cache
        fake_store.delete_voice(sample.voice_id)
        tts_service.invalidate_voice(sample.voice_id)

        # Verify removed from cache
        assert handle.key not in tts_service._voice_cache

    @pytest.mark.asyncio
    async def test_prepare_deleted_voice_raises(self, tts_service, fake_store):
        """Preparing a deleted voice raises an error."""
        sample = fake_store.upload(b"test audio" * 2000, "test.wav", "Test")

        await tts_service.load()

        # Delete the voice
        fake_store.delete_voice(sample.voice_id)

        settings = VoiceSettings(voice_id=sample.voice_id, language="English")
        with pytest.raises(ValueError, match="not found"):
            await tts_service.prepare_voice(settings)


class TestVoicePreparationFailure:
    """Test voice preparation failure handling (T38/MED-11)."""

    @pytest.mark.asyncio
    async def test_prepare_failure_runtime_error(self):
        """Runtime failure during create_voice_clone_prompt is handled."""
        from pi_chat.tts_service import TTSService

        # Runtime that fails on create_voice_clone_prompt
        runtime = FakeOmniVoiceRuntime(fail_on_clone_prompt=True)
        store = FakeVoiceStore()
        sample = store.upload(b"test audio" * 2000, "test.wav", "Test")

        service = TTSService(
            runtime_factory=lambda: runtime,
            voice_store=store,
        )
        await service.load()

        settings = VoiceSettings(voice_id=sample.voice_id, language="English")
        with pytest.raises(RuntimeError, match="create_voice_clone_prompt"):
            await service.prepare_voice(settings)


class TestWaveformValidationBeforeOmniVoice:
    """Test waveform validation before passing to OmniVoice (T35/MED-3)."""

    @pytest.mark.asyncio
    async def test_silent_waveform_rejected(self):
        """Silent waveform is rejected before OmniVoice call."""
        from pi_chat.tts_service import TTSService
        from unittest.mock import patch
        import numpy as np

        runtime = FakeOmniVoiceRuntime()
        store = FakeVoiceStore()
        sample = store.upload(b"test audio" * 2000, "test.wav", "Test")

        service = TTSService(
            runtime_factory=lambda: runtime,
            voice_store=store,
        )
        await service.load()

        # Mock load_waveform to return silent waveform
        silent = np.zeros(24000 * 5, dtype=np.float32)
        with patch.object(store, 'load_waveform', return_value=(silent, 24000)):
            settings = VoiceSettings(voice_id=sample.voice_id, language="English")
            with pytest.raises(ValueError, match="silent"):
                await service.prepare_voice(settings)

    @pytest.mark.asyncio
    async def test_nan_waveform_rejected(self):
        """Waveform with NaN values is rejected before OmniVoice call."""
        from pi_chat.tts_service import TTSService
        from unittest.mock import patch
        import numpy as np

        runtime = FakeOmniVoiceRuntime()
        store = FakeVoiceStore()
        sample = store.upload(b"test audio" * 2000, "test.wav", "Test")

        service = TTSService(
            runtime_factory=lambda: runtime,
            voice_store=store,
        )
        await service.load()

        # Mock load_waveform to return NaN waveform
        nan_wave = np.full(24000 * 5, np.nan, dtype=np.float32)
        with patch.object(store, 'load_waveform', return_value=(nan_wave, 24000)):
            settings = VoiceSettings(voice_id=sample.voice_id, language="English")
            with pytest.raises(ValueError, match="invalid"):
                await service.prepare_voice(settings)

    @pytest.mark.asyncio
    async def test_empty_waveform_rejected(self):
        """Empty waveform is rejected before OmniVoice call."""
        from pi_chat.tts_service import TTSService
        from unittest.mock import patch
        import numpy as np

        runtime = FakeOmniVoiceRuntime()
        store = FakeVoiceStore()
        sample = store.upload(b"test audio" * 2000, "test.wav", "Test")

        service = TTSService(
            runtime_factory=lambda: runtime,
            voice_store=store,
        )
        await service.load()

        # Mock load_waveform to return empty waveform
        empty = np.array([], dtype=np.float32)
        with patch.object(store, 'load_waveform', return_value=(empty, 24000)):
            settings = VoiceSettings(voice_id=sample.voice_id, language="English")
            with pytest.raises(ValueError, match="empty"):
                await service.prepare_voice(settings)
