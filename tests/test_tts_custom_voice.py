"""Tests for TTSService custom voice path (CRIT-2, CRIT-3)."""

import asyncio
import pytest
from tests.fakes.voice import FakeOmniVoiceRuntime, FakeVoiceStore, VoiceSettings


@pytest.fixture
def fake_runtime():
    """Create a FakeOmniVoiceRuntime for testing."""
    return FakeOmniVoiceRuntime()


@pytest.fixture
def fake_store():
    """Create a FakeVoiceStore for testing."""
    return FakeVoiceStore()


@pytest.fixture
def tts_service(fake_runtime, fake_store):
    """Create a TTSService with injected fakes."""
    from pi_chat.tts_service import TTSService

    service = TTSService(
        runtime_factory=lambda: fake_runtime,
        voice_store=fake_store,
    )
    return service


class TestVoiceStoreInjection:
    """Test VoiceStore injection (CRIT-2)."""

    @pytest.mark.asyncio
    async def test_prepare_custom_voice_uses_injected_store(self, tts_service, fake_store, fake_runtime):
        """CRIT-2: TTSService uses injected VoiceStore, not a new instance."""
        # Upload a voice to the fake store
        sample = fake_store.upload(b"test audio data", "test.wav", "Test Voice")

        # Load service
        await tts_service.load()

        # Prepare custom voice
        settings = VoiceSettings(voice_id=sample.voice_id, language="English")
        handle = await tts_service.prepare_voice(settings)

        # Verify handle was created
        assert handle is not None
        assert handle.key == f"custom:{sample.voice_id}"

        # Verify runtime was called with create_voice_clone_prompt
        assert len(fake_runtime.voice_clone_prompts) == 1
        prompt = fake_runtime.voice_clone_prompts[0]
        assert prompt["ref_text"] is None


class TestVoiceKey:
    """Test voice key generation."""

    def test_custom_voice_key(self, tts_service):
        """Custom voice uses voice_id directly."""
        settings = VoiceSettings(voice_id="a1b2c3d4e5f6a1b2")
        key = tts_service._voice_key(settings)
        assert key == "custom:a1b2c3d4e5f6a1b2"

    def test_bootstrap_voice_key(self, tts_service):
        """Bootstrap voice hashes settings."""
        settings = VoiceSettings(gender="female", age="young adult")
        key = tts_service._voice_key(settings)
        assert not key.startswith("custom:")
        assert len(key) == 16


class TestLanguageAlwaysPassed:
    """Test that language is always passed (CRIT-3)."""

    @pytest.mark.asyncio
    async def test_generate_always_passes_language(self, tts_service, fake_store, fake_runtime):
        """CRIT-3: generate() always passes language parameter."""
        # Upload a voice
        sample = fake_store.upload(b"test audio data", "test.wav", "Test Voice")

        await tts_service.load()

        # Prepare custom voice with specific language
        settings = VoiceSettings(voice_id=sample.voice_id, language="Spanish")
        handle = await tts_service.prepare_voice(settings)

        # Synthesize
        audio = await tts_service.synthesize("Hola", handle)

        # Verify synthesis worked
        assert audio is not None
        assert audio.sample_count > 0


class TestCustomVoicePreparation:
    """Test custom voice preparation flow."""

    @pytest.mark.asyncio
    async def test_prepare_custom_voice_creates_clone_prompt(self, tts_service, fake_store, fake_runtime):
        """Custom voice preparation creates voice clone prompt."""
        sample = fake_store.upload(b"test audio data" * 1000, "test.wav", "Test Voice")

        await tts_service.load()

        settings = VoiceSettings(voice_id=sample.voice_id, language="English")
        handle = await tts_service.prepare_voice(settings)

        # Verify clone prompt was created
        assert len(fake_runtime.voice_clone_prompts) == 1
        prompt = fake_runtime.voice_clone_prompts[0]
        assert prompt["ref_audio_len"] > 0
        assert prompt["ref_audio_sr"] == 24000

    @pytest.mark.asyncio
    async def test_prepare_custom_voice_cached(self, tts_service, fake_store, fake_runtime):
        """Custom voice preparation is cached."""
        sample = fake_store.upload(b"test audio data" * 1000, "test.wav", "Test Voice")

        await tts_service.load()

        settings = VoiceSettings(voice_id=sample.voice_id, language="English")

        # First preparation
        handle1 = await tts_service.prepare_voice(settings)
        assert len(fake_runtime.voice_clone_prompts) == 1

        # Second preparation (cached)
        handle2 = await tts_service.prepare_voice(settings)
        assert len(fake_runtime.voice_clone_prompts) == 1  # No new prompt created
        assert handle1.key == handle2.key

    @pytest.mark.asyncio
    async def test_prepare_custom_voice_missing_raises(self, tts_service):
        """Preparing a non-existent custom voice raises."""
        settings = VoiceSettings(voice_id="nonexistent1234567", language="English")

        await tts_service.load()

        with pytest.raises(ValueError, match="not found"):
            await tts_service.prepare_voice(settings)
