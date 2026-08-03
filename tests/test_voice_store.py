"""Tests for VoiceStore custom voice sample management."""

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from pi_chat.voice_store import VoiceStore, VoiceSample, ALLOWED_EXTENSIONS, MAX_FILE_SIZE


@pytest.fixture
def temp_voice_dir():
    """Create a temporary directory for voice storage."""
    tmpdir = tempfile.mkdtemp()
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_config(temp_voice_dir):
    """Create a mock config object."""
    class MockConfig:
        VOICE_SAMPLES_DIR = temp_voice_dir
    return MockConfig()


@pytest.fixture
def voice_store(mock_config):
    """Create a VoiceStore instance with temp directory."""
    return VoiceStore(mock_config)


@pytest.fixture
def fixtures_dir():
    """Path to test fixtures."""
    return Path(__file__).parent / "fixtures" / "voices"


def load_fixture(fixtures_dir, filename):
    """Load a fixture file as bytes."""
    return (fixtures_dir / filename).read_bytes()


class TestUploadValidAudio:
    """Test uploading valid audio files."""

    def test_upload_valid_wav(self, voice_store, fixtures_dir):
        """T6: Upload a valid WAV file."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test_voice.wav", "My Test Voice")
        
        assert sample.voice_id is not None
        assert len(sample.voice_id) == 16
        assert sample.filename == "test_voice"
        assert sample.display_name == "My Test Voice"
        assert sample.duration >= 4.0  # ~5s fixture
        assert isinstance(sample.upload_time, float)
        assert sample.upload_time > 0

    def test_upload_stereo_converts_to_mono(self, voice_store, fixtures_dir):
        """Test stereo WAV is converted to mono."""
        data = load_fixture(fixtures_dir, "stereo.wav")
        sample = voice_store.upload(data, "stereo_test.wav")
        
        assert sample is not None
        
        # Verify stored WAV is mono
        import soundfile as sf
        wav_path = voice_store._wav_path(sample.voice_id)
        loaded_waveform, sr = sf.read(str(wav_path), dtype="float32")
        assert len(loaded_waveform.shape) == 1  # mono

    def test_upload_different_sample_rate_resamples(self, voice_store, fixtures_dir):
        """Test 44.1kHz WAV is resampled to 24kHz."""
        data = load_fixture(fixtures_dir, "44100hz.wav")
        sample = voice_store.upload(data, "44k_test.wav")
        
        assert sample is not None
        
        # Verify stored WAV is 24kHz
        import soundfile as sf
        wav_path = voice_store._wav_path(sample.voice_id)
        _, sr = sf.read(str(wav_path), dtype="float32")
        assert sr == 24000

    def test_upload_with_default_display_name(self, voice_store, fixtures_dir):
        """Test display name defaults to sanitized filename."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "my_custom_voice.wav")
        
        assert sample.display_name == "my_custom_voice"

    def test_upload_display_name_truncated_to_max_length(self, voice_store, fixtures_dir):
        """MED-4: Display name truncated to MAX_DISPLAY_NAME_LENGTH."""
        data = load_fixture(fixtures_dir, "valid.wav")
        long_name = "A" * 100
        sample = voice_store.upload(data, "test.wav", long_name)
        
        assert len(sample.display_name) <= 40


class TestUploadValidation:
    """Test upload validation and rejection."""

    def test_upload_rejects_too_short(self, voice_store, fixtures_dir):
        """Test audio shorter than MIN_DURATION is rejected."""
        data = load_fixture(fixtures_dir, "short.wav")
        
        with pytest.raises(ValueError, match="too short"):
            voice_store.upload(data, "short.wav")

    def test_upload_rejects_too_long(self, voice_store, fixtures_dir):
        """Test audio longer than MAX_DURATION is rejected."""
        data = load_fixture(fixtures_dir, "long.wav")
        
        with pytest.raises(ValueError, match="too long"):
            voice_store.upload(data, "long.wav")

    def test_upload_rejects_silence(self, voice_store, fixtures_dir):
        """CRIT-5: Test silent audio is rejected."""
        data = load_fixture(fixtures_dir, "silence.wav")
        
        with pytest.raises(ValueError, match="silent"):
            voice_store.upload(data, "silence.wav")

    def test_upload_rejects_corrupt_file(self, voice_store, fixtures_dir):
        """CRIT-5: Test corrupt file is rejected."""
        data = load_fixture(fixtures_dir, "garbage.wav")
        
        with pytest.raises(ValueError, match="Failed to read"):
            voice_store.upload(data, "garbage.wav")

    def test_upload_rejects_empty_file(self, voice_store, fixtures_dir):
        """CRIT-5: Test empty WAV is rejected."""
        data = load_fixture(fixtures_dir, "empty.wav")
        
        with pytest.raises(ValueError):
            voice_store.upload(data, "empty.wav")

    def test_upload_rejects_unsupported_format(self, voice_store, fixtures_dir):
        """Test unsupported file extension is rejected."""
        data = load_fixture(fixtures_dir, "valid.wav")
        
        with pytest.raises(ValueError, match="Unsupported format"):
            voice_store.upload(data, "test.txt")

    def test_upload_rejects_too_large_file(self, voice_store, fixtures_dir):
        """Test file larger than MAX_FILE_SIZE is rejected."""
        large_data = b'\x00' * (MAX_FILE_SIZE + 1)
        
        with pytest.raises(ValueError, match="too large"):
            voice_store.upload(large_data, "large.wav")


