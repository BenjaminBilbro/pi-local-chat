"""The transcript-follow rules, checked from pytest.

The assertions live in `tools/test_stream_follow.mjs`, because the state machine is JavaScript and the failure
modes are felt rather than seen: a follow that never resumes strands the reader mid-answer. It skips honestly
where node is absent rather than counting a claim it did not make.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "test_stream_follow.mjs"
NODE = shutil.which("node")
CLAIMS = 10


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_stream_follow_rules():
    result = subprocess.run([NODE, str(SCRIPT)], capture_output=True, text=True, timeout=60)

    assert "FAIL" not in result.stdout, result.stdout
    passed, total = result.stdout.strip().splitlines()[-1].split(" ")[0].split("/")
    assert int(passed) == int(total) == CLAIMS, result.stdout
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
