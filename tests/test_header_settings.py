"""The header's contents and the Settings dialog that absorbed what left it (S1).

`tools/test_header_settings.mjs` drives the real `static/index.html` in jsdom. The claim worth defending is
the ordered bar: it is the one place where "we are not adding more chrome" has to be written down, because
the alternative is a slow accretion of icon buttons that nobody decided on.

Skips when node or jsdom is missing rather than failing a machine that has no toolchain.
"""

from __future__ import annotations

import shutil
import subprocess

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test_header_settings.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (ROOT / "node_modules" / "jsdom").is_dir(),
    reason="needs node and `npm ci` (jsdom)",
)


def test_header_and_settings_dialog():
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
    assert len(claims) == 15, f"expected 14 claims, the script changed shape:\n{result.stdout}"
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
