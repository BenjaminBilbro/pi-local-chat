"""Routes that read a caller's bytes, and the one that spawns node.

None of these had tests. `/api/upload-file` and `/api/voices/upload` both buffered a request body
before deciding whether it was allowed, and `/api/debug/session` ran a whole-session node render
inline on the event loop that every chat websocket shares.
"""

import json
import re
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from pi_chat import app as app_module
from pi_chat import uploads
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
    # Attachments are filed under the same root the pi child runs in, and that root is a separate
    # import in each module. Patching one and not the other is how a test writes into the real tree.
    monkeypatch.setattr(uploads, "PROJECT_ROOT", tmp_path)
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


SCOPE = "8f3c2a1e-4b5d-4e6f-9a8b-7c6d5e4f3a2b"


class TestDocumentUpload:
    def test_a_small_text_file_comes_back_as_text(self, client):
        response = client.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": ("notes.txt", "hello from a note".encode("utf-8"), "text/plain")},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["content"] == "hello from a note"
        assert body["mimeType"] == "text/plain"
        assert body["inlined"] is True
        # The bytes stay on disk and the response says where: the transcription is what the model
        # reads, and the file is what it opens afterwards.
        assert body["bytes"] == len(b"hello from a note")
        path = Path(body["path"])
        assert path.is_file()
        assert path.read_bytes() == b"hello from a note"
        assert path.suffix == ".txt"
        assert body["ref"] == f"{SCOPE}/{path.name}"

    def test_a_file_too_big_to_inline_comes_back_as_a_path(self, client, monkeypatch):
        monkeypatch.setattr(uploads, "INLINE_MAX_CHARS", 8)
        response = client.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": ("long.txt", b"a very long document indeed", "text/plain")},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["content"] is None, "no body means the prompt sends no body"
        assert body["inlined"] is False
        assert Path(body["path"]).is_file(), "and the path is the whole way to the file"

    def test_an_oversized_file_is_refused(self, client):
        oversized = b"x" * (MAX_DOCUMENT_SIZE + 1024)

        response = client.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": ("big.txt", oversized, "text/plain")},
        )

        assert response.status_code == 413
        assert "5MB" in response.json()["detail"]

    def test_an_unsupported_extension_is_refused_before_anything_is_read(self, client):
        response = client.post("/api/upload-file", files={"file": ("evil.exe", b"MZ\x00", "application/octet-stream")})

        assert response.status_code == 400
        assert "Unsupported file type" in response.json()["detail"]

    def test_a_scope_that_is_not_one_is_refused(self, client):
        """The scope becomes a path segment, so it is checked rather than believed."""
        response = client.post(
            "/api/upload-file",
            data={"scope": "../../sessions/r"},
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )

        assert response.status_code == 400
        assert "scope" in response.json()["detail"]

    def test_an_upload_without_a_scope_is_refused(self, client):
        response = client.post(
            "/api/upload-file",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )

        assert response.status_code == 400

    def test_non_utf8_text_is_refused_rather_than_replaced(self, client):
        response = client.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": ("bin.txt", b"\xff\xfe\x00bad", "text/plain")},
        )

        assert response.status_code == 400

    def test_an_anonymous_upload_is_refused(self, app):
        """SEC-05: this route reads and returns the contents of a file. An attacker's only cost used
        to be knowing the URL; it is now the same as every other route."""
        anonymous = TestClient(app)

        response = anonymous.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": ("notes.txt", b"someone else's note", "text/plain")},
        )

        assert response.status_code in (401, 403)


class TestLimitsRoute:
    """The composer repeats the server's ceilings so an oversized photo is refused with a sentence
    instead of by the socket, which drops an over-large frame and closes the connection."""

    def test_the_ceilings_are_the_server_ones(self, client):
        response = client.get("/api/limits")

        assert response.status_code == 200
        body = response.json()
        assert body["documentMaxBytes"] == MAX_DOCUMENT_SIZE
        assert body["inlineMaxChars"] == uploads.INLINE_MAX_CHARS
        assert body["imageMaxBytes"] == uploads.MAX_IMAGE_BYTES
        # The base64 of an image at the cap has to fit inside uvicorn's 16 MiB incoming frame with
        # room for the JSON around it. If these two ever cross, the failure is a dropped chat rather
        # than an error message, which is the whole reason the client gets told at all.
        assert uploads.MAX_IMAGE_BYTES * 4 // 3 + 64 * 1024 < 16 * 1024 * 1024

    def test_an_anonymous_caller_gets_nothing(self, app):
        assert TestClient(app).get("/api/limits").status_code in (401, 403)


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


# ---------------------------------------------------------------- the kinds, and the three states


