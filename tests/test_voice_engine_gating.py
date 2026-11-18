"""The voice settings dialog vs the engine that is actually running (S2).

`tools/test_voice_engine_gating.mjs` drives the real `static/index.html` in jsdom and hands the dialog each
engine's capabilities answer, because an install can only ever report one and both directions need pinning.
Two bugs live behind this: a list of OmniVoice ids that the markup had outgrown, and `hidden` losing to
`.voice-setting-label { display: flex }`, which made the gate invisible from the browser's side while the JS
looked correct.

Skips when node or jsdom is missing rather than failing a machine that has no toolchain.
"""

from __future__ import annotations

import shutil
import subprocess

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test_voice_engine_gating.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (ROOT / "node_modules" / "jsdom").is_dir(),
    reason="needs node and `npm ci` (jsdom)",
)


def test_voice_engine_gating():
    result = subprocess.run(
        [NODE, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=ROOT,
    )
    claims = [line for line in result.stdout.splitlines() if line.startswith(("PASS ", "FAIL "))]

    assert not [line for line in claims if line.startswith("FAIL")], result.stdout
    assert len(claims) == 10, f"expected 10 claims, the script changed shape:\n{result.stdout}"
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
