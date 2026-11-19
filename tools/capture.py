"""Capture ``pi --mode rpc`` output for fixtures, chunk-exactly.

Two jobs, because a fixture has to answer two different questions:

* *What did pi say?* -> ``<out>.jsonl``, one parsed event per line, in arrival order.
* *Exactly what came out of the pipe, and where were the read() boundaries?* ->
  ``<out>.raw.gz``, the stdout byte stream verbatim, plus ``chunk``/``offset``/``bytes`` on every
  parsed record so a boundary can be tied back to an event without decompressing anything.

The raw stream is not decoration. Chunk boundaries are a property of the pipe, not of the protocol,
so a client that assumes "one read == one event" works in a fixture and breaks in production. Any
change to how pi-chat reads pi's stdout should be tested against a real boundary distribution, which
only the raw stream can give.

Usage:
    uv run python tools/capture.py --list
    uv run python tools/capture.py --scenario markers
    uv run python tools/capture.py --prompt "..." --output data-samples/custom.jsonl
    uv run python tools/capture.py --census "data-samples/*.jsonl"   # no inference, offline

The census classifies each file from its first record, because `data-samples/` holds both pi session
files and RPC captures and only one of those kinds has wire markers to check. It exits 2 when the
corpus as a whole cannot show a core marker, or prints the event names pi emits that this tool has
never heard of, which is how a pi upgrade that renames an event gets noticed.

A scenario that is not ``--no-session`` also copies pi's own session file next to the capture, so the
live path (RPC stream) and the settled path (session JSONL) can be rendered from one run and compared.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pi_chat.process import resolve_pi_command  # noqa: E402  (dev tool: same launcher as the app)

DATA_DIR = PROJECT_ROOT / "data-samples"
WORKSPACE = DATA_DIR / "capture-workspace"
PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"

# Top-level AgentSessionEvent types worth asserting on. Anything pi emits that is not in KNOWN goes
# to the census's "unexpected" line, which is how a pi upgrade that renames an event gets noticed.
MARKERS = [
    "agent_start",
    "agent_end",
    "agent_settled",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "queue_update",
    "entry_appended",
    "session_info_changed",
    "thinking_level_changed",
    "response",
    "extension_ui_request",
    "extension_error",
    "bash_execution_update",
    "ping",
    "error",
]

# assistantMessageEvent types (the message_update payload the browser actually renders).
STREAM_MARKERS = [
    "start",
    "text_start",
    "text_delta",
    "text_end",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
    "toolcall_start",
    "toolcall_delta",
    "toolcall_end",
    "done",
    "error",
]

# Absence of these *across the corpus* is a real gap, so the census fails on that. Absence in one file
# is not: two captures here have no tool call in them by design, and a per-file requirement is what kept
# the truth-check permanently red. The corpus is the unit that has to cover the renderer's needs.
CORE_MARKERS = [
    "agent_start", "agent_end", "agent_settled", "turn_start", "turn_end",
    "message_start", "message_update", "message_end",
    "tool_execution_start", "tool_execution_end",
]
# Reachable, but only by asking for them (an extension, an RPC command, a queued message).
OPTIONAL_MARKERS = [
    "tool_execution_update", "queue_update", "entry_appended", "session_info_changed",
    "thinking_level_changed", "response", "extension_ui_request", "extension_error",
    "bash_execution_update", "ping", "error",
]
# Provider-level stream events. The agent loop translates start/done/error into message_start and
# message_end before they reach the wire, so they are legitimately absent from any RPC capture.
NOT_ON_WIRE = {"start", "done", "error"}

KNOWN = set(MARKERS) | set(STREAM_MARKERS)

# Every scenario runs against a scratch workspace instead of the repo: a capture should cost a few
# hundred tokens of context, not the whole pi-chat tree, and it must not land in the profile's real
# session list. --no-context-files keeps an injected AGENTS.md out of the transcript too.
SCRATCH_FILES = {
    "seed.txt": "alpha\nbravo\ncharlie\ndelta\necho\n",
    "notes.md": "# Notes\n\nLine one.\nLine two.\nLine three.\n",
}

SCENARIOS: dict[str, dict] = {
    # The fixture the streaming renderer is written against: every marker the browser can be asked to
    # render, in one short run. --thinking low keeps the thinking blocks real but cheap on a local
    # engine; --no-extensions/--no-skills keeps the stream about tools rather than about this machine.
    "markers": {
        "description": "All stream markers: long write args, read, edit, failed tool, aborted run",
        "pi_flags": [
            "--mode", "rpc", "--approve",
            "--thinking", "low",
            "--no-context-files", "--no-skills", "--no-mcp", "--offline",
        ],
        # The probe extension emits what no model call will: entry_appended, extension_ui_request,
        # extension_error. It costs nothing because the slash commands are handled in-process.
        "probe": True,
        "workspace": True,
        "steps": [
            {
                "label": "long write + read back",
                # A write with enough content is what produces a long tail of toolcall_delta, which is
                # exactly the case the UI was blind to.
                "message": (
                    "Use the write tool to create out/report.txt containing 30 lines, one per line, "
                    "each line reading exactly 'row <n> <a short latin word>' for n from 1 to 30. "
                    "Then use the read tool on out/report.txt and tell me only what its 7th line says."
                ),
                "timeout": 900,
                # A second message sent while the run is still going is the only way to make pi emit a
                # queue_update, which is what the chat UI's queue chips are drawn from.
                "follow_up_after": 6.0,
                "follow_up": "When you finish, add one final line reading 'queued note' to the same file.",
            },
            {
                # RPC commands rather than a prompt: each of these emits a marker no prompt reliably
                # emits, and none of them costs a token of inference.
                "label": "rpc commands (no inference)",
                "commands": [
                    # set_thinking_level clamps to the model's available levels and emits
                    # thinking_level_changed only when the effective level actually moves, so a fixed
                    # level can be a silent no-op. Cycling cannot be.
                    {"type": "get_available_thinking_levels"},
                    {"type": "cycle_thinking_level"},
                    {"type": "cycle_thinking_level"},
                    {"type": "set_session_name", "name": "capture-markers"},
                    {"id": "cap-bash", "type": "bash", "command": "echo probe-$RANDOM && sleep 0.2 && echo done"},
                    {"type": "prompt", "message": "/capture-probe hello"},
                    {"type": "prompt", "message": "/capture-probe-fail"},
                ],
                "timeout": 90,
            },
            {
                "label": "edit + failing read",
                "message": (
                    "Use the edit tool to change the 3rd line of out/report.txt so its word becomes "
                    "'zebra'. Then use the read tool on the file missing-7b3c.txt. Then tell me in one "
                    "short sentence what happened to each of those two tool calls."
                ),
                "timeout": 600,
            },
            {
                # The shape a user cancel leaves on the wire: a tool call that is announced, streamed
                # and started, but never answered, plus an agent_settled that says aborted. That state is
                # only derivable from position (the rule enrichToolCalls implements), so it has to be in
                # the fixture. It needs a tool that is still running when the abort lands - built-in
                # tools finish in single-digit milliseconds, so the step asks for a slow streaming one:
                # its partial output is also the only way to see tool_execution_update on the wire.
                "label": "abort during a streaming bash call",
                "message": (
                    "Use the bash tool to run exactly this command and wait for it to finish: "
                    "`for i in 1 2 3 4 5 6 7 8; do echo chunk-$i; sleep 1; done`. "
                    "Then tell me how many chunks printed."
                ),
                "timeout": 600,
                "abort_after_first_tool": 2.5,
            },
        ],
        "output": "data-samples/markers_rpc_capture.jsonl",
        "copy_native": True,
        "native_output": "data-samples/markers_session.jsonl",
    },
    "general": {
        "description": "General RPC events (thinking, tools, text)",
        "pi_flags": ["--mode", "rpc", "--no-session", "--approve"],
        "steps": [
            {"label": "Initial prompt",
             "message": "What files are in the current directory? List them using a tool.",
             "timeout": 120},
            {"label": "Follow-up",
             "message": "Read the server.py file and summarize what it does in 2 sentences.",
             "timeout": 120},
        ],
        "output": "data-samples/general_rpc_capture.jsonl",
        "copy_native": False,
    },
    "subagent": {
        "description": "Sub-agent spawn RPC events",
        "pi_flags": ["--mode", "rpc", "--no-session", "--approve"],
        "steps": [
            {"label": "Sub-agent prompt",
             "message": (
                 "Spawn a sub-agent and have it do the following 3 simple tool calls: "
                 "1) `ls -la` via bash, 2) print 2+2 via bash, 3) `echo hello world` via bash. "
                 "Then report back the results of all 3 commands."
             ),
             "timeout": 600},
        ],
        "output": "data-samples/subagent_rpc_capture.jsonl",
        "copy_native": False,
    },
    "nested": {
        "description": "Nested sub-agent RPC events + native session copy",
        "pi_flags": ["--mode", "rpc", "--approve"],
        "workspace": True,
        "steps": [
            {"label": "Initial prompt", "message": "What is the current date? Tell me briefly.",
             "timeout": 60},
            {"label": "Nested sub-agent prompt",
             "message": (
                 "Spawn a sub-agent named 'outer' and ask it to do the following: "
                 "make 3 simple tool calls (bash: `echo hello`, bash: `date`, bash: `whoami`), "
                 "THEN have THAT sub-agent spawn another sub-agent named 'inner' that makes "
                 "3 more simple tool calls (bash: `pwd`, bash: `uname -a`, bash: `echo done`). "
                 "Finally report back all results from both sub-agents."
             ),
             "timeout": 600},
        ],
        "output": "data-samples/nested_subagent_rpc_capture.jsonl",
        "copy_native": True,
        "native_output": "data-samples/nested_subagent_session.jsonl",
    },
    # Image generation, captured the way a pi-chat child actually runs: `--no-context-files`, the preview
    # extension loaded by path, and a preview store directory in the environment. image-gen itself is not
    # passed with -e: it lives in ~/.pi/agent/extensions and pi auto-loads it there, which is exactly how a
    # web-UI child gets the tool. The point of this capture is the *sequence*: a `pichat_preview` open with
    # state "rendering" while the draw is still borrowing VRAM, a run of `tool_execution_update` carrying
    # sd-cli's phase and percent, then the `update` that hands over the PNG.
    # `--thinking low` is the one unfaithful flag: it shortens the turns around the draws without touching the image
    # event sequence, and the rest of the corpus already covers thinking deltas.
    "imagegen": {
        "description": "generate_image over sd-cli: rendering entry, progress updates, ready entry, second aspect",
        "pi_flags": ["--mode", "rpc", "--approve", "--no-context-files", "--thinking", "low",
                     "-e", str(PROJECT_ROOT / "pi_chat" / "extension" / "preview.ts")],
        "workspace": True,
        "preview_scope": "capture",
        "steps": [
            {
                "label": "generate a square logo",
                "message": (
                    "Call the generate_image tool exactly once, with size 512x512 and the prompt: "
                    "a flat vector logo of a single red hexagon centred on a white background, no text. "
                    "Do not read or write any other file and do not run any other tool. "
                    "When it returns, reply with only the output path and the pixel size."
                ),
                "timeout": 900,
            },
            {
                "label": "generate a wide banner",
                "message": (
                    "Call the generate_image tool exactly once more, this time at 1024x512, with the prompt: "
                    "a wide flat banner of three orange circles on a navy background, no text. No other tools. "
                    "Reply with only the output path."
                ),
                "timeout": 900,
            },
        ],
        "output": "data-samples/imagegen_rpc_capture.jsonl",
        "copy_native": True,
        "native_output": "data-samples/imagegen_session.jsonl",
    },
    # The shape a user cancel leaves in the middle of a draw: the rendering entry is already out, the tool
    # never answers with an image, and the tool result says `cancelled` with the VRAM restored. The pane's
    # "interrupted" state is modelled on this, and so is the rule that a killed sd-cli must give the memory back.
    "imagegen_abort": {
        "description": "Abort mid-draw: rendering entry, progress, then a cancelled result",
        "pi_flags": ["--mode", "rpc", "--approve", "--no-context-files", "--thinking", "low",
                     "-e", str(PROJECT_ROOT / "pi_chat" / "extension" / "preview.ts")],
        "workspace": True,
        "preview_scope": "capture-abort",
        "steps": [
            {
                "label": "abort during a large draw",
                "message": (
                    "Call the generate_image tool exactly once at 2048x2048 with the prompt: a photorealistic "
                    "close-up of a siamese cat's face, soft window light, no text. Then tell me the path."
                ),
                "timeout": 900,
                "abort_after_first_tool": 40.0,
            },
        ],
        "output": "data-samples/imagegen_abort_rpc_capture.jsonl",
        "copy_native": True,
        "native_output": "data-samples/imagegen_abort_session.jsonl",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


# ── census ───────────────────────────────────────────────────────────


def classify_kind(first_type: str | None) -> str:
    """`rpc` for a stdout capture, `session` for a pi session file, `unknown` for neither.

    Decided from the first record rather than the filename. `expand()` already skips `*_session.jsonl`,
    which is only the copies this tool makes: pi's own session files are named
    `<timestamp>_<uuid>.jsonl`, and that is what made `--census` exit 2 on the repo's own corpus with
    "19 core markers missing" about a file that was never a wire capture.
    """
    if first_type == "session":
        return "session"
    if first_type in KNOWN:
        return "rpc"
    return "unknown"


def census_file(path: Path) -> dict:
    """Count wire markers in one capture file. Reads both record shapes: the {"event": {...}} wrapper
    this tool writes and a bare event line."""
    counts: dict[str, int] = {}
    stream: dict[str, int] = {}
    unexpected: dict[str, int] = {}
    notes: dict[str, object] = {}
    records = 0
    tool_uses: dict[str, dict] = {}
    aborted = False
    first_type: str | None = None

    def bump(table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            bump(unexpected, "<unparsable line>")
            continue
        if not isinstance(record, dict):
            continue

        event = record.get("event") if isinstance(record.get("event"), dict) else record
        etype = event.get("type")
        if not etype:
            continue
        if first_type is None:
            first_type = etype
        records += 1
        bump(counts, etype)
        if etype not in KNOWN:
            bump(unexpected, etype)

        if etype == "message_update":
            am = event.get("assistantMessageEvent") or {}
            atype = am.get("type", "<none>")
            bump(stream, atype)
            if atype == "toolcall_start":
                # The 1.1.0 wire strips every `partial` snapshot and puts id/toolName on the event.
                # Captures from before that carry only `partial`, so both shapes are worth knowing.
                notes["toolcall_start_has_id"] = bool(am.get("id"))
                notes["toolcall_start_has_toolName"] = bool(am.get("toolName"))
                notes["has_partial_snapshots"] = "partial" in am
                notes["toolcall_start_has_delta"] = "delta" in am
            if atype == "toolcall_delta":
                notes["toolcall_delta_has_delta"] = isinstance(am.get("delta"), str)
                notes["toolcall_delta_has_partial"] = "partial" in am
                notes["toolcall_delta_has_id"] = bool(am.get("id"))
            if atype == "toolcall_end":
                call = (event.get("toolCall") or am.get("toolCall") or {})
                if call.get("id"):
                    tool_uses.setdefault(call["id"], {})["name"] = call.get("name")

        if etype == "tool_execution_start":
            entry = tool_uses.setdefault(event.get("toolCallId") or "?", {})
            entry["name"] = event.get("toolName") or entry.get("name")
            entry["started"] = True
        if etype == "tool_execution_end":
            entry = tool_uses.setdefault(event.get("toolCallId") or "?", {})
            entry["result"] = True
            if event.get("isError"):
                entry["error"] = True
        if etype == "agent_settled" and event.get("aborted"):
            aborted = True

    started = [k for k, v in tool_uses.items() if v.get("started")]
    answered = [k for k, v in tool_uses.items() if v.get("result")]
    notes["tools_ok"] = sum(1 for k in answered if not tool_uses[k].get("error"))
    notes["tools_error"] = sum(1 for k in answered if tool_uses[k].get("error"))
    notes["tools_unanswered"] = len([k for k in tool_uses if k not in answered])
    notes["runs_aborted"] = int(aborted)
    notes["parallel_tool_calls"] = int(len(started) > 1)

    # Which pi produced this. `toolcall_start` carried only a `partial` snapshot before 1.1.0, and 1.1.0
    # put id/toolName on the event and stripped the partials. Five captures here are still the old shape
    # and still pass, which is how the July stub that lied about `partial` survived to 2026-11-11.
    if notes.get("toolcall_start_has_id"):
        wire = "1.1.0+"
    elif "toolcall_start_has_id" in notes:
        wire = "pre-1.1.0"
    else:
        wire = "not observable (no toolcall_start)"

    return {"path": path, "kind": classify_kind(first_type), "wire": wire,
            "records": records, "counts": counts, "stream": stream,
            "unexpected": unexpected, "notes": notes}


def print_census(paths: list[Path]) -> int:
    seen_new: dict[str, int] = {}
    kinds: dict[str, int] = {}
    corpus: dict[str, int] = {}
    corpus_stream: dict[str, int] = {}
    unknown: list[str] = []
    summaries = [census_file(path) for path in paths]
    for summary in summaries:
        path = summary["path"]
        kind = summary["kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
        print(f"\n{path}  ({summary['records']} records, {kind})")
        print("  events : " + ", ".join(f"{k}={v}" for k, v in sorted(summary["counts"].items())))
        if kind == "unknown":
            # Neither a session file nor a wire capture. Passing quietly over it would be the same
            # mistake in the other direction, so this is a failure the operator has to look at.
            unknown.append(Path(summary["path"]).name)
            print("  note   : first record is neither a pi session header nor a known RPC event")
            continue
        if kind != "rpc":
            # A session file is a different format with its own vocabulary. Reporting its missing wire
            # markers as gaps was the false alarm; "NEW" for `message` and `model_change` was the same
            # mistake in the other direction.
            print("  note   : not an RPC capture, so the marker rules below do not apply to it")
            continue
        print("  wire   : " + summary["wire"])
        for name, count in summary["counts"].items():
            corpus[name] = corpus.get(name, 0) + count
        for name, count in summary["stream"].items():
            corpus_stream[name] = corpus_stream.get(name, 0) + count
        print("  stream : " + (", ".join(f"{k}={v}" for k, v in sorted(summary["stream"].items())) or "-"))
        print("  notes  : " + ", ".join(f"{k}={v}" for k, v in sorted(summary["notes"].items())))
        if summary["unexpected"]:
            print("  NEW    : " + ", ".join(f"{k}={v}" for k, v in sorted(summary["unexpected"].items())))
            for name, count in summary["unexpected"].items():
                seen_new[name] = seen_new.get(name, 0) + count

        core_missing = [m for m in CORE_MARKERS if not summary["counts"].get(m)]
        core_stream_missing = [m for m in STREAM_MARKERS
                               if m not in NOT_ON_WIRE and not summary["stream"].get(m)]
        optional_missing = [m for m in OPTIONAL_MARKERS if not summary["counts"].get(m)]
        if core_missing or core_stream_missing:
            # A gap in one capture is not a gap in the corpus. Two of these files have no tool call in
            # them at all, by design, and asking every capture to contain every marker is how the
            # truth-check sat permanently red: the question is whether the fixtures *together* cover
            # what the renderer has to handle. The corpus verdict below is where it fails.
            print("  no core marker here: " + ", ".join(core_missing + core_stream_missing))
        if optional_missing:
            print("  absent (optional): " + ", ".join(optional_missing))
        print("  never on the RPC wire: " + ", ".join(sorted(NOT_ON_WIRE)))

    # A verdict line. Before this, the census printed eight blocks of counts and exited quietly, so
    # "the truth-check is red" was only discoverable from an exit code nobody looked at.
    rpc = [s for s in summaries if s["kind"] == "rpc"]
    current = [s for s in rpc if s["wire"] == "1.1.0+"]
    old = [s for s in rpc if s["wire"] == "pre-1.1.0"]
    unobserved = [s for s in rpc if s["wire"].startswith("not observable")]
    corpus_missing = ([] if not rpc else
                      [m for m in CORE_MARKERS if not corpus.get(m)]
                      + [m for m in STREAM_MARKERS
                         if m not in NOT_ON_WIRE and not corpus_stream.get(m)])
    core_gap = bool(corpus_missing) or bool(unknown) or not rpc
    if unknown:
        print("unrecognizable file(s): " + ", ".join(unknown))
    print(f"\ncensus: {len(rpc)} RPC capture(s), {kinds.get('session', 0)} session fixture(s) skipped; "
          f"wire generation: {len(current)} current, {len(old)} pre-1.1.0, "
          f"{len(unobserved)} not observable (no tool call in them)")
    if seen_new:
        print("  event names pi emits that this tool has never heard of: "
              + ", ".join(f"{k}={v}" for k, v in sorted(seen_new.items())))
    if old:
        print("  " + ", ".join(Path(s["path"]).name for s in old))
        print("  these still pass, and that is the trap: a fixture on a stale wire shape cannot catch a "
              "change to a field it does not carry. Re-capture them with the installed pi when inference "
              "is free, since a capture spawns a real child.")
    if not rpc:
        # Grading a set that contains no wire capture is a bad glob, not a clean bill of health.
        print("core markers: nothing graded, this set holds no RPC capture. Check the glob.")
    else:
        print("core markers across the corpus: "
              + ("MISSING " + ", ".join(corpus_missing) if corpus_missing else "covered"))
    return 0 if not core_gap else 2


# ── capture ──────────────────────────────────────────────────────────


class StreamRecorder:
    """Parses pi's stdout while keeping the bytes it came from intact.

    `feed` is handed one complete line and the identity of the read() that completed it. Offsets are
    stream-relative byte offsets, matching the .raw.gz file exactly.
    """

    def __init__(self, out_path: Path, raw_path: Path | None, on_event) -> None:
        self.out_path = out_path
        self.raw_path = raw_path
        self.on_event = on_event
        self.raw = gzip.open(raw_path, "wb") if raw_path else None
        self.out = out_path.open("w", encoding="utf-8")
        self.buffer = b""
        self.buffer_offset = 0
        self.chunk_index = -1
        self.chunk_bytes = 0
        self.seq = 0

    def chunk(self, data: bytes) -> None:
        """One read() from the pipe, verbatim."""
        self.chunk_index += 1
        self.chunk_bytes = len(data)
        if self.raw:
            self.raw.write(data)
        self.buffer += data
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                break
            line = self.buffer[:newline]
            line_offset = self.buffer_offset
            self.buffer = self.buffer[newline + 1:]
            self.buffer_offset += newline + 1
            self.line(line, line_offset, newline + 1)

    def line(self, line: bytes, offset: int, size: int) -> None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            event = {"type": "<non-json>", "text": text[:2000]}

        self.seq += 1
        record = {
            "seq": self.seq,
            "ts": now_iso(),
            "chunk": self.chunk_index,
            "chunk_bytes": self.chunk_bytes,
            "offset": offset,
            "bytes": size,
            "event": event,
        }
        self.out.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.out.flush()
        self.on_event(event)

    def close(self) -> None:
        for handle in (self.raw, self.out):
            try:
                if handle:
                    handle.close()
            except Exception:  # noqa: BLE001 - a capture must not die on teardown
                pass


class Capture:
    """One capture run: pi child, recorder, and the settle/abort bookkeeping a scripted run needs."""

    def __init__(self, argv: list[str], steps: list[dict], out_path: Path,
                 raw_path: Path | None, cwd: Path | None,
                 env: dict[str, str] | None = None) -> None:
        self.argv = argv
        self.steps = steps
        self.out_path = out_path
        self.raw_path = raw_path
        self.cwd = cwd
        # A child that behaves differently from the one the app spawns is a fixture that models the wrong
        # thing, so the base is the real environment and a scenario can only add to it.
        self.env = {**os.environ, **(env or {})} if env else None
        self.proc: asyncio.subprocess.Process | None = None
        self.recorder: StreamRecorder | None = None
        self.stderr: list[str] = []
        self.settled = asyncio.Event()
        self.first_tool = asyncio.Event()
        self.last_type = ""
        self.boundary_log: list[str] = []

    def observe(self, event: dict) -> None:
        etype = event.get("type", "?")
        self.last_type = etype
        if etype == "message_update":
            am = event.get("assistantMessageEvent") or {}
            etype = f"message_update/{am.get('type', '?')}"
            if am.get("type") == "toolcall_delta":
                return  # deltas would drown the progress line
        if etype == "tool_execution_start":
            self.first_tool.set()
            self.boundary_log.append(f"tool_execution_start {event.get('toolName')}")
        extra = ""
        if etype == "tool_execution_end":
            extra = f" error={bool(event.get('isError'))} {event.get('durationMs')}ms"
        elif etype == "tool_execution_start":
            extra = f" {event.get('toolName')}"
        print(f"  #{self.recorder.seq if self.recorder else 0:4d} {etype}{extra}")
        if etype == "agent_settled":
            self.settled.set()

    async def read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            data = await self.proc.stdout.read(65536)
            if not data:
                break
            assert self.recorder
            self.recorder.chunk(data)

    async def read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            data = await self.proc.stderr.read(4096)
            if not data:
                break
            for text in data.decode("utf-8", errors="replace").splitlines():
                if text.strip():
                    self.stderr.append(text)

    def send(self, payload: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

    async def delayed_send(self, delay: float, payload: dict) -> None:
        await asyncio.sleep(delay)
        self.send(payload)
        print(f"  -> sent {payload.get('type')} mid-run")

    async def run(self) -> dict:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text("", encoding="utf-8")
        self.recorder = StreamRecorder(self.out_path, self.raw_path, self.observe)

        started = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd) if self.cwd else None,
            env=self.env,
        )
        readers = [asyncio.create_task(self.read_stdout()), asyncio.create_task(self.read_stderr())]

        for step in self.steps:
            self.settled.clear()
            self.first_tool.clear()
            label = step.get("label", "prompt")
            timeout = float(step.get("timeout", 300))
            step_result: dict = {"label": label, "settled": False, "aborted_by_us": False}

            if step.get("commands"):
                # A batch of RPC commands against an idle session. Nothing here settles because nothing
                # here runs the model, so the wait is just time for the events to come back.
                print(f"\n>>> [{label}] {len(step['commands'])} rpc commands")
                for payload in step["commands"]:
                    self.send(payload)
                    await asyncio.sleep(0.5)
                await asyncio.sleep(min(timeout, 4.0))
                step_result["settled"] = True
            else:
                print(f"\n>>> [{label}] {step['message'][:76]}...")
                self.send({"type": "prompt", "message": step["message"]})

                helper = None
                if step.get("follow_up_after") is not None:
                    helper = asyncio.create_task(self.delayed_send(
                        float(step["follow_up_after"]),
                        {"type": "follow_up", "message": step.get("follow_up", "Also summarise briefly.")},
                    ))

                try:
                    if step.get("abort_after_first_tool") is not None:
                        await asyncio.wait_for(self.first_tool.wait(), timeout=timeout)
                        await asyncio.sleep(float(step["abort_after_first_tool"]))
                        print("  -> sending abort")
                        self.send({"type": "abort"})
                        step_result["aborted_by_us"] = True
                    await asyncio.wait_for(self.settled.wait(), timeout=timeout)
                    step_result["settled"] = True
                    print("  settled")
                except asyncio.TimeoutError:
                    print(f"  !! no agent_settled within {timeout}s (last event {self.last_type})")
                finally:
                    if helper:
                        helper.cancel()

            step_result["last_event"] = self.last_type
            step_result["events"] = self.recorder.seq
            self.boundary_log.append(f"step {label}: {step_result}")
            step["result"] = step_result

        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

        await asyncio.gather(*readers, return_exceptions=True)
        self.recorder.close()

        return {
            "seconds": round(time.monotonic() - started, 1),
            "events": self.recorder.seq,
            "steps": [step.get("result", {}) for step in self.steps],
        }


def find_latest_session(work_dir: Path) -> Path | None:
    """Newest pi session file whose header cwd is `work_dir`."""
    if not PI_SESSIONS_DIR.exists():
        return None
    resolved = work_dir.resolve()
    best: Path | None = None
    for jsonl in PI_SESSIONS_DIR.rglob("*.jsonl"):
        try:
            header_line = jsonl.open(encoding="utf-8", errors="replace").readline()
            header = json.loads(header_line or "{}")
            if Path(header.get("cwd", "")).resolve() == resolved:
                if best is None or jsonl.stat().st_mtime > best.stat().st_mtime:
                    best = jsonl
        except Exception:  # noqa: BLE001 - unreadable session is not our problem
            continue
    return best


def prepare_workspace() -> Path:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    for name, content in SCRATCH_FILES.items():
        target = WORKSPACE / name
        target.write_text(content, encoding="utf-8")
    for leftover in WORKSPACE.glob("out"):
        shutil.rmtree(leftover, ignore_errors=True)
    return WORKSPACE


async def run_scenario(name: str, scenario: dict, output_override: str | None,
                       use_raw: bool) -> None:
    out_path = PROJECT_ROOT / (output_override or scenario["output"])
    raw_path = out_path.with_suffix(out_path.suffix + ".gz") if use_raw else None

    cwd = prepare_workspace() if scenario.get("workspace") else None
    flags = list(scenario.get("pi_flags", []))
    if scenario.get("probe"):
        flags += ["-e", str(PROJECT_ROOT / "tests" / "fixtures" / "capture_probe.ts")]
    argv = [*resolve_pi_command(), *flags]
    # A preview store, laid out the way `preview.connection_scope_dir` lays one out for a live connection,
    # because that directory is the switch the image tool reads: with it the tool opens a pane, without it the
    # same call is a terminal draw and emits nothing. `PI_CHAT_PORT`/`_TOKEN` stay unset on purpose: no server
    # is listening here, and a fixture should show the child's honest "cannot ask the pane" path, not a fake.
    env: dict[str, str] = {}
    scope = scenario.get("preview_scope")
    if scope:
        scope_dir = PROJECT_ROOT / "previews" / "b" / str(scope)
        scope_dir.mkdir(parents=True, exist_ok=True)
        env["PI_CHAT_PREVIEW_DIR"] = str(scope_dir)
    print(f"argv     : {' '.join(argv)}")
    print(f"cwd      : {cwd or '(this directory)'}")
    print(f"previews : {env.get('PI_CHAT_PREVIEW_DIR', '(none: terminal-parity child)')}")
    print(f"capture  : {out_path}\nraw      : {raw_path or '(disabled)'}")

    capture = Capture(argv, [dict(s) for s in scenario["steps"]], out_path, raw_path, cwd, env or None)
    result = await capture.run()

    if capture.stderr:
        err_path = out_path.with_suffix(out_path.suffix + ".stderr.txt")
        err_path.write_text("\n".join(capture.stderr) + "\n", encoding="utf-8")
        print(f"stderr   : {err_path} ({len(capture.stderr)} lines)")

    meta = {
        "captured_at": now_iso(),
        "tool": "tools/capture.py",
        "argv": argv,
        "scenario": name,
        "cwd": str(cwd) if cwd else str(PROJECT_ROOT),
        "workspace_files": sorted(SCRATCH_FILES) if cwd else [],
        "preview_scope": env.get("PI_CHAT_PREVIEW_DIR"),
        "capture": str(out_path),
        "raw": str(raw_path) if raw_path else None,
        "raw_bytes": raw_path.stat().st_size if raw_path and raw_path.exists() else None,
        "session_file": None,
        "session_header": None,
        "result": result,
        "boundary_notes": capture.boundary_log[-12:],
    }

    if scenario.get("copy_native") and cwd:
        native = find_latest_session(cwd)
        if native:
            dest = PROJECT_ROOT / scenario.get("native_output", "data-samples/capture_session.jsonl")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(native, dest)
            meta["session_file"] = str(native)
            meta["session_header"] = json.loads(native.open(encoding="utf-8").readline() or "{}")
            print(f"session  : {native} -> {dest}")
        else:
            print("!! no session file found for the capture cwd")

    meta_path = out_path.with_suffix(out_path.suffix + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"meta     : {meta_path}")

    print(f"\ncaptured {result['events']} events in {result['seconds']}s")
    print_census([out_path])


async def run_prompt(prompt: str, output: str, timeout: int, use_raw: bool) -> None:
    out_path = PROJECT_ROOT / output
    raw_path = out_path.with_suffix(out_path.suffix + ".gz") if use_raw else None
    argv = [*resolve_pi_command(), "--mode", "rpc", "--no-session", "--approve"]
    scenario = {
        "steps": [{"label": "Custom", "message": prompt, "timeout": timeout}],
        "pi_flags": [],
    }
    capture = Capture(argv, [dict(s) for s in scenario["steps"]], out_path, raw_path, None)
    result = await capture.run()
    print(f"\ncaptured {result['events']} events in {result['seconds']}s -> {out_path}")
    print_census([out_path])


def expand(patterns: list[str]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        path = Path(pattern)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if any(ch in pattern for ch in "*?["):
            found.extend(sorted(path.parent.glob(path.name)))
        elif path.exists():
            found.append(path)
        else:
            print(f"!! no such file: {path}")
    return [p for p in found if p.suffix == ".jsonl" and not p.name.endswith("_session.jsonl")]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture pi --mode rpc events, chunk-exactly, for fixtures.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {k:<9} {v['description']}" for k, v in SCENARIOS.items()) + """

