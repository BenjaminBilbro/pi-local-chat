"""Pytest configuration for pi-chat tests.

Adds project root to sys.path so pi_chat modules are importable.
"""

import sys
from pathlib import Path

# Add project root (parent of tests/) to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# The mobile viewport check drives a real Camoufox browser and reaches outside this repo for
# its dependency (a hardcoded venv path). Skip collection where camoufox is absent instead of
# breaking every headless/Windows run of the suite.
try:  # pragma: no cover - environment dependent
    import camoufox  # noqa: F401
except ImportError:  # pragma: no cover - environment dependent
    collect_ignore = ["mobile_viewport_test.py"]
