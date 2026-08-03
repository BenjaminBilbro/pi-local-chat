"""Integration tests for custom voice API endpoints."""

import io
import json
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pi_chat.app import create_app
from pi_chat.auth import AuthManager, hash_password


@pytest.fixture
def temp_voice_dir():
    """Create a temporary voice samples directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def test_app(temp_voice_dir):
    """Create a test app with isolated voice store."""
    # Create AuthManager and set a known password hash directly
    auth = AuthManager()
    auth.password_hashes["b"] = hash_password("test123")

    with patch("pi_chat.config.VOICE_SAMPLES_DIR", temp_voice_dir):
        app = create_app(auth_manager=auth)
        yield app


@pytest.fixture
def client(test_app):
    """Create a TestClient."""
    return TestClient(test_app)


@pytest.fixture
def auth_cookies(client):
    """Login and return cookies."""
    resp = client.post("/api/login", json={
        "account": "b",
        "password": "test123",
    })
    assert resp.status_code == 200
    return client.cookies


def generate_test_wav(duration_seconds=5.0, sample_rate=24000):
    """Generate a simple WAV file in memory."""
    import struct
    import math

    sr = sample_rate
    num_samples = int(duration_seconds * sr)
    frequency = 440.0

    # Generate sine wave samples
    samples = bytearray()
    for i in range(num_samples):
        t = i / sr
        value = int(0.5 * 32767 * math.sin(2 * math.pi * frequency * t))
        samples.extend(struct.pack("<h", value))

    # Build WAV file
    buf = io.BytesIO()
    num_frames = len(samples) // 2
    num_channels = 1
    bytes_per_sample = 2
    block_align = num_channels * bytes_per_sample
    byte_rate = sr * block_align
    data_size = num_frames * block_align
    file_size = 36 + data_size

    buf.write(b"RIFF")
    buf.write(struct.pack("<I", file_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))
    buf.write(struct.pack("<H", 1))
    buf.write(struct.pack("<H", num_channels))
    buf.write(struct.pack("<I", sr))
    buf.write(struct.pack("<I", byte_rate))
    buf.write(struct.pack("<H", block_align))
    buf.write(struct.pack("<H", bytes_per_sample * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(samples)

    return buf.getvalue()


class TestUploadVoice:
    """Test POST /api/voices."""

    def test_upload_valid_wav(self, client, auth_cookies):
        """Upload a valid WAV file."""
        wav_data = generate_test_wav(5.0)

        resp = client.post(
            "/api/voices",
            files={"file": ("test.wav", wav_data, "audio/wav")},
            data={"display_name": "My Voice"},
            cookies=auth_cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "voiceId" in data
        assert len(data["voiceId"]) == 16
        assert data["displayName"] == "My Voice"
        assert data["duration"] > 0

    def test_upload_requires_auth(self, client):
        """Upload requires authentication."""
        wav_data = generate_test_wav(5.0)
        resp = client.post(
            "/api/voices",
            files={"file": ("test.wav", wav_data, "audio/wav")},
        )
        assert resp.status_code == 401

    def test_upload_no_file(self, client, auth_cookies):
        """Upload with no file returns 422 (FastAPI validation error)."""
        resp = client.post("/api/voices", cookies=auth_cookies)
        assert resp.status_code in (400, 422)

    def test_upload_invalid_format(self, client, auth_cookies):
        """Upload with unsupported format returns 400."""
        resp = client.post(
            "/api/voices",
            files={"file": ("test.xyz", b"garbage", "application/octet-stream")},
            cookies=auth_cookies,
        )
        assert resp.status_code == 400

    def test_upload_too_short(self, client, auth_cookies):
        """Upload with audio too short returns 400."""
        wav_data = generate_test_wav(0.5)  # 0.5 seconds

        resp = client.post(
            "/api/voices",
            files={"file": ("short.wav", wav_data, "audio/wav")},
            cookies=auth_cookies,
        )
        assert resp.status_code == 400

    def test_upload_too_long(self, client, auth_cookies):
        """Upload with audio too long returns 400."""
        wav_data = generate_test_wav(40.0)  # 40 seconds

        resp = client.post(
            "/api/voices",
            files={"file": ("long.wav", wav_data, "audio/wav")},
            cookies=auth_cookies,
        )
        assert resp.status_code == 400

    def test_upload_duplicate_returns_existing(self, client, auth_cookies):
        """Uploading the same file returns existing voice."""
        wav_data = generate_test_wav(5.0)

        resp1 = client.post(
            "/api/voices",
            files={"file": ("test.wav", wav_data, "audio/wav")},
            cookies=auth_cookies,
        )
        resp2 = client.post(
            "/api/voices",
            files={"file": ("test.wav", wav_data, "audio/wav")},
            cookies=auth_cookies,
        )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["voiceId"] == resp2.json()["voiceId"]


class TestListVoices:
    """Test GET /api/voices."""

    def test_list_empty(self, client, auth_cookies):
        """List voices when none exist."""
        resp = client.get("/api/voices", cookies=auth_cookies)
        assert resp.status_code == 200
        assert resp.json()["voices"] == []

    def test_list_after_upload(self, client, auth_cookies):
        """List voices after upload."""
        wav_data = generate_test_wav(5.0)
        client.post(
            "/api/voices",
            files={"file": ("test.wav", wav_data, "audio/wav")},
            cookies=auth_cookies,
        )

        resp = client.get("/api/voices", cookies=auth_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["voices"]) == 1
        assert "voiceId" in data["voices"][0]
        assert "displayName" in data["voices"][0]
        assert "uploadTime" in data["voices"][0]


class TestUpdateVoice:
    """Test PATCH /api/voices/{id}."""

    def test_update_display_name(self, client, auth_cookies):
        """Update a voice's display name."""
        wav_data = generate_test_wav(5.0)
        upload_resp = client.post(
            "/api/voices",
            files={"file": ("test.wav", wav_data, "audio/wav")},
            cookies=auth_cookies,
        )
        voice_id = upload_resp.json()["voiceId"]

        resp = client.patch(
            f"/api/voices/{voice_id}",
            json={"displayName": "New Name"},
            cookies=auth_cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_update_invalid_json(self, client, auth_cookies):
        """Update with invalid JSON returns 400."""
        resp = client.patch(
            "/api/voices/a1b2c3d4e5f6a1b2",
            content=b"not json",
            cookies=auth_cookies,
        )
        assert resp.status_code == 400

    def test_update_missing_display_name(self, client, auth_cookies):
        """Update without displayName returns 400."""
        resp = client.patch(
            "/api/voices/a1b2c3d4e5f6a1b2",
            json={},
            cookies=auth_cookies,
        )
        assert resp.status_code == 400

    def test_update_not_found(self, client, auth_cookies):
        """Update non-existent voice returns 404."""
        resp = client.patch(
            "/api/voices/nonexistent1234567",
            json={"displayName": "Test"},
            cookies=auth_cookies,
        )
        assert resp.status_code == 404


class TestDeleteVoice:
    """Test DELETE /api/voices/{id}."""

    def test_delete_voice(self, client, auth_cookies):
        """Delete a voice."""
        wav_data = generate_test_wav(5.0)
        upload_resp = client.post(
            "/api/voices",
            files={"file": ("test.wav", wav_data, "audio/wav")},
            cookies=auth_cookies,
        )
        voice_id = upload_resp.json()["voiceId"]

        resp = client.delete(f"/api/voices/{voice_id}", cookies=auth_cookies)
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify deleted
        list_resp = client.get("/api/voices", cookies=auth_cookies)
        assert all(v["voiceId"] != voice_id for v in list_resp.json()["voices"])

    def test_delete_not_found(self, client, auth_cookies):
        """Delete non-existent voice returns 404."""
        resp = client.delete(
            "/api/voices/nonexistent1234567",
            cookies=auth_cookies,
        )
        assert resp.status_code == 404


class TestGetVoiceAudio:
    """Test GET /api/voices/{id}/audio."""

    def test_get_audio(self, client, auth_cookies):
        """Get audio file for preview."""
        wav_data = generate_test_wav(5.0)
        upload_resp = client.post(
            "/api/voices",
            files={"file": ("test.wav", wav_data, "audio/wav")},
            cookies=auth_cookies,
        )
        voice_id = upload_resp.json()["voiceId"]

        resp = client.get(f"/api/voices/{voice_id}/audio", cookies=auth_cookies)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert len(resp.content) > 0

    def test_get_audio_not_found(self, client, auth_cookies):
        """Get audio for non-existent voice returns 404."""
        resp = client.get(
            "/api/voices/nonexistent1234567/audio",
            cookies=auth_cookies,
        )
        assert resp.status_code == 404