Examples:
  %(prog)s --list
  %(prog)s --scenario markers
  %(prog)s --prompt "list the files here" --output data-samples/custom.jsonl
  %(prog)s --census "data-samples/*.jsonl"
""",
    )
    parser.add_argument("--scenario", choices=list(SCENARIOS), help="Run a built-in capture scenario")
    parser.add_argument("--prompt", help="Capture a single custom prompt instead")
    parser.add_argument("--output", help="Output JSONL path, relative to the project root")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Seconds to wait per prompt in --prompt mode")
    parser.add_argument("--no-raw", action="store_true", help="Skip the byte-exact .raw.gz stream")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit")
    parser.add_argument("--census", nargs="+", metavar="FILE",
                        help="Report marker coverage for existing captures and exit (offline)")
    args = parser.parse_args()

    if args.list:
        for name, scenario in SCENARIOS.items():
            print(f"{name:<9} {scenario['description']}")
        return

    if args.census:
        sys.exit(print_census(expand(args.census)))

    if args.scenario:
        asyncio.run(run_scenario(args.scenario, SCENARIOS[args.scenario], args.output,
                                 use_raw=not args.no_raw))
    elif args.prompt:
        asyncio.run(run_prompt(args.prompt, args.output or "data-samples/custom_rpc_capture.jsonl",
                               args.timeout, use_raw=not args.no_raw))
    else:
        parser.error("one of --scenario, --prompt, --census, or --list is required")


if __name__ == "__main__":
    main()
