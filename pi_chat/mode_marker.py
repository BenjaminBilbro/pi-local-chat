"""The voice-mode marker, and the one thing the server has to know about it.

pi's RPC has no system-prompt lever a client can pull, so a mode change reaches the model as a *user*
message wrapped in this tag. That is Ben's design, and the writer is `systemEnvelope` in
`static/utils.js`; the browser half of the reader is `isSystemEnvelope` in the same file, which is how
neither direction of the marker is ever drawn in the transcript. The tag travels inside the text rather
than as a field on the command because pi is what echoes the message back, and pi knows nothing about our
fields.

The server needs the same predicate for a different reason. A marker turn is an instruction about *how to
talk*, not something the person asked, so its reply must not be synthesized: the handshake answer costs a
Phonon pass for a sentence nobody asked to hear, and while that stream is open the browser believes the
assistant is speaking, which is what used to throw away the first real sentence of a voice session
(`docs/voice-mode.md`, "Starting up").

Two halves of one contract, deliberately not shared as a generated constant: this file and `utils.js` are
read by different runtimes, and the tag is pinned on both sides by
`tests/test_mode_marker_turn.py::test_the_tag_is_the_same_string_on_both_halves`.
"""

from __future__ import annotations

from typing import Any

MODE_MARKER_TAG = "PI-CHAT-SYSTEM-MESSAGE"

_OPEN = f"<{MODE_MARKER_TAG}>"


def is_mode_marker_text(text: Any) -> bool:
    """True when this text is a mode marker rather than something the person typed.

    Checked at the start of the text, not anywhere inside it, so a message that quotes the tag in a code
    block is still a real message. Same rule as `isSystemEnvelope` on the browser side.
    """
    return isinstance(text, str) and text.lstrip().startswith(_OPEN)


def content_text(content: Any) -> str:
    """The text of a pi message's `content`, which is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""


def is_mode_marker_message(message: Any) -> bool:
    """True for a pi message (`{"role": "user", "content": ...}`) that is a mode marker."""
    if not isinstance(message, dict):
        return False
    return is_mode_marker_text(content_text(message.get("content")))
