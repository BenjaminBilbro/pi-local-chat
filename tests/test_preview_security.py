"""The isolation boundaries around framed preview documents.

`tests/test_preview_api.py` covers what the store accepts and refuses. This file covers the other
half: what a browser is told to do with what comes back. Two mechanisms hold the line and each can
silently disappear in a refactor, which is why they are asserted from the shipped artifacts rather
than from the constants that produce them:

1. the iframe's `sandbox` attribute, with no `allow-same-origin`, so an HTML document runs in an
   opaque origin and cannot read this app's cookie;
2. a per-kind `Content-Security-Policy` on the response, which overrides the site-wide
   ``frame-ancestors 'none'`` for the kinds that legitimately get framed and refuses framing for
   the kind that never should be.

What pytest cannot check is that the browser obeys. ``tools/screenshot_ui.mjs --stage html`` opens a
probe document that prints its effective origin, whether ``parent.location``, ``document.cookie`` and
``localStorage`` throw, and whether a same-origin ``fetch`` succeeded; ``--stage pdf`` proves the
native viewer renders for the one kind that goes unsandboxed. Both waits assert the isolation the
frame actually ended up with, not the kind that was requested.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi_chat import config
from pi_chat import preview
from pi_chat.app import create_app
from pi_chat.auth import AuthManager, hash_password
from pi_chat.preview import (
    HTML_DOCUMENT_CSP,
    IMAGE_DOCUMENT_CSP,
    PDF_DOCUMENT_CSP,
    TEXT_DOCUMENT_CSP,
)

REPO = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO / "static" / "index.html"
PREVIEW_JS = REPO / "static" / "preview.js"

MARKDOWN = b"# Title\n\nBody **text**\n"
HTML = b"<!doctype html><html><body><h1>Doc</h1><script>alert(1)</script></body></html>"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
# Suffixed `.png`, magic-numbered, and never decoded by anything here: the route decides from the
# suffix, and the whole point of these tests is what arrives around the bytes.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def preview_root(tmp_path, monkeypatch):
    root = tmp_path / "previews"
    monkeypatch.setattr(config, "PREVIEW_BASE_DIR", root)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PREVIEW_EXTRA_ROOTS", [])
    monkeypatch.setattr(config, "DEV_MODE", False)
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def client(preview_root):
    auth = AuthManager()
    auth.password_hashes["b"] = hash_password("test123")
    auth.password_hashes["r"] = hash_password("profile-r-pw")
    return TestClient(create_app(auth_manager=auth))


@pytest.fixture
def ben(client):
    assert client.post("/api/login", json={"account": "b", "password": "test123"}).status_code == 200
    return client


def store_doc(account: str, doc_id: str, kind: str, data: bytes, scope: str = "sess1234") -> Path:
    return preview.write_document(preview.scope_dir(account, scope, create=True), doc_id, kind, data)


def frame_tag() -> str:
    match = re.search(r"<iframe[^>]*data-doc-view=[\"']frame[\"'][^>]*>", INDEX_HTML.read_text("utf-8"))
    assert match, "the preview pane has no frame element in static/index.html"
    return match.group(0)


class TestFrameMarkup:
    """The attribute that decides whether a document shares this origin."""

    def test_the_frame_is_sandboxed_in_the_markup(self):
        # Static markup, not a runtime assignment: a document that reaches the frame before any
        # script has had a chance to configure it still has to be sandboxed.
        assert 'sandbox="allow-scripts"' in frame_tag()

    def test_the_frame_never_asks_for_a_same_origin(self):
        assert "allow-same-origin" not in frame_tag()

    def test_the_renderer_never_adds_a_same_origin(self):
        # The one token that would turn `sandbox` back into a same-origin frame, and so the only way
        # a future "fix" for a broken document could quietly reopen the hole. Comments may name it
        # to explain why it must not appear; code may not use it.
        offenders = [
            line.strip()
            for line in PREVIEW_JS.read_text("utf-8").splitlines()
            if "allow-same-origin" in line
            and not line.strip().startswith(("*", "/*", "//"))
        ]
        assert offenders == []

    def test_a_pdf_frame_drops_the_attribute_and_everything_else_keeps_it(self):
        # Measured 2026-11-09: with `allow-scripts` the built-in viewer refuses a PDF, so that one
        # kind goes unsandboxed and the omission has to stay deliberate.
        source = PREVIEW_JS.read_text("utf-8")
        assert "html: 'allow-scripts'" in source
        assert "pdf: null" in source
        # Anything the table does not name keeps the strict setting rather than defaulting to open.
        assert "=== undefined ? 'allow-scripts'" in source
        # And the attribute is only ever removed when the table explicitly says so.
        assert "if (wanted === null)" in source

    def test_the_frame_is_never_written_with_injected_markup(self):
        # Document bytes travel as a URL only. Any write of content into the pane would have to go
        # through here, and markdown already has its own sanitized DOM path.
        source = PREVIEW_JS.read_text("utf-8")
        for sink in ("document.write", "outerHTML", "insertAdjacentHTML"):
            assert sink not in source
        assert "frameEl.srcdoc" not in source

    def test_opening_in_a_new_tab_cannot_reach_back_to_the_app(self):
        tag = re.search(
            r"<a[^>]*data-doc-external[^>]*>", INDEX_HTML.read_text("utf-8")
        ).group(0)
        assert 'target="_blank"' in tag
        assert "noopener" in tag


class TestDocumentHeaders:
    """Per-kind CSP, asserted on real responses rather than on the constants."""

    def test_html_arrives_sandboxed_and_framable_by_us(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "html", HTML)

        response = ben.get(f"/api/preview/{doc_id}", params={"kind": "html"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        csp = response.headers["content-security-policy"]
        assert "sandbox allow-scripts" in csp
        assert "allow-same-origin" not in csp
        assert "frame-ancestors 'self'" in csp
        assert "default-src 'none'" in csp
        assert "connect-src 'none'" in csp
        assert "form-action 'none'" in csp
        # One CSP, the document's own: the site-wide policy must not ride along on the same header.
        assert len(response.headers.get_list("content-security-policy")) == 1

    def test_pdf_is_framable_without_a_sandbox_directive(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "pdf", PDF)

        response = ben.get(f"/api/preview/{doc_id}", params={"kind": "pdf"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/pdf")
        # Inline, or the browser downloads the document instead of showing it.
        assert response.headers["content-disposition"].startswith("inline")
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'self'" in csp
        # No `sandbox` token here. The frame carries no sandbox attribute for this kind either (the
        # viewer needs both off), which is why a PDF is the one preview whose body is read as an
        # application body of this origin rather than in an opaque frame. preview.py's
        # PDF_DOCUMENT_CSP records what makes that safe to accept.
        assert "sandbox" not in csp
        assert "default-src 'none'" in csp

    def test_markdown_refuses_to_be_framed_at_all(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        response = ben.get(f"/api/preview/{doc_id}")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    def test_no_preview_response_may_be_cached(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "html", HTML)

        response = ben.get(f"/api/preview/{doc_id}", params={"kind": "html"})

        # Documents are rewritten in place under one doc id, so a cached revision would be a
        # stale document that still looks authoritative.
        assert response.headers["cache-control"] == "no-store"

    def test_every_preview_response_refuses_to_be_sniffed(self, ben, preview_root):
        for kind, payload in (("markdown", MARKDOWN), ("html", HTML), ("pdf", PDF)):
            doc_id = preview.new_doc_id()
            store_doc("b", doc_id, kind, payload)

            response = ben.get(f"/api/preview/{doc_id}", params={"kind": kind})

            assert response.headers["x-content-type-options"] == "nosniff"

    def test_the_four_kinds_do_not_share_a_policy_carelessly(self):
        # Three policies for four kinds. Markdown and an image genuinely share one: both are read by
        # the application's own page, so both need only the refusal to be framed. The tripwire is the
        # other way - collapsing html or pdf into that pair would break the viewer or make model output
        # framable.
        assert len({HTML_DOCUMENT_CSP, PDF_DOCUMENT_CSP, TEXT_DOCUMENT_CSP, IMAGE_DOCUMENT_CSP}) == 3
        assert IMAGE_DOCUMENT_CSP == TEXT_DOCUMENT_CSP

    def test_an_image_arrives_unframable(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "image", PNG)

        response = ben.get(f"/api/preview/{doc_id}")
        csp = response.headers["content-security-policy"]

        assert csp == IMAGE_DOCUMENT_CSP
        # An image response is never a document, so it must not gain a sandbox or a framing allowance
        # by drifting toward the pdf policy - that would be a second origin this app can be framed in.
        assert "sandbox" not in csp
        assert "frame-ancestors" in csp


class TestKindContractStillHoldsOverHttp:
    """The kind chooses both the renderer and the CSP, so a mismatch has to be a hard refusal."""

    def test_a_frame_cannot_be_handed_the_wrong_kind(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "html", HTML)

        # A PDF frame runs without a sandbox attribute. Serving it HTML would be the one way to get
        # script execution in this origin, so the request that would cause it is refused outright.
        assert ben.get(f"/api/preview/{doc_id}", params={"kind": "pdf"}).status_code == 404
        assert ben.get(f"/api/preview/{doc_id}", params={"kind": "markdown"}).status_code == 404

    def test_an_unknown_kind_is_refused_before_any_file_lookups(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "html", HTML)

        assert ben.get(f"/api/preview/{doc_id}", params={"kind": "exe"}).status_code == 400

    def test_a_foreign_profile_cannot_read_another_profiles_document(self, client, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "html", HTML)
        assert client.post("/api/login", json={"account": "r", "password": "profile-r-pw"}).status_code == 200

        assert client.get(f"/api/preview/{doc_id}", params={"kind": "html"}).status_code == 404

    def test_denials_are_indistinguishable(self, ben, preview_root):
        missing = ben.get(f"/api/preview/{'0' * 32}")
        outside = ben.get("/api/preview/anything", params={"path": "C:/Windows/win.ini"})

        assert missing.status_code == outside.status_code == 404
        assert "win.ini" not in outside.text
        assert "Windows" not in outside.text
