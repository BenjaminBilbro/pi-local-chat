"""The preview column as a permanent part of the layout (S6).

`tools/test_preview_pane_state.mjs` drives the real `static/index.html` in jsdom. The behaviour worth
defending is the boring kind: the column opens empty instead of refusing to, it stops being `inert` only while
it is on screen, a remembered collapse stays remembered, a narrow window never gets a full-screen sheet
dropped on it at load, and closing the last document does not take the column away with it.

Skips when node or jsdom is missing rather than failing a machine that has no toolchain.
"""

from __future__ import annotations

import shutil
import subprocess

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test_preview_pane_state.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (ROOT / "node_modules" / "jsdom").is_dir(),
    reason="needs node and `npm ci` (jsdom)",
)


def test_preview_pane_state():
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
