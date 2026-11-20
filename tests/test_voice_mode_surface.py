"""Voice Mode: the surface, the mode markers, and the interrupt gate (S9).

`tools/test_voice_mode_surface.mjs` drives the real `static/index.html` in jsdom with fake `stt` and `voice`
controllers. The two claims worth reading are the ones about the injected marker: it is a user turn the person
did not type, so it has to stay out of the transcript on both paths that draw user messages, pi's mid-run echo
and a session reload. A reloaded session that printed the whole speaking-style instruction as if Ben had said
it would be worse than never injecting it.

The gate claims are arithmetic rather than audio on purpose: `bargeDecision` is exported precisely so the level
and the hold can be argued with here. What the microphone actually hears while the assistant is talking is
`tools/barge_in_echo.mjs`, which needs a browser and a speaker, and `tests/test_barge_in_echo.py` skips it
unless `PI_CHAT_ECHO_TEST=1` says a speaker is plugged in and nobody will be surprised.

Skips when node or jsdom is missing rather than failing a machine that has no toolchain.
"""

from __future__ import annotations

import shutil
import subprocess

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test_voice_mode_surface.mjs"
CLAIMS = 54  # 40 through S9, +5 mute contract, +9 on 2026-11-19 for the startup handshake and Loading.

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (ROOT / "node_modules" / "jsdom").is_dir(),
    reason="needs node and `npm ci` (jsdom)",
)


def test_voice_mode_surface():
    result = subprocess.run(
        [NODE, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        cwd=ROOT,
    )
    claims = [line for line in result.stdout.splitlines() if line.startswith(("PASS ", "FAIL "))]

    assert not [line for line in claims if line.startswith("FAIL")], result.stdout
    assert len(claims) == CLAIMS, f"expected {CLAIMS} claims, the script changed shape:\n{result.stdout}"
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
