"""The image pane's state machine, as a jsdom harness.

`tools/test_preview_image.mjs` drives the real `static/preview.js` against the real markup: an entry
that says `rendering`, the tool's progress updates, then the entry that says `ready`. That sequence is
the whole feature, and none of it is visible in a screenshot of a finished picture.

Skips when node is missing rather than failing a machine with no toolchain.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test_preview_image.mjs"
NODE = shutil.which("node")
CLAIMS = 25

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")


def test_image_pane_claims():
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
    assert len(claims) == CLAIMS, f"expected {CLAIMS} claims, the script changed shape:\n{result.stdout}"
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_progress_reaches_the_pane_through_app_js():
    """The pipe has to exist, or the pane holds a document it can never update.

    Progress is deliberately not a persisted entry, so `entry_appended` is not a route to it and there is
    no other caller. This is the one line that connects sd-cli's stdout to a percent on screen.
    """
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "message.event.type === 'tool_execution_update'" in app
    assert "docPane.toolUpdate(message.event)" in app, "app.js stopped forwarding tool progress to the pane"


def test_the_pane_owns_the_decision_about_belonging():
    """app.js must not start deciding which tool updates matter.

    Only the pane knows that `details.doc` names a tab it is holding. Duplicating that test in the
    transport layer is how an update gets dropped for a document the pane is perfectly able to draw.
    """
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    pane = (ROOT / "static" / "preview.js").read_text(encoding="utf-8")

    assert "details" not in app.split("tool_execution_update")[1][:200], (
        "app.js is inspecting the payload it forwards; the pane decides"
    )
    assert "partialResult" in pane, "the pane is no longer the one reading details.doc"
