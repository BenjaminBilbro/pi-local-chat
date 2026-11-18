"""The composer's primary button, checked from pytest.

The assertions live in `tools/test_composer_mode.mjs`, because they read the markup and the stylesheet as well
as the module, and that pairing is the point: CSS paints the icon from the same attribute JS dispatches on. It
skips honestly where node is absent rather than counting a claim it did not make.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "test_composer_mode.mjs"
NODE = shutil.which("node")
CLAIMS = 9


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_composer_primary_button_table():
    result = subprocess.run([NODE, str(SCRIPT)], capture_output=True, text=True, timeout=60)

    assert "FAIL" not in result.stdout, result.stdout
    passed, total = result.stdout.strip().splitlines()[-1].split(" ")[0].split("/")
    assert int(passed) == int(total) == CLAIMS, result.stdout
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
