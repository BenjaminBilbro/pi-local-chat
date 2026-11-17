"""Mid-run message delivery: queue by default, steer on demand.

The bug this locks out: a prompt sent while pi was streaming arrived with no
``streamingBehavior``, pi refused it, and the refusal path unlocked the composer by tearing down
the streaming timeline - so one typed message broke the run in flight. The server now declares a
behaviour on every prompt, and the two queue actions (promote, retract) go through ``clear_queue``
because pi has no per-entry primitive.

These are dispatch-layer tests with a stub pi: the interesting part is which commands go out and
in what order, which a subprocess would only show through a real model call.
"""

from __future__ import annotations

import asyncio
import json

from pi_chat.process import PiProcess
from pi_chat.websocket import _dispatch_command, _locate_queued


class StubPi:
    """The slice of PiProcess the dispatch layer touches, with the wire recorded."""

    def __init__(
        self,
        *,
        streaming: bool = True,
        running: bool = True,
        queued: dict[str, list[str]] | None = None,
    ) -> None:
        self.sent: list[dict] = []
        self.requested: list[dict] = []
        self.browser: list[dict] = []
        self.is_streaming = streaming
        self.proc = object() if running else None
        self._queued = queued or {"steering": [], "followUp": []}

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def send_and_wait(self, command: dict, request_id: str, timeout: float = 30.0) -> dict:
        self.requested.append(command)
        if command["type"] == "clear_queue":
            return {"success": True, "data": {key: list(value) for key, value in self._queued.items()}}
        raise AssertionError(f"unexpected request command: {command['type']}")

    async def send_browser_json(self, payload: dict) -> None:
        self.browser.append(payload)


class StubSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def dispatch(pi, message: dict) -> tuple[StubSocket, StubPi]:
    """Send one command and hand back the single stream the browser would have seen.

    Every handler reports through PiProcess now, so the socket and the process share one recorded
    list: an error arriving ahead of the events it explains is a failure, and this is what catches it.
    """
    socket = StubSocket()
    socket.sent = pi.browser
    await _dispatch_command(pi, "b", message, None, None)
    return socket, pi


class TestPromptDelivery:
    async def test_default_prompt_declares_the_queue_behaviour(self):
        _, pi = await dispatch(StubPi(streaming=False), {"type": "prompt", "message": "hello"})

        assert pi.sent == [
            {"type": "prompt", "message": "hello", "streamingBehavior": "followUp"}
        ]

    async def test_send_now_asks_pi_to_steer(self):
        _, pi = await dispatch(
            StubPi(),
            {"type": "prompt", "message": "look here instead", "delivery": "now"},
        )

        assert pi.sent[0]["streamingBehavior"] == "steer"

    async def test_images_survive_the_rewrap(self):
        _, pi = await dispatch(
            StubPi(streaming=False),
            {
                "type": "prompt",
                "message": "what is this",
                "images": [{"data": "aGk=", "mimeType": "image/png"}],
            },
        )

        assert pi.sent[0]["images"] == [{"type": "image", "data": "aGk=", "mimeType": "image/png"}]

    async def test_unknown_delivery_is_refused_before_pi_sees_it(self):
        socket, pi = await dispatch(
            StubPi(streaming=False),
            {"type": "prompt", "message": "hello", "delivery": "immediately"},
        )

        assert pi.sent == []
        assert socket.sent[0]["type"] == "error"

    async def test_blank_prompt_is_still_refused(self):
        socket, pi = await dispatch(StubPi(streaming=False), {"type": "prompt", "message": "   "})

        assert pi.sent == []
        assert socket.sent[0]["type"] == "error"


