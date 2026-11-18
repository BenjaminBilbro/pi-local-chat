"""The debug-mode audit log: what it must never write, and what it must not lose.

Two jobs, and they pull against each other. The log has to be detailed enough that "I pressed mute and it
kept talking" can be checked against it, and shaped enough that leaving the flag on in a house where a family
member talks to this thing is not an incident. So the privacy half of this file is written as a denial test:
put a sentence in, assert no part of it comes out. A positive test for redaction proves nothing, because the
easiest way to pass one is to log less.

The second half is the wiring. Both hooks are in code paths that already swallow their own failures - the
browser outbox drops messages when a tab stops reading, and the voice session closes its echo window on a
timer - so a recorder that silently fails to attach would reproduce the exact blindness this exists to cure.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pi_chat import audit, config


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def audit_on(tmp_path, monkeypatch):
    """Turn the flag on for one test and point the files at a temp directory."""
    monkeypatch.setattr(config, "AUDIT_ENABLED", True)
    monkeypatch.setattr(config, "AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "AUDIT_MAX_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(config, "AUDIT_COALESCE_BYTES", 65536)
    return tmp_path


# ── the flag is the whole feature ────────────────────────────────────


def test_nothing_is_written_unless_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_ENABLED", False)
    monkeypatch.setattr(config, "AUDIT_DIR", str(tmp_path))
    assert audit.open_log("b") is None
    assert list(tmp_path.iterdir()) == []


def test_enabled_names_the_account_and_lands_in_the_directory(audit_on):
    log = audit.open_log("b")
    assert log is not None
    assert log.path.parent == audit_on
    assert log.path.name.startswith("audit-b-")
    log.close()


# ── shapes, not bytes ────────────────────────────────────────────────


def test_a_prompt_never_reaches_the_file(audit_on):
    secret = "the vet said Pinto needs to lose another half kilo by december"
    log = audit.open_log("b")
    log.json_in({"type": "prompt", "message": secret, "delivery": "queue"})
    log.close()

    raw = log.path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "Pinto" not in raw and "vet" not in raw
    line = read_lines(log.path)[-2]
    assert line["data"]["message"].startswith(f"str({len(secret)} ")
    # The machine-state half survives, or the log cannot answer "was it a steer or a queue?"
    assert line["data"]["delivery"] == "queue"


def test_a_short_word_on_an_unclaimed_key_is_still_content(audit_on):
    """`delivery: "queue"` is state and `message: "hi"` is a person.

    Length alone cannot tell those apart, so the key has to. This is the test that keeps a well-meaning
    widening of the rule - "keep strings under 48 characters" - from turning transcripts back on.
    """
    log = audit.open_log("b")
    log.json_in({"type": "prompt", "message": "hi"})
    log.close()
    data = read_lines(log.path)[-2]["data"]
    assert data["message"].startswith("str(2 ")


def test_credentials_are_redacted_not_summarized(audit_on):
    log = audit.open_log("b")
    log.json_in({"type": "login", "password": "hunter2", "token": "abc123"})
    log.close()
    raw = log.path.read_text(encoding="utf-8")
    assert "hunter2" not in raw
    assert "abc123" not in raw
    data = read_lines(log.path)[-2]["data"]
    assert data["password"] == "redacted" and data["token"] == "redacted"


def test_nested_payloads_are_shaped_too(audit_on):
    log = audit.open_log("b")
    log.client_event("click", {"id": "voice-mute", "phase": "speaking",
                               "label": "the last thing the assistant said out loud"})
    log.close()
    data = read_lines(log.path)[-2]["data"]
    assert data["id"] == "voice-mute" and data["phase"] == "speaking"
    assert "assistant" not in json.dumps(data)


# ── volume, or the log stops being readable ──────────────────────────


def test_audio_packets_are_totalled_not_transcribed(audit_on):
    log = audit.open_log("b")
    for _ in range(40):
        log.bytes_in(1024)
    log.event("turn_settled")
    log.close()

    lines = read_lines(log.path)
    totals = [line for line in lines if line["type"] == "totals"]
    assert len(totals) == 1, "a second of microphone is dozens of packets and one interesting line"
    assert totals[0]["data"]["bytes_in"] == 40 * 1024
    assert lines[-2]["type"] == "turn_settled", "the totals land before the event they explain"


def test_a_long_run_rolls_over_instead_of_losing_the_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_ENABLED", True)
    monkeypatch.setattr(config, "AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "AUDIT_COALESCE_BYTES", 1 << 20)
    monkeypatch.setattr(config, "AUDIT_MAX_ROTATIONS", 8)
    log = audit.AuditLog(tmp_path / "audit-b-small.jsonl", "t", "b", max_bytes=600)
    for i in range(500):
        log.event("chatter", index=i)
    log.close()

    first = tmp_path / "audit-b-small.jsonl"
    first_lines = read_lines(first)
    assert len(first_lines) < 500
    assert first_lines[-1]["type"] == "rotating"
    second = tmp_path / "audit-b-small-part2.jsonl"
    assert second.exists(), "the tail of the session is the part someone asks about"

    def indices(path):
        return [(line.get("data") or {}).get("index") for line in read_lines(path)]

    assert max(value for value in indices(second) if value is not None) > \
        max(value for value in indices(first) if value is not None), "part2 continues; it does not restart"


def test_the_rotation_ceiling_still_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_ENABLED", True)
    monkeypatch.setattr(config, "AUDIT_MAX_BYTES", 400)
    monkeypatch.setattr(config, "AUDIT_MAX_ROTATIONS", 2)
    monkeypatch.setattr(config, "AUDIT_COALESCE_BYTES", 1 << 20)
    log = audit.AuditLog(tmp_path / "audit-b-cap.jsonl", "t", "b", max_bytes=400)
    for i in range(400):
        log.event("chatter", index=i)
    log.close()

    files = list(tmp_path.glob("audit-b-cap*.jsonl"))
    assert sorted(path.name for path in files) == [
        "audit-b-cap-part2.jsonl", "audit-b-cap-part3.jsonl", "audit-b-cap.jsonl",
    ], "continuations are named from the first file, so part2 never becomes part2-part2"
    kinds = {line["type"] for path in files for line in read_lines(path)}
    assert "truncated" in kinds, "the backstop has to engage eventually"


def test_one_message_is_one_line(tmp_path, monkeypatch):
    """The per-message totals counter wrote a second line for every streaming delta.

    Half a live session's bytes were a counter restating a line that already existed, which is how a log
    becomes something you skim instead of read.
    """
    monkeypatch.setattr(config, "AUDIT_ENABLED", True)
    monkeypatch.setattr(config, "AUDIT_MAX_BYTES", 1 << 20)
    monkeypatch.setattr(config, "AUDIT_COALESCE_BYTES", 1 << 20)
    log = audit.AuditLog(tmp_path / "audit-b-json.jsonl", "t", "b")
    for i in range(20):
        log.json_out({"type": "pi_event", "event": {"type": "message_update"}})
    log.close()

    lines = read_lines(tmp_path / "audit-b-json.jsonl")
    assert sum(1 for line in lines if line["type"] == "totals") == 0
    assert sum(1 for line in lines if line["type"] == "pi_event") == 20


def test_a_streaming_delta_records_its_kind_and_length_without_its_text(audit_on):
    """The flicker hunt needs the rate of deltas, and nothing more.

    `pi_event` alone describes hundreds of different things. The sub-kind and the character count are state,
    so they are kept; the delta itself is a length and a digest.
    """
    secret = "the muting fix looks good so far"
    log = audit.open_log("b")
    log.json_out({
        "type": "pi_event",
        "event": {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": secret}},
    })
    log.close()

    raw = log.path.read_text(encoding="utf-8")
    assert secret not in raw and "muting" not in raw
    data = read_lines(log.path)[-2]["data"]
    assert data["eventKind"] == "message_update"
    assert data["subKind"] == "text_delta"
    assert data["chars"] == len(secret)


def test_a_failing_log_never_breaks_the_session(tmp_path, monkeypatch):
    """The recorder is a witness, not a dependency."""
    monkeypatch.setattr(config, "AUDIT_ENABLED", True)
    monkeypatch.setattr(config, "AUDIT_MAX_BYTES", 1 << 20)
    log = audit.AuditLog(tmp_path / "audit-b.jsonl", "t", "b")
    log._fh.close()  # the disk went away mid-session
    log.json_in({"type": "prompt"})
    log.bytes_in(4096)
    log.close()  # must not raise


# ── the hooks, which is where a recorder usually goes quietly dead ───


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_bytes(self, payload):
        self.sent.append(payload)


def test_everything_the_browser_receives_is_recorded(audit_on):
    from pi_chat.process import PiProcess

    async def scenario():
        process = PiProcess()
        process.ws = _FakeSocket()
        process.audit = audit.open_log("b")
        await process.send_browser_json({"type": "voice_stream_start", "streamId": 7})
        await process.send_browser_bytes(b"\x00" * 2048)
        process.browser_dead = True
        await process.send_browser_bytes(b"\x00" * 2048)
        process.audit.close()
        return process.audit.path

    path = asyncio.run(scenario())
    kinds = [(line["type"], line["dir"]) for line in read_lines(path)]
    assert ("voice_stream_start", "out") in kinds
    totals = [line for line in read_lines(path) if line["type"] == "totals"]
    assert sum(line["data"]["bytes_out"] for line in totals) == 2048, \
        "the bytes that were dropped must not be counted as delivered"
    assert ("browser_dropped", "event") in kinds, "a silent drop is the finding this whole file is about"


def test_the_voice_window_is_visible_in_the_log(audit_on):
    """The stuck state that goes deaf: the echo window opens and nothing ever closes it.

    Nothing on the wire shows it. `voice_stopped` with `window_open: true` and no matching close is the whole
    diagnosis, and before this hook it was invisible from outside the process.
    """
    from pi_chat.voice_session import VoiceSession

    events = []

    async def send_json(payload):
        return None

    async def send_bytes(payload):
        return None

    session = VoiceSession(
        tts_service=object(),
        send_json=send_json,
        send_bytes=send_bytes,
        config_=config,
        audit=lambda kind, **data: events.append((kind, data)),
    )

    session._append_reference(b"\x00\x01" * 320, 16000)
    assert ("aec_window", True) in [(kind, data.get("open")) for kind, data in events]

    session._append_reference(b"\x00\x01" * 320, 16000)
    opens = [data for kind, data in events if kind == "aec_window" and data.get("open")]
    assert len(opens) == 1, "the window event is a transition, not a per-chunk tick"

    session.on_playback_end(session._stream_id)
    closes = [data for kind, data in events if kind == "aec_window" and data.get("open") is False]
    assert closes and closes[-1]["why"] == "playback_end"

    asyncio.run(session.stop_current(reason="stopped"))
    stopped = [data for kind, data in events if kind == "voice_stopped"]
    assert stopped and stopped[-1]["reason"] == "stopped" and stopped[-1]["window_open"] is False


def test_state_words_the_server_writes_about_itself_survive_the_shaping(audit_on):
    """A machine-state token that arrives as a digest is a field nobody can read.

    The voice hooks pass `why` and `error` words that mean nothing except as words, and the recorder shapes
    every string it has not been vouched for. So the vocabulary these hooks emit is part of the log format,
    and a new token has to be claimed in `SAFE_KEYS` or the file quietly degrades into hashes. This goes
    through a real `AuditLog` rather than a collector for exactly that reason: the other voice test in this
    file passed while the shaping was eating `why`.
    """
    from pi_chat.voice_session import VoiceSession

    async def send_json(payload):
        return None

    async def send_bytes(payload):
        return None

    log = audit.open_log("b")
    session = VoiceSession(
        tts_service=object(), send_json=send_json, send_bytes=send_bytes,
        config_=config, audit=lambda kind, **data: log.event(kind, **data),
    )
    session._append_reference(b"\x00\x01" * 320, 16000)
    session.on_playback_end(session._stream_id)
    log.close()

    raw = log.path.read_text(encoding="utf-8")
    assert "reference_buffered" in raw
    assert "playback_end" in raw


def test_a_session_without_a_recorder_is_unaffected(audit_on):
    from pi_chat.voice_session import VoiceSession

    async def send_json(payload):
        return None

    async def send_bytes(payload):
        return None

    session = VoiceSession(tts_service=object(), send_json=send_json, send_bytes=send_bytes, config_=config)
    session._append_reference(b"\x00\x01" * 320, 16000)
    session.on_playback_end(session._stream_id)
    assert session.is_speaking is False
