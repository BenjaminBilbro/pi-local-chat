"""`VOICE-07`: `PI_CHAT_TTS_MAX_HOLD_MS` has to reach the chunker that honors it.

Two components read this setting and only one of them used to get the configured value: the session's hold
timer read `config.TTS_MAX_HOLD_MS`, while the chunker was constructed with no argument and kept its own
450 ms default. The flush the timer fires is gated by the chunker's value, so the knob was **effective only
upward** - `PI_CHAT_TTS_MAX_HOLD_MS=200` changed nothing, and `tests/test_voice_session.py`'s own
`FakeConfig` has carried `TTS_MAX_HOLD_MS = 100` the whole time without a single test noticing that the
number was going nowhere.

The behavioral half of this file passes `now=` explicitly instead of sleeping, so it says exactly what the
knob means: how long buffered text may sit before it is forced out at a safe boundary.
"""

from __future__ import annotations

import pytest

from pi_chat.tts_chunking import StreamingSpeechChunker
from pi_chat.voice_session import VoiceSession
from tests.fakes.voice import FakeTTSService, VoiceSettings

FILLER = "alpha " * 30  # 180 characters, no sentence end: long enough to be hold-gated, under HARD_MAX


class HoldConfig:
    """The three settings `VoiceSession` reads off config, with the hold under test."""

    TTS_MAX_QUEUE_CHUNKS = 12
    TTS_MAX_QUEUE_CHARS = 1800
    TTS_MAX_HOLD_MS = 200


@pytest.mark.asyncio
async def test_the_configured_hold_reaches_the_chunker():
    """Not the timer: the timer was always correct. The chunker is what ignored the setting."""
    tts = FakeTTSService()
    await tts.load()

    async def send_json(_msg):
        pass

    session = VoiceSession(tts, send_json, lambda _data: None, config_=HoldConfig())
    assert session.chunker._max_hold_ms == 200


def test_a_short_hold_forces_buffered_text_out_soon():
    chunker = StreamingSpeechChunker(max_hold_ms=50)
    assert chunker.feed("First sentence.", now=100.0) == ["First sentence."]

    chunks = chunker.feed(FILLER, now=100.2)

    assert chunks, "50 ms has passed, so the buffer should be flushed rather than held"
    assert sum(len(c) for c in chunks) >= len(FILLER) - 10


def test_a_long_hold_keeps_buffered_text_back_until_it_elapses():
    chunker = StreamingSpeechChunker(max_hold_ms=5000)
    assert chunker.feed("First sentence.", now=100.0) == ["First sentence."]

    assert chunker.feed(FILLER, now=100.2) == [], "0.2 s into a 5 s hold the text must stay buffered"
    assert chunker.pending_characters == len(FILLER)

    released = chunker.feed("", now=105.0)
    assert released, "the hold has now elapsed; the text cannot stay buffered forever"


def test_the_default_is_unchanged_for_everyone_who_does_not_set_the_knob():
    """The fix passes the config value in; it must not have quietly moved the default."""
    assert StreamingSpeechChunker()._max_hold_ms == 450.0