class TestQueueActions:
    async def test_send_now_promotes_one_entry_and_keeps_the_rest_in_order(self):
        pi = StubPi(
            queued={
                "steering": [],
                "followUp": ["first waiting", "second waiting", "third waiting"],
            }
        )

        await dispatch(
            pi,
            {"type": "steer_now", "kind": "followUp", "index": 1, "text": "second waiting"},
        )

        assert pi.requested == [{"type": "clear_queue"}]
        assert pi.sent == [
            {"type": "steer", "message": "second waiting"},
            {"type": "follow_up", "message": "first waiting"},
            {"type": "follow_up", "message": "third waiting"},
        ]
        assert pi.browser == [{"type": "queue_action", "action": "steer", "ok": True}]

    async def test_promote_finds_the_text_in_the_other_bucket(self):
        pi = StubPi(queued={"steering": ["already steering"], "followUp": []})

        await dispatch(pi, {"type": "steer_now", "kind": "followUp", "index": 0, "text": "already steering"})

        assert pi.sent == [{"type": "steer", "message": "already steering"}]

    async def test_stale_chip_restores_the_queue_and_never_resends(self):
        # pi delivered it: clear_queue comes back without it, so the only correct action is to put
        # back what pi actually holds and say so. Resending would duplicate the message.
        pi = StubPi(queued={"steering": [], "followUp": ["something else"]})
        socket, pi = await dispatch(
            pi,
            {"type": "steer_now", "kind": "followUp", "index": 3, "text": "already delivered"},
        )

        assert pi.sent == [{"type": "follow_up", "message": "something else"}]
        assert socket.sent[0]["type"] == "error"

    async def test_promote_with_nothing_running_leaves_the_message_queued(self):
        pi = StubPi(streaming=False, queued={"steering": [], "followUp": ["waiting"]})
        socket, pi = await dispatch(
            pi,
            {"type": "steer_now", "kind": "followUp", "index": 0, "text": "waiting"},
        )

        assert pi.sent == [{"type": "follow_up", "message": "waiting"}]
        assert socket.sent[0]["type"] == "error"

    async def test_retract_drops_the_entry_without_steering_it(self):
        pi = StubPi(queued={"steering": ["keep me"], "followUp": ["drop me", "keep me too"]})

        await dispatch(pi, {"type": "retract_queued", "kind": "followUp", "index": 0, "text": "drop me"})

        assert pi.sent == [
            {"type": "steer", "message": "keep me"},
            {"type": "follow_up", "message": "keep me too"},
        ]
        assert pi.browser == [{"type": "queue_action", "action": "retract", "ok": True}]

    async def test_queue_action_without_text_is_refused(self):
        socket, pi = await dispatch(StubPi(), {"type": "retract_queued", "kind": "followUp"})

        assert (pi.sent, pi.requested) == ([], [])
        assert socket.sent[0]["type"] == "error"

    def test_locator_prefers_a_position_that_still_matches(self):
        queues = {"steering": [], "followUp": ["dup", "dup"]}

        assert _locate_queued(queues, "followUp", 1, "dup") == ("followUp", 1)

    def test_locator_falls_back_to_text_and_then_the_other_queue(self):
        queues = {"steering": ["moved"], "followUp": []}

        assert _locate_queued(queues, "followUp", 0, "moved") == ("steering", 0)
        assert _locate_queued(queues, "followUp", 0, "absent") == ("followUp", None)


class TestStdoutReader:
    """The reader that turns pi's events into what the browser sees."""

    class LineStream:
        def __init__(self, lines: list[dict]) -> None:
            self._lines = [json.dumps(line) for line in lines]

        def at_eof(self) -> bool:
            return not self._lines

        async def readline(self) -> bytes:
            if not self._lines:
                return b""
            return (self._lines.pop(0) + "\n").encode("utf-8")

    class DeadProc:
        """A child that prints these lines and then goes away, code -9 (the OOM shape)."""

        def __init__(self, lines: list[dict]) -> None:
            self.stdout = TestStdoutReader.LineStream(lines)
            self.stdin = None
            self.returncode = -9

    async def read(self, events: list[dict]) -> list[dict]:
        """Run the reader over a finished stream and return everything the browser received.

        Delivery is queued, so the writer has to be given its turn: `join()` returns once every
        enqueued message has actually gone out, which is the same guarantee the process relies on.
        """
        pi = PiProcess()
        socket = StubSocket()
        pi.proc = self.DeadProc(events)
        pi.ws = socket
        await pi._read_stdout()
        assert pi._browser_out is not None
        await asyncio.wait_for(pi._browser_out.join(), timeout=1.0)
        pi._browser_writer_task.cancel()
        # The stream ending IS the child dying, and `proc` has to reflect that: every respawn guard in
        # websocket.py is `if not pi.proc`, and it was unreachable while a dead child stayed non-None.
        assert pi.proc is None
        return socket.sent

    async def test_every_event_still_arrives_wrapped(self):
        sent = await self.read([{"type": "agent_start"}, {"type": "agent_settled"}])

        events = [message for message in sent if message["type"] == "pi_event"]
        assert [message["event"]["type"] for message in events] == ["agent_start", "agent_settled"]

        # The stream ending IS the child dying: the reader reports it and clears `proc`, which is what
        # makes the `if not pi.proc` respawn guards in websocket.py reachable again.
        exit_notice = [m for m in sent if m.get("code") == "pi_exited"]
        assert len(exit_notice) == 1, sent
        assert exit_notice[0]["type"] == "error"
        assert exit_notice[0]["exitCode"] == -9

    async def test_a_queue_change_is_also_announced_as_a_plain_state(self):
        sent = await self.read([
            {"type": "agent_start"},
            {"type": "queue_update", "steering": [], "followUp": ["later"]},
            {"type": "text_delta"},
        ])

        states = [message for message in sent if message["type"] == "queue_state"]
        assert len(states) == 1, "only a queue_update earns a state message"
        assert states[0]["followUp"] == ["later"]
        assert states[0]["steering"] == []
        # The pi_event for the same queue_update still goes through: history replay and the parity
        # harness both read the raw event, so the state message is an addition, not a replacement.
        assert sum(1 for message in sent if message["type"] == "pi_event") == 3