class TestDocumentKinds:
    """What the composer may attach, and what it is told when nothing could be read.

    `.md` is the kind that was missing: an attached PDF of a licence worked and an attached notes file
    did not, blocked by a list and an `accept` attribute that had never been revisited (Ben,
    2026-11-19). Markdown, CSV and JSON need no parser, so the only thing between them and the model
    was that list. Anything that needs a parser is a different change.
    """

    @pytest.mark.parametrize(
        ("suffix", "mime"),
        [(".txt", "text/plain"), (".md", "text/markdown"), (".csv", "text/csv"), (".json", "application/json")],
    )
    def test_a_plain_text_kind_arrives_whole_and_typed(self, client, suffix, mime):
        response = client.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": (f"notes{suffix}", "# Body\nthe tail".encode(), mime)},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mimeType"] == mime
        assert body["inlineState"] == "inlined"
        assert body["content"] == "# Body\nthe tail"
        # The bytes are filed under the name the caller used, suffix and all: a `.md` that comes back
        # as `.txt` is a file the agent then reads with the wrong assumptions.
        assert body["filename"].endswith(suffix)
        assert Path(body["path"]).read_text(encoding="utf-8") == "# Body\nthe tail"

    def test_the_refusal_names_every_kind_that_works(self, client):
        response = client.post(
            "/api/upload-file", files={"file": ("doc.docx", b"PK\x03\x04", "application/octet-stream")}
        )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "Unsupported file type" in detail
        for suffix in uploads.accepted_document_suffixes():
            assert suffix in detail, f"the message refuses {suffix}, which the route actually takes"

    def test_the_limits_route_carries_the_list_the_composer_gates_on(self, client):
        """Three places have to agree about the kinds: this route, the file gate in `static/chat.js`,
        and the picker's `accept` attribute. The first two share one answer over HTTP; the third is a
        filter on a dialog, and a test keeps it from quietly falling behind."""
        body = client.get("/api/limits").json()

        assert body["documentSuffixes"] == list(uploads.accepted_document_suffixes())

    def test_the_picker_offers_every_kind_the_server_takes(self):
        html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        line = [line for line in html.splitlines() if 'id="file-input"' in line]
        assert len(line) == 1, "the composer's file input moved, and this check needs to follow it"
        accept = line[0].split('accept="')[1].split('"')[0].split(",")

        for suffix in uploads.accepted_document_suffixes():
            assert suffix in accept, f"{suffix} is uploadable and the picker never offers it"



