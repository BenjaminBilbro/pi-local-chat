"""Delivery and death of the pi child, and of the browser on the other end.

These are the paths the 2026-11-17 audit called out as silent: a child that exits on its own was
indistinguishable from a working session, a browser that stopped reading could wedge the child, and
both swallowed the evidence. Every test here is written against the old behavior first, so it fails
against it, and the file says which claim each one holds.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from pi_chat import process as process_module
from pi_chat.process import PiProcess, parse_queue_snapshot


class LineStream:
    """pi's stdout: these lines, then EOF, which is what a dead child looks like from here."""

    def __init__(self, lines: list[dict]) -> None:
        self._lines = [json.dumps(line) for line in lines]

    def at_eof(self) -> bool:
        return not self._lines

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return (self._lines.pop(0) + "\n").encode("utf-8")


class SilentStream:
    """A child that is alive and saying nothing: readline never returns."""

    def at_eof(self) -> bool:
        return False

    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""


class FakeChild:
    def __init__(self, lines: list[dict] | None = None, *, returncode: int | None = -9) -> None:
        self.stdout = LineStream(lines or [])
        self.stderr = LineStream([])
        self.stdin = None
        self.returncode = returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


class RecordingSocket:
    """A browser that keeps up: it records what arrived, in order."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.close_code: int | None = None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(("json", payload))

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(("bytes", payload))

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


class HangingSocket(RecordingSocket):
    """A tab that stopped reading. The first send never finishes."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(("json", payload))
        self.entered.set()
        await asyncio.Event().wait()


class BrokenSocket(RecordingSocket):
    """A tab that is gone: every send raises, the way a closed websocket does."""

    async def send_json(self, payload: dict) -> None:
        self.sent.append(("json", payload))
        raise RuntimeError("connection is already finished")


async def _drain(pi: PiProcess, timeout: float = 1.0) -> None:
    """Wait until everything enqueued so far has been handed to the socket."""
    assert pi._browser_out is not None, "nothing was ever queued, so there is nothing to drain"
    await asyncio.wait_for(pi._browser_out.join(), timeout=timeout)


async def _stopped_writer(pi: PiProcess) -> None:
    if pi._browser_writer_task and not pi._browser_writer_task.done():
        pi._browser_writer_task.cancel()


class TestShutdownSilence:
    """`BE-05`: the shutdown path that swallowed everything it did not expect.

    The timeout branch of the outbox drain was logged, and every other exception was `pass`. That is the
    narrowest possible window into the one subsystem whose whole failure mode is "pi answered and nothing
    appeared", and it is the same mistake the send paths next door already fixed.
    """

    async def test_an_outbox_that_raises_during_the_drain_says_so(self, caplog):
        pi = PiProcess()
        pi.proc = FakeChild([])
        pi.ws = RecordingSocket()

        class ExplodingOutbox:
            """Not slow, broken: `join()` itself refuses, which is what the timeout branch cannot report."""

            def join(self):
                raise RuntimeError("queue torn down under us")

        async def pending_forever() -> None:
            await asyncio.Event().wait()

        pi._browser_out = ExplodingOutbox()
        pi._browser_writer_task = asyncio.create_task(pending_forever())

        with caplog.at_level(logging.WARNING, logger="pi_chat.process"):
            await asyncio.wait_for(pi.kill(), timeout=5.0)

        assert "Browser outbox drain raised RuntimeError" in caplog.text
        assert pi._browser_writer_task is None, "teardown still has to finish, log or no log"
        assert pi._browser_out is None


# ---------------------------------------------------------------- the child dying on its own


class TestUnexpectedChildExit:
    """BE-10: EOF on pi's stdout used to just stop the reader loop."""

    async def test_the_browser_is_told_once_and_the_child_is_reported_gone(self):
        pi = PiProcess()
        socket = RecordingSocket()
        pi.proc = FakeChild([{"type": "agent_start"}, {"type": "agent_settled"}], returncode=137)
        pi.ws = socket

        await asyncio.wait_for(pi._read_stdout(), timeout=2.0)
        await _drain(pi)
        await _stopped_writer(pi)

        notices = [p for _, p in socket.sent if p.get("code") == "pi_exited"]
        assert len(notices) == 1, "one explanation, not one per dropped event"
        assert notices[0]["type"] == "error", "the browser already renders this type"
        assert notices[0]["exitCode"] == 137
        assert pi.proc is None, "`if not pi.proc` is the respawn guard; it must mean what it says"

    async def test_a_waiter_is_released_instead_of_waiting_out_its_timeout(self):
        pi = PiProcess()
        pi.ws = RecordingSocket()
        pi.proc = FakeChild([])
        waiter = asyncio.get_running_loop().create_future()
        pi._pending_requests["abc"] = waiter

        await asyncio.wait_for(pi._read_stdout(), timeout=2.0)
        await _drain(pi)
        await _stopped_writer(pi)

        assert pi._pending_requests == {}, "nothing stays registered against a child that is gone"
        with pytest.raises(RuntimeError, match="exited"):
            waiter.result()

    async def test_an_expected_kill_is_not_reported_as_a_crash(self):
        """`kill()` cancels the reader. That EOF is us ending pi, and the browser must not be told
        that pi stopped unexpectedly when we are the ones who stopped it."""
        pi = PiProcess()
        socket = RecordingSocket()
        pi.proc = FakeChild([])
        pi.proc.stdout = SilentStream()  # the reader is parked here when we call kill()
        pi.ws = socket

        pi.reader_task = asyncio.create_task(pi._read_stdout())
        await asyncio.sleep(0)  # let the reader get parked

        await pi.kill()
        await _stopped_writer(pi)

        assert not [p for _, p in socket.sent if p.get("code") == "pi_exited"]

    async def test_sending_to_a_dead_child_raises_rather_than_vanishing(self):
        pi = PiProcess()
        pi.proc = None

        with pytest.raises(RuntimeError, match="not running"):
            await pi.send({"type": "prompt", "message": "hi"})

        with pytest.raises(RuntimeError, match="not running"):
            await pi.send_and_wait({"type": "get_messages"}, "id-1")


