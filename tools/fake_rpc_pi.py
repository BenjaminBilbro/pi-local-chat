#!/usr/bin/env python
"""A fake ``pi --mode rpc`` for smoke-testing pi-chat with no model and no network.

It speaks just enough of the RPC protocol (see pi's docs/rpc.md + AGENTS.md
"pi RPC Event Format") to drive the whole browser pipeline: command acks, the
agent/turn/message lifecycle, streamed text deltas, one tool call, the steering and
follow-up queues, and the request/response commands the UI waits on.

Why this exists: pointing the app at a real pi during a smoke test burns an LLM
call, and on this machine pi's configured provider is a local inference server
already saturated by another session. Tests should not depend on either.

A turn streams slowly enough to interrupt when FAKE_RPC_TURN_MS is set, which is how
the queue gets exercised: stdin keeps being read during the turn, so a prompt that
lands mid-run is queued by its ``streamingBehavior`` exactly as a real pi would queue
it, and the queue_update announcements that follow are what the browser renders.
FAKE_RPC_TOOL_MS holds a tool call open between its start and its result, which is how
the running tool mark gets exercised: a check mark on a call that is still executing is
the bug that mark exists to avoid.

FAKE_RPC_STREAM_TOOLS=1 changes the shape of the tool call to match a real provider: the call is
announced with ``toolcall_start`` naming it, its argument JSON then arrives as ``toolcall_delta``
chunks, the assistant message ends, and only then does execution start. That is the difference
between a ``write`` appearing after its whole body has streamed and appearing the moment pi knows
what it is about to do, so it is how the streamed tool line gets exercised. ``FAKE_RPC_STREAM_MS``
paces each argument delta (a multi-KB body is a hundred-plus deltas, which is why it is not shared
with TURN_MS) and ``FAKE_RPC_WRITE_KB`` sizes the body. Off by default: the browser stages written
before it assert the older inline ordering.

LONG_RUN (with FAKE_RPC_STREAM_TOOLS) replaces the two-message turn with N rounds of
``thinking -> streamed write -> result``, each in its own turn, and one closing sentence. That is the
shape a session makes when it works for ten minutes, and the shape the reader asked to fold away.

An ``abort`` command interrupts the turn that is in flight, the way pi's own abort does: the partial
assistant message ends ``stopReason: "aborted"``, a call that had started executing gets no
``tool_execution_end`` and no tool result (pi kills the tool and leaves the call unanswered), then
``agent_end``, ``agent_settled``, and finally the abort response, which real pi only sends once the
session is idle.

Usage (it ignores argv, so it survives the app's fixed flags):

    PI_CHAT_PI_BIN="$PWD/tools/fake_rpc_pi.py" PI_CHAT_B_PASSWORD_HASH="$HASH" \\
        FAKE_RPC_TURN_MS=400 FAKE_RPC_TOOL_MS=2000 uv run python server.py

    # the streamed form, as --stage tool-streaming runs it
    PI_CHAT_PI_BIN="$PWD/tools/fake_rpc_pi.py" FAKE_RPC_STREAM_TOOLS=1 \\
        FAKE_RPC_STREAM_MS=25 uv run python server.py

Run standalone to check the protocol side:

    printf '{"type":"prompt","message":"hi"}\\n' | python tools/fake_rpc_pi.py --mode rpc
"""

from __future__ import annotations

import json
import os
import queue
import struct
import sys
import threading
import time
import uuid
import zlib
from pathlib import Path

TEXT = "Fake assistant reply from the RPC stub: alpha beta gamma."

# Characters in the streamed reply. The default one-sentence reply is what every older stage asserts
# on, and it is also why nothing ever caught the streaming path re-rendering the whole answer per
# delta: no reply this short can show it. Measured 2026-11-17, a 4.2 KB answer cost 359 render passes
# and 298x its own length handed to the sanitizer. Set this to stream a long answer instead.
REPLY_CHARS = int(os.environ.get("FAKE_RPC_REPLY_CHARS", "0"))

# Markdown, deliberately: headings, lists, inline code and a link are what the parser and the
# sanitizer actually have to work on. A wall of one word repeated would understate the cost.
_REPLY_BLOCK = (
    "## Section\n\n"
    "The quick brown fox **jumps** over the lazy dog, and does it in `monospace` too.\n\n"
    "- first item\n"
    "- second item, longer than the first, with a [link](https://example.test) in it\n"
    "- third item, which exists to make the list non-trivial\n\n"
)


