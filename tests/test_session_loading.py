"""How fast a session opens, and what has to be true before the browser sees it.

Two slownesses Ben measured and I reproduced: the sidebar taking seconds on every login, and opening a
session taking ten. The sidebar was re-reading every session file in the whole pi store (109 files,
103 MB here) inside the event loop; the session open spawned pi and waited for `switch_session` before
sending text that was already on disk. Both are locked here, because both are the kind of fix that
silently reverts the next time someone "simplifies" the order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import pytest

from pi_chat import sessions as session_tools
from pi_chat.websocket import _load_session


def session_lines(work_dir: Path, marker: str) -> list[str]:
    return [
        json.dumps({"type": "session", "cwd": str(work_dir), "id": "s-1", "timestamp": "2026-11-10T00:00:00.000Z"}),
        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": marker}]}}),
        json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}),
    ]


def test_a_session_whose_cwd_will_not_resolve_is_skipped_out_loudly(tmp_path, monkeypatch, caplog):
    """`BE-05`: this path used to `return None` with nothing written anywhere.

    Refusing to list the session is correct: ownership is decided by comparing the resolved header cwd with
    the account's work directory, so an unresolvable cwd cannot be proven to belong to this profile. What was
    wrong is that every other failure in the same function logs and this one did not, which made "a session
    vanished from the sidebar" a report with no evidence behind it.
    """
    work = tmp_path / "work"
    path = Path(write_session(work, "hello"))

    class Unresolvable:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self):
            raise OSError("no such device")

    monkeypatch.setattr(session_tools, "Path", Unresolvable)
    with caplog.at_level(logging.WARNING, logger="pi_chat.sessions"):
        assert session_tools._read_summary(path) is None

    assert "Skipping session" in caplog.text
    assert "unreadable cwd" in caplog.text


@pytest.fixture()
def sessions_store(tmp_path, monkeypatch):
    """A sessions directory of our own, for tests that drive `load_session`.

    Containment and ownership are two different questions (S3): where the file sits is what the guard checks,
    and the header `cwd` inside it is what ownership checks. A session opened from the browser therefore has
    to live inside this store while still claiming `work_dir` as its working directory, which is exactly what
    a real session looks like. Tests about listing or about the real store leave it alone.
    """
    root = tmp_path / "pi-sessions"
    root.mkdir()
    monkeypatch.setattr(session_tools, "pi_sessions_dir", lambda: root)
    return root


def write_session(
    work_dir: Path, marker: str, name: str = "s.jsonl", store: Path | None = None
) -> str:
    work_dir.mkdir(parents=True, exist_ok=True)
    path = (store or work_dir) / name
    path.write_text("\n".join(session_lines(work_dir, marker)) + "\n", encoding="utf-8")
    return str(path)


class StubPi:
    """A pi child that answers, slowly, in a recorded order."""

    def __init__(self, project_root: Path, *, switch_delay: float = 0.0, switch_ok: bool = True,
                 capture_messages: list | None = None) -> None:
        self.proc = None
        self.project_root = project_root
        self.session_id = "abc123"
        self.browser: list[dict] = []
        self.journal: list[str] = []
        self.switch_delay = switch_delay
        self.switch_ok = switch_ok
        self.capture_messages = capture_messages or []

    async def spawn(self) -> None:
        self.proc = object()

    async def send_browser_json(self, payload: dict) -> None:
        self.journal.append(payload["type"])
        self.browser.append(payload)

    async def send_and_wait(self, command: dict, request_id: str, timeout: float = 30.0) -> dict:
        self.journal.append(command["type"])
        if command["type"] == "switch_session":
            if self.switch_delay:
                await asyncio.sleep(self.switch_delay)
            return {"success": self.switch_ok, "data": {}} if self.switch_ok else {
                "success": False, "error": "child not ready",
            }
        if command["type"] == "get_messages":
            return {"success": True, "data": {"messages": self.capture_messages}}
        raise AssertionError(f"unexpected command: {command['type']}")


def _payload(pi, kind: str) -> dict:
    """The last payload of that type the handler sent. `session_started` arrives first and the order of
    the two is the thing under test, so look them up by type instead of by index."""
    matches = [entry for entry in pi.browser if entry.get("type") == kind]
    assert matches, f"no {kind} sent; got {[e.get('type') for e in pi.browser]}"
    return matches[-1]


class StubWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


@pytest.fixture
def owned(monkeypatch):
    """Skip the ownership guard. It reads the session header and has its own tests; here the question
    is what the handler does and in what order."""
    monkeypatch.setattr("pi_chat.websocket.session_belongs_to_account", lambda *a, **k: True)


class TestLoadSessionOrdering:
    async def test_the_conversation_is_sent_before_the_child_is_asked(
        self, tmp_path, owned, sessions_store
    ):
        path = write_session(tmp_path, "render me first", store=sessions_store)
        pi = StubPi(tmp_path, switch_delay=0.4)

        started = time.perf_counter()
        await _load_session(pi, "b", path)

        # Order is the assertion: `session_loaded` is recorded before `switch_session` is even sent, and
        # the switch is the slow step (0.4 s here, seconds on a cold engine). A browser gets its text
        # while the child is still booting.
        assert pi.journal[:3] == ["session_started", "session_loaded", "switch_session"], pi.journal
        assert time.perf_counter() - started >= 0.4, "the switch must still complete before the handler returns"

        loaded = _payload(pi, "session_loaded")
        assert loaded["messageCount"] == 2
        assert "previews" in loaded

    async def test_a_switch_failure_still_leaves_the_conversation_on_screen(
        self, tmp_path, owned, sessions_store
    ):
        path = write_session(tmp_path, "shown even when pi will not switch", store=sessions_store)
        pi = StubPi(tmp_path, switch_ok=False)
        websocket = StubWebSocket()

        await _load_session(pi, "b", path)

        assert _payload(pi, "session_loaded")["messageCount"] == 2
        # The error travels the same outbox as `session_loaded`, so ordering is part of the contract.
        assert pi.browser[-1]["type"] == "error"
        # The message has to say which half worked: the text is real, pi is pointed somewhere else.
        assert "disk" in pi.browser[-1]["message"]

    async def test_a_capture_file_still_waits_for_the_child(self, tmp_path, owned, sessions_store):
        # An RPC capture has no session header, so `parse_jsonl_messages` returns None and only pi can
        # produce a history. Losing that path would show an empty conversation instead of slowing down.
        capture = sessions_store / "capture.jsonl"
        capture.write_text(json.dumps({"type": "response", "success": True}) + "\n", encoding="utf-8")
        pi = StubPi(tmp_path, capture_messages=[{"role": "user", "content": []}])

        await _load_session(pi, "b", str(capture))

        assert "get_messages" in pi.journal
        assert _payload(pi, "session_loaded")["messageCount"] == 1

    async def test_no_session_means_no_spawn(self, tmp_path, owned):
        pi = StubPi(tmp_path)

        await _load_session(pi, "b", "")

        assert pi.browser[-1]["message"] == "No session path provided"
        # `journal` records browser messages and child commands together, and the only entry is the
        # error: nothing was asked of pi, and no child was spawned.
        assert pi.journal == ["error"]
        assert pi.proc is None


@pytest.fixture(autouse=True)
def clean_summary_cache():
    session_tools.clear_summary_cache()
    yield
    session_tools.clear_summary_cache()


class TestListingCache:
    def test_an_unchanged_file_is_not_read_twice(self, tmp_path, monkeypatch):
        work = tmp_path / "sessions" / "b"
        write_session(work, "cached listing")
        home = tmp_path / "fakehome"
        store = home / ".pi" / "agent" / "sessions"
        derived = session_tools._derived_session_dir(work.resolve(), store)
        derived.mkdir(parents=True)
        (derived / "s.jsonl").write_text(
            "\n".join(session_lines(work.resolve(), "cached listing")) + "\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        reads = []
        original = session_tools._open_jsonl

        def counting(path, *args, **kwargs):
            reads.append(str(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(session_tools, "_open_jsonl", counting)

        first = session_tools.list_sessions(tmp_path, "b")
        assert len(first) == 1
        assert reads, "the first listing has to read the file"

        reads.clear()
        second = session_tools.list_sessions(tmp_path, "b")
        assert second == first
        assert reads == [], f"a cached, unchanged file was read again: {reads}"

    def test_appending_to_a_live_session_invalidates_its_entry(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        store = home / ".pi" / "agent" / "sessions"
        work = tmp_path / "sessions" / "b"
        work.mkdir(parents=True)
        derived = session_tools._derived_session_dir(work.resolve(), store)
        derived.mkdir(parents=True)
        target = derived / "live.jsonl"
        target.write_text("\n".join(session_lines(work.resolve(), "growing")) + "\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert session_tools.list_sessions(tmp_path, "b")[0]["messageCount"] == 2

        # A real turn appends and moves mtime; size alone would not catch an equal-length rewrite, so
        # both are part of the key.
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": []}}) + "\n")
        info = target.stat()
        os.utime(target, ns=(info.st_atime_ns + 10**9, info.st_mtime_ns + 10**9))

        assert session_tools.list_sessions(tmp_path, "b")[0]["messageCount"] == 3

    def test_callers_cannot_write_into_the_cache(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        store = home / ".pi" / "agent" / "sessions"
        work = tmp_path / "sessions" / "b"
        work.mkdir(parents=True)
        derived = session_tools._derived_session_dir(work.resolve(), store)
        derived.mkdir(parents=True)
        (derived / "s.jsonl").write_text("\n".join(session_lines(work.resolve(), "mutate me")) + "\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        first = session_tools.list_sessions(tmp_path, "b")
        first[0]["firstMessage"] = "scribbled over"

        assert session_tools.list_sessions(tmp_path, "b")[0]["firstMessage"] == "mutate me"

    def test_the_derived_directory_holds_nothing_this_account_can_lack(self, tmp_path, monkeypatch):
        """The shortcut is a guess about pi's naming. When it stops holding, the listing must not go empty."""
        home = tmp_path / "fakehome"
        store = home / ".pi" / "agent" / "sessions"
        work = tmp_path / "sessions" / "b"
        work.mkdir(parents=True)
        elsewhere = store / "--whatever-pi-calls-it-now--"
        elsewhere.mkdir(parents=True)
        (elsewhere / "s.jsonl").write_text("\n".join(session_lines(work.resolve(), "found anyway")) + "\n", encoding="utf-8")
        session_tools._derived_session_dir(work.resolve(), store).mkdir(parents=True)  # empty, so a shortcut-only scan finds nothing
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        found = session_tools.list_sessions(tmp_path, "b")

        assert [s["firstMessage"] for s in found] == ["found anyway"]


