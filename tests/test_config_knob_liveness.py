"""`WP-13`: every documented knob has to change something.

The audit carried "take the 52 knobs toward something smaller" as open work for two sweeps. The pass over them
found nothing to delete: all of them are read by application code, the two pairs that look like aliases
(`PI_CHAT_PREVIEW_MAX_MB` against `PI_CHAT_PREVIEW_MAX_DOC_MB`, `PI_CHAT_SESSION_TTL` against
`PI_CHAT_PREVIEW_TTL_DAYS`) cap different things and one even validates against the other, and every keyword the
speech-input path hands `AudioToTextRecorder` is a real parameter of that constructor at the pinned commit, with
no `**kwargs` there to swallow a typo. The perceived bloat was engine scope, which `docs/configuration.md` now
states instead of hiding.

So the finding is "the premise was wrong", and a one-off script proving that is worth less than nothing, because
it rots the day someone adds a knob and forgets to read it. This file is the check, permanently:

  A name in the generated knob table must be read by the application or by a tool, and a constant bound from an
  environment variable must be mentioned again after the line that binds it.

The second clause is the one with teeth. "It is read" is not evidence when the only place it appears is its own
assignment, which is exactly what a dead knob looks like and exactly what a knob looks like the moment someone
removes the code that used it and leaves the setting behind.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "pi_chat" / "config.py"
TOOL = ROOT / "tools" / "config_reference.py"

# Sources where a read counts as "something acts on this": the server and the tools that ship with it.
# `tests/` is deliberately excluded, because a knob whose only consumer is its own test is the finding.
LIVE_SOURCES = [
    *sorted(ROOT.glob("pi_chat/**/*.py")),
    ROOT / "server.py",
    *sorted(ROOT.glob("tools/*.py")),
]
LIVE_SOURCES = [p for p in LIVE_SOURCES if p.is_file() and "__pycache__" not in str(p)]


def documented_knobs() -> set[str]:
    """Names in the generated table, taken from the generator itself rather than a copy of its output."""
    result = subprocess.run(
        [sys.executable, str(TOOL), "--print"], capture_output=True, text=True, timeout=120, check=True
    )
    return set(re.findall(r"^\| `(PI_CHAT_[A-Z0-9_]+)` \|", result.stdout, re.MULTILINE))


def config_bindings() -> dict[str, int]:
    """`PI_CHAT_X` -> the line in config.py that binds it."""
    source = CONFIG.read_text(encoding="utf-8")
    bound: dict[str, int] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        else:
            continue
        if not targets:
            continue
        for call in ast.walk(value):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "get"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
                and call.args[0].value.startswith("PI_CHAT_")
            ):
                bound[call.args[0].value] = node.lineno
                break
    return bound


def test_the_knob_table_is_populated():
    # Guards the parse above: an empty or tiny set would make every other assertion pass by checking nothing.
    names = documented_knobs()
    assert len(names) > 40, f"the knob table produced {len(names)} names; something broke upstream"


def test_every_bound_constant_is_read_after_it_is_bound():
    missing = []
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in LIVE_SOURCES}
    for env, lineno in config_bindings().items():
        const = CONFIG.read_text(encoding="utf-8").splitlines()[lineno - 1]
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", const)
        assert match, f"{env}: config.py:{lineno} does not look like a binding"
        name = match.group(1)

        pattern = re.compile(rf"\b{re.escape(name)}\b")
        reads = 0
        for path, text in texts.items():
            if path == CONFIG:
                # In config.py only a mention below the binding counts: the binding itself is not a read,
                # and neither is the line where the value arrives from the environment.
                lines = text.splitlines()
                reads += sum(1 for line in lines[lineno:] if pattern.search(line))
            else:
                reads += len(pattern.findall(text))
        if reads == 0:
            missing.append(f"{env} -> config.{name} (config.py:{lineno})")

    assert not missing, (
        "These environment variables fill a constant that nothing ever reads again. Delete the knob, or wire it "
        "to the behavior its name promises:\n  " + "\n  ".join(sorted(missing))
    )


def test_every_documented_name_is_read_by_the_app_or_a_tool():
    bindings = config_bindings()
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in LIVE_SOURCES}
    unread = []
    for env in sorted(documented_knobs()):
        if env in bindings:
            continue  # the stricter constant-level check above already covers it
        hits = sum(len(re.findall(rf"\b{re.escape(env)}\b", text)) for text in texts.values())
        if hits == 0:
            unread.append(env)

    assert not unread, (
        "Documented as a knob, read by nothing in `pi_chat/`, `server.py` or `tools/`: "
        + ", ".join(unread)
        + ". Either it moved and the docs were not told, or it is a knob that does nothing."
    )


@pytest.mark.parametrize(
    "pair",
    [
        ("PI_CHAT_PREVIEW_MAX_MB", "PI_CHAT_PREVIEW_MAX_DOC_MB"),
        ("PI_CHAT_SESSION_TTL", "PI_CHAT_PREVIEW_TTL_DAYS"),
    ],
)
def test_knobs_that_look_like_aliases_are_actually_different(pair):
    """The two pairs a reader reasonably suspects are duplicates.

    They are not, and the suspicion is worth pinning down rather than answering in chat: one caps a document
    handed to the preview pane and the other caps everything stored under it, with a validation between them
    that fails startup if they are configured backwards; and one is a session lifetime in seconds while the
    other is a preview-scratch retention period in days.
    """
    source = CONFIG.read_text(encoding="utf-8")
    for env in pair:
        assert env in source, f"{env} is gone from config.py but the docs may still list it"
    assert "PI_CHAT_PREVIEW_MAX_MB must be at least PI_CHAT_PREVIEW_MAX_DOC_MB" in source, (
        "The cross-validation is what makes this pair safe to keep as two knobs; losing it turns a typo into "
        "a pane that stores documents it can never serve."
    )
