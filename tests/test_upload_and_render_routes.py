"""Routes that read a caller's bytes, and the one that spawns node.

None of these had tests. `/api/upload-file` and `/api/voices/upload` both buffered a request body
before deciding whether it was allowed, and `/api/debug/session` ran a whole-session node render
inline on the event loop that every chat websocket shares.
"""

import json
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from pi_chat import app as app_module
from pi_chat.app import MAX_DOCUMENT_SIZE, _read_capped, create_app
from pi_chat.auth import AuthManager, hash_password


class FakeUpload:
    """An UploadFile that reports how many bytes the server actually asked for."""

    def __init__(self, total: int, chunk_size: int = 1 << 20) -> None:
        self.remaining = total
        self.chunk_size = chunk_size
        self.requested = 0

    async def read(self, size: int = -1) -> bytes:
        take = self.chunk_size if size < 0 else min(size, self.chunk_size)
        take = min(take, self.remaining)
        self.remaining -= take
        self.requested += take
        return b"x" * take


@pytest.fixture
def app(tmp_path, monkeypatch):
    auth = AuthManager()
    auth.password_hashes["b"] = hash_password("test123")
    auth.password_hashes["r"] = hash_password("profile-r-pw")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(app_module, "DEV_MODE", False)
    (tmp_path / "sessions" / "b").mkdir(parents=True)
    return create_app(auth_manager=auth)


@pytest.fixture
def client(app):
    client = TestClient(app)
    assert client.post("/api/login", json={"account": "b", "password": "test123"}).status_code == 200
    return client


# ---------------------------------------------------------------- the capped reader itself


class TestCappedRead:
    async def test_a_body_under_the_limit_arrives_whole(self):
        upload = FakeUpload(1024)
        assert len(await _read_capped(upload, MAX_DOCUMENT_SIZE)) == 1024

    async def test_an_oversized_body_is_refused_without_being_read_whole(self):
        """The whole point of the change: the limit has to bound the memory, not just the response.

        A 2 GB upload used to be fully buffered before the 413 was produced.
        """
        two_gb = 2 * 1024 * 1024 * 1024
        upload = FakeUpload(two_gb, chunk_size=1 << 20)

        with pytest.raises(HTTPException) as caught:
            await _read_capped(upload, MAX_DOCUMENT_SIZE, detail="too big")

        assert caught.value.status_code == 413
        assert caught.value.detail == "too big"
        assert upload.requested <= MAX_DOCUMENT_SIZE + (1 << 20), (
            f"the server pulled {upload.requested / 1e6:.0f} MB to refuse a "
            f"{MAX_DOCUMENT_SIZE / 1e6:.0f} MB limit"
        )

    async def test_the_status_code_follows_the_route_that_asked(self):
        """The voice route has always answered 400 here; the document route answers 413."""
        with pytest.raises(HTTPException) as caught:
            await _read_capped(FakeUpload(100), 10, status_code=400, detail="Maximum is 10MB.")
        assert caught.value.status_code == 400


class TestDocumentUpload:
    def test_a_small_text_file_comes_back_as_text(self, client):
        response = client.post(
            "/api/upload-file",
            files={"file": ("notes.txt", "hello from a note".encode("utf-8"), "text/plain")},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["content"] == "hello from a note"
        assert body["mimeType"] == "text/plain"

    def test_an_oversized_file_is_refused(self, client):
        oversized = b"x" * (MAX_DOCUMENT_SIZE + 1024)

        response = client.post(
            "/api/upload-file",
            files={"file": ("big.txt", oversized, "text/plain")},
        )

        assert response.status_code == 413
        assert "5MB" in response.json()["detail"]

    def test_an_unsupported_extension_is_refused_before_anything_is_read(self, client):
        response = client.post("/api/upload-file", files={"file": ("evil.exe", b"MZ\x00", "application/octet-stream")})

        assert response.status_code == 400
        assert "Unsupported file type" in response.json()["detail"]

    def test_non_utf8_text_is_refused_rather_than_replaced(self, client):
        response = client.post("/api/upload-file", files={"file": ("bin.txt", b"\xff\xfe\x00bad", "text/plain")})

        assert response.status_code == 400

    def test_an_anonymous_upload_is_refused(self, app):
        """SEC-05: this route reads and returns the contents of a file. An attacker's only cost used
        to be knowing the URL; it is now the same as every other route."""
        anonymous = TestClient(app)

        response = anonymous.post(
            "/api/upload-file",
            files={"file": ("notes.txt", b"someone else's note", "text/plain")},
        )

        assert response.status_code in (401, 403)


class TestVoiceUploadLimit:
    def test_an_oversized_sample_keeps_its_existing_400(self, client):
        """The cap moved into the reader; the status code this route answers with did not."""
        oversized = b"\x00" * (10 * 1024 * 1024 + 1024)

        response = client.post(
            "/api/voices",
            files={"file": ("big.wav", oversized, "audio/wav")},
            data={"display_name": "Big"},
        )

        assert response.status_code == 400
        assert "10MB" in response.json()["detail"]


class TestDebugRenderRoute:
    """BE-11: `subprocess.run(node)` ran on the event loop that every websocket shares."""

    class Completed:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = json.dumps({"userHtml": ["<p>hi</p>"], "assistantHtml": []})
            self.stderr = ""

    @pytest.fixture
    def session_file(self, tmp_path):
        work = tmp_path / "sessions" / "b"
        path = work / "s.jsonl"
        path.write_text(
            "\n".join([
                json.dumps({"type": "session", "id": "s", "timestamp": "2026-11-17T00:00:00Z", "cwd": str(work)}),
                json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}),
            ]) + "\n",
            encoding="utf-8",
        )
        return path

    @pytest.fixture
    def spy(self, monkeypatch):
        seen = {}

        def fake_run(*args, **kwargs):
            seen["thread"] = threading.current_thread().name
            seen["is_main"] = threading.current_thread() is threading.main_thread()
            seen["kwargs"] = kwargs
            return self.Completed()

        monkeypatch.setattr(subprocess, "run", fake_run)
        return seen

    def test_node_runs_off_the_event_loop(self, client, spy, session_file, monkeypatch):
        response = client.get("/api/debug/session", params={"session_path": str(session_file)})

        assert response.status_code == 200
        assert response.json()["messageCount"] == 1
        assert response.json()["assistantHtml"] == []
        assert spy["is_main"] is False, (
            "node ran on the event loop thread: every profile's chat freezes for the length of the "
            "render, which for a large session is seconds"
        )

    def test_a_foreign_session_is_still_404(self, client, spy, tmp_path):
        foreign = tmp_path / "someone-else.jsonl"
        foreign.write_text(json.dumps({"type": "session", "cwd": str(tmp_path)}) + "\n", encoding="utf-8")

        response = client.get("/api/debug/session", params={"session_path": str(foreign)})

        assert response.status_code == 404

    def test_a_node_timeout_is_a_500_rather_than_a_traceback(self, client, monkeypatch, session_file):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="node", timeout=30)

        monkeypatch.setattr(subprocess, "run", boom)

        response = client.get("/api/debug/session", params={"session_path": str(session_file)})

        assert response.status_code == 500
        assert "timed out" in response.json()["detail"]