class TestAgainstTheRealStore:
    """These read Ben's actual session tree, read-only. They are the only way to know the shortcut and
    the cache agree with the old full scan on files nobody wrote for a test."""

    def test_derived_directory_matches_the_full_scan(self):
        repo = Path(__file__).resolve().parents[1]
        account = "b"
        work = (repo / "sessions" / account).resolve()
        store = Path.home() / ".pi" / "agent" / "sessions"
        if not (store / session_tools._derived_session_dir(work, store).name).is_dir():
            pytest.skip("this machine has no sessions for that account in the derived layout")

        shortcut = session_tools.list_sessions(repo, account)
        session_tools.clear_summary_cache()
        full = [
            summary
            for summary in (
                session_tools._summarize_session(f, expected_work_dir=work)
                for f in store.rglob("*.jsonl")
            )
            if summary
        ]
        full.sort(key=lambda s: s.get("timestamp", ""), reverse=True)

        assert [s["path"] for s in shortcut] == [s["path"] for s in full]


class TestOwnershipCheck:
    """`load_session` pays this before anything reaches the screen."""

    class HeaderOnlyHandle:
        """A file that refuses to be scanned, so a regression here is a test failure and not a
        four-second click on a machine where the sessions happen to be small."""

        def __init__(self, path) -> None:
            self._handle = open(path, "r", encoding="utf-8")

        def readline(self, *args):
            return self._handle.readline(*args)

        def __iter__(self):
            raise AssertionError("the ownership check read past the first line")

        def close(self) -> None:
            self._handle.close()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.close()

    def test_belonging_is_answered_by_the_header_alone(self, tmp_path, monkeypatch):
        work = tmp_path / "sessions" / "b"
        path = Path(write_session(work, "owned by b"))
        # Everything after the header is unparseable on purpose: if the check still passes, it never
        # looked, which is the behavior under test rather than a tolerance for corrupt files.
        body = path.read_text(encoding="utf-8")
        header = body.splitlines()[0]
        # chr(10) rather than an escape: this file is written with write_text, and the header line is
        # all the check under test is allowed to look at.
        path.write_text(header + chr(10) + ("{" * 200_000), encoding="utf-8")
        monkeypatch.setattr(session_tools, "_open_jsonl", lambda session_file: self.HeaderOnlyHandle(session_file))

        assert session_tools.session_belongs_to_account(str(path), tmp_path, "b") is True

    def test_a_foreign_work_dir_is_still_refused(self, tmp_path):
        work = tmp_path / "sessions" / "r"
        path = write_session(work, "owned by r")

        assert session_tools.session_belongs_to_account(str(path), tmp_path, "b") is False
        assert session_tools.session_belongs_to_account(str(path), tmp_path, "r") is True

    def test_a_file_that_only_looks_like_a_session_is_refused(self, tmp_path):
        foreign = tmp_path / "elsewhere.jsonl"
        foreign.write_text(json.dumps({"type": "message"}) + chr(10), encoding="utf-8")

        assert session_tools.session_belongs_to_account(str(foreign), tmp_path, "b") is False