def _reply_text() -> tuple[str, int]:
    """The reply to stream, and where to split it into two messages.

    The split exists because pi's default queue mode delivers one steering message at a boundary, so
    a turn with two messages is the only shape that exercises it. For the default reply the split also
    happens to land after "from the RPC stub", which is what the harness's stub-identity guard looks
    for, so it is not moved.
    """
    if REPLY_CHARS <= 0:
        return TEXT, TEXT.index(": ") + 2
    body = (_REPLY_BLOCK * (REPLY_CHARS // len(_REPLY_BLOCK) + 1))[:REPLY_CHARS]
    return body, len(body) // 2
TOOL_NAME = "bash"
STREAMED_TOOL_NAME = "write"
STREAMED_WRITE_PATH = "notes/rpc-stub-streamed-write.md"

# Milliseconds spent per streamed chunk. Non-zero makes the run long enough to send at.
TURN_MS = int(os.environ.get("FAKE_RPC_TURN_MS", "0"))
# Milliseconds the tool call stays unreturned. A long bash command is the case where a check mark on
# an executing tool is a lie, so this is how the UI's running state gets exercised without a model.
TOOL_MS = int(os.environ.get("FAKE_RPC_TOOL_MS", "0"))
# A prompt can ask for a slow tool by itself, so that one stage gets a call that is genuinely still
# unreturned without every turn in the harness paying for it. `tools/smoke_test_ws.py` sends this marker to
# cancel a call mid-flight; the hold runs through `_pause_and_serve`, which keeps serving stdin and raises
# `_Aborted` the moment an abort is acknowledged, so the turn unwinds where the user stopped it.
HOLD_TOOL_MARKER = "FAKE_RPC_HOLD_TOOL"
HOLD_TOOL_MS = int(os.environ.get("FAKE_RPC_HOLD_TOOL_MS", "6000"))
_hold_tool_ms = 0
# Announce and stream the tool call the way a real provider does. See the module docstring.
STREAM_TOOLS = os.environ.get("FAKE_RPC_STREAM_TOOLS", "") == "1"
# Milliseconds per streamed tool-argument delta.
STREAM_MS = int(os.environ.get("FAKE_RPC_STREAM_MS", "0"))
# Kilobytes of file content in the streamed write body. Long on purpose: the point is a tool call
# whose arguments take seconds to arrive.
WRITE_KB = int(os.environ.get("FAKE_RPC_WRITE_KB", "6"))
# Rounds of thinking -> write -> result in one run. 0 keeps the short turn every older stage expects.
LONG_RUN = int(os.environ.get("FAKE_RPC_LONG_RUN", "0"))

# Image generation. FAKE_RPC_IMAGE=1 replaces the turn with a `generate_image` call whose event
# sequence is copied from a real capture (`data-samples/imagegen_rpc_capture.jsonl`), including the two
# `pichat_preview` entries the tool writes itself and the phase/percent stream it puts on
# `tool_execution_update`. The whole loading story lives between those two entries, so a stub that
# emitted only a finished result could not exercise any of it - and the dots are the part Ben asked for.
IMAGE_MODE = os.environ.get("FAKE_RPC_IMAGE", "") == "1"
IMAGE_CANVAS = os.environ.get("FAKE_RPC_IMAGE_CANVAS", "512x512")
# The simulated draw has to outlast the harness's settle time or the screenshot catches the answer
# instead of the wait. Nine seconds is long enough to shoot mid-draw and short enough to stay boring.
IMAGE_MS = int(os.environ.get("FAKE_RPC_IMAGE_MS", "9000"))
# Stop the draw after N progress updates and report it cancelled: the pane's error state, on the same
# wire shape the abort capture recorded.
IMAGE_ABORT_AFTER = int(os.environ.get("FAKE_RPC_IMAGE_ABORT_AFTER", "0"))
# The progress stream of one real 512x512 draw: phase, pct, step, total, eta seconds, in the order
# sd-cli's stdout produced them. Copied rather than invented because the shape of the middle - the
# percent that repeats while the weights load, the eta that appears only at the step boundary - is what
# a placeholder has to survive.
IMAGE_PROGRESS = (
    ("borrow", 2, None, None, None),
    ("prepare", 6, 65, 397, None),
    ("prepare", 8, 159, 397, None),
    ("prepare", 10, 254, 397, None),
    ("prepare", 12, 351, 397, None),
    ("prepare", 15, 43, 265, None),
    ("prepare", 16, 100, 265, None),
    ("prepare", 18, 157, 265, None),
    ("prepare", 20, 221, 265, None),
    ("sample", 36, 1, 8, 43),
    ("sample", 58, 5, 8, 3),
    ("sample", 74, 8, 8, None),
    ("sample", 74, 8, 8, None),
    ("sample", 74, 8, 8, None),
    ("sample", 74, 8, 8, None),
    ("decode", 76, 1, 9, 18),
    ("decode", 88, 6, 9, 1),
    ("decode", 95, 9, 9, None),
    ("save", 99, 9, 9, None),
)
# Characters of argument JSON per toolcall_delta. Real providers differ; one word is too gentle to
# catch a UI that only looks at the parsed arguments.
STREAM_CHUNK = 48

# The identity a partial assistant message carries, so anything reading the model off the stream
# reads the same thing here as it does from a capture.
MODEL_INFO = {"api": "openai-completions", "provider": "fake", "model": "fake/stub"}

_state = {"turns": 0, "messages": [], "busy": False, "abort_requested": False, "abort_ack": None}
# The turn in flight, so an abort can close it with what actually streamed instead of pretending.
_turn = {
    "turn_open": False,
    "message_open": False,
    "blocks": [],
    "tool": None,
}
_queues = {"steering": [], "followUp": []}
_pending: queue.Queue[str | None] = queue.Queue()
_stdin_done = threading.Event()
_emit_lock = threading.Lock()


class _Aborted(Exception):
    """Raised inside a streaming turn when an abort arrives, to unwind it at that point."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def emit(event: dict) -> None:
    with _emit_lock:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _response(command: str, request_id: str | None = None, **extra) -> dict:
    """Build a response event without emitting it, for the one command that answers later."""
    event: dict = {"type": "response", "command": command, "success": True}
    if request_id:
        event["id"] = request_id
    event.update(extra)
    return event


def zero_usage() -> dict:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def assistant_partial(blocks: list[dict], stop_reason: str = "stop") -> dict:
    """The ``partial`` every message_update carries: the assistant message as it stands mid-stream.

    A capture's partial is a whole assistant message, not just the block being streamed, and the
    renderer is allowed to read anything out of it, so it is built that way here too.
    """
    return {
        "role": "assistant",
        "content": [dict(block) for block in blocks],
        **MODEL_INFO,
        "usage": zero_usage(),
        "stopReason": stop_reason,
        "timestamp": _now_ms(),
    }


def assistant_message(text: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {"input": 12, "output": len(text.split()), "totalTokens": 12 + len(text.split())},
        "stopReason": "stop",
        "timestamp": int(time.time() * 1000),
    }


def user_message(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": int(time.time() * 1000)}


def announce_queue() -> None:
    emit({"type": "queue_update", "steering": list(_queues["steering"]), "followUp": list(_queues["followUp"])})


def queue_message(kind: str, text: str) -> None:
    """Queue like pi does: the announcement carries the complete queue, not the delta."""
    _queues[kind].append(text)
    announce_queue()


def run_prompt(prompt_text: str) -> None:
    """Emit one believable agent turn, serving any command that arrives while it streams.

    The turn is two assistant messages with a boundary between them, because that boundary is where a
    steered message actually lands: after the tool batch, before the model's next call. A stub that
    delivered only at the end of the run could not catch a UI that renders a message in the wrong
    place, which is the bug this shape exists to reproduce.
    """
    global _hold_tool_ms
    _hold_tool_ms = HOLD_TOOL_MS if HOLD_TOOL_MARKER in prompt_text else TOOL_MS
    _state["turns"] += 1
    _state["busy"] = True
    _state["abort_requested"] = False
    # pi announces the run before it replays the prompt that started it, so a browser that keys its
    # assistant block off agent_start sees the same order here as it does live.
    emit({"type": "agent_start"})
    try:
        if LONG_RUN and STREAM_TOOLS:
            _run_long_run(LONG_RUN, prompt_text)
            emit({"type": "agent_end", "messages": list(_state["messages"])})
            return
        if IMAGE_MODE:
            _run_image_turn(prompt_text)
            emit({"type": "agent_end", "messages": list(_state["messages"])})
            return
        # Split so the two-message queue boundary is exercised. For the default reply that split
        # lands after "from the RPC stub", which is what the harness's stub-identity guard reads.
        reply, split = _reply_text()
        _begin_turn()
        emit_user_message(prompt_text)
        first = _stream_message(reply[:split])
        _end_turn(first)

        # pi's default queue mode is one-at-a-time, so a boundary takes exactly one steering message,
        # and it leaves the queue in the same event that delivers it.
        if _queues["steering"]:
            delivered = _queues["steering"].pop(0)
            announce_queue()
            emit_user_message(delivered)

        _begin_turn()
        second = _stream_message(reply[split:], with_tool_call=False)
        _end_turn(second)
        emit({"type": "agent_end", "messages": list(_state["messages"])})
    except _Aborted:
        # Stopped mid-turn: close what is open with what streamed, and leave an executing call
        # unanswered, because that is the state a real abort leaves in the session file.
        _finish_aborted_turn()
    finally:
        _turn["message_open"] = False
        _turn["turn_open"] = False
        _turn["blocks"] = []
        _turn["tool"] = None
        _state["busy"] = False
        emit({"type": "agent_settled"})
        # Real pi's abort waits for the session to go idle before it answers, so the response goes
        # after the settle rather than at the moment the command was read.
        if _state["abort_requested"] and _state["abort_ack"] is not None:
            emit(_state["abort_ack"])
        _state["abort_requested"] = False
        _state["abort_ack"] = None

    _run_next_queued()


def _run_image_turn(prompt_text: str) -> None:
    """One `generate_image` call, in the order a real one reaches the browser.

    The two preview entries are the point. `open` arrives with a canvas and no bytes, which is the only
    reason a pane can draw something the right shape before sd-cli has produced anything; `update` arrives
    with the file, and the pane's job is to be holding the same rectangle by then. Everything between
    them is `tool_execution_update`, which is deliberately not persisted anywhere.
    """
    doc_id = uuid.uuid4().hex
    call_id = f"call-image-{doc_id[:12]}"
    prompt = (prompt_text or "A red fox")[:240]
    title = prompt[:60]
    model = "q4"

    _begin_turn()
    emit_user_message(prompt_text)
    _begin_message()
    _stream_text(f"Drawing that now: {prompt}")

    emit({
        "type": "tool_execution_start",
        "toolCallId": call_id,
        "toolName": "generate_image",
        "args": {"prompt": prompt, "size": IMAGE_CANVAS},
    })
    _emit_preview({
        "v": 2,
        "op": "open",
        "docId": doc_id,
        "kind": "image",
        "title": title,
        "revision": 1,
        "state": "rendering",
        "canvas": IMAGE_CANVAS,
        "note": "Qwen Turbo - rendering",
    })

    steps = IMAGE_PROGRESS
    slice_ms = IMAGE_MS // max(1, len(steps))
    failed: str | None = None
    for index, (phase, pct, step, total, eta) in enumerate(steps):
        if IMAGE_ABORT_AFTER and index >= IMAGE_ABORT_AFTER:
            failed = "cancelled (restored (cache 14336 MiB, 822 MiB free))"
            break
        progress: dict = {"phase": phase, "pct": pct, "canvas": IMAGE_CANVAS, "model": model}
        if step is not None:
            progress["step"] = step
            progress["total"] = total
        if eta is not None:
            progress["etaSeconds"] = eta
        emit({
            "type": "tool_execution_update",
            "toolCallId": call_id,
            "toolName": "generate_image",
            "args": {"prompt": prompt, "size": IMAGE_CANVAS},
            "partialResult": {
                "content": [{"type": "text", "text": f"{phase} {step or 0}/{total or 0} {pct}% ({model} {IMAGE_CANVAS})"}],
                "details": {
                    "tool": "generate_image",
                    "size": IMAGE_CANVAS,
                    "model": model,
                    "progress": progress,
                    "doc": doc_id,
                },
            },
        })
        _pause_and_serve(slice_ms)
        _raise_if_aborted()

    if failed:
        _emit_preview({
            "v": 2,
            "op": "update",
            "docId": doc_id,
            "kind": "image",
            "title": title,
            "revision": 2,
            "state": "error",
            "canvas": IMAGE_CANVAS,
            "note": failed,
        })
        emit({
            "type": "tool_execution_end",
            "toolCallId": call_id,
            "toolName": "generate_image",
            "result": {"content": [{"type": "text", "text": f"generate_image failed: {failed}"}]},
            "isError": True,
        })
    else:
        written = _write_preview_png(doc_id)
        _emit_preview({
            "v": 2,
            "op": "update",
            "docId": doc_id,
            "kind": "image",
            "title": title,
            "revision": 2,
            "state": "ready",
            **({"file": written} if written else {}),
            "canvas": IMAGE_CANVAS,
            "note": f"Qwen Turbo - {IMAGE_CANVAS} - {IMAGE_MS / 1000:.1f}s - seed 545702411",
        })
        output = f"Wrote image {doc_id}.png ({IMAGE_CANVAS})"
        emit({
            "type": "tool_execution_end",
            "toolCallId": call_id,
            "toolName": "generate_image",
            "result": {"content": [{"type": "text", "text": output}], "details": {"doc": doc_id}},
            "isError": False,
        })
        result = {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": "generate_image",
            "content": [{"type": "text", "text": output}],
            "isError": False,
            "timestamp": _now_ms(),
        }
        emit({"type": "message_start", "message": result})
        emit({"type": "message_end", "message": result})
        _state["messages"].append(result)

    final = assistant_message("Cancelled." if failed else "There it is.")
    _turn["message_open"] = False
    _state["messages"].append(final)
    emit({"type": "message_end", "message": final})
    _end_turn(final)


def _emit_preview(data: dict) -> None:
    """One persisted preview entry, wrapped the way pi wraps a custom entry.

    The real tool appends these with `pi.appendEntry`, and the server forwards them to the browser inside
    an `entry_appended` event whose `entry.data` is the payload. The pane reads `data` only, so getting
    the wrapper right is what makes this stub test the same code path as the tool.
    """
    emit({
        "type": "entry_appended",
        "entry": {
            "type": "custom",
            "customType": "pichat_preview",
            "data": data,
            "id": uuid.uuid4().hex[:8],
            "parentId": uuid.uuid4().hex[:8],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z",
        },
    })


def _write_preview_png(doc_id: str) -> str | None:
    """Put real bytes where the server will look for them, and return the file name.

    Without this the `ready` state is only ever tested against a 404, which is the one thing a pane must
    not be able to pass by accident. The scope directory is the one the server made for this connection
    and pointed the child at, so writing here is exactly what the image tool does.
    """
    directory = (os.environ.get("PI_CHAT_PREVIEW_DIR") or "").strip()
    if not directory:
        return None
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        wide, tall = (part.strip() for part in IMAGE_CANVAS.lower().replace("\u00d7", "x").split("x"))
        _sample_png(Path(directory) / f"{doc_id}.png", int(wide), int(tall))
    except (OSError, ValueError):
        return None
    return f"{doc_id}.png"


def _sample_png(path: Path, width: int, height: int) -> None:
    """Write a decodable PNG with no dependencies: a gradient and a hexagon.

    Hand-rolled because the fake must run with nothing installed, and the browser has to be able to
    decode the result - a `.png` full of zeros would prove the fetch works and nothing else.
    """
    width = max(8, min(width, 2048))
    height = max(8, min(height, 2048))
    radius = min(width, height) * 0.3
    centre_x, centre_y = width / 2, height / 2
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 per scanline
        for x in range(width):
            t = (x / max(1, width - 1) + y / max(1, height - 1)) / 2
            red = int(11 + t * (255 - 11))
            green = int(32 + t * (54 - 32))
            blue = int(38 + t * (33 - 38))
            # A soft-edged hexagon in the accent ramp, so a resized image is visibly resized.
            dx, dy = x - centre_x, y - centre_y
            distance = max(abs(dx), abs(dx * 0.5 + dy * 0.866), abs(-dx * 0.5 + dy * 0.866))
            if distance < radius:
                red, green, blue = 255, 54 + int(120 * (distance / radius)), 33
            raw += bytes((red, green, blue))
    chunks = [
        b"\x89PNG\r\n\x1a\n",
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)),
        _png_chunk(b"IEND", b""),
    ]
    path.write_bytes(b"".join(chunks))


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def _run_long_run(rounds: int, prompt_text: str) -> None:
    """N rounds of thinking -> streamed write -> result, then one closing sentence.

    Each round is its own turn, because that is what a real run does: thinking and the call share one
    assistant message, the result opens the next turn, and prose appears only when the model has
    something to say. A fold has to survive that shape, not just one long message, since the rounds
    are separate messages in the file.
    """
    _begin_turn()
    emit_user_message(prompt_text)
    for index in range(rounds):
        if index:
            _begin_turn()

        call_id = f"tool-long-{index + 1}"
        _begin_message(full=True)
        thinking_index = _stream_thinking(
            f"Round {index + 1}: check the tree, then write the note for this step."
        )
        block = _stream_tool_arguments(call_id, path=f"notes/round-{index + 1}.md")
        final = _close_message_with_call(thinking_index, block)
        _run_tool_execution(
            call_id, STREAMED_TOOL_NAME, block["arguments"],
            f"Saved {block['arguments']['path']}", record=True,
        )
        _end_turn(final)
        _raise_if_aborted()

    _begin_turn()
    closing = _stream_message(
        "Rounds done: the tree is clean and every step left a note behind.",
        with_tool_call=False,
    )
    _end_turn(closing)


def _begin_turn() -> None:
    emit({"type": "turn_start"})
    _turn["turn_open"] = True


def _end_turn(message: dict) -> None:
    _turn["turn_open"] = False
    emit({"type": "turn_end", "message": message})


def _finish_aborted_turn() -> None:
    """Close a turn pi was told to stop, in the order pi closes one.

    The message ends ``aborted`` holding whatever had streamed, and a call whose execution had
    started gets no result at all: pi kills the tool and leaves the call unanswered. That missing
    result is the only signal the UI has that a call was cancelled rather than completed, so a stub
    that tidied it up would hide the case.
    """
    if _turn["message_open"]:
        _finish_message("aborted")
        _turn["message_open"] = False
    if _turn["turn_open"]:
        _turn["turn_open"] = False
        last = _state["messages"][-1] if _state["messages"] else {}
        emit({"type": "turn_end", "message": last})
    emit({"type": "agent_end", "messages": list(_state["messages"]), "willRetry": False})


def _stream_message(text: str, *, with_tool_call: bool = True) -> dict:
    """Stream one assistant message, optionally closing it with a tool call and its result.

    With FAKE_RPC_STREAM_TOOLS the call is announced and streamed first and the message ends before
    execution starts, which is real pi's order. The older inline ordering stays behind that gate:
    the browser stages written before it assert that shape, and a default that shifts them buys
    nothing.
    """
    call_id = f"tool-{_state['turns']}"

    if STREAM_TOOLS and with_tool_call:
        _begin_message(full=True)
        return _stream_message_streamed(text, call_id)

    _begin_message()
    streamed = _stream_text(text)

    if with_tool_call:
        _run_tool_execution(call_id, TOOL_NAME, {"command": "echo hi"}, "hi")

    final = assistant_message(streamed)
    _turn["message_open"] = False
    _state["messages"].append(final)
    emit({"type": "message_end", "message": final})
    return final


def _begin_message(*, full: bool = False) -> None:
    """Open an assistant message the way pi does: an empty skeleton, then the blocks stream in."""
    _turn["blocks"] = []
    _turn["tool"] = None
    _turn["message_open"] = True
    skeleton = {"role": "assistant", "content": []}
    if full:
        skeleton = {**skeleton, **MODEL_INFO, "usage": zero_usage(), "stopReason": "stop", "timestamp": _now_ms()}
    emit({"type": "message_start", "message": skeleton})


def _stream_text(text: str) -> str:
    """Stream one text block and leave it on the message, as a capture does."""
    index = len(_turn["blocks"])
    block = {"type": "text", "text": ""}
    _turn["blocks"].append(block)
    emit({"type": "message_update", "assistantMessageEvent": {"type": "text_start"}})
    for chunk in _chunks(text, 12):
        block["text"] += chunk
        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": chunk}})
        _pause_and_serve()
    emit({
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_end", "content": block["text"], "contentIndex": index},
    })
    return block["text"]


def _stream_message_streamed(text: str, call_id: str) -> dict:
    """An assistant message whose tool call streams, in the order a capture shows it.

    thinking, text, then the call: ``toolcall_start`` names it and every ``toolcall_delta`` adds
    argument text. ``thinking_end`` comes late, right before the block that ends last, because a
    capture emits it that way and the renderer keys its thinking item off those two events. The
    message ends, and only then does execution start: the tool line has to be on screen while the
    arguments are still arriving, which is the entire point of this path.
    """
    thinking_index = _stream_thinking(
        "The run should leave a file behind, so I will write one from the RPC stub."
    )
    _stream_text(text)
    block = _stream_tool_arguments(call_id)
    final = _close_message_with_call(thinking_index, block)
    _run_tool_execution(call_id, STREAMED_TOOL_NAME, block["arguments"], f"Saved {block['arguments']['path']}", record=True)
    return final


def _close_message_with_call(thinking_index: int, block: dict) -> dict:
    """End an assistant message that a streamed call closes, in a capture's order.

    ``thinking_end`` arrives late, right before the block that ends last, and the message ends before
    execution starts. Shared by the single-call turn and the long run so the two cannot drift.
    """
    emit({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "thinking_end",
            "contentIndex": thinking_index,
            "content": _turn["blocks"][thinking_index]["thinking"],
        },
    })
    emit({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "toolcall_end",
            "contentIndex": len(_turn["blocks"]) - 1,
            "toolCall": {"type": "toolCall", "id": block["id"], "name": block["name"], "arguments": block["arguments"]},
        },
    })

    _turn["message_open"] = False
    return _finish_message("toolUse")


def _stream_thinking(text: str) -> int:
    index = len(_turn["blocks"])
    block = {"type": "thinking", "thinking": "", "thinkingSignature": "reasoning_content"}
    _turn["blocks"].append(block)
    emit({
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_start", "contentIndex": index},
    })
    for chunk in _chunks(text, 24):
        block["thinking"] += chunk
        emit({
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "contentIndex": index,
                "delta": chunk,
            },
        })
        _pause_and_serve()
    return index


def _stream_tool_arguments(call_id: str, path: str | None = None) -> dict:
    """Announce the call, then stream its argument JSON.

    The shape is pi's RPC shape, not the SDK's: ``toJsonAssistantMessageEvent`` deletes every
    cumulative ``partial`` snapshot before the event reaches stdout and puts ``id`` and ``toolName`` on
    ``toolcall_start`` by hand. So ``toolcall_delta`` carries a string fragment and a block index and
    nothing else - no id, and no arguments until ``toolcall_end`` delivers the finished call. A stub
    that adds ``partial`` back is a stub the UI can pass while the real thing leaves it blind, which is
    the bug this file used to hide.
    """
    args = {"path": path or STREAMED_WRITE_PATH, "content": _write_body(WRITE_KB)}
    raw = json.dumps(args, ensure_ascii=False)
    index = len(_turn["blocks"])
    block = {
        "type": "toolCall",
        "id": call_id,
        "name": STREAMED_TOOL_NAME,
        "arguments": {},
    }
    _turn["blocks"].append(block)
    _turn["tool"] = block
    emit({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "toolcall_start",
            "contentIndex": index,
            "id": call_id,
            "toolName": STREAMED_TOOL_NAME,
        },
    })
    _pause_stream()

    for chunk in _chunks(raw, STREAM_CHUNK):
        emit({
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "toolcall_delta",
                "contentIndex": index,
                "delta": chunk,
            },
        })
        _pause_stream()

    block["arguments"] = args
    return block


def _write_body(kb: int) -> str:
    """A file body long enough that streaming its arguments takes seconds, not microseconds."""
    lines = ["# Streamed write from the RPC stub", "", "Written one argument chunk at a time.", ""]
    n = 1
    while sum(len(line) + 1 for line in lines) < kb * 1024:
        lines.append(f"{n:04d} - alpha beta gamma delta epsilon zeta {n * 7 % 97}")
        n += 1
    return "\n".join(lines) + "\n"


def _finish_message(stop_reason: str) -> dict:
    """End the open assistant message with the blocks it actually streamed."""
    message = {
        "role": "assistant",
        "content": [dict(block) for block in _turn["blocks"]],
        **MODEL_INFO,
        "usage": zero_usage(),
        "stopReason": stop_reason,
        "timestamp": _now_ms(),
    }
    _state["messages"].append(message)
    emit({"type": "message_end", "message": message})
    return message


def _run_tool_execution(call_id: str, name: str, args: dict, output: str, *, record: bool = False) -> None:
    """The execution half of a call: start, one update, the hold, the result.

    ``record`` adds the result to the message list. Only the streamed path does that, so the older
    path's agent_end stays byte-identical for the stages already written against it.
    """
    emit({"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": args})
    emit({"type": "tool_execution_update", "toolCallId": call_id, "toolName": name, "partialResult": output})
    if _hold_tool_ms:
        _pause_and_serve(_hold_tool_ms)
    emit(
        {
            "type": "tool_execution_end",
            "toolCallId": call_id,
            "toolName": name,
            "result": {"content": [{"type": "text", "text": output}]},
            "isError": False,
        }
    )
    result = {
        "role": "toolResult",
        "toolCallId": call_id,
        "toolName": name,
        "content": [{"type": "text", "text": output}],
        "isError": False,
        "timestamp": _now_ms(),
    }
    emit({"type": "message_start", "message": result})
    emit({"type": "message_end", "message": result})
    if record:
        _state["messages"].append(result)
    _pause_and_serve()


def _pause_stream() -> None:
    """Pace one streamed argument delta, still serving stdin."""
    _pause_and_serve(STREAM_MS if STREAM_MS else (TURN_MS // 4 if TURN_MS else 0))


def emit_user_message(text: str) -> None:
    """A user message is emitted where it is delivered, which is not always where it was typed."""
    user = user_message(text)
    _state["messages"].append(user)
    emit({"type": "message_start", "message": user})
    emit({"type": "message_end", "message": user})


def _run_next_queued() -> None:
    """Whatever the turn boundaries did not reach runs as its own turn, pi-style."""
    for kind in ("steering", "followUp"):
        if not _queues[kind]:
            continue
        text = _queues[kind].pop(0)
        announce_queue()
        run_prompt(text)
        return


def _pause_and_serve(ms: int | None = None) -> None:
    """Wait out the chunk budget, serving commands that arrive: pi never stops reading stdin.

    Raises _Aborted when a command it served asked for the run to stop, so the turn unwinds at the
    point the user cancelled rather than finishing the chunk, the call, or the 20-minute script.
    """
    _raise_if_aborted()
    deadline = time.monotonic() + (TURN_MS if ms is None else ms) / 1000.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            line = _pending.get(timeout=min(remaining, 0.05))
        except queue.Empty:
            continue
        if line is None:
            return
        dispatch(line)
        _raise_if_aborted()


def _raise_if_aborted() -> None:
    if _state["abort_requested"]:
        raise _Aborted()


def dispatch(line: str | None) -> None:
    if not line:
        return
    try:
        command = json.loads(line)
    except json.JSONDecodeError:
        emit({"type": "response", "command": "unknown", "success": False, "error": "invalid JSON"})
        return
    try:
        handle(command)
    except Exception as error:  # keep the pipe alive like the real thing
        emit({"type": "response", "command": command.get("type"), "success": False, "error": str(error)})


def handle(command: dict) -> None:
    ctype = command.get("type")
    request_id = command.get("id")

    def ack(success: bool = True, **extra) -> None:
        event: dict = {"type": "response", "command": ctype, "success": success}
        if request_id:
            event["id"] = request_id
        event.update(extra)
        emit(event)

    def queued_ack() -> None:
        ack(data={"disposition": "queued"})

    if ctype == "prompt":
        text = str(command.get("message", ""))
        behavior = command.get("streamingBehavior")
        if _state["busy"]:
            # The real pi refuses a bare prompt mid-run; declaring a behavior is what queues it.
            if behavior == "steer":
                ack(data={"disposition": "queued"})
                queue_message("steering", text)
            elif behavior == "followUp":
                ack(data={"disposition": "queued"})
                queue_message("followUp", text)
            else:
                ack(False, error="Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the message.")
            return
        ack(data={"disposition": "started"})
        run_prompt(text)
    elif ctype == "steer":
        queued_ack()
        queue_message("steering", str(command.get("message", "")))
    elif ctype == "follow_up":
        queued_ack()
        queue_message("followUp", str(command.get("message", "")))
    elif ctype == "clear_queue":
        payload = {kind: list(items) for kind, items in _queues.items()}
        for kind in _queues:
            _queues[kind] = []
        ack(data=payload)
        announce_queue()
    elif ctype == "get_messages":
        ack(data={"messages": list(_state["messages"])})
    elif ctype == "get_state":
        ack(
            data={
                "sessionId": "fake-session",
                "sessionFile": None,
                "model": "fake/stub",
                "isStreaming": _state["busy"],
                "pendingMessageCount": len(_queues["steering"]) + len(_queues["followUp"]),
            }
        )
    elif ctype == "abort":
        # Mid-run: mark the abort and let the streaming turn unwind at its next pause, then answer
        # once the session is idle, which is when real pi's abort response arrives. Idle: answer now.
        if _state["busy"]:
            _state["abort_requested"] = True
            _state["abort_ack"] = _response(ctype, request_id, data={"cancelled": True})
        else:
            ack(data={"cancelled": True})
    elif ctype in {"new_session", "switch_session", "compact"}:
        if ctype == "new_session":
            _state["messages"] = []
            _state["turns"] = 0
            _queues["steering"] = []
            _queues["followUp"] = []
        ack(data={"cancelled": False})
    else:
        ack()


def _read_stdin() -> None:
    """A thread so a streaming turn never blocks the next command, like pi's async run loop."""
    for line in sys.stdin:
        _pending.put(line.strip())
    _stdin_done.set()
    _pending.put(None)


def main() -> int:
    # The app pipes UTF-8 JSONL; on Windows stdin/stdout default to the console codepage.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")

    emit({"type": "ready", "pid": __import__("os").getpid()})

    threading.Thread(target=_read_stdin, daemon=True).start()

    while True:
        try:
            line = _pending.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:
            return 0
        dispatch(line)


if __name__ == "__main__":
    raise SystemExit(main())
