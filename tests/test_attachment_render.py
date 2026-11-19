"""The attachment block and the bubble it builds, checked from pytest.

The assertions live in `tools/test_attachment_render.mjs`, because the thing at risk is a DOM shape:
an attached document has no bytes in the session file, so its only record is the `<file>` tag the
server put in the prompt text, and both the live echo and the replay have to read that tag to draw the
same bubble. Python cannot see that disagreement and `compare_render.py` compares assistant DOM only,
which is why user bubbles needed their own check. It skips honestly where node is absent.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "test_attachment_render.mjs"
NODE = shutil.which("node")
CLAIMS = 19


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_attachment_parsing_and_bubble_parity():
    result = subprocess.run([NODE, str(SCRIPT)], capture_output=True, text=True, timeout=60)

    assert "FAIL" not in result.stdout, result.stdout
    passed, total = result.stdout.strip().splitlines()[-1].split(" ")[0].split("/")
    assert int(passed) == int(total) == CLAIMS, result.stdout
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
