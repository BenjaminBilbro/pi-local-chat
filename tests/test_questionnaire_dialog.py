"""The question dialog, checked in a real DOM from pytest.

The assertions live in `tools/test_questionnaire_dialog.mjs`, because what is at risk is a DOM shape:
a child process asks a question through a route, and the only thing that releases it is an answer this
browser sends. A server-side test can prove the relay waits and releases; it cannot see a Submit button
that stays dead, a text box that never appears under "Type something", or a refusal that closes the
dialog without ever telling the child. Skips honestly where node is absent.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "test_questionnaire_dialog.mjs"
NODE = shutil.which("node")
# 24 through the voice-mode pass, +1 on 2026-11-19 for the settled-card-on-reload claim.
CLAIMS = 25


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_questionnaire_dialog_claims():
    result = subprocess.run([NODE, str(SCRIPT)], capture_output=True, text=True, timeout=120)

    assert "FAIL" not in result.stdout, result.stdout
    passed, total = result.stdout.strip().splitlines()[-1].split(" ")[0].split("/")
    assert int(passed) == int(total) == CLAIMS, result.stdout
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
