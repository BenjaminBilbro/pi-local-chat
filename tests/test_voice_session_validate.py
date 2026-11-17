"""Tests for validate_settings with voice_id support (CRIT-1)."""

import pytest
from pi_chat.voice_session import validate_settings


class TestValidateSettingsVoiceId:
    """Test voice_id acceptance in validate_settings."""

    def test_accepts_voice_id(self):
        """CRIT-1: validate_settings must accept voice_id."""
        result = validate_settings({"voice_id": "a1b2c3d4e5f6a1b2"})
        assert result is not None
        assert result.voice_id == "a1b2c3d4e5f6a1b2"

    def test_accepts_voice_id_with_other_keys(self):
        """voice_id works with other settings."""
        result = validate_settings({
            "voice_id": "a1b2c3d4e5f6a1b2",
            "gender": "female",
            "language": "English",
        })
        assert result is not None
        assert result.voice_id == "a1b2c3d4e5f6a1b2"
        assert result.gender == "female"
        assert result.language == "English"

    def test_rejects_invalid_voice_id_format_non_hex(self):
        """Invalid voice_id format (non-hex) rejected."""
        result = validate_settings({"voice_id": "xyz"})
        assert result is None

    def test_rejects_invalid_voice_id_format_wrong_length(self):
        """Invalid voice_id format (wrong length) rejected."""
        result = validate_settings({"voice_id": "a1b2c3d4"})
        assert result is None

    def test_rejects_invalid_voice_id_format_uppercase(self):
        """Invalid voice_id format (uppercase) rejected."""
        result = validate_settings({"voice_id": "A1B2C3D4E5F6A1B2"})
        assert result is None

    def test_rejects_voice_id_not_string(self):
        """voice_id must be a string."""
        result = validate_settings({"voice_id": 123})
        assert result is None

    def test_rejects_voice_id_none_explicit(self):
        """voice_id=None is treated as no custom voice."""
        result = validate_settings({"voice_id": None})
        assert result is not None
        assert result.voice_id is None

    def test_rejects_unknown_keys(self):
        """Unknown keys still rejected."""
        result = validate_settings({"unknown_key": "value"})
        assert result is None

    def test_accepts_valid_bootstrap_settings(self):
        """Bootstrap settings without voice_id still work."""
        result = validate_settings({
            "gender": "male",
            "age": "young adult",
            "pitch": "low pitch",
            "accent": "british accent",
            "language": "English",
        })
        assert result is not None
        assert result.voice_id is None
        assert result.gender == "male"

    def test_validates_other_fields_with_voice_id(self):
        """Other field validation still applies with voice_id."""
        # Invalid gender with valid voice_id
        result = validate_settings({
            "voice_id": "a1b2c3d4e5f6a1b2",
            "gender": "invalid_gender",
        })
        assert result is None

        # Invalid speed with valid voice_id
        result = validate_settings({
            "voice_id": "a1b2c3d4e5f6a1b2",
            "speed": 999.0,
        })
        assert result is None