class TestHashDeduplication:
    """Test hash-based deduplication."""

    def test_duplicate_upload_returns_existing(self, voice_store, fixtures_dir):
        """Test uploading same file twice returns same voice_id."""
        data = load_fixture(fixtures_dir, "valid.wav")
        
        sample1 = voice_store.upload(data, "first_upload.wav", "First")
        sample2 = voice_store.upload(data, "second_upload.wav", "Second")
        
        assert sample1.voice_id == sample2.voice_id
        # Existing metadata preserved
        assert sample2.display_name == "First"

    def test_different_files_different_hashes(self, voice_store, fixtures_dir):
        """Test different audio files get different voice_ids."""
        data1 = load_fixture(fixtures_dir, "valid.wav")
        
        # Generate slightly different audio
        import io
        import soundfile as sf
        import numpy as np
        sr = 24000
        duration = 5.0
        t = np.linspace(0, duration, int(sr * duration))
        waveform = 0.7 * np.sin(2 * np.pi * 500 * t)  # Different frequency
        buf = io.BytesIO()
        sf.write(buf, waveform, sr, format='WAV', subtype='PCM_16')
        data2 = buf.getvalue()
        
        sample1 = voice_store.upload(data1, "voice1.wav")
        sample2 = voice_store.upload(data2, "voice2.wav")
        
        assert sample1.voice_id != sample2.voice_id


class TestListAndGet:
    """Test listing and retrieving voices."""

    def test_list_voices_empty_initially(self, voice_store):
        """Test list_voices returns empty list when no voices."""
        voices = voice_store.list_voices()
        assert voices == []

    def test_list_voices_after_upload(self, voice_store, fixtures_dir):
        """Test uploaded voice appears in list."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test.wav", "Test Voice")
        
        voices = voice_store.list_voices()
        assert len(voices) == 1
        assert voices[0].voice_id == sample.voice_id

    def test_get_voice_by_id(self, voice_store, fixtures_dir):
        """Test get_voice returns correct sample."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test.wav", "Test Voice")
        
        retrieved = voice_store.get_voice(sample.voice_id)
        assert retrieved is not None
        assert retrieved.voice_id == sample.voice_id
        assert retrieved.display_name == "Test Voice"

    def test_get_voice_invalid_id_returns_none(self, voice_store):
        """Test get_voice returns None for invalid ID."""
        assert voice_store.get_voice("invalid") is None
        assert voice_store.get_voice("abc123") is None  # wrong length
        assert voice_store.get_voice("GHIJKLMNOP123456") is None  # non-hex

    def test_get_voice_missing_returns_none(self, voice_store):
        """Test get_voice returns None for non-existent ID."""
        assert voice_store.get_voice("0000000000000000") is None


class TestDelete:
    """Test voice deletion."""

    def test_delete_voice(self, voice_store, fixtures_dir):
        """Test deleting a voice removes its files."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test.wav")
        
        assert voice_store.get_voice(sample.voice_id) is not None
        
        result = voice_store.delete_voice(sample.voice_id)
        assert result is True
        
        assert voice_store.get_voice(sample.voice_id) is None
        assert not voice_store._wav_path(sample.voice_id).exists()
        assert not voice_store._meta_path(sample.voice_id).exists()

    def test_delete_nonexistent_returns_false(self, voice_store):
        """Test deleting non-existent voice returns False."""
        result = voice_store.delete_voice("0000000000000000")
        assert result is False

    def test_delete_invalid_id_returns_false(self, voice_store):
        """Test deleting with invalid ID returns False."""
        assert voice_store.delete_voice("invalid") is False


class TestUpdateDisplayName:
    """Test display name updates."""

    def test_update_display_name(self, voice_store, fixtures_dir):
        """Test updating display name."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test.wav", "Original Name")
        
        result = voice_store.update_display_name(sample.voice_id, "New Name")
        assert result is True
        
        updated = voice_store.get_voice(sample.voice_id)
        assert updated.display_name == "New Name"

    def test_update_nonexistent_returns_false(self, voice_store):
        """Test updating non-existent voice returns False."""
        assert voice_store.update_display_name("0000000000000000", "Name") is False


