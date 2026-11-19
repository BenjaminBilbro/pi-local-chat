"""The one number mute and the server have to agree on, across the language boundary.

Mute closes the frame gate in `static/stt.js`. It does not tear anything down, so an utterance that was
already open when the button was pressed is still sitting in the server's recorder, waiting for the silence
that tells it the speaker has stopped: `post_speech_silence_duration`, which is
`PI_CHAT_STT_POST_SPEECH_SILENCE` and reaches the recorder in `pi_chat/stt_session.py`.

So mute hands the recorder a burst of silence before it closes the gate. That burst has to be longer than
the server's window, and if someone raises the window the burst stops being long enough and a finished
sentence is stranded with no error anywhere - which is the exact defect Ben reported from a live test on
2026-11-19 ("if im speaking and finish speaking, but mute before it gets sent to the model, the entire
message goes away"). A constant in a JavaScript file and a float in a Python module will never be checked
against each other by the compiler, so this is where they meet.

The behavioural half of the contract lives in `tools/test_voice_mode_surface.mjs` section 8, and the
end-to-end half in `tools/voice_mic_rig.mjs` (a file played as a microphone). This file is only the number.
"""

from __future__ import annotations

import re
from pathlib import Path

from pi_chat import config

ROOT = Path(__file__).resolve().parent.parent
STT_JS = ROOT / "static" / "stt.js"

FLUSH = re.compile(r"const MUTE_FLUSH_MS = (\d+);")


def test_the_mute_flush_outlasts_the_servers_silence_window() -> None:
    source = STT_JS.read_text(encoding="utf-8")
    match = FLUSH.search(source)
    assert match, (
        "static/stt.js no longer defines MUTE_FLUSH_MS. Mute has to feed the server's endpointer its "
        "end-of-utterance silence before it closes the gate, or a sentence finished at the moment of "
        "muting never becomes a message."
    )
    flush_ms = int(match.group(1))
    window_ms = config.STT_SILENCE_DURATION * 1000

    assert flush_ms > window_ms, (
        f"PI_CHAT_STT_POST_SPEECH_SILENCE is {config.STT_SILENCE_DURATION}s ({window_ms:.0f} ms) and the "
        f"mute flush is {flush_ms} ms. The recorder will not have heard enough silence to close the "
        "utterance, so muting while a sentence is in flight loses it. Raise MUTE_FLUSH_MS in static/stt.js."
    )
    # A margin, not just a strict inequality: the window is read at server start and the constant is not,
    # so a deployment can raise the knob above a flush that passes this test on the default.
    assert flush_ms >= window_ms + 200, (
        f"the mute flush ({flush_ms} ms) clears the silence window ({window_ms:.0f} ms) by less than 200 ms. "
        "Give the endpointer room; the cost of the burst is a few kilobytes of zeros."
    )


def test_the_flush_is_not_so_long_that_mute_feels_delayed() -> None:
    """The burst is built and sent inside the click handler, so it is work done before the button responds."""
    flush_ms = int(FLUSH.search(STT_JS.read_text(encoding="utf-8")).group(1))
    assert flush_ms <= 3000, f"a {flush_ms} ms mute flush is work the click does before it responds"
