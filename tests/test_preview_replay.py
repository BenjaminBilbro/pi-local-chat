"""Rebuilding the preview pane when a session is reopened.

pi persists every preview as a `custom` session entry, and those entries never reach the browser
through pi: measured 2026-11-09 against a session holding two of them, `get_messages` returned 8
messages and no custom records. So the server reads the JSONL itself and puts the replay list on the
`session_loaded` payload. Two failure modes are worth locking: a replay list that loses order or picks
up another extension's entries, and a document whose file cleanup already took, which must arrive as
"this is gone" rather than as a pointer the pane 404s on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_chat import config
from pi_chat import preview
from pi_chat.sessions import parse_jsonl_preview_entries
from pi_chat.websocket import _load_session

MD_ID = "a" * 32
HTML_ID = "b" * 32


def entry_line(data: dict, entry_id: str = "e1") -> str:
    return json.dumps(
        {
            "type": "custom",
            "customType": "pichat_preview",
            "data": data,
            "id": entry_id,
            "parentId": "root",
            "timestamp": "2026-11-09T22:39:24.923Z",
        }
    )


def message_line(text: str) -> str:
    return json.dumps(
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    )


@pytest.fixture
def preview_root(tmp_path, monkeypatch):
    """Point the preview store and the sessions tree at a temporary directory."""
    root = tmp_path / "previews"
    monkeypatch.setattr(config, "PREVIEW_BASE_DIR", root)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PREVIEW_EXTRA_ROOTS", [])
    monkeypatch.setattr(config, "DEV_MODE", False)
    (tmp_path / "sessions" / "b").mkdir(parents=True, exist_ok=True)
    return root


def write_session(tmp_path: Path, lines: list[str], name: str = "session.jsonl") -> str:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class TestRecordedEntries:
    def test_entries_come_back_in_transcript_order(self, preview_root, tmp_path):
        path = write_session(
            tmp_path,
            [
                json.dumps({"type": "session", "cwd": str(tmp_path)}),
                message_line("make me a doc"),
                entry_line({"v": 1, "op": "open", "docId": MD_ID, "kind": "markdown", "title": "One", "revision": 1, "file": f"{MD_ID}.md"}, "e1"),
                entry_line({"v": 1, "op": "open", "docId": HTML_ID, "kind": "html", "title": "Two", "revision": 1, "file": f"{HTML_ID}.html"}, "e2"),
                entry_line({"v": 1, "op": "update", "docId": MD_ID, "kind": "markdown", "title": "One", "revision": 2, "file": f"{MD_ID}.md"}, "e3"),
                entry_line({"v": 1, "op": "close", "docId": HTML_ID}, "e4"),
                entry_line({"v": 1, "op": "clear"}, "e5"),
            ],
        )

        entries = parse_jsonl_preview_entries(path)

        assert [entry["op"] for entry in entries] == ["open", "open", "update", "close", "clear"]
        # The payload is passed through untouched: the pane needs the revision and the file name to
        # rebuild the same state, and re-shaping it here would invent a second protocol.
        assert entries[2]["revision"] == 2
        assert entries[0]["file"] == f"{MD_ID}.md"

    def test_another_extensions_entries_are_ignored(self, preview_root, tmp_path):
        path = write_session(
            tmp_path,
            [
                json.dumps({"type": "custom", "customType": "memory_rule", "data": {"fired": ["x"]}}),
                entry_line({"op": "open", "docId": MD_ID, "kind": "markdown", "file": f"{MD_ID}.md"}),
                json.dumps({"type": "custom", "customType": "pichat_voice", "data": {"op": "open"}}),
            ],
        )

        assert [entry["op"] for entry in parse_jsonl_preview_entries(path)] == ["open"]

    def test_junk_lines_do_not_end_the_replay(self, preview_root, tmp_path):
        path = write_session(
            tmp_path,
            [
                entry_line({"op": "open", "docId": MD_ID, "kind": "markdown"}, "e1"),
                "not json at all",
                "",
                json.dumps({"type": "custom", "customType": "pichat_preview", "data": "not a dict"}),
                json.dumps({"type": "custom", "customType": "pichat_preview"}),
                entry_line({"op": "close", "docId": MD_ID}, "e2"),
            ],
        )

        assert [entry["op"] for entry in parse_jsonl_preview_entries(path)] == ["open", "close"]

    def test_a_session_without_previews_is_an_empty_list(self, preview_root, tmp_path):
        path = write_session(tmp_path, [message_line("hi"), message_line("hello")])

        assert parse_jsonl_preview_entries(path) == []

    def test_a_missing_or_unreadable_file_is_not_an_error(self, preview_root, tmp_path):
        assert parse_jsonl_preview_entries(str(tmp_path / "nope.jsonl")) == []
        assert parse_jsonl_preview_entries(str(tmp_path)) == []

    def test_a_limit_bounds_a_pathological_session(self, preview_root, tmp_path):
        lines = [entry_line({"op": "open", "docId": f"{i:032x}", "kind": "markdown"}, f"e{i}") for i in range(5)]
        path = write_session(tmp_path, lines)

        assert len(parse_jsonl_preview_entries(path, limit=2)) == 2


class TestAvailability:
    """The server has the disk and the browser does not, so the server answers the question."""

    def stored(self, doc_id: str, kind: str, body: bytes) -> None:
        preview.write_document(preview.scope_dir("b", "sess1234", create=True), doc_id, kind, body)

    def test_a_document_still_on_disk_reports_its_size_and_mtime(self, preview_root, tmp_path):
        self.stored(MD_ID, "markdown", b"# Hello")
        path = write_session(tmp_path, [entry_line({"op": "open", "docId": MD_ID, "kind": "markdown", "file": f"{MD_ID}.md"})])

        described = preview.describe_preview_entries("b", parse_jsonl_preview_entries(path))

        assert described[0]["available"] is True
        assert described[0]["bytes"] == len(b"# Hello")
        assert str(described[0]["mtime"]).endswith("+00:00")

    def test_a_cleaned_up_document_says_so_instead_of_pointing_nowhere(self, preview_root, tmp_path):
        self.stored(MD_ID, "markdown", b"# Hello")
        path = write_session(tmp_path, [entry_line({"op": "open", "docId": MD_ID, "kind": "markdown", "file": f"{MD_ID}.md"})])
        scope = preview.scope_dir("b", "sess1234")
        next(scope.glob(f"{MD_ID}.*")).unlink()

        described = preview.describe_preview_entries("b", parse_jsonl_preview_entries(path))

        assert described[0]["available"] is False
        assert described[0]["bytes"] is None
        # The recorded name survives: "the file is gone" is useful, "something is missing" is not.
        assert described[0]["file"] == f"{MD_ID}.md"

    def test_a_referenced_path_is_judged_by_the_file_the_pane_would_fetch(self, preview_root, tmp_path):
        live = tmp_path / "sessions" / "b" / "report.md"
        live.write_text("# Report", encoding="utf-8")
        path = write_session(
            tmp_path,
            [
                entry_line({"op": "open", "docId": MD_ID, "kind": "markdown", "path": str(live)}, "e1"),
                entry_line({"op": "open", "docId": HTML_ID, "kind": "html", "path": str(tmp_path.parent / "elsewhere.html")}, "e2"),
            ],
        )

        described = preview.describe_preview_entries("b", parse_jsonl_preview_entries(path))

        assert described[0]["available"] is True
        # A path outside the allowed roots is not served, so the pane could not read it either. The
        # answer is the same as for a deleted file, and for the same reason: never confirm existence.
        assert described[1]["available"] is False

    def test_close_and_clear_entries_carry_no_availability(self, preview_root, tmp_path):
        path = write_session(
            tmp_path,
            [
                entry_line({"op": "close", "docId": MD_ID}, "e1"),
                entry_line({"op": "clear"}, "e2"),
            ],
        )

        described = preview.describe_preview_entries("b", parse_jsonl_preview_entries(path))

        assert [entry["op"] for entry in described] == ["close", "clear"]
        assert all("available" not in entry for entry in described)

    def test_pi_bookkeeping_stays_out_of_the_browser_payload(self, preview_root, tmp_path):
        path = write_session(
            tmp_path,
            [entry_line({"v": 1, "op": "open", "docId": MD_ID, "kind": "markdown", "revision": 1}, "entry-uuid-1")],
        )
        # The persisted record also carries id/parentId/timestamp, which are pi's, and the entry above
        # deliberately has no `file`: a payload must not grow fields the pane cannot use.
        described = preview.describe_preview_entries("b", parse_jsonl_preview_entries(path))

        assert set(described[0]) == {"v", "op", "docId", "kind", "revision", "available", "bytes", "mtime"}


class StubPi:
    """The slice of PiProcess `_load_session` touches, with what reached the browser recorded."""

    def __init__(self, project_root: Path) -> None:
        self.proc = object()
        self.project_root = project_root
        self.session_id = "01955f6a-0000-7000-8000-000000000000"
        self.browser: list[dict] = []

    async def send_and_wait(self, command: dict, request_id: str, timeout: float = 30.0) -> dict:
        if command["type"] == "switch_session":
            return {"success": True, "data": {}}
        if command["type"] == "get_messages":
            return {"success": True, "data": {"messages": [{"role": "user", "content": []}]}}
        raise AssertionError(f"unexpected request command: {command['type']}")

    async def send_browser_json(self, payload: dict) -> None:
        self.browser.append(payload)


class StubWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def test_loading_a_session_hands_the_browser_its_replay_list(preview_root, tmp_path, monkeypatch):
    preview.write_document(
        preview.scope_dir("b", "sess1234", create=True), MD_ID, "markdown", b"# Rebuilt"
    )
    path = write_session(
        tmp_path,
        [
            entry_line({"v": 1, "op": "open", "docId": MD_ID, "kind": "markdown", "title": "Rebuilt", "revision": 1, "file": f"{MD_ID}.md"}, "e1"),
            entry_line({"v": 1, "op": "open", "docId": HTML_ID, "kind": "html", "title": "Gone", "revision": 1, "file": f"{HTML_ID}.html"}, "e2"),
        ],
    )
    # The ownership check reads the session header; this test is about the payload, not the guard,
    # and `tests/test_session_serving.py` covers the guard itself.
    monkeypatch.setattr("pi_chat.websocket.session_belongs_to_account", lambda *args, **kwargs: True)
    pi = StubPi(tmp_path)

    await _load_session(pi, "b", path)

    payload = pi.browser[0]
    assert payload["type"] == "session_loaded"
    assert [entry["op"] for entry in payload["previews"]] == ["open", "open"]
    assert payload["previews"][0]["available"] is True
    assert payload["previews"][1]["available"] is False
    assert payload["previews"][1]["title"] == "Gone"