class TestRunAndQueueMirror:
    """The server's copy of pi's state, which is what makes a refusal unreachable."""

    @staticmethod
    def process() -> PiProcess:
        return PiProcess()

    def test_agent_lifecycle_tracks_the_run(self):
        pi = self.process()

        assert pi.is_streaming is False
        pi._observe_run_event({"type": "agent_start"})
        assert pi.is_streaming is True
        pi._observe_run_event({"type": "agent_settled"})
        assert pi.is_streaming is False

    def test_queue_update_replaces_the_whole_queue(self):
        pi = self.process()

        pi._observe_run_event({"type": "queue_update", "steering": ["a"], "followUp": ["b"]})
        assert pi.queue == {"steering": ["a"], "followUp": ["b"]}
        assert pi._observe_run_event({"type": "queue_update", "steering": [], "followUp": []}) is True
        assert pi.queue == {"steering": [], "followUp": []}

    def test_announcements_carrying_no_lists_read_as_empty(self):
        pi = self.process()
        pi.queue = {"steering": ["stale"], "followUp": ["stale"]}

        pi._observe_run_event({"type": "queue_update"})

        assert pi.queue == {"steering": [], "followUp": []}

    def test_switching_session_drops_the_old_conversations_queue(self):
        pi = self.process()
        pi._observe_run_event({"type": "queue_update", "steering": ["mine"], "followUp": []})
        pi._observe_run_event({"type": "agent_start"})

        pi._observe_run_event({"type": "response", "command": "new_session", "success": True})

        assert pi.queue == {"steering": [], "followUp": []}
        assert pi.is_streaming is False

    def test_unrelated_events_leave_the_mirror_alone(self):
        pi = self.process()
        pi._observe_run_event({"type": "agent_start"})
        pi._observe_run_event({"type": "queue_update", "steering": ["mine"], "followUp": []})

        pi._observe_run_event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}})

        assert pi.is_streaming is True
        assert pi.queue == {"steering": ["mine"], "followUp": []}


class TestCancelRun:
    """Cancelling a tool call means stopping the run: pi has no per-tool primitive.

    The order is the whole test. ``clear_queue`` first, because anything still queued would otherwise
    be delivered into a run the reader just stopped, and because its answer is the authoritative queue
    - then ``abort``, which fires the run's abort signal and kills the tool's process tree.
    """

    async def test_cancel_clears_the_queue_before_aborting(self):
        pi = StubPi(queued={"steering": ["check the tests"], "followUp": ["and the docs"]})
        _, pi = await dispatch(pi, {"type": "cancel_tool"})

        assert [command["type"] for command in pi.requested] == ["clear_queue"]
        assert [command["type"] for command in pi.sent] == ["abort"]

        # The cleared text goes to the browser rather than being resent: pi has already delivered
        # whatever left the queue, and a delivered message must not appear twice.
        assert pi.browser == [
            {"type": "cancel_result", "cleared": ["check the tests", "and the docs"]}
        ]

    async def test_the_cancel_reaches_the_browser_before_the_abort(self):
        # pi answers abort only when the session goes idle, and the tool being escaped is what holds
        # that open, so waiting to answer the browser until after it would reintroduce the wait.
        order: list[str] = []

        class OrderedPi(StubPi):
            async def send_browser_json(self, payload: dict) -> None:
                order.append("browser")
                await super().send_browser_json(payload)

            async def send(self, message: dict) -> None:
                order.append(message["type"])
                await super().send(message)

        await dispatch(OrderedPi(), {"type": "cancel_tool"})
        assert order == ["browser", "abort"]

    async def test_a_wedged_clear_queue_still_stops_the_run(self):
        class WedgedQueuePi(StubPi):
            async def send_and_wait(self, command, request_id, timeout=30.0):  # noqa: ANN001
                raise TimeoutError("pi never answered clear_queue")

        _, pi = await dispatch(WedgedQueuePi(), {"type": "cancel_tool"})

        assert [command["type"] for command in pi.sent] == ["abort"]
        assert pi.browser == [{"type": "cancel_result", "cleared": []}]
