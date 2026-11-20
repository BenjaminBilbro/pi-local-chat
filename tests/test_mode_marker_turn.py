"""A voice-mode marker turn is answered and never spoken.

The mode marker is how the model is told to answer in speech, since pi's RPC has no system-prompt lever a
client can pull. It costs a turn, and while that turn ran the browser believed the assistant was speaking,
which is what threw away the first sentence of a fresh voice session (Ben, 2026-11-19, and
`docs/voice-mode.md` "Starting up"). The fix is that a marker turn gets no audio at all, and these tests are
the two directions of that claim:

* the handshake's own reply is never synthesized, and never opens a stream;
* an answer that is already being heard is never silenced by a marker arriving next to it.

Uses FakeTTSService; no torch, no GPU.
"""

import asyncio
from pathlib import Path

import pytest

from pi_chat.mode_marker import (
    MODE_MARKER_TAG,
    content_text,
    is_mode_marker_message,
    is_mode_marker_text,
)
from pi_chat.voice_session import VoiceSession
from tests.fakes.voice import FakeTTSService, VoiceSettings

REPO_ROOT = Path(__file__).resolve().parents[1]


def envelope(body: str) -> str:
    """The exact shape `systemEnvelope` in static/utils.js writes."""
    return f"<{MODE_MARKER_TAG}>\n{body}\n</{MODE_MARKER_TAG}>"


USER_MARKER = {"type": "message_start", "message": {"role": "user", "content": envelope("Voice mode is now ON.")}}
USER_REAL = {"type": "message_start", "message": {"role": "user", "content": "What day is it?"}}
ASSISTANT_START = {"type": "message_start", "message": {"role": "assistant", "content": []}}


def text_events(sentence: str):
    return [
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": sentence}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": sentence}},
    ]


class FakeConfig:
    TTS_MAX_QUEUE_CHUNKS = 12
    TTS_MAX_QUEUE_CHARS = 1800
    TTS_MAX_HOLD_MS = 100


@pytest.fixture
async def captured():
    return {"json": [], "bytes": []}


@pytest.fixture
async def voice(captured):
    tts = FakeTTSService()
    await tts.load()

    async def send_json(msg):
        captured["json"].append(msg)

    async def send_bytes(data):
        captured["bytes"].append(data)

    session = VoiceSession(tts, send_json, send_bytes, config_=FakeConfig())
    session.enabled = True
    session.ready = True
    session.voice_handle = await tts.prepare_voice(VoiceSettings())
    return session


def stream_starts(captured):
    return [m for m in captured["json"] if m.get("type") == "voice_stream_start"]


async def speak(voice, captured, sentence="Two plus two is four. Anything else?"):
    for event in text_events(sentence):
        await voice.observe_pi_event(event)
    await asyncio.sleep(0.25)
    return len(captured["bytes"])


# ── the predicate ───────────────────────────────────────────────────────


class TestMarkerPredicate:
    def test_an_envelope_is_a_marker(self):
        assert is_mode_marker_text(envelope("Voice mode is now ON."))

    @pytest.mark.parametrize("text", [
        "What day is it?",
        "",
        "   ",
        None,
        17,
        # Quoting the tag is not the same as being the tag: the check is at the start of the text, so a
        # message that explains the marker, or shows it in a code block, stays a real message.
        f"I was sent this: {envelope('Voice mode is now ON.')}",
    ])
    def test_anything_else_is_a_message(self, text):
        assert not is_mode_marker_text(text)

    def test_leading_whitespace_still_counts(self):
        assert is_mode_marker_text(f"\n  {envelope('Voice mode is now OFF.')}")

    def test_content_blocks_are_read_not_just_a_string(self):
        assert is_mode_marker_message({
            "role": "user",
            "content": [{"type": "text", "text": envelope("Voice mode is now ON.")}],
        })
        assert not is_mode_marker_message({
            "role": "user",
            "content": [{"type": "text", "text": "What day is it?"}],
        })
        assert content_text([{"type": "text", "text": "a"}, {"type": "toolResult"}, {"type": "text", "text": "b"}]) == "a\nb"

    @pytest.mark.parametrize("message", [None, "x", {"role": "user"}, {"role": "user", "content": None}])
    def test_junk_shaped_like_a_message_is_not_a_marker(self, message):
        assert not is_mode_marker_message(message)

    def test_the_tag_is_the_same_string_on_both_halves(self):
        """The tag is the whole contract between the writer in the browser and this reader.

        Nothing generates it, so a rename on one side would silently start synthesizing handshake turns (or
        silently stop drawing markers) with no test failing but this one.
        """
        source = (REPO_ROOT / "static" / "utils.js").read_text(encoding="utf-8")
        assert f"export const SYSTEM_ENVELOPE_TAG = '{MODE_MARKER_TAG}';" in source


# ── the handshake turn gets no audio ────────────────────────────────────


@pytest.mark.asyncio
async def test_an_armed_marker_turn_opens_no_stream_and_synthesizes_nothing(voice, captured):
    """The normal startup order: browser submits a marker, so the run is armed before `agent_start`."""
    voice.arm_mode_marker_turn()
    await voice.observe_pi_event({"type": "agent_start"})

    assert stream_starts(captured) == [], "a stream_start alone sets the browser's `speaking`"
    assert not voice.active_run

    await speak(voice, captured, "Listening. Go ahead.")
    assert captured["bytes"] == []


