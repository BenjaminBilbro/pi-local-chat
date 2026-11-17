"""The RPC stub has to speak like a real provider, or the UI is only tested against a fantasy.

Two behaviours are asserted here, and neither was reachable before:

1. A tool call is announced by name *before* its argument JSON arrives, as `toolcall_start` followed
   by `toolcall_delta` events, in the shape pi 1.1.0 actually puts on stdout (`tools/capture.py`
   re-measures it into `data-samples/markers_rpc_capture.jsonl`): the start carries `id` and
   `toolName`, every delta carries a `delta` string and a `contentIndex`, and no `message_update` on
   the wire carries a cumulative `partial` snapshot.
2. `abort` interrupts the turn in flight rather than merely acknowledging: the partial assistant
   message ends `stopReason: "aborted"`, a call that never answered stays unanswered, and the abort
   response comes only after the session goes idle.

No model, no network, no browser. Every read carries a deadline and the child is always terminated,
because a test that hangs costs whoever ran it their whole session: an unbounded read on a subprocess
pipe is how one verification ate a 50 minute worker budget.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKE_PI = ROOT / "tools" / "fake_rpc_pi.py"
READ_TIMEOUT = 25.0


class Stub:
    """The stub with bounded line reads, so a silent child fails the test instead of hanging it."""

    def __init__(self, **env):
        self.process = subprocess.Popen(
            [sys.executable, str(FAKE_PI), "--mode rpc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8", **{f"FAKE_RPC_{k}": str(v) for k, v in env.items()}},
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        assert self.next()["type"] == "ready", "the stub did not announce itself"

    def _pump(self) -> None:
        for line in self.process.stdout:
            self._lines.put(line.strip())
        self._lines.put(None)

    def send(self, **command) -> None:
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()

    def next(self, timeout: float = READ_TIMEOUT) -> dict:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"the stub emitted nothing for {timeout}s") from None
        if line is None:
            raise AssertionError("the stub closed stdout")
        return json.loads(line)

    def until(self, predicate, note: str, timeout: float = READ_TIMEOUT) -> list[dict]:
        """Every event up to and including the first that matches `predicate`."""
        seen: list[dict] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.next(max(0.1, deadline - time.monotonic()))
            seen.append(event)
            if predicate(event):
                return seen
        raise AssertionError(
            f"no event matched ({note}); saw: {[event.get('type') for event in seen][-30:]}"
        )

    def collect(self) -> list[dict]:
        """Everything through agent_settled. Only valid for a turn that runs to completion."""
        return self.until(lambda e: e["type"] == "agent_settled", "agent_settled")

    def close(self) -> None:
        # Only this test's own child, always: never a kill by image name on this machine.
        self.process.kill()
        self.process.wait(timeout=10)


def tool_block(event: dict) -> dict:
    """The tool block a streamed event is about, exactly the way the browser reads it."""
    message_event = event["assistantMessageEvent"]
    return message_event["partial"]["content"][message_event["contentIndex"]]


@pytest.fixture
def spawn():
    created: list[Stub] = []

    def make(**env) -> Stub:
        instance = Stub(**env)
        created.append(instance)
        return instance

    yield make
    for instance in created:
        instance.close()


def test_a_streamed_call_is_named_before_its_arguments_arrive(spawn):
    turn = spawn(STREAM_TOOLS="1", STREAM_MS="0", TOOL_MS="0", WRITE_KB="3")
    turn.send(type="prompt", message="write the file")
    seen = turn.collect()

    kinds = [
        event["assistantMessageEvent"]["type"] if event["type"] == "message_update" else event["type"]
        for event in seen
    ]
    assert "toolcall_start" in kinds, kinds
    start = kinds.index("toolcall_start")
    deltas = [index for index, kind in enumerate(kinds) if kind == "toolcall_delta"]
    assert deltas and min(deltas) > start, "arguments streamed before the call was announced"
    assert kinds.index("toolcall_end") > max(deltas), "the call completed before its arguments arrived"
    assert kinds.index("toolcall_end") < kinds.index("tool_execution_start"), \
        "execution started before the argument stream closed"

    start_event = seen[start]["assistantMessageEvent"]
    # pi's RPC transform deletes every cumulative `partial` snapshot and hand-adds id and toolName to
    # toolcall_start. A stub that sends `partial` instead lets the UI pass while real pi leaves it
    # blind, so the absence is the assertion, not an optimisation.
    assert start_event["id"], start_event
    assert start_event["toolName"] == "write", start_event
    assert "partial" not in start_event, start_event

    # Nothing parsed arrives with the announcement, which is the whole point: a UI that waits for
    # arguments sees nothing for seconds.
    completed = next(event["assistantMessageEvent"]["toolCall"] for event in seen
                     if event["type"] == "message_update"
                     and event["assistantMessageEvent"]["type"] == "toolcall_end")
    assert completed["id"] == start_event["id"], "the call was announced under one id and completed as another"

    # A multi-KB body arrives in many pieces, and the fragments are the arguments in order. This is the
    # only way to know a call's target before `toolcall_end`: the deltas are the stream.
    streamed = [event["assistantMessageEvent"] for event in seen
                if event["type"] == "message_update"
                and event["assistantMessageEvent"]["type"] == "toolcall_delta"]
    assert len(streamed) > 20, f"the streamed body is too short to exercise anything: {len(streamed)} deltas"
    assert all(piece["delta"] for piece in streamed), "an empty delta"
    assert all(piece["contentIndex"] == start_event["contentIndex"] for piece in streamed), \
        "the deltas moved to another content block, so a UI keying on the index would lose the call"
    assert all("id" not in piece and "partial" not in piece for piece in streamed), \
        "a delta carried an id or a snapshot, which pi 1.1.0 does not send"

    body = "".join(piece["delta"] for piece in streamed)
    assert json.loads(body) == completed["arguments"], \
        "the streamed fragments are not the arguments that completed the call"

    # And the honesty rule for the whole stub: no event on this stream carries a snapshot.
    assert not [event for event in seen if event["type"] == "message_update"
                and "partial" in event["assistantMessageEvent"]], \
        "the stub is streaming `partial`, which real pi strips"


def test_the_reference_capture_is_a_wire_the_stub_agrees_with(spawn, tmp_path):
    """Compare the stub against a capture of a real pi, marker by marker.

    The stub and the browser agreed while real pi disagreed with both, because nothing checked the
    stub against a capture. This is that check: the same marker vocabulary, the same field names, the
    same event types, on both sides.
    """
    capture = ROOT / "data-samples" / "markers_rpc_capture.jsonl"
    if not capture.exists():
        pytest.skip("no capture in this checkout; run `uv run python tools/capture.py --scenario markers")

    recorded = [
        json.loads(line)["event"] for line in capture.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    real = _markers_of(recorded)

    turn = spawn(STREAM_TOOLS="1", STREAM_MS="0", TOOL_MS="0", WRITE_KB="2")
    turn.send(type="prompt", message="write the file")
    stub = _markers_of(turn.collect())

    # Everything the stub claims to emit has to be something pi really emits, in the same shape.
    for marker, fields in stub.items():
        assert marker in real, f"the stub emits {marker}, which a real run never did"
        missing = fields - real[marker]
        assert not missing, f"{marker}: the stub sends fields pi does not ({sorted(missing)})"

    # And the fields the browser renders tool calls from must be present on the real wire, because
    # that is the code path a stripped snapshot used to leave dead.
    assert "id" in real["message_update/toolcall_start"], real.get("message_update/toolcall_start")
    assert "toolName" in real["message_update/toolcall_start"], real.get("message_update/toolcall_start")
    assert "delta" in real["message_update/toolcall_delta"], real.get("message_update/toolcall_delta")
    assert "partial" not in real["message_update/toolcall_start"], \
        "this capture predates the stripped wire; re-capture with `tools/capture.py --scenario markers"
    assert "partial" not in real["message_update/toolcall_delta"], \
        "this capture predates the stripped wire; re-capture with `tools/capture.py --scenario markers"
    assert "toolCall" in real["message_update/toolcall_end"], real.get("message_update/toolcall_end")


def _markers_of(events: list[dict]) -> dict[str, set[str]]:
    """marker -> the union of field names it was seen with, for message_update payloads and the rest."""
    markers: dict[str, set[str]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "message_update":
            payload = event.get("assistantMessageEvent") or {}
            name = f"message_update/{payload.get('type')}"
            markers.setdefault(name, set()).update(payload.keys())
        elif event.get("type"):
            markers.setdefault(event["type"], set()).update(event.keys())
    return markers


def test_abort_interrupts_the_turn_and_leaves_the_call_unanswered(spawn):
    turn = spawn(STREAM_TOOLS="1", STREAM_MS="12", TOOL_MS="0", WRITE_KB="12")
    turn.send(type="prompt", message="write the file")
    turn.until(
        lambda e: e["type"] == "message_update" and e["assistantMessageEvent"]["type"] == "toolcall_delta",
        "the first argument delta",
    )

    turn.send(type="abort")
    tail = turn.until(
        lambda e: e["type"] == "response" and e.get("command") == "abort",
        "the abort response",
    )

    kinds = [event["type"] for event in tail]
    assert "tool_execution_end" not in kinds, "an aborted call answered itself"
    assert "toolResult" not in [event.get("message", {}).get("role") for event in tail
                                if event["type"] == "message_end"], "an aborted call got a result"

    aborted = [
        event["message"] for event in tail
        if event["type"] == "message_end" and event["message"].get("stopReason") == "aborted"
    ]
    assert aborted, "the partial assistant message never ended as aborted"

    # pi answers abort only once the session is idle, so the settle has to precede the response.
    assert "agent_end" in kinds and "agent_settled" in kinds, kinds
    assert kinds.index("agent_settled") < len(kinds) - 1, kinds

    # The cancelled call still has to survive the run: the browser rebuilds the timeline from
    # agent_end.messages, so a partial message left out of that list would vanish on screen.
    agent_end = next(event for event in tail if event["type"] == "agent_end")
    assert any(
        message.get("stopReason") == "aborted" for message in agent_end["messages"]
    ), "agent_end dropped the partial message, so the cancelled call would disappear"


def test_the_older_inline_shape_is_untouched_by_default(spawn):
    """The streamed shape is opt-in, or every browser stage written before it silently shifts."""
    turn = spawn(TOOL_MS="0")
    turn.send(type="prompt", message="hi")
    seen = turn.collect()

    updates = [
        event["assistantMessageEvent"]["type"]
        for event in seen if event["type"] == "message_update"
    ]
    assert "toolcall_start" not in updates and "toolcall_delta" not in updates, updates
    kinds = [event["type"] for event in seen]
    assert "tool_execution_start" in kinds and "tool_execution_end" in kinds, kinds
