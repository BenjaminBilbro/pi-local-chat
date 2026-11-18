#!/usr/bin/env python3
"""Generate the knob table in `docs/configuration.md` from the module that actually reads the environment.

`BE-05` is not really a complaint about having 43 knobs. It is a complaint about nobody being able to tell a
live knob from a decorative one: there was no reference at all, and `.env.example` pointed at "documented in
AGENTS.md" for a list AGENTS.md never held. A hand-typed table would repeat that within a month, so the table
is generated from the AST of `pi_chat/config.py`, between two markers, and CI runs this script with `--check`.

Only the table is generated. What a knob is for, which engine has to be selected for it to mean anything, and
which side of the boundary reads it are prose, written by hand in the document, because a column of "TTS
knob" is documentation that teaches nothing.

    .venv/Scripts/python.exe tools/config_reference.py            # rewrite the marked section
    .venv/Scripts/python.exe tools/config_reference.py --check    # exit 1 if it would change (CI)
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "pi_chat" / "config.py"
DOC = ROOT / "docs" / "configuration.md"
START = "<!-- knobs:start -->"
END = "<!-- knobs:end -->"
PREFIX = "PI_CHAT_"

# Group order in the table. A knob lands in the first group whose token appears in its name, so specific
# groups must precede the general ones.
GROUPS = [
    ("Credentials", ("PASSWORD_HASH", "SECRET")),
    ("Previews", ("PREVIEW",)),
    ("Speech synthesis", ("TTS",)),
    ("Speech input", ("STT",)),
    ("Voice storage and cloning", ("VOICE",)),
    ("Audit and telemetry", ("AUDIT",)),
    ("Server and child process", ("HOST", "PORT", "PI_BIN", "PI_ARGS", "ACCOUNT", "LOG_", "DEV", "SESSION_TTL", "CONTEXT_FILES", "ALLOW_DEV_REMOTE")),
    ("Test harness", ("SMOKE_",)),
]

# Knobs whose name alone will mislead someone, keyed by env var. Anything here that is not a real knob fails
# the drift test, so this cannot quietly rot into a list of former knobs.
NOTES = {
    "PI_CHAT_B_PASSWORD_HASH": "scrypt hash that makes profile `b` exist at all; write it with `python -m pi_chat.auth`",
    "PI_CHAT_R_PASSWORD_HASH": "scrypt hash that makes profile `r` exist at all; same helper",
    "PI_CHAT_SCRUB_LITERALS": "one or more `literal = replacement` pairs, bar-separated, for "
                              "`tools/scrub_captures.py`; machine-local like `.scrub-literals`, and for the "
                              "same reason (a note in this table cannot contain the bar itself)",
    "PI_CHAT_HOST": "`server.py` defaults to `0.0.0.0`, which the tailnet reaches; testing gets `127.0.0.1`",
    "PI_CHAT_PREVIEW_BASE_DIR": "server side; the child gets `PI_CHAT_PREVIEW_DIR`, a different name",
    "PI_CHAT_DEV": "also skips session ownership checks; never set it on a reachable host",
    "PI_CHAT_AUDIT": "writes an event log of every connection to `logs/`; implies nothing about PI_CHAT_DEV, and is implied by neither",
    "PI_CHAT_CONTEXT_FILES": "`0` spawns pi with `--no-context-files`; see `docs/system-prompt-composition.md`",
    "PI_CHAT_TTS_ENGINE": "default is `phonon`; `omnivoice` needs the extra and a GPU",
    "PI_CHAT_TTS_VRAM_REQUIRED_GB": "OmniVoice path only; Phonon never touches the GPU",
    "PI_CHAT_TTS_PHONON_LIVE_CATALOG": "1 asks the Hub for the voice catalog instead of the committed snapshot",
    "PI_CHAT_STT_ENABLED": "on by default; the transcriber loads on first use (`uv sync --extra stt` installs it)",
    "PI_CHAT_SESSION_TTL": "seconds; a restart is required either way, so the value is read once at import",
}

# Names that are not `config.py` knobs but are still part of the configuration story, because they cross the
# boundary between the server and the `pi --mode rpc` child. Hand-written, and checked: each name must still
# appear in `pi_chat/process.py`, so a rename there fails here instead of turning this table into fiction.
CHILD_SIDE = [
    ("PI_CHAT_PI_BIN", "server reads",
     "the executable spawned per connection, a pi binary or the package's `cli.js`. The harness points it at "
     "`tools/fake_rpc_pi.py`"),
    ("PI_CHAT_PI_ARGS", "server reads",
     "extra argv for every child, shlex-split. Test and privacy runs use it, and an over-long token gets "
     "truncated so it cannot smuggle a secret"),
    ("PI_CHAT_PREVIEW_DIR", "server writes for the child",
     "the per-connection preview scope, read by `pi_chat/extension/preview.ts`. Setting it on the server does "
     "nothing, because the server's own knob is `PI_CHAT_PREVIEW_BASE_DIR`"),
    ("PI_CHAT_ACCOUNT", "server writes for the child",
     "only when the account is not `main`. This is the whole seam between this app and the local memory "
     "extension, which keys persona and memory off it"),
]
PROCESS_SOURCE = ROOT / "pi_chat" / "process.py"


def group_of(name: str) -> str:
    stripped = name.removeprefix(PREFIX)
    for label, tokens in GROUPS:
        if any(token in stripped for token in tokens):
            return label
    return "Other"


NON_KNOB_MARKER = "<!-- documented-non-knob:"


def documented_non_knobs(document: Path | None = None) -> dict[str, str]:
    """Names a document deliberately shows that are not real knobs, each with the reason it is there.

    A worked example of a typo is the only good reason, and it has to be written down rather than remembered,
    because two tests ask the same question about this document from opposite directions: "does the doc name a
    variable nothing reads?" (which must fail on a stale document) and "does the doc still show the typo whose
    warning it describes?" (which must pass). Without a marker in the file itself, one of the two ends up with
    a private allow-list, and private allow-lists are how checks stop checking.
    """
    path = document or DOC
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith(NON_KNOB_MARKER):
            continue
        body = stripped[len(NON_KNOB_MARKER):].split("-->")[0].strip()
        name, _, reason = body.partition(" ")
        if name:
            found[name] = reason.strip("()") or "no reason given"
    return found


def collect() -> list[dict[str, str]]:
    """Every `PI_CHAT_*` name the application can read, with its default and where it is read.

    Two passes, because one is not enough. The AST pass over `config.py` sees defaults across line breaks and
    is the authority there. The literal pass over the rest of the application catches the names that are not
    `config.py` knobs at all: `server.py` reads `PI_CHAT_HOST` and `PI_CHAT_PORT` itself, `app.py` reads
    `PI_CHAT_LOG_LEVEL`, the profile hashes live in a dict literal, and `tools/smoke_test_ws.py` owns its own
    namespace. An earlier version of this tool scanned `config.py` alone and quietly documented 39 of the 53
    names in the namespace, which is the same blind spot the document exists to close.
    """
    source = CONFIG.read_text(encoding="utf-8")
    rows: dict[str, dict[str, str]] = {}

    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        else:
            continue
        if not targets or value is None:
            continue
        for call in ast.walk(value):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr != "get" or not call.args or not isinstance(call.args[0], ast.Constant):
                continue
            name = call.args[0].value
            if not isinstance(name, str) or not name.startswith(PREFIX):
                continue
            default = None
            if len(call.args) > 1:
                default = ast.get_source_segment(source, call.args[1])
            rows[name] = {
                "env": name,
                "default": default if default else "*unset*",
                "read_in": "`config.py`",
                "const": targets[0],
                "line": str(node.lineno),
                "group": group_of(name),
            }

    # Literal pass over everything else. Defaults are only read from a single-line `environ.get`, which is
    # every case outside config.py; anything subtler reports *unset* rather than guessing.
    literal = re.compile(rf"{PREFIX}[A-Z0-9_]+")
    one_line_default = re.compile(
        rf"""environ\.get\(\s*["']({PREFIX}[A-Z0-9_]+)["']\s*,\s*(.+?)\)\s*[\n)]"""
    )
    for path in sorted((ROOT / "pi_chat").rglob("*.py")) + [ROOT / "server.py"] + sorted((ROOT / "tools").glob("*.py")):
        if path == CONFIG or path == Path(__file__) or not path.is_file() or "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT).as_posix()
        defaults = {m.group(1): m.group(2).strip() for m in one_line_default.finditer(text)}
        for match in literal.finditer(text):
            name = match.group(0)
            where = f"`{rel}`"
            existing = rows.get(name)
            if existing is None:
                rows[name] = {
                    "env": name,
                    "default": defaults.get(name, "*unset*"),
                    "read_in": where,
                    "const": "-",
                    "line": str(text[: match.start()].count("\n") + 1),
                    "group": group_of(name),
                }
            elif where not in existing["read_in"]:
                existing["read_in"] += f", {where}"

    ordered = sorted(rows.values(), key=lambda r: (r["group"], r["env"]))
    return [r for r in ordered if r["env"] != f"{PREFIX}_"]  # the prefix constant itself is not a knob


def render(rows: list[dict[str, str]]) -> str:
    lines = [
        START,
        "<!-- Generated by tools/config_reference.py from the sources that read the environment. Prose above and",
        "     below is hand-written; this table is not, and CI fails if it is stale. -->",
        "",
        f"{len(rows)} names in the `PI_CHAT_` namespace, read from `config.py`, `server.py`, `app.py` and the "
        "test harness. Every one of them is read once, at import, so **a change needs a server restart**; "
        "there is no settings endpoint that re-reads the environment.",
        "",
        "| Knob | Default | Read in | Group | Note |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        note = NOTES.get(row["env"], "")
        lines.append(
            f"| `{row['env']}` | `{row['default']}` | {row['read_in']} | {row['group']} | {note} |"
        )
    lines.append("")
    lines.append(f"_Source: every `PI_CHAT_*` literal in `pi_chat/`, `server.py` and `tools/`, with defaults "
                 f"taken from the `environ.get` call that supplies them ({len(rows)} names)._")
    lines.append("")
    lines.append("### Names that cross into the child process")
    lines.append("")
    lines.append("Not `config.py` knobs. These are read from, or written into, the environment of the spawned")
    lines.append("`pi --mode rpc` process by `pi_chat/process.py`, and confusing them with the table above is the")
    lines.append("most common way to configure something that never runs.")
    lines.append("")
    lines.append("| Name | Direction | What it does |")
    lines.append("|---|---|---|")
    for name, direction, note in CHILD_SIDE:
        lines.append(f"| `{name}` | {direction} | {note} |")
    lines.append("")
    lines.append(f"_Source: `pi_chat/process.py`, and `tools/config_reference.py` fails if a name here stops "
                 f"appearing in that file._")
    lines.append(END)
    return "\n".join(lines)


def splice(document: str, table: str) -> str:
    if START not in document or END not in document:
        raise SystemExit(f"{DOC.name} lost its {START} / {END} markers")
    head, _, rest = document.partition(START)
    _, _, tail = rest.partition(END)
    return head + table + tail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the generated section is not current")
    parser.add_argument("--print", dest="print_only", action="store_true", help="write the table to stdout")
    args = parser.parse_args()

    rows = collect()
    table = render(rows)

    if args.print_only:
        print(table)
        return 0

    document = DOC.read_text(encoding="utf-8")
    updated = splice(document, table)

    stale_names = [name for name in NOTES if name not in {r["env"] for r in rows}]
    if stale_names:
        raise SystemExit(f"NOTES names knobs that no longer exist: {', '.join(sorted(stale_names))}")

    process_text = PROCESS_SOURCE.read_text(encoding="utf-8")
    orphaned = [name for name, _, _ in CHILD_SIDE if name not in process_text]
    if orphaned:
        raise SystemExit(
            f"CHILD_SIDE names no longer appear in pi_chat/process.py: {', '.join(orphaned)}. Either the "
            "boundary moved and this table is fiction, or the rename is half done."
        )

    if args.check:
        if updated != document:
            print(f"{DOC} is stale: run tools/config_reference.py", file=sys.stderr)
            return 1
        print(f"{DOC.name}: table matches config.py ({len(rows)} knobs)")
        return 0

    if updated != document:
        DOC.write_text(updated, encoding="utf-8")
        print(f"wrote {len(rows)} knobs into {DOC}")
    else:
        print(f"{DOC.name}: already current ({len(rows)} knobs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
