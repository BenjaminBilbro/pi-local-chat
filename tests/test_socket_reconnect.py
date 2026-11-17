"""FE-13: the browser's reconnect schedule, checked from pytest.

The assertions live in `tools/test_socket_reconnect.mjs`, because that is where the schedule is
decided and this repo has no JS runner. Running the script from here means a regression in
`static/socket.js` fails the suite like a Python regression does. It skips honestly when node is not
installed rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "test_socket_reconnect.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_socket_reconnect_schedule():
    result = subprocess.run(
        [NODE, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert "FAIL" not in result.stdout, result.stdout
    passed, total = result.stdout.strip().splitlines()[-1].split(" ")[0].split("/")
    assert int(passed) == int(total) == 7, result.stdout
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
