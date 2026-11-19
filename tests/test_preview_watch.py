"""The preview pane's watcher logic, as a pure-logic harness.

`tools/test_preview_watch.mjs` imports `static/preview-watch.js` directly. The claims are about timing
that no screenshot can show: a token caught mid-write must not reload, a hidden page must not poll,
a background tab must call itself stale, and a stored copy must never be watched.

Skips when node is missing rather than failing a machine with no toolchain. No jsdom needed: the
decisions were deliberately kept out of the DOM half.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "test_preview_watch.mjs"
NODE = shutil.which("node")
CLAIMS = 16

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")


def test_preview_watch_logic():
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


def test_the_pane_imports_the_watchers_decisions_rather_than_reinventing_them():
    """`static/preview.js` must defer to the module under test, not copy its rules.

    A duplicated rule is how a fix in one place becomes a lie in another, which this repo has already
    paid for once (the same gotcha lived in three files before `AGENTS.md` was cut down).
    """
    pane = (ROOT / "static" / "preview.js").read_text(encoding="utf-8")

    assert "from './preview-watch.js'" in pane
    for imported in ("shouldPoll", "observeToken", "tabFreshness", "watchingFor", "freshnessQuery"):
        assert imported in pane, f"preview.js no longer uses {imported}"
