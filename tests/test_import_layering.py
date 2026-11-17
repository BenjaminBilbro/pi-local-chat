"""`VOICE-06`: production must not import from the test tree.

Until 2026-11-17 `pi_chat/tts_service.py` and `pi_chat/tts_phonon.py` did
`from tests.fakes.voice import ...`. Sharing one definition between the real engine and the fake is right;
having production reach into `tests/` to get it is not, and nothing failed when it happened, because
`tests/` is present on a developer machine and absent from a wheel (`pyproject` ships `packages =
["pi_chat"]`). So the failure mode was invisible here and structural elsewhere, which is exactly the kind
of thing a test should hold rather than a convention in a document.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import pi_chat
import pi_chat.tts_types as tts_types

REPO_ROOT = Path(pi_chat.__file__).resolve().parent.parent
PRODUCTION = REPO_ROOT / "pi_chat"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_no_production_module_imports_from_tests():
    offenders = {
        str(path.relative_to(REPO_ROOT)): sorted(name for name in _imports(path) if name == "tests" or name.startswith("tests."))
        for path in sorted(PRODUCTION.rglob("*.py"))
    }
    broken = {name: mods for name, mods in offenders.items() if mods}
    assert not broken, "production imports the test tree; move the shared type to pi_chat/tts_types.py: " + ", ".join(
        f"{name} -> {', '.join(mods)}" for name, mods in broken.items()
    )


def test_production_modules_import_the_shared_voice_types_from_production():
    for name in ("tts_service.py", "tts_phonon.py"):
        assert "pi_chat.tts_types" in _imports(PRODUCTION / name), f"{name} no longer imports the shared types"


def test_the_fakes_reexport_the_same_objects_rather_than_copies():
    """Two definitions of one contract is the bug this whole file is about."""
    from tests.fakes import voice as fakes

    for name in ("VoiceSettings", "VoiceHandle", "SynthesizedAudio", "TTSRuntime"):
        assert getattr(fakes, name) is getattr(tts_types, name), f"tests.fakes.voice.{name} is a different object"


def test_the_shared_types_import_nothing_from_the_package_they_serve():
    """The fakes advertise that they pull no heavy engine in at import time; that promise now lives here.

    If `tts_types` ever imports `pi_chat.config` (or anything that reaches torch or numpy), importing the
    fakes drags the engine stack with it and the cheap voice tests stop being cheap.
    """
    assert not any(
        name == "pi_chat" or name.startswith("pi_chat.") for name in _imports(PRODUCTION / "tts_types.py")
    ), "pi_chat/tts_types.py must stay a leaf module"
    tree = ast.parse((PRODUCTION / "tts_types.py").read_text(encoding="utf-8"))
    module_names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    module_names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert module_names <= {"__future__", "dataclasses", "typing"}, f"unexpected import: {sorted(module_names)}"


@pytest.mark.parametrize("field_name", ["phonon_voice", "phonon_rate", "gender", "voice_id"])
def test_one_settings_type_carries_both_engines_fields(field_name):
    """Both engines' fields live on one dataclass on purpose, so the moved type must not have been trimmed."""
    assert field_name in tts_types.VoiceSettings.__dataclass_fields__