@pytest.mark.asyncio
async def test_a_delivered_marker_is_enough_even_without_the_hint(voice, captured):
    """The delivered message is the authority, not the arming hint.

    This is the order that would otherwise be wrong: a run starts, pi delivers the prompt it is answering,
    and only then does the reply begin. Nothing has been synthesized yet, so the stream that was opened on
    the assumption of an answer is closed rather than sat on.
    """
    await voice.observe_pi_event({"type": "agent_start"})
    await voice.observe_pi_event(USER_MARKER)
    assert voice.suppress_current_run

    await voice.observe_pi_event(ASSISTANT_START)
    await speak(voice, captured, "Standing by.")
    assert captured["bytes"] == []


@pytest.mark.asyncio
async def test_the_turn_after_the_handshake_is_spoken(voice, captured):
    """Suppression is one turn wide: the person's own question is answered out loud."""
    voice.arm_mode_marker_turn()
    await voice.observe_pi_event({"type": "agent_start"})
    await voice.observe_pi_event(USER_MARKER)
    await voice.observe_pi_event(ASSISTANT_START)
    await speak(voice, captured, "Listening. Go ahead.")
    await voice.observe_pi_event({"type": "agent_settled"})
    assert captured["bytes"] == []

    await voice.observe_pi_event({"type": "agent_start"})
    await voice.observe_pi_event(USER_REAL)
    await voice.observe_pi_event(ASSISTANT_START)
    assert not voice.suppress_current_run
    assert stream_starts(captured) != []
    assert await speak(voice, captured, "It is Thursday.") > 0


# ── and an answer already being heard is never silenced ─────────────────


@pytest.mark.asyncio
async def test_a_marker_queued_behind_an_answer_does_not_silence_that_answer(voice, captured):
    """The exit case, and the reason suppression is taken from the delivery and not the submission.

    Leaving voice mode sends the OFF marker, which pi delivers while the answer to the previous question is
    still streaming its text. Silencing whatever is in flight would have traded one swallowed sentence for
    another, so a marker only ever claims the assistant message that comes after it.
    """
    # Deliberately not armed: this is the OFF marker arriving beside a running answer, not the handshake.
    await voice.observe_pi_event({"type": "agent_start"})
    await voice.observe_pi_event(USER_REAL)
    await voice.observe_pi_event(ASSISTANT_START)
    before = await speak(voice, captured, "It is Thursday, the nineteenth of November.")
    assert before > 0, "the answer has to be audible for this test to mean anything"
    assert voice._sequence > 0

    await voice.observe_pi_event(USER_MARKER)
    assert not voice.suppress_current_run, "audio already went out on this stream; it is being heard"

    after = await speak(voice, captured, "And the weather is mild.")
    assert after > before, "the rest of the answer is still synthesized"


@pytest.mark.asyncio
async def test_a_real_message_clears_a_marker_turn(voice, captured):
    voice.arm_mode_marker_turn()
    await voice.observe_pi_event({"type": "agent_start"})
    await voice.observe_pi_event(USER_MARKER)
    assert voice._marker_turn

    await voice.observe_pi_event(USER_REAL)
    assert not voice._marker_turn
    await voice.observe_pi_event(ASSISTANT_START)
    assert not voice.suppress_current_run


# ── the route that arms it ──────────────────────────────────────────


class TestPromptRoute:
    """`agent_start` carries no text, so the browser's prompt command is the only moment the server can learn
    a run is a handshake. These two claims are that wire, and the reason it must not fire for prose."""

    @staticmethod
    def _pi():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        return SimpleNamespace(proc=object(), send=AsyncMock())

    @staticmethod
    def _voice():
        from types import SimpleNamespace

        armed = []
        return SimpleNamespace(armed=armed, arm_mode_marker_turn=lambda: armed.append(1)), armed

    @pytest.mark.asyncio
    async def test_a_marker_prompt_arms_the_hint(self):
        from pi_chat.websocket import _handle_prompt

        pi = self._pi()
        voice, armed = self._voice()
        await _handle_prompt(pi, {"message": envelope("Voice mode is now ON."), "delivery": "queue"}, "b", voice=voice)

        assert armed == [1]
        assert pi.send.await_args.args[0]["message"].startswith(f"<{MODE_MARKER_TAG}>")

    @pytest.mark.asyncio
    async def test_the_persons_own_prompt_does_not(self):
        """Otherwise every message in a voice session would be silent, which is the opposite defect."""
        from pi_chat.websocket import _handle_prompt

        pi = self._pi()
        voice, armed = self._voice()
        await _handle_prompt(pi, {"message": "What day is it?", "delivery": "queue"}, "b", voice=voice)

        assert armed == []
        assert pi.send.await_args.args[0]["message"] == "What day is it?"

    @pytest.mark.asyncio
    async def test_no_voice_session_is_not_an_error(self):
        """TTS off, or a connection that never asked for voice: a marker is still a prompt that has to go out."""
        from pi_chat.websocket import _handle_prompt

        pi = self._pi()
        await _handle_prompt(pi, {"message": envelope("Voice mode is now ON."), "delivery": "queue"}, "b", voice=None)
        assert pi.send.await_args.args[0]["message"].startswith(f"<{MODE_MARKER_TAG}>")


@pytest.mark.asyncio
async def test_arm_is_idempotent_and_consumed_once(voice, captured):
    """Arming twice is one handshake; and the hint must not survive the run it was set for."""
    voice.arm_mode_marker_turn()
    voice.arm_mode_marker_turn()
    await voice.observe_pi_event({"type": "agent_start"})
    assert voice.suppress_current_run
    assert not voice._marker_armed

    await voice.observe_pi_event({"type": "agent_start"})
    assert not voice.suppress_current_run
    assert stream_starts(captured) != []
