"""S3: the sessions list's current-row and delete behaviour, checked from pytest.

The claims live in `tools/test_session_list_state.mjs`, next to the module they describe. This is what puts
them in the suite: the defect Ben reported was a row that stayed greyed forever, which is invisible to every
Python test in the repository, and a delete affordance that did not exist at all.

Skips honestly when node or jsdom is missing rather than passing silently. The script reads the real
`static/index.html`, so a renamed id fails here rather than in the browser.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test_session_list_state.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (ROOT / "node_modules" / "jsdom").is_dir(),
    reason="needs node and `npm ci` (jsdom)",
)


def test_session_list_state():
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
    assert len(claims) == 12, f"expected 12 claims, the script changed shape:\n{result.stdout}"
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
