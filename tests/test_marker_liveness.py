"""`QA-12`: a pytest marker that no test carries is a lie told by four files at once.

`audio_gpu` lived for months as exactly that. It was declared in `pyproject.toml`, deselected in the CI pytest
invocation, quoted in the card's own test command and in the fresh-clone verifier, and carried by zero tests.
Four documents agreeing about a lane of the suite that does not exist, and invisible in exactly the way that
matters: deselecting nothing looks identical to deselecting a lane nobody wrote, so the green check said the
same thing before and after the tests it was filtering stopped existing.

Removed 2026-11-17 rather than filled in: writing a GPU test to justify the marker would have needed the GPU
that strata holds, and an unrunnable test is the same fiction with more lines. This file is what keeps the
fiction from coming back, in both directions.
"""

from __future__ import annotations

import re
import tomllib

import pytest

from pi_chat import config

PYPROJECT = config.PROJECT_ROOT / "pyproject.toml"
TEST_DIR = config.PROJECT_ROOT / "tests"

# Marks provided by pytest and its plugins, so they are used without being declared here. Anything else has to
# appear in `[tool.pytest.ini_options] markers`, because a misspelled marker is not an error: pytest warns and
# then runs the test in the default lane, which is how a "slow, GPU only" test ends up in a 50 second suite.
PLUGIN_MARKS = {
    "asyncio", "parametrize", "skip", "skipif", "xfail", "usefixtures",
    "filterwarnings", "timeout", "no_cover", "tryfirst", "trylast",
}


def declared_markers() -> list[str]:
    """The names in `[tool.pytest.ini_options] markers`, before the colon that starts the description."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    entries = data["tool"]["pytest"]["ini_options"]["markers"]
    return [entry.split(":", 1)[0].strip() for entry in entries]


def marks_used_in_tests() -> set[str]:
    """Every `pytest.mark.<name>` written anywhere under `tests/`, decorator or `pytestmark`."""
    used: set[str] = set()
    for path in sorted(TEST_DIR.rglob("*.py")):
        used.update(re.findall(r"pytest\.mark\.([A-Za-z_][A-Za-z0-9_]*)", path.read_text(encoding="utf-8")))
    return used


def test_every_declared_marker_is_carried_by_at_least_one_test():
    carried = marks_used_in_tests()
    uncarried = sorted(name for name in declared_markers() if name not in carried)

    assert uncarried == [], (
        f"markers declared and never used: {uncarried}. Delete them, or write the test; a declaration that no "
        "test carries gets deselected in CI, quoted in the card, and believed by everyone who reads either"
    )


def test_every_marker_used_on_a_test_is_declared():
    declared = set(declared_markers())
    undeclared = sorted(
        name for name in marks_used_in_tests() if name not in declared and name not in PLUGIN_MARKS
    )

    assert undeclared == [], (
        f"marks that no marker declaration covers: {undeclared}. pytest only warns about these, then runs the "
        "test in the default lane, so a typo turns a slow test into a normal one"
    )


def test_the_audio_gpu_marker_stays_gone():
    """The specific removal, pinned.

    Not paranoia: `audio_gpu` reads like a reasonable thing to have, so it is exactly the kind of name someone
    re-adds while solving a different problem, and the fix is one line in a file nobody reviews.
    """
    assert "audio_gpu" not in PYPROJECT.read_text(encoding="utf-8")
