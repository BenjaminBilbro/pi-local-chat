"""Attachments reach the agent as files, not only as text.

The defect this locks out is the one Ben hit by using the app: an image was handed to pi as base64 and
never written anywhere, so an assistant asked to edit a photo could see it and had no way to name the
file to `edit_image`. A PDF was worse - transcribed, then dropped, with the original bytes thrown away
and the transcription capped at nothing at all, so a 5 MB text file cost about 1.2 million tokens.

So: bytes land under the account's own work directory, the prompt carries the absolute path, and the
inline copy of a document has a ceiling above which the agent is given the path instead. These are
dispatch-layer tests with a stub pi, because the interesting part is what goes out on the wire, and a
real subprocess would only show it through a model call.
"""

from __future__ import annotations

import base64
import os
import time

import pytest

from pi_chat import uploads
from pi_chat.websocket import _dispatch_command

SCOPE = "8f3c2a1e-4b5d-4e6f-9a8b-7c6d5e4f3a2b"
PNG = base64.decodebytes(b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4\n2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==\n")


@pytest.fixture(autouse=True)
def work_tree(tmp_path, monkeypatch):
    """A scratch `sessions/`, so no test writes into the real one."""
    monkeypatch.setattr(uploads, "PROJECT_ROOT", tmp_path)
    return tmp_path


class StubPi:
    """The slice of PiProcess the prompt path touches, with the wire recorded."""

    def __init__(self, account: str = "b") -> None:
        self.account = account
        self.sent: list[dict] = []
        self.browser: list[dict] = []
        self.proc = object()

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def send_browser_json(self, payload: dict) -> None:
        self.browser.append(payload)


async def send_prompt(pi, command: dict) -> None:
    await _dispatch_command(pi, pi.account, {"type": "prompt", **command}, None, None)


def stored_images(work_tree, account: str = "b") -> list:
    root = work_tree / "sessions" / account / "uploads"
    return sorted(root.rglob("*") if root.is_dir() else [])


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


class TestUploadStore:
    def test_a_saved_file_lands_under_the_account_work_dir(self, work_tree):
        stored = uploads.store(
            "b", SCOPE, original_name="note.txt", data=b"hello", mime_type="text/plain"
        )
        assert stored is not None
        assert stored.path == work_tree / "sessions" / "b" / "uploads" / SCOPE / stored.path.name
        assert stored.path.read_bytes() == b"hello"
        assert stored.ref == f"{SCOPE}/{stored.path.name}"
        assert stored.bytes == 5

    def test_a_scope_must_look_like_one(self, work_tree):
        for bad in ("../../etc", "", "Not-A-Uuid", SCOPE + "/extra", None):
            assert uploads.is_valid_scope(bad) is False
            assert uploads.store("b", bad, original_name="x.txt", data=b"x") is None
            assert uploads.scope_dir("b", bad) is None

    def test_a_filename_is_reduced_to_one_safe_segment(self):
        assert uploads.safe_name(r"..\..\windows\notes.txt") == "notes.txt"
        assert uploads.safe_name("two  spaces & quotes'.pdf") == "two-spaces-quotes.pdf"
        assert uploads.safe_name("x" * 200 + ".txt").startswith("x" * 60)
        assert len(uploads.safe_name("x" * 200 + ".txt")) <= 64
        assert uploads.safe_name("no extension here") == "no-extension-here"
        # A suffix that is not a plausible extension is dropped, not trusted.
        assert uploads.safe_name("notes.this-is-not-an-extension") == "notes.this-is-not-an-extension"
        assert uploads.safe_name("") == "file"

    def test_two_files_of_the_same_name_do_not_collide(self, work_tree):
        first = uploads.store("b", SCOPE, original_name="IMG_4913.jpg", data=b"a")
        second = uploads.store("b", SCOPE, original_name="IMG_4913.jpg", data=b"b")
        assert first.path != second.path
        assert second.path.read_bytes() == b"b"

    def test_an_image_keeps_a_suffix_the_image_tools_will_open(self):
        assert uploads.image_suffix_for("photo.jpeg", "application/octet-stream") == ".jpg"
        assert uploads.image_suffix_for("shot", "image/webp") == ".webp"
        # A TIFF named .png is still a mystery; png is the safe guess, and the name is not trusted.
        assert uploads.image_suffix_for("scan.tiff", "image/tiff") == ".png"

    def test_a_ref_resolves_only_inside_this_account(self, work_tree):
        stored = uploads.store("b", SCOPE, original_name="note.txt", data=b"hello")
        assert uploads.resolve_ref("b", stored.ref) == stored.path
        assert uploads.resolve_ref("r", stored.ref) is None
        assert uploads.resolve_ref("b", f"{SCOPE}/nope.txt") is None
        assert uploads.resolve_ref("b", f"../{SCOPE}/note.txt") is None
        assert uploads.resolve_ref("b", f"{SCOPE}/../uploads") is None
        assert uploads.resolve_ref("b", SCOPE) is None
        assert uploads.resolve_ref("b", "") is None
        assert uploads.resolve_ref("b", None) is None
        # A directory is not a file even when it is in the right place.
        assert uploads.resolve_ref("b", SCOPE) is None

    def test_a_ref_cannot_reach_the_wider_disk(self, work_tree):
        root = work_tree / "sessions" / "b" / "uploads"
        root.mkdir(parents=True)
        (root.parent / "secret.txt").write_text("not for the browser", encoding="utf-8")
        (work_tree / "sessions" / "r" / "uploads").mkdir(parents=True)
        (work_tree / "sessions" / "r" / "uploads" / "other.txt").write_text("else", encoding="utf-8")
        assert uploads.resolve_ref("b", f"{SCOPE}/../secret.txt") is None
        assert uploads.resolve_ref("b", f"{SCOPE}%2F..%2Fsecret.txt") is None
        # Another profile's upload is not this profile's to read, even with a well-formed ref.
        assert uploads.resolve_ref("b", "uploads/other.txt") is None

    def test_prune_forgets_the_old_and_keeps_the_recent(self, work_tree, monkeypatch):
        old = uploads.store("b", SCOPE, original_name="old.txt", data=b"old")
        fresh = uploads.store("b", SCOPE, original_name="new.txt", data=b"new")
        stale = time.time() - 40 * 86400
        os.utime(old.path, (stale, stale))
        assert uploads.prune() == 1
        assert not old.path.exists()
        assert fresh.path.exists()

    def test_prune_removes_the_scope_directory_it_emptied(self, work_tree):
        stored = uploads.store("b", SCOPE, original_name="old.txt", data=b"old")
        stale = time.time() - 40 * 86400
        os.utime(stored.path, (stale, stale))
        uploads.prune()
        assert not (work_tree / "sessions" / "b" / "uploads" / SCOPE).exists()

    def test_a_zero_retention_means_leave_everything_alone(self, work_tree, monkeypatch):
        stored = uploads.store("b", SCOPE, original_name="old.txt", data=b"old")
        stale = time.time() - 400 * 86400
        os.utime(stored.path, (stale, stale))
        monkeypatch.setattr(uploads, "RETENTION_DAYS", 0)
        assert uploads.prune() == 0
        assert stored.path.exists()


# --------------------------------------------------------------------------
# the tags the model reads
# --------------------------------------------------------------------------


class TestAttachmentTags:
    def test_a_file_tag_carries_the_path_and_the_body(self):
        tag = uploads.file_tag(
            name="note.txt", path="C:\\u\\0a-note.txt", bytes_=5, text="BODY", mime_type="text/plain"
        )
        assert 'name="note.txt"' in tag
        assert 'path="C:\\u\\0a-note.txt"' in tag
        assert tag.endswith(">BODY</file>")

    def test_a_name_with_a_quote_cannot_close_its_own_attribute(self):
        tag = uploads.file_tag(name='a"b>x.txt', path="/tmp/x", bytes_=1, text="x")
        assert 'name="a&quot;b&gt;x.txt"' in tag

    def test_a_document_left_out_says_why_rather_than_looking_empty(self, monkeypatch):
        monkeypatch.setattr(uploads, "INLINE_MAX_CHARS", 1234)
        tag = uploads.file_tag(name="big.log", path="/tmp/big.log", bytes_=99, text=None)
        assert "not inlined" in tag
        # The limit it quotes is the one in force, not a literal copied into a template.
        assert "1,234 characters" in tag

    def test_the_block_wraps_so_one_rule_removes_it(self):
        block = uploads.attachment_block(['<file name="a" path="/a" bytes="1">x</file>'])
        assert block.startswith("<attachments>")
        assert block.endswith("</attachments>")
        assert "read, edit_image, generate_image" in block
        assert uploads.attachment_block([]) == ""


# --------------------------------------------------------------------------
# the prompt that goes out
# --------------------------------------------------------------------------


class TestPromptCarriesPaths:
    async def test_an_image_is_written_and_its_path_is_sent(self, work_tree):
        pi = StubPi()
        await send_prompt(pi, {
            "message": "fix the glare",
            "uploadScope": SCOPE,
            "images": [{
                "type": "image",
                "data": base64.b64encode(PNG).decode(),
                "mimeType": "image/png",
                "filename": "IMG_4913.jpg",
            }],
        })

        prompt = pi.sent[0]["message"]
        assert prompt.startswith("fix the glare\n\n<attachments>")
        assert "edit_image" in prompt
        files = [p for p in stored_images(work_tree) if p.is_file()]
        assert len(files) == 1
        # The bytes are stored as they arrived, under a name the image tools will open.
        assert files[0].read_bytes() == PNG
        assert files[0].name.endswith("-IMG_4913.jpg")
        assert f'path="{files[0]}"' in prompt
        # And pi still gets the picture itself: the path is an address, not a replacement.
        assert pi.sent[0]["images"][0]["data"] == base64.b64encode(PNG).decode()
        assert pi.sent[0]["images"][0]["mimeType"] == "image/png"

    async def test_a_data_url_still_arrives_as_bytes(self, work_tree):
        pi = StubPi()
        await send_prompt(pi, {
            "message": "what is this",
            "uploadScope": SCOPE,
            "images": [{
                "type": "image",
                "data": f"data:image/png;base64,{base64.b64encode(PNG).decode()}",
                "mimeType": "image/png",
            }],
        })
        assert pi.sent[0]["images"][0]["data"] == base64.b64encode(PNG).decode()
        assert [p for p in stored_images(work_tree) if p.is_file()][0].read_bytes() == PNG

    async def test_a_document_sends_its_text_and_its_path(self, work_tree):
        stored = uploads.store(
            "b", SCOPE, original_name="report.pdf", data=b"%PDF-1.4 body", mime_type="application/pdf"
        )
        pi = StubPi()
        await send_prompt(pi, {
            "message": "summarise",
            "uploadScope": SCOPE,
            "attachments": [{"ref": stored.ref, "name": "report.pdf", "text": "THE TEXT", "mimeType": "application/pdf"}],
        })

        prompt = pi.sent[0]["message"]
        assert f'path="{stored.path}"' in prompt
        assert ">THE TEXT</file>" in prompt
        # The random prefix keeps two uploads apart; the name the model repeats should not wear it.
        assert 'name="report.pdf"' in prompt

    async def test_an_oversized_document_is_path_only(self, work_tree, monkeypatch):
        monkeypatch.setattr(uploads, "INLINE_MAX_CHARS", 20)
        stored = uploads.store("b", SCOPE, original_name="huge.txt", data=b"x" * 5000)
        pi = StubPi()
        await send_prompt(pi, {
            "message": "read this",
            "uploadScope": SCOPE,
            "attachments": [{"ref": stored.ref, "name": "huge.txt", "text": "y" * 5000}],
        })

        prompt = pi.sent[0]["message"]
        assert "y" * 5000 not in prompt
        assert "not inlined" in prompt
        assert f'path="{stored.path}"' in prompt

    async def test_a_missing_file_refuses_the_prompt_rather_than_losing_it_quietly(self, work_tree):
        pi = StubPi()
        await send_prompt(pi, {
            "message": "look at this",
            "uploadScope": SCOPE,
            "attachments": [{"ref": f"{SCOPE}/00000000-gone.pdf", "name": "gone.pdf", "text": "x"}],
        })
        assert pi.sent == [], "a prompt that lost its attachment must not go out at all"
        errors = [m for m in pi.browser if m.get("type") == "error"]
        assert errors, "and the reader has to be told, not left waiting"
        assert "Attach it again" in errors[0]["message"]

    async def test_an_oversized_image_is_refused_with_the_number_that_matters(self, work_tree, monkeypatch):
        monkeypatch.setattr(uploads, "MAX_IMAGE_BYTES", 16)
        pi = StubPi()
        await send_prompt(pi, {
            "message": "edit",
            "uploadScope": SCOPE,
            "images": [{"type": "image", "data": base64.b64encode(b"z" * 400).decode(), "mimeType": "image/png"}],
        })
        assert pi.sent == []
        assert any("over the" in m.get("message", "") for m in pi.browser if m.get("type") == "error")

    async def test_a_file_with_no_message_is_still_a_message(self, work_tree):
        stored = uploads.store("b", SCOPE, original_name="note.txt", data=b"hello")
        pi = StubPi()
        await send_prompt(pi, {
            "message": "",
            "uploadScope": SCOPE,
            "attachments": [{"ref": stored.ref, "name": "note.txt", "text": "hello"}],
        })
        assert pi.sent[0]["message"].startswith("<attachments>")

    async def test_an_empty_prompt_is_still_refused(self, work_tree):
        pi = StubPi()
        await send_prompt(pi, {"message": "   ", "uploadScope": SCOPE})
        assert pi.sent == []
        assert any(m.get("type") == "error" for m in pi.browser)

    async def test_a_tab_without_a_scope_still_sends_the_picture(self, work_tree, monkeypatch):
        """Old tab, no scope: degraded to how this always worked, not broken."""
        pi = StubPi()
        await send_prompt(pi, {
            "message": "what is this",
            "images": [{"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}],
        })
        assert pi.sent[0]["images"], "the bytes still reach the model"
        assert "<attachments>" not in pi.sent[0]["message"], "and no path is invented"
        assert not any(path.is_file() for path in stored_images(work_tree))

    async def test_two_attachments_are_numbered_by_the_note_that_counts_them(self, work_tree):
        stored = uploads.store("b", SCOPE, original_name="a.txt", data=b"a")
        pi = StubPi()
        await send_prompt(pi, {
            "message": "both",
            "uploadScope": SCOPE,
            "attachments": [{"ref": stored.ref, "name": "a.txt", "text": "a"}],
            "images": [{"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}],
        })
        prompt = pi.sent[0]["message"]
        assert "The 2 attached files on disk are at these exact paths." in prompt
        assert prompt.count("<file ") == 1
        assert prompt.count("<image ") == 1
