"""The layout sampler's rules, checked from pytest.

The assertions live in `tools/test_geometry_watch.mjs`: the module decides when to sample, when to speak and
when to shut up, and those decisions are only reachable from JavaScript. It skips honestly where node is
absent rather than counting a claim it did not make.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "test_geometry_watch.mjs"
NODE = shutil.which("node")
CLAIMS = 10


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_geometry_watch_rules():
    result = subprocess.run([NODE, str(SCRIPT)], capture_output=True, text=True, timeout=60)

    assert "FAIL" not in result.stdout, result.stdout
    passed, total = result.stdout.strip().splitlines()[-1].split(" ")[0].split("/")
    assert int(passed) == int(total) == CLAIMS, result.stdout
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
