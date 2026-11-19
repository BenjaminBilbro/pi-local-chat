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
CLAIMS = 19

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


def _load_active_body() -> str:
    """The source of `loadActive`, the only function a tab switch and a poll reload both render through."""
    pane = (ROOT / "static" / "preview.js").read_text(encoding="utf-8")
    start = pane.index("function loadActive(")
    end = pane.index("function showView(", start)
    return pane[start:end]


def test_a_forced_reload_renders_the_object_it_cleared():
    """`loadActive(true)` has to hand `renderMarkdown` the object it just wrote to the map.

    The shipped version cleared the cached body on a copy put in the map, then rendered the reference
    read before that write, so `renderMarkdown` found `raw` still present, re-painted the bytes already
    on screen, never fetched, and the poll went on reporting "Reloaded from disk". Against a file whose
    revision never moves that made live edits land on every other write (2026-11-19).
    """
    body = _load_active_body()

    assert "docs.set(doc.id, target)" in body, "the forced copy is no longer what the map holds"
    assert (
        "renderMarkdown(target" in body
    ), "a forced reload renders an object other than the one whose cache it cleared"


def test_switching_tabs_moves_which_view_is_visible():
    """A tab switch has to change which view element is on top, not only repaint inside the current one.

    `select()` never goes through `show()`, so `loadActive` is the only place that decision can live.
    Without it, an HTML document that put the frame on top keeps covering the markdown a tab click just
    painted into a hidden element, and every markdown tab looks like it is being ignored.
    """
    body = _load_active_body()

    assert "showView(" in body, "loadActive no longer decides which view is visible"
