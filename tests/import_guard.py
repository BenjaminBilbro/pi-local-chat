"""Order-independent import guards.

The old form of these assertions, `"torch" not in sys.modules`, is only true by accident:
pytest imports every test module before running the first test, and
`tests/test_tts_custom_voice.py` legitimately drives the real torch path. So whichever file
ran second was asserting about test order rather than about the module under test.

These probes import the module under test in a fresh interpreter, which is what the guard
actually means: importing this module must not drag torch/omnivoice into memory (that is
roughly 1-2 GB of RSS for a user who never enabled voice mode).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def imported_modules_for(module_name: str, timeout: float = 300.0) -> set[str]:
    """Return the module names loaded by a fresh interpreter that imports `module_name`."""
    code = (
        "import json, sys, importlib; "
        f"importlib.import_module({module_name!r}); "
        "print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"importing {module_name} in a subprocess failed: {result.stderr[-2000:]}"
        )
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def has_module(modules: set[str], package: str) -> bool:
    """True if `package` or any submodule of it was imported."""
    return any(name == package or name.startswith(package + ".") for name in modules)
