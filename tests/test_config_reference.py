"""`docs/configuration.md` may not drift away from the code that reads the environment (`BE-05`).

Three separate failures are being prevented here, and they are the three ways this repository's
documentation has lied before:

  1. The generated table going stale after a knob is added, renamed or deleted. Checked by running the
     generator itself in `--check` mode, so the test cannot disagree with the tool about what "current" means.
  2. Prose naming a knob that no longer exists, which is what a reader follows into a dead end. Checked by
     extracting every `PI_CHAT_*` token from the document and requiring it to be a name the application can
     actually read.
  3. The child-side table describing a boundary that has moved. The generator refuses to run when a
     `CHILD_SIDE` name stops appearing in `pi_chat/process.py`, so this test inherits that check for free by
     invoking `--check`.

`.env.example` was deleted on 2026-11-17 rather than maintained in parallel with this document: a template
file with five commented lines out of 52 names is how people ended up guessing.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from pi_chat import config
from tests.doc_policy import documented_non_knobs

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "configuration.md"
TOOL = ROOT / "tools" / "config_reference.py"

NAME = re.compile(r"\bPI_CHAT_[A-Z0-9_]+\b")


def test_configuration_table_matches_the_code():
    result = subprocess.run(
        [sys.executable, str(TOOL), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "docs/configuration.md is stale. Fix it by running:\n"
        f"  {sys.executable} tools/config_reference.py\n"
        f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
    )
    assert "table matches" in result.stdout


def test_document_only_names_knobs_that_exist():
    documented = set(NAME.findall(DOC.read_text(encoding="utf-8")))
    excused = set(documented_non_knobs(DOC))
    unknown = sorted(name for name in documented if name not in config.known_env_names() and name not in excused)
    assert not unknown, (
        f"docs/configuration.md names {', '.join(unknown)}, which nothing reads. If the knob was deleted, "
        "delete the sentence; if it was renamed, the table regenerates itself and the prose does not."
    )


def test_a_documented_typo_states_why_it_is_there():
    """The escape hatch is only honest if it says what it is excusing.

    An undocumented name in the document is a stale claim. An excused one has to carry a reason, or the
    marker becomes a way to hide future mistakes instead of a way to describe a deliberate example.
    """
    reasons = documented_non_knobs(DOC)
    reasonless = sorted(name for name, reason in reasons.items() if reason == "no reason given")
    assert not reasonless, f"documented-non-knob markers with no reason: {', '.join(reasonless)}"


def test_every_knob_the_code_reads_is_documented():
    """A knob nobody documented is a knob nobody will find, which is half of what `BE-05` was about."""
    documented = set(NAME.findall(DOC.read_text(encoding="utf-8")))
    # `known_env_names()` includes names that only appear inside this test suite's own fixtures; the
    # generator is the authority on what the application reads, so the two are reconciled here rather than
    # by widening the scan to include tests, which would register typos as real settings.
    generated = subprocess.run(
        [sys.executable, str(TOOL), "--print"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    missing = sorted(set(NAME.findall(generated)) - documented)
    assert not missing, f"knobs read by the application but absent from the docs: {', '.join(missing)}"


def test_generated_section_has_its_markers():
    text = DOC.read_text(encoding="utf-8")
    assert "<!-- knobs:start -->" in text and "<!-- knobs:end -->" in text, (
        "The markers are what makes the table regenerable. Removing them turns the table into hand-written "
        "prose that will be wrong by the next refactor."
    )
    assert text.index("<!-- knobs:start -->") < text.index("<!-- knobs:end -->")


@pytest.mark.parametrize("marker", ["<!-- knobs:start -->", "<!-- knobs:end -->"])
def test_markers_appear_exactly_once(marker):
    text = DOC.read_text(encoding="utf-8")
    assert text.count(marker) == 1, f"{marker} appears {text.count(marker)} times; the splice needs one"
