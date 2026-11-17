"""`BE-03`: a `PI_CHAT_*` name nobody reads has to stop being invisible.

Two things make this worth a test file rather than a function. The known-name set is derived from the
sources, so a test is the only thing that notices when the derivation stops covering a place names are read
(and `PI_CHAT_PREVIEW_DIR`, being TypeScript, is exactly such a place), or starts covering a place it must
not (its own fixtures, which happened). And the docs claim knobs exist, which is a claim that can rot: the
live documentation is held to it and the dated snapshots are not.
"""

from __future__ import annotations

import logging

import pytest

from pi_chat import config
from tests.doc_policy import documented_non_knobs as _documented_non_knobs

LIVE_DOCS = [
    "README.md",
    "AGENTS.md",
    "docs/architecture.md",
    "docs/protocol.md",
    "docs/dev-workflow.md",
    "docs/rendering-parity.md",
    "docs/configuration.md",
    "docs/phonon-tts-engine.md",
    "docs/tool-call-lifecycle.md",
    "docs/preview-pane-handoff.md",
    "docs/system-prompt-composition.md",
]


def test_the_known_set_covers_every_place_names_are_actually_read():
    known = config.known_env_names()

    # One name per source of truth: this module, the entry point, and the extension written in TypeScript.
    assert "PI_CHAT_TTS_ENGINE" in known
    assert "PI_CHAT_HOST" in known, "server.py reads the host and port itself; the scan must see that file"
    assert "PI_CHAT_PREVIEW_DIR" in known, "the preview extension is TypeScript and is not a .py file"
    assert len(known) > 40, f"the scan found only {len(known)} names; it has probably stopped scanning"


def test_the_names_this_file_uses_as_typos_are_not_known():
    """The scan must never swallow its own evidence.

    This file proves the diagnostic by setting a misspelled name and an invented one. If the known-name scan
    ever grows to read `tests/`, both become "real" settings, `unknown_env_names` stops reporting them, and
    every test here still passes while the feature quietly accepts typos in the field. That is exactly what
    happened the first time `tests/` went into the scan roots, and it was only visible because these two
    assertions existed afterwards.
    """
    known = config.known_env_names()

    assert "PI_CHAT_TTS_MAX_HOL_MS" not in known
    assert "PI_CHAT_ZEBRA" not in known


def test_a_typo_is_reported_alongside_the_name_it_meant():
    unknown = config.unknown_env_names({"PI_CHAT_TTS_MAX_HOL_MS": "450"})

    assert [name for name, _ in unknown] == ["PI_CHAT_TTS_MAX_HOL_MS"]
    assert unknown[0][1] == "PI_CHAT_TTS_MAX_HOLD_MS"


def test_an_unrelated_name_is_reported_without_an_invented_suggestion():
    unknown = config.unknown_env_names({"PI_CHAT_ZEBRA": "1"})

    assert unknown == [("PI_CHAT_ZEBRA", None)]


def test_names_the_application_reads_produce_no_complaint():
    env = {"PATH": "/usr/bin", "PI_CHAT_TTS_ENGINE": "phonon", "PI_CHAT_PORT": "9000"}

    assert config.unknown_env_names(env) == []


def test_this_machines_real_environment_is_clean():
    """Whatever is set here right now should be spelled the way the code spells it.

    Not a tautology: this box is where the app is actually run, and a stray `PI_CHAT_*` in a launcher or a
    shell profile is the exact bug the check exists to find.
    """
    assert config.unknown_env_names() == []


@pytest.mark.parametrize("relative", LIVE_DOCS)
def test_live_documentation_only_names_knobs_that_exist(relative):
    """Docs that describe today may not name a variable that nothing reads.

    `docs/audit/*` is exempt on purpose: those are dated snapshots and proposals, and a proposal naming a
    variable that was never built is history rather than a lie. `docs/archive/*` is exempt for the same
    reason in the other direction: those plans describe an engine that used to be the default.

    `AGENTS.md` joined this list on 2026-11-17, once the card's camofox section stopped exporting scratch
    shell variables under the application prefix. Its recipes used to name nine `PI_CHAT_*` names that
    nothing reads, which is precisely the rot this test hunts, and the exemption was written because the
    test was right about the card rather than because the card was convenient.
    """
    import re
    from pathlib import Path

    text = (config.PROJECT_ROOT / relative).read_text(encoding="utf-8")
    named = set(config._ENV_NAME.findall(text))
    # A document may show a name that does not exist, but only by saying so in the file itself. The marker
    # lives in the document and `tools/config_reference.py` parses it, so this check and the one in
    # `tests/test_config_reference.py` cannot drift into disagreeing about what a documented typo is.
    excused = set(_documented_non_knobs(config.PROJECT_ROOT / relative))
    unknown = sorted(name for name in named if name not in config.known_env_names() and name not in excused)

    assert not unknown, f"{relative} tells the reader to set variables nothing reads: {unknown}"


def test_the_preview_naming_trap_is_said_out_loud_when_only_half_is_set(caplog):
    """`_BASE_DIR` is the server's root; `_DIR` is the per-connection scope the extension is handed.

    Both are real, both are correct in the right place, and setting one while meaning both produces a
    server that serves from defaults and says nothing about it.
    """
    with caplog.at_level(logging.WARNING, logger="test-env"):
        config.log_env_diagnostics(
            logging.getLogger("test-env"), {"PI_CHAT_PREVIEW_DIR": "/tmp/scope-only"}
        )

    assert any("different halves of the preview pane" in record.message for record in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="test-env"):
        config.log_env_diagnostics(
            logging.getLogger("test-env"),
            {"PI_CHAT_PREVIEW_DIR": "/tmp/scope", "PI_CHAT_PREVIEW_BASE_DIR": "/tmp/base"},
        )

    assert not any("preview pane" in record.message for record in caplog.records)


def test_diagnosing_the_environment_never_logs_a_value(caplog):
    """The accepted-names log exists to answer "what is this process running with", and one of those names
    is a password hash. Names in effect are useful; values in a log file are the thing the log-hygiene pass
    exists to remove."""
    secret = "scrypt$16384$8$1$deadbeef$cafebabe"
    logger = logging.getLogger("test-env-values")

    with caplog.at_level(logging.DEBUG, logger="test-env-values"):
        config.log_env_diagnostics(logger, {"PI_CHAT_B_PASSWORD_HASH": secret})

    assert any("names only" in record.message for record in caplog.records)
    assert not any(secret in record.getMessage() for record in caplog.records)