class TestPreviewTier:
    """Between the two old limits, a document goes as head, a counted gap, and tail.

    The reason is the cost of the top of that range: 200,000 characters is about 50k tokens, it rides in
    every later turn of the session, and it is re-prefilled from scratch whenever the engine's prefix
    cache misses. Ben's own session supplied the other half of the argument: a 10,156-byte notes file
    went in whole at roughly 2.5k tokens and the assistant spent a turn proving to itself that it had not
    been truncated, because nothing in the tag said how big the file was.

    So the marker counts, in the same unit the cap is quoted in. A gap you can measure is a fact the
    model can act on by reading the path; a silent cut is a thing it has to discover.
    """

    def test_a_small_document_still_arrives_whole(self, client):
        """Below the floor a preview is worse than the file: two fragments, more tokens, less sense."""
        body = "note\n" + ("filler word " * 200)  # ~2.1 KB, under the 4,000-character floor
        payload = client.post(
            "/api/upload-file", data={"scope": SCOPE}, files={"file": ("note.md", body.encode(), "text/markdown")}
        ).json()

        assert payload["inlineState"] == "inlined"
        assert payload["content"] == body
        assert uploads.PREVIEW_MARKER not in payload["content"]

    def test_a_longer_document_arrives_as_head_tail_and_a_count(self, client):
        body = "# Plan of record\n" + ("alpha bravo charlie delta\n" * 600) + "\nTAIL: https://example.test/docs"
        assert len(body) > uploads.PREVIEW_MIN_CHARS

        payload = client.post(
            "/api/upload-file", data={"scope": SCOPE}, files={"file": ("plan.md", body.encode(), "text/markdown")}
        ).json()

        content = payload["content"]
        assert payload["inlineState"] == "preview"
        assert uploads.PREVIEW_MARKER in content
        assert content.startswith("# Plan of record")
        assert content.rstrip().endswith("TAIL: https://example.test/docs"), "the tail is where conclusions live"
        assert "\n".join(["alpha bravo charlie delta"] * 3) in content
        assert body[3000:-3000] not in content, "and the middle is genuinely not being sent"

    def test_the_marker_counts_add_up(self, client):
        """The marker states four numbers: total, head, tail, omitted. The model acts on the omission
        without checking, so they have to sum. A gap that does not add up is worse than no gap, because
        the agent's only way to check it is to read the whole file, which is the cost this tier exists to
        avoid."""
        body = ("x" * 900 + " ") * 12  # 10,812 characters, a space every 901st so cuts have somewhere to go
        payload = client.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": ("wide.txt", body.encode(), "text/plain")},
        ).json()
        content = payload["content"]

        head, separator, rest = content.partition(uploads.PREVIEW_MARKER)
        assert separator, "no marker between head and tail"
        marker, closer, tail = rest.partition("]\n\n")
        assert closer, "the marker line is not closed"
        head = head.rstrip("\n")
        assert head in body and tail in body, "a preview quotes the file, it does not paraphrase it"

        stated = {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", marker)}
        assert len(body) in stated, f"the total ({len(body)}) is not stated"
        assert len(head) in stated, f"the head length ({len(head)}) is not stated"
        assert len(tail) in stated, f"the tail length ({len(tail)}) is not stated"
        omitted = len(body) - len(head) - len(tail)
        assert omitted in stated, f"the omission ({omitted}) is not stated"
        assert omitted > 0

    def test_cuts_land_on_whitespace(self, client):
        """Neither cut may land inside a word. A preview that opens mid-token reads like a corrupted
        file, and the model has no way to tell that from the file actually being corrupted."""
        body = " ".join(f"word{n}" for n in range(1200))
        preview = uploads.preview_body(body)

        head, separator, rest = preview.partition(uploads.PREVIEW_MARKER)
        head = head.rstrip("\n")
        marker, closer, tail = rest.partition("]\n\n")
        assert separator and closer

        assert head == body[: len(head)]
        assert body[len(head)].isspace(), "the head must stop at whitespace, not inside a word"
        assert body.endswith(tail)
        assert body[len(body) - len(tail) - 1].isspace(), "the tail must start at whitespace"
        assert not head.endswith("wor"), "and specifically not three characters into 'word123'"

    def test_a_previewed_body_survives_the_prompt_path_unchanged(self, client):
        """The tier is a shape, not an enforcement: the browser sends the string back and the prompt path
        re-runs the same verdict on it. Both sides have to hand the model the same bytes, or the live turn
        and the stored one say different things about the same file."""
        body = ("lorem ipsum dolor sit amet " * 400)[:9000]
        payload = client.post(
            "/api/upload-file", data={"scope": SCOPE}, files={"file": ("lorem.txt", body.encode(), "text/plain")}
        ).json()
        assert payload["inlineState"] == "preview"

        tag = uploads.file_tag(
            name="lorem.txt", path="/tmp/lorem.txt", bytes_=len(body), text=payload["content"], mime_type="text/plain"
        )
        assert payload["content"] in tag, "the prompt must carry the preview, not a re-derivation of it"
        assert "over the limit" not in tag and "no text layer" not in tag

class TestInlineStates:
    """`inlined`, `empty` and `over` are three facts, and one of them used to be reported as another.

    The route decided with `len(text) <= cap`, which a scanned PDF passes at length 0; the prompt path
    decided with `0 < len(text) <= cap`, which it fails. The document therefore reached the model blamed
    for a size it did not have, and the advice that follows a size is to read the file in chunks, which
    yields nothing from a page with no text layer either. So the route names the state, and one function
    in `pi_chat/uploads.py` decides it for both places.
    """

    def test_a_document_that_yielded_nothing_says_so_rather_than_blaming_its_size(self, client, monkeypatch):
        # Lowering the cap makes a size complaint *plausible*, which is what makes its absence a claim.
        monkeypatch.setattr(uploads, "INLINE_MAX_CHARS", 40)

        class NoTextPage:
            @staticmethod
            def extract_text():
                return None

        class NoTextPdf:
            pages = [NoTextPage(), NoTextPage()]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(app_module.pdfplumber, "open", lambda raw: NoTextPdf())

        body = client.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": ("licence.pdf", b"%PDF-1.7 not a real pdf", "application/pdf")},
        ).json()

        assert body["inlineState"] == "empty"
        assert body["inlined"] is False
        assert body["content"] is None
        # The path is the whole point of an empty extraction: there is a file, and it is a picture.
        assert Path(body["path"]).exists()

    def test_whitespace_counts_as_no_text_layer(self, client):
        """pdfplumber answers a scanned page with newlines. A body of three `\n` is that same document,
        and handing it over as an inlined body tells the model the file was empty when all it proves is
        that nothing was extracted."""
        body = client.post(
            "/api/upload-file", data={"scope": SCOPE}, files={"file": ("blank.txt", b"\n \n", "text/plain")}
        ).json()

        assert body["inlineState"] == "empty"
        assert body["content"] is None

    def test_a_document_over_the_cap_is_still_blamed_on_its_size(self, client, monkeypatch):
        monkeypatch.setattr(uploads, "INLINE_MAX_CHARS", 20)

        body = client.post(
            "/api/upload-file",
            data={"scope": SCOPE},
            files={"file": ("long.txt", b"z" * 60, "text/plain")},
        ).json()

        assert body["inlineState"] == "over"
        assert body["content"] is None
        assert body["inlined"] is False