# ---------------------------------------------------------------- the browser falling behind


class TestBrowserCannotStallPi:
    """BE-12: the reader used to await a client send for every pi event."""

    async def test_a_client_that_stops_reading_does_not_stop_the_reader(self):
        events = [{"type": "text_delta", "n": i} for i in range(20)]
        pi = PiProcess()
        socket = HangingSocket()
        pi.proc = FakeChild(events)
        pi.ws = socket

        # The whole point: this returns even though the browser never finishes its first send.
        await asyncio.wait_for(pi._read_stdout(), timeout=2.0)

        # Nothing was held back waiting on the client: all 20 events plus the exit notice are queued.
        assert pi._browser_out.qsize() >= len(events), "queued, not blocked"
        assert pi.proc is None, "the fake stream ends, which is an exit like any other"
        await _stopped_writer(pi)

    async def test_delivery_order_is_exactly_enqueue_order_across_both_kinds(self):
        """The voice contract (static/voice.js drops a frame whose sequence is not greater) depends
        on this, and it used to depend on lock acquisition order rather than on construction."""
        pi = PiProcess()
        socket = RecordingSocket()
        pi.ws = socket

        await pi.send_browser_json({"type": "pi_event", "event": {"type": "agent_start"}})
        await pi.send_browser_bytes(b"\x01\x02")
        await pi.send_browser_json({"type": "pi_event", "event": {"type": "agent_settled"}})
        await pi.send_browser_bytes(b"\x03\x04")
        await _drain(pi)
        await _stopped_writer(pi)

        assert socket.sent == [
            ("json", {"type": "pi_event", "event": {"type": "agent_start"}}),
            ("bytes", b"\x01\x02"),
            ("json", {"type": "pi_event", "event": {"type": "agent_settled"}}),
            ("bytes", b"\x03\x04"),
        ]

    async def test_a_client_that_cannot_be_delivered_to_is_closed_and_counted(self):
        pi = PiProcess()
        socket = BrokenSocket()
        pi.ws = socket

        await pi.send_browser_json({"type": "pi_event", "event": {"type": "agent_start"}})
        await asyncio.sleep(0)  # let the writer take it
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert pi.browser_dead is True
        assert socket.close_code == 1011, "the disconnect path tears it down, including the child"
        assert pi._browser_send_failures >= 1, "the failure is counted somewhere, not swallowed"

        # Everything after a broken send is a no-op rather than another exception per event.
        before = len(socket.sent)
        for _ in range(5):
            await pi.send_browser_json({"type": "pi_event", "event": {"type": "text_delta"}})
        assert len(socket.sent) == before

    async def test_an_overflowing_client_is_closed_rather_than_buffered_forever(self, monkeypatch):
        monkeypatch.setattr(process_module, "_BROWSER_OUT_MAX", 3)
        pi = PiProcess()
        socket = HangingSocket()
        pi.ws = socket

        for i in range(10):
            await pi.send_browser_json({"type": "pi_event", "event": {"type": "text_delta"}})

        assert pi.browser_dead is True
        assert socket.close_code == 1011
        await _stopped_writer(pi)

    async def test_kill_lets_a_live_browser_finish_receiving(self):
        """Cancelling the writer outright would drop the tail of a run that just settled cleanly."""
        pi = PiProcess()
        socket = RecordingSocket()
        pi.ws = socket

        for i in range(5):
            await pi.send_browser_json({"type": "pi_event", "event": {"type": "text_delta", "i": i}})

        pi.proc = None  # nothing to terminate; this test is about the outbox
        await pi.kill()

        delivered = [p for kind, p in socket.sent if kind == "json"]
        assert len(delivered) == 5, "kill() drains before it cancels"
        assert pi._browser_out is None and pi.browser_dead is False, "the next connection starts clean"


# ---------------------------------------------------------------- the queue mirror's one reader


class TestQueueSnapshot:
    """BE-20: the same payload was opened three different ways in two files."""

    def test_a_queue_update_event_carries_the_lists_at_the_top_level(self):
        assert parse_queue_snapshot(
            {"type": "queue_update", "steering": ["a"], "followUp": ["b", "c"]}
        ) == {"steering": ["a"], "followUp": ["b", "c"]}

    def test_a_clear_queue_response_carries_them_under_data(self):
        assert parse_queue_snapshot(
            {"type": "response", "command": "clear_queue", "data": {"steering": [], "followUp": ["x"]}}
        ) == {"steering": [], "followUp": ["x"]}

    def test_absent_and_non_string_values_survive(self):
        assert parse_queue_snapshot({}) == {"steering": [], "followUp": []}
        assert parse_queue_snapshot({"data": None}) == {"steering": [], "followUp": []}
        assert parse_queue_snapshot({"steering": [1, None]}) == {"steering": ["1", "None"], "followUp": []}

    def test_the_mirror_uses_the_same_reader_as_the_handlers(self):
        pi = PiProcess()
        assert pi._observe_run_event({"type": "queue_update", "steering": ["z"]}) is True
        assert pi.queue == {"steering": ["z"], "followUp": []}
