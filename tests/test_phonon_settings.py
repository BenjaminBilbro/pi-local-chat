"""Phonon voice/rate settings: the catalog, the validation, the API the dialog reads, and the
cache key that makes a voice change actually take effect.

Nothing here loads a model. The real engine path is tests/test_tts_phonon_real.py.
"""

from __future__ import annotations

import re
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pi_chat import config, phonon_voices
from pi_chat.app import create_app
from pi_chat.auth import AuthManager, hash_password
from pi_chat.tts_service import TTSService
from pi_chat.voice_session import PHONON_VOICE_PATTERN, validate_settings
from tests.fakes.voice import VoiceSettings

pytestmark = pytest.mark.skipif(
    not phonon_voices.SNAPSHOT_PATH.exists(),
    reason=f"catalog snapshot missing at {phonon_voices.SNAPSHOT_PATH}",
)


@pytest.fixture(autouse=True)
def _fresh_catalog():
    phonon_voices.reset_cache()
    yield
    phonon_voices.reset_cache()


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


def test_snapshot_ships_the_catalog():
    voices = phonon_voices.catalog()
    assert len(voices) > 100
    english = [v for v in voices if (v.get("language") or "").startswith("en")]
    assert len(english) > 30
    assert all(v.get("name") for v in voices)


def test_default_voice_exists_and_a_typo_does_not():
    assert phonon_voices.known("Russell")
    assert phonon_voices.known("russell")  # ptts matches in any case
    assert not phonon_voices.known("Russel")  # and NOT fuzzily
    assert not phonon_voices.known("")
    assert not phonon_voices.known(None)


def test_describe_canonicalizes_the_spelling():
    assert phonon_voices.describe("russell")["name"] == "Russell"


def test_every_catalog_name_survives_the_shape_check():
    """The shape guard must not reject a voice Gradium actually ships.

    Names include "Captain Silas Blackwater", "Elodie-Rose" and accented forms, so the pattern
    is only safe if it accepts the whole catalog. This test is what keeps a future tightening
    from bricking the picker.
    """
    bad = [v["name"] for v in phonon_voices.catalog() if not PHONON_VOICE_PATTERN.match(v["name"])]
    assert bad == []


def test_shape_check_rejects_things_that_are_not_names():
    for hostile in ["../sessions/b", "a/b", 'voice"; rm', "x" * 200, "set --conditions", " lead", ""]:
        assert not PHONON_VOICE_PATTERN.match(hostile), hostile


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_settings_accept_a_phonon_voice_and_rate():
    settings = validate_settings({"phonon_voice": "Zoey", "phonon_rate": "fastest"})
    assert settings is not None
    assert settings.phonon_voice == "Zoey"
    assert settings.phonon_rate == "fastest"


def test_settings_canonicalize_the_catalog_spelling():
    settings = validate_settings({"phonon_voice": "russell"})
    assert settings is not None
    assert settings.phonon_voice == "Russell"


def test_settings_reject_an_unknown_rate_step():
    assert validate_settings({"phonon_rate": "hyper"}) is None
    assert validate_settings({"phonon_rate": config.TTS_PHONON_RATE}) is not None


def test_settings_reject_a_malformed_voice_name():
    assert validate_settings({"phonon_voice": "../../etc/passwd"}) is None
    assert validate_settings({"phonon_voice": 42}) is None


def test_settings_allow_an_explicit_null_meaning_server_default():
    settings = validate_settings({"phonon_voice": None, "phonon_rate": None})
    assert settings is not None
    assert settings.phonon_voice is None and settings.phonon_rate is None


def test_default_settings_are_unchanged_by_the_new_fields():
    settings = validate_settings({})
    assert settings == VoiceSettings()


# ---------------------------------------------------------------------------
# The capabilities endpoint the dialog reads
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    auth = AuthManager()
    auth.password_hashes["b"] = hash_password("test123")
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("pi_chat.config.VOICE_SAMPLES_DIR", tmpdir):
            application = create_app(auth_manager=auth)
            yield TestClient(application)


def _login(client) -> None:
    resp = client.post("/api/login", json={"account": "b", "password": "test123"})
    assert resp.status_code == 200


def test_capabilities_describe_an_omnivoice_install(client):
    _login(client)
    with patch.object(config, "TTS_ENGINE", "omnivoice"):
        body = client.get("/api/tts/capabilities").json()
    assert body["engine"] == "omnivoice"
    assert body["design"] is True
    assert body["customVoices"] is True
    assert body["voices"] == [] and body["rates"] == []


def test_capabilities_list_voices_and_the_ladder_for_phonon(client):
    _login(client)
    with patch.object(config, "TTS_ENGINE", "phonon"), patch.object(
        config, "TTS_PHONON_VOICE", "Russell"
    ), patch.object(config, "TTS_PHONON_RATE", "fast"):
        body = client.get("/api/tts/capabilities").json()

    assert body["engine"] == "phonon"
    assert body["design"] is False
    # Uploaded voices are a cloning feature; Phonon has no speaker encoder.
    assert body["customVoices"] is False
    assert body["defaultVoice"] == "Russell"
    assert body["defaultRate"] == "fast"
    assert [r["label"] for r in body["rates"]] == list(config.TTS_PHONON_RATE_LABELS)
    assert body["rates"][3]["multiplier"] == pytest.approx(1.17)
    names = [v["name"] for v in body["voices"]]
    assert "Russell" in names
    assert all(v["voiceId"] for v in body["voices"])


def test_capabilities_requires_a_session(client):
    assert client.get("/api/tts/capabilities").status_code in (401, 403)


# ---------------------------------------------------------------------------
# The service side: a voice or rate change must not reuse a cached handle
# ---------------------------------------------------------------------------


def test_voice_cache_key_separates_phonon_voices_and_rates():
    service = TTSService(runtime_factory=lambda: None)
    base = service._voice_key(VoiceSettings(phonon_voice="Russell", phonon_rate="fast"))
    other_voice = service._voice_key(VoiceSettings(phonon_voice="Zoey", phonon_rate="fast"))
    other_rate = service._voice_key(VoiceSettings(phonon_voice="Russell", phonon_rate="slowest"))
    default = service._voice_key(VoiceSettings())

    assert base != other_voice != other_rate
    assert base != default
    # Stable, so a reload does not invalidate the whole cache.
    assert base == service._voice_key(VoiceSettings(phonon_voice="Russell", phonon_rate="fast"))


def test_omnivoice_keys_are_unchanged_by_phonon_defaults():
    """Fields that are None must not perturb the key OmniVoice has always produced."""
    service = TTSService(runtime_factory=lambda: None)
    legacy = service._voice_key(VoiceSettings(gender="male", age="elderly"))
    with_explicit_nulls = service._voice_key(
        VoiceSettings(gender="male", age="elderly", phonon_voice=None, phonon_rate=None)
    )
    assert legacy == with_explicit_nulls


def test_rate_labels_and_ladder_stay_in_lockstep():
    assert config.TTS_PHONON_RATE_LABELS == tuple(config.TTS_PHONON_RATE_LADDER)
    assert set(config.TTS_PHONON_RATE_MEASURED) == set(config.TTS_PHONON_RATE_LADDER)
    # fast is the shipped default, per Ben's listening test on 2026-11-13.
    assert config.TTS_PHONON_RATE in config.TTS_PHONON_RATE_LABELS
    assert config.TTS_PHONON_RATE_LADDER[config.TTS_PHONON_RATE] == -0.20
