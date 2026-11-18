"""S5: the tool dialog's cancel control, checked from pytest.

The claims live in `tools/test_tool_detail_cancel.mjs`, next to the module they describe, because this repo
has no JS runner. Running the script from here is what makes a regression in `static/toolDetail.js` fail the
suite instead of failing quietly in the browser, which is how it shipped: the dialog cached a DOM row,
settlement rebuilt the timeline, and the button sat on "Stopping the run" forever because it kept re-rendering
a node that was no longer in the document.

Skips honestly when node or jsdom is missing rather than passing silently. The script reads the real
`static/index.html`, so a renamed id fails here rather than in the app.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test_tool_detail_cancel.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (ROOT / "node_modules" / "jsdom").is_dir(),
    reason="needs node and `npm ci` (jsdom)",
)


def test_tool_detail_cancel_control():
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
