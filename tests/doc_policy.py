"""Helpers shared by the documentation-drift tests.

Not a test module. Two test files need the same answer to the same question, "does this document excuse this
name?", and the answer lives in `tools/config_reference.py`, which parses the `documented-non-knob` markers in
`docs/configuration.md`. Duplicating the loader would give each file a private allow-list, and the last time
this repository had two private opinions about one rule they disagreed inside a month.

`tools/` is not a package, so the module is loaded by path. That is the same pattern
`tests/test_scrub_captures.py` already uses for `tools/scrub_captures.py`.
"""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def config_reference_tool():
    """Load `tools/config_reference.py` as a module without making `tools/` importable."""
    path = ROOT / "tools" / "config_reference.py"
    spec = importlib.util.spec_from_file_location("config_reference", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def documented_non_knobs(document: Path) -> dict[str, str]:
    """Names `document` may mention that are not real knobs, keyed by name with the stated reason."""
    return config_reference_tool().documented_non_knobs(Path(document))