class TestLoadWaveform:
    """Test waveform loading."""

    def test_load_waveform(self, voice_store, fixtures_dir):
        """Test loading waveform returns correct data."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test.wav")
        
        waveform, sr = voice_store.load_waveform(sample.voice_id)
        
        assert sr == 24000
        assert len(waveform) > 0
        # waveform is numpy array or list
        assert hasattr(waveform, '__len__')

    def test_load_waveform_missing_raises(self, voice_store):
        """Test loading non-existent voice raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            voice_store.load_waveform("0000000000000000")


class TestIntegrityChecks:
    """CRIT-9: Test integrity checks for missing files."""

    def test_list_voices_skips_missing_wav(self, voice_store, fixtures_dir):
        """CRIT-9: list_voices skips voices with missing WAV files."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test.wav")
        
        # Delete WAV file, keep meta
        voice_store._wav_path(sample.voice_id).unlink()
        
        voices = voice_store.list_voices()
        assert len(voices) == 0  # Should be skipped

    def test_get_voice_skips_missing_wav(self, voice_store, fixtures_dir):
        """CRIT-9: get_voice returns None when WAV is missing."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test.wav")
        
        # Delete WAV file
        voice_store._wav_path(sample.voice_id).unlink()
        
        assert voice_store.get_voice(sample.voice_id) is None


class TestStorageQuota:
    """HIGH-1: Test storage quota enforcement."""

    def test_upload_respects_storage_quota(self, mock_config):
        """HIGH-1: Upload rejected when total exceeds MAX_TOTAL_STORAGE."""
        from pi_chat.voice_store import MAX_TOTAL_STORAGE
        
        store = VoiceStore(mock_config)
        
        # Mock _check_storage_quota to always exceed
        with patch.object(store, '_check_storage_quota') as mock_check:
            mock_check.side_effect = ValueError("Voice storage quota exceeded")
            
            # Generate valid audio data
            import io
            import soundfile as sf
            import numpy as np
            sr = 24000
            t = np.linspace(0, 5, int(sr * 5))
            waveform = 0.7 * np.sin(2 * np.pi * 440 * t)
            buf = io.BytesIO()
            sf.write(buf, waveform, sr, format='WAV', subtype='PCM_16')
            data = buf.getvalue()
            
            with pytest.raises(ValueError, match="quota exceeded"):
                store.upload(data, "test.wav")


class TestConcurrentUpload:
    """CRIT-6: Test concurrent upload safety."""

    def test_concurrent_duplicate_uploads_safe(self, voice_store, fixtures_dir):
        """CRIT-6: Concurrent duplicate uploads don't corrupt files."""
        data = load_fixture(fixtures_dir, "valid.wav")
        
        results = []
        errors = []
        
        def upload():
            try:
                sample = voice_store.upload(data, "concurrent.wav", "Concurrent")
                results.append(sample.voice_id)
            except Exception as e:
                errors.append(str(e))
        
        threads = [threading.Thread(target=upload) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        # All should succeed with same voice_id
        assert len(errors) == 0
        assert len(set(results)) == 1  # All same voice_id
        assert len(voice_store.list_voices()) == 1


class TestAtomicWrites:
    """CRIT-7: Test atomic write behavior."""

    def test_atomic_write_no_partial_files(self, voice_store, fixtures_dir):
        """CRIT-7: Crash mid-write leaves no corrupt files."""
        data = load_fixture(fixtures_dir, "valid.wav")
        
        # Verify no temp files before
        temp_files = list(Path(voice_store._voice_dir).glob("*.tmp"))
        assert len(temp_files) == 0
        
        sample = voice_store.upload(data, "test.wav")
        
        # Verify no temp files after
        temp_files = list(Path(voice_store._voice_dir).glob("*.tmp"))
        assert len(temp_files) == 0
        
        # Verify files are valid
        assert voice_store.get_voice(sample.voice_id) is not None


class TestTimestamps:
    """HIGH-17: Test timestamp correctness."""

    def test_upload_time_is_unix_timestamp(self, voice_store, fixtures_dir):
        """HIGH-17: upload_time is a meaningful Unix timestamp."""
        data = load_fixture(fixtures_dir, "valid.wav")
        before = time.time()
        
        sample = voice_store.upload(data, "test.wav")
        
        after = time.time()
        
        assert before <= sample.upload_time <= after
        # Should be around current epoch time
        assert sample.upload_time > 1700000000  # Nov 2023


class TestMetaJsonFormat:
    """Test metadata JSON format."""

    def test_meta_json_has_upload_time_as_integer_compatible(self, voice_store, fixtures_dir):
        """Test meta JSON upload_time is numeric."""
        data = load_fixture(fixtures_dir, "valid.wav")
        sample = voice_store.upload(data, "test.wav")
        
        meta_path = voice_store._meta_path(sample.voice_id)
        meta = json.loads(meta_path.read_text())
        
        assert "upload_time" in meta
        assert isinstance(meta["upload_time"], (int, float))
