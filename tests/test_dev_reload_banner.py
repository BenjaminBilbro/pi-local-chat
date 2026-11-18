"""The hot-reload banner's decisions, checked from pytest.

The assertions live in `tools/test_dev_reload_banner.mjs`, because that is where the decisions are made and
this repo has no JavaScript runner. Running the script from here is what keeps a regression in
`static/devreload.js` from passing the suite unnoticed. It skips honestly where node is absent rather than
counting a claim it did not make.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "test_dev_reload_banner.mjs"
NODE = shutil.which("node")
CLAIMS = 11


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_dev_reload_banner_decisions():
    result = subprocess.run([NODE, str(SCRIPT)], capture_output=True, text=True, timeout=60)

    assert "FAIL" not in result.stdout, result.stdout
    passed, total = result.stdout.strip().splitlines()[-1].split(" ")[0].split("/")
    assert int(passed) == int(total) == CLAIMS, result.stdout
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
