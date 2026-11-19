"""Preview store and the /api/preview surface.

The pane is a file read endpoint with a nice name, so the interesting cases are the denials:
another profile's document, a path outside the allowed roots, a suffix that is not a preview kind.
Every denial must look identical (404), and the two framed kinds must arrive with a CSP that
overrides the site-wide ``frame-ancestors 'none'`` without becoming same-origin.
"""

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi_chat import config
from pi_chat import preview
from pi_chat.app import create_app
from pi_chat.auth import AuthManager, hash_password

MARKDOWN = b"# Title\n\nBody **text**\n"
HTML = b"<!doctype html><html><body><h1>Doc</h1><script>x=1</script></body></html>"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
# The route decides a content type from the suffix, so these bytes never need to decode. What matters
# is the magic number: it is the thing a sniffer would key on if `nosniff` ever stopped being sent.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def preview_root(tmp_path, monkeypatch):
    """Point the whole preview store, and the sessions tree, at a temporary directory."""
    root = tmp_path / "previews"
    monkeypatch.setattr(config, "PREVIEW_BASE_DIR", root)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PREVIEW_EXTRA_ROOTS", [])
    monkeypatch.setattr(config, "DEV_MODE", False)
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def app(preview_root):
    auth = AuthManager()
    auth.password_hashes["b"] = hash_password("test123")
    auth.password_hashes["r"] = hash_password("profile-r-pw")
    return create_app(auth_manager=auth)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def ben(client):
    assert client.post("/api/login", json={"account": "b", "password": "test123"}).status_code == 200
    return client


def store_doc(account: str, doc_id: str, kind: str, data: bytes, scope: str = "sess1234") -> Path:
    return preview.write_document(preview.scope_dir(account, scope, create=True), doc_id, kind, data)


class TestStoredDocuments:
    def test_markdown_is_served_as_text_without_framing(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        response = ben.get(f"/api/preview/{doc_id}")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert response.content == MARKDOWN
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    def test_html_is_served_sandboxed_yet_frameable(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "html", HTML)

        response = ben.get(f"/api/preview/{doc_id}")
        csp = response.headers["content-security-policy"]

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        # The opaque origin is the isolation; scripts only run inside it.
        assert "sandbox allow-scripts" in csp
        assert "allow-same-origin" not in csp
        assert "frame-ancestors 'self'" in csp
        assert "frame-ancestors 'none'" not in csp

    def test_pdf_is_inlined_for_the_browser_viewer(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "pdf", PDF)

        response = ben.get(f"/api/preview/{doc_id}")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert "inline" in response.headers["content-disposition"]
        assert "attachment" not in response.headers["content-disposition"]
        # CSP sandbox on a PDF response interferes with the built-in viewer.
        assert "sandbox" not in response.headers["content-security-policy"]
        assert "frame-ancestors 'self'" in response.headers["content-security-policy"]

    def test_documents_are_never_cacheable_and_never_sniffed(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "html", HTML)

        response = ben.get(f"/api/preview/{doc_id}")

        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_byte_range_request_is_honoured_for_pdf(self, ben, preview_root):
        # The native viewer streams a PDF with range requests; a 200-everything server stalls on
        # large documents.
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "pdf", PDF)

        response = ben.get(f"/api/preview/{doc_id}", headers={"Range": "bytes=0-7"})

        assert response.status_code == 206
        assert response.content == PDF[:8]
        assert response.headers["content-range"] == f"bytes 0-7/{len(PDF)}"

    def test_a_later_revision_of_a_doc_id_replaces_the_first(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        scope = preview.scope_dir("b", "sess1234", create=True)
        preview.write_document(scope, doc_id, "markdown", MARKDOWN)
        preview.write_document(scope, doc_id, "markdown", b"# Second\n")

        response = ben.get(f"/api/preview/{doc_id}")

        assert response.content == b"# Second\n"
        assert list((preview_root / "b").glob(f"**/{doc_id}.*"))[0].read_bytes() == b"# Second\n"


class TestKindContract:
    """The renderer and the CSP are chosen from the kind, so a kind mismatch must not resolve."""

    def test_a_shared_doc_id_resolves_to_the_newest_file(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        md = store_doc("b", doc_id, "markdown", MARKDOWN)
        html = store_doc("b", doc_id, "html", HTML)
        old = time.time() - 60
        os.utime(md, (old, old))
        os.utime(html, (time.time(), time.time()))

        response = ben.get(f"/api/preview/{doc_id}")

        assert response.headers["content-type"].startswith("text/html")

    def test_an_explicit_kind_selects_that_document(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)
        store_doc("b", doc_id, "html", HTML)

        markdown = ben.get(f"/api/preview/{doc_id}", params={"kind": "markdown"})
        html = ben.get(f"/api/preview/{doc_id}", params={"kind": "html"})

        assert markdown.content == MARKDOWN
        assert "frame-ancestors 'none'" in markdown.headers["content-security-policy"]
        assert html.content == HTML
        assert "sandbox allow-scripts" in html.headers["content-security-policy"]

    def test_a_kind_that_does_not_match_the_document_is_not_found(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        assert ben.get(f"/api/preview/{doc_id}", params={"kind": "html"}).status_code == 404

    def test_an_unknown_kind_is_refused(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        assert ben.get(f"/api/preview/{doc_id}", params={"kind": "exe"}).status_code == 400

    def test_a_path_may_not_be_claimed_as_another_kind(self, ben, preview_root, tmp_path):
        work_dir = tmp_path / "sessions" / "b"
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / "notes.md"
        target.write_bytes(MARKDOWN)

        assert ben.get(
            "/api/preview/x", params={"path": str(target), "kind": "html"}
        ).status_code == 404
        assert ben.get(
            "/api/preview/x", params={"path": str(target), "kind": "markdown"}
        ).status_code == 200


class TestDenials:
    def test_another_profiles_document_is_not_found(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("r", doc_id, "markdown", MARKDOWN)

        assert ben.get(f"/api/preview/{doc_id}").status_code == 404

    @pytest.mark.parametrize(
        "doc_id",
        ["..%2f..%2fsecret", "*", "nope", "A" * 32, "0" * 31, ""],
    )
    def test_a_doc_id_that_is_not_a_stored_id_is_not_found(self, ben, doc_id, preview_root):
        store_doc("b", preview.new_doc_id(), "markdown", MARKDOWN)

        assert ben.get(f"/api/preview/{doc_id}").status_code in (400, 404)

    def test_anonymous_read_is_rejected(self, client, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        assert TestClient(app=create_app(auth_manager=AuthManager())).get(
            f"/api/preview/{doc_id}"
        ).status_code == 401

    @pytest.mark.parametrize("raw", ["", "notes.md", "../outside.md", ".\\outside.md"])
    def test_a_relative_path_is_not_found(self, ben, raw, preview_root):
        assert ben.get("/api/preview/x", params={"path": raw}).status_code == 404

    def test_dot_dot_segments_cannot_climb_out_of_the_root(self, ben, preview_root, tmp_path):
        work_dir = tmp_path / "sessions" / "b"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "report.md").write_bytes(MARKDOWN)
        # A real file that exists three levels up, so the denial proves containment rather than
        # merely noticing the path is absent.
        escapee = tmp_path.parent / "outside-secret.md"
        escapee.write_bytes(MARKDOWN)
        sneaky = work_dir / ".." / ".." / ".." / escapee.name
        try:
            assert ben.get(
                "/api/preview/x", params={"path": str(work_dir / "report.md")}
            ).status_code == 200
            assert ben.get(
                "/api/preview/x", params={"path": str(sneaky)}
            ).status_code == 404
            assert preview.resolve_external_path(str(sneaky), "b") is None
        finally:
            escapee.unlink(missing_ok=True)

    def test_a_directory_is_not_a_document(self, ben, preview_root, tmp_path):
        work_dir = tmp_path / "sessions" / "b"
        (work_dir / "folder.html").mkdir(parents=True, exist_ok=True)

        assert ben.get(
            "/api/preview/x", params={"path": str(work_dir / "folder.html")}
        ).status_code == 404

    def test_an_absolute_path_outside_the_allowed_roots_is_not_found(self, ben, preview_root, tmp_path):
        outside = tmp_path.parent / "outside.md"
        outside.write_bytes(MARKDOWN)
        try:
            assert ben.get("/api/preview/x", params={"path": str(outside)}).status_code == 404
        finally:
            outside.unlink(missing_ok=True)

    def test_a_sensitive_suffix_inside_an_allowed_root_is_still_not_served(
        self, ben, preview_root, tmp_path
    ):
        secret = tmp_path / "sessions" / "b"
        secret.mkdir(parents=True, exist_ok=True)
        (secret / "credentials.md").write_bytes(b"secret")
        (secret / ".env").write_bytes(b"SECRET=1")

        assert ben.get(
            "/api/preview/x", params={"path": str(secret / ".env")}
        ).status_code == 404
        assert ben.get(
            "/api/preview/x", params={"path": str(secret / "credentials.md")}
        ).status_code == 200  # a real preview kind, inside the profile's own tree

    @pytest.mark.skipif(os.name != "nt" and not hasattr(os, "symlink"), reason="needs symlinks")
    def test_a_symlink_pointing_out_of_the_root_is_not_served(self, ben, preview_root, tmp_path):
        secret = tmp_path.parent / "outside-secret.html"
        secret.write_bytes(HTML)
        link_dir = tmp_path / "sessions" / "b"
        link_dir.mkdir(parents=True, exist_ok=True)
        link = link_dir / "slippery.html"
        try:
            link.symlink_to(secret, target_is_directory=False)
        except (OSError, NotImplementedError):
            pytest.skip("this user/session may not create symlinks")
        try:
            assert ben.get("/api/preview/x", params={"path": str(link)}).status_code == 404
        finally:
            link.unlink(missing_ok=True)
            secret.unlink(missing_ok=True)


class TestAllowedRoots:
    def test_a_file_in_the_profile_session_tree_is_served(self, ben, preview_root, tmp_path):
        work_dir = tmp_path / "sessions" / "b"
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / "report.html"
        target.write_bytes(HTML)

        response = ben.get("/api/preview/x", params={"path": str(target)})

        assert response.status_code == 200
        assert response.content == HTML

    def test_an_opt_in_root_is_served_until_it_is_removed(self, ben, preview_root, tmp_path, monkeypatch):
        extra = tmp_path / "shared-docs"
        extra.mkdir()
        target = extra / "notes.md"
        target.write_bytes(MARKDOWN)

        assert ben.get("/api/preview/x", params={"path": str(target)}).status_code == 404

        monkeypatch.setattr(config, "PREVIEW_EXTRA_ROOTS", [extra])

        assert ben.get("/api/preview/x", params={"path": str(target)}).status_code == 200

    def test_the_other_profiles_session_tree_is_not_a_root(self, ben, preview_root, tmp_path):
        hers = tmp_path / "sessions" / "r"
        hers.mkdir(parents=True, exist_ok=True)
        target = hers / "private.md"
        target.write_bytes(MARKDOWN)

        assert ben.get("/api/preview/x", params={"path": str(target)}).status_code == 404

    def test_dev_mode_relaxes_the_roots(self, ben, preview_root, tmp_path, monkeypatch):
        outside = tmp_path.parent / "anywhere.html"
        outside.write_bytes(HTML)
        monkeypatch.setattr(config, "DEV_MODE", True)
        try:
            assert ben.get("/api/preview/x", params={"path": str(outside)}).status_code == 200
        finally:
            outside.unlink(missing_ok=True)

    def test_the_site_wide_policy_still_denies_framing_the_app(self, ben):
        csp = ben.get("/").headers["content-security-policy"]

        assert "frame-ancestors 'none'" in csp


class TestStoreHelpers:
    def test_unknown_profile_cannot_become_a_path_segment(self, preview_root):
        with pytest.raises(preview.PreviewError):
            preview.account_dir("../../etc")

    def test_a_hostile_scope_name_cannot_escape_its_account_directory(self, preview_root):
        scope = preview.scope_dir("b", "..\\..\\pwned", create=True)

        assert scope.is_dir()
        assert scope.relative_to(preview_root / "b")

    def test_an_oversized_document_is_refused(self, preview_root, monkeypatch):
        monkeypatch.setattr(config, "PREVIEW_MAX_DOC_MB", 1)
        scope = preview.scope_dir("b", "s", create=True)

        with pytest.raises(preview.PreviewError, match="limit"):
            preview.write_document(scope, preview.new_doc_id(), "markdown", b"x" * (2 * 1024 * 1024))

    def test_a_failed_write_leaves_no_partial_document(self, preview_root, monkeypatch):
        monkeypatch.setattr(config, "PREVIEW_MAX_DOC_MB", 1)
        doc_id = preview.new_doc_id()
        scope = preview.scope_dir("b", "s", create=True)

        with pytest.raises(preview.PreviewError):
            preview.write_document(scope, doc_id, "markdown", b"x" * (2 * 1024 * 1024))

        assert list(scope.glob("*")) == []

    def test_an_unsupported_kind_is_refused(self, preview_root):
        with pytest.raises(preview.PreviewError):
            preview.document_path(preview_root, preview.new_doc_id(), "exe")

    def test_svg_is_not_a_kind_the_store_can_hold(self, preview_root):
        # An SVG is script. It is absent from the suffix tables on purpose, and a kind that cannot be
        # named cannot be written, served, or polled for.
        with pytest.raises(preview.PreviewError):
            preview.document_path(preview_root, preview.new_doc_id(), "svg")
        assert preview.kind_for(Path("drawing.svg")) is None
        assert preview.content_type_for(Path("drawing.svg")) is None


class TestImages:
    """A generated image is a document like any other, and an `<img>` is not a browsing context.

    The pane fetches these into the application's own page, so the interesting properties are that the
    response cannot be framed, cannot be read across profiles, and cannot be a suffix the tables never
    listed. The bytes themselves are the image tool's business, not the route's.
    """

    def test_a_stored_image_is_served_as_png_and_cannot_be_framed(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "image", PNG)

        response = ben.get(f"/api/preview/{doc_id}")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        assert response.content == PNG
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        # Inline with a filename: the pane's download button relies on `download` overriding inline,
        # and a saved file should not be named after a doc id.
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("inline")
        assert f"{doc_id}.png" in disposition

    def test_an_image_is_not_readable_by_the_other_profile(self, client, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "image", PNG)
        assert client.post("/api/login", json={"account": "r", "password": "profile-r-pw"}).status_code == 200

        assert client.get(f"/api/preview/{doc_id}", params={"kind": "image"}).status_code == 404

    def test_an_image_cannot_be_asked_for_as_another_kind(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "image", PNG)

        # The renderer and the CSP both come from `kind`, so a stored PNG fetched by a markdown viewer
        # would be handed raw bytes to a text renderer that then treats them as model output.
        assert ben.get(f"/api/preview/{doc_id}", params={"kind": "markdown"}).status_code == 404
        assert ben.get(f"/api/preview/{doc_id}", params={"kind": "image"}).status_code == 200

    def test_a_png_inside_the_profile_session_tree_is_served_by_path(self, ben, preview_root, tmp_path):
        tree = tmp_path / "sessions" / "b"
        tree.mkdir(parents=True, exist_ok=True)
        photo = tree / "photo.png"
        photo.write_bytes(PNG)

        response = ben.get("/api/preview/anything", params={"path": str(photo), "kind": "image"})

        assert response.status_code == 200
        assert response.content == PNG

    def test_a_referenced_svg_is_refused_even_inside_an_allowed_root(self, ben, preview_root, tmp_path):
        tree = tmp_path / "sessions" / "b"
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "diagram.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")

        assert ben.get(
            "/api/preview/anything", params={"path": str(tree / "diagram.svg")}
        ).status_code == 404

    def test_freshness_answers_for_an_image_document(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        path = store_doc("b", doc_id, "image", PNG)

        response = ben.get("/api/preview/fresh", params={"doc": doc_id, "kind": "image"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["served"] is True
        assert payload["bytes"] == path.stat().st_size
        # The pane re-fetches on movement of this token, so it must be the validator and not a display
        # timestamp - and it must not survive the bytes being deleted.
        assert payload["token"] == f"{path.stat().st_mtime_ns}-{path.stat().st_size}"
        path.unlink()
        assert ben.get("/api/preview/fresh", params={"doc": doc_id, "kind": "image"}).json()["served"] is False

    def test_a_draw_that_has_not_written_yet_is_reported_as_absent_not_as_an_error(self, preview_root):
        # image-gen opens the document before sd-cli has produced a byte, so "no file" is the normal
        # state of a `rendering` entry. It has to be the same answer as any other miss, because the
        # pane decides "still drawing" from `state`, not from a new error shape.
        doc_id = preview.new_doc_id()

        assert preview.head("b", doc_id=doc_id, kind="image") == preview.NOT_FOUND

    def test_replay_carries_the_stage_and_the_canvas(self, preview_root):
        # Without this the fields exist only in the live event, and a page reloaded mid-draw would open
        # a document with neither a shape nor a reason, which is worse than not reopening it at all.
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "image", PNG)
        entry = {
            "v": 2,
            "op": "open",
            "docId": doc_id,
            "kind": "image",
            "title": "Fox, 2048x2048",
            "revision": 1,
            "file": f"{doc_id}.png",
            "state": "rendering",
            "canvas": "2048x2048",
            "watch": False,
            # pi's own bookkeeping must not reach a browser payload with the rest of the record.
            "turnId": "t-99",
        }

        described = preview.describe_preview_entries("b", [entry])

        assert described[0]["state"] == "rendering"
        assert described[0]["canvas"] == "2048x2048"
        # Following is per document since S8, and a human's choice has to survive a reload or the pane
        # starts polling a tab they deliberately froze.
        assert described[0]["watch"] is False
        assert described[0]["available"] is True
        assert "turnId" not in described[0]


class TestCleanup:
    def test_expired_documents_are_removed_and_fresh_ones_survive(self, preview_root, monkeypatch):
        old = store_doc("b", preview.new_doc_id(), "markdown", MARKDOWN)
        fresh = store_doc("b", preview.new_doc_id(), "markdown", MARKDOWN)
        stale = time.time() - 8 * 86400
        os.utime(old, (stale, stale))
        monkeypatch.setattr(config, "PREVIEW_TTL_DAYS", 7)

        removed, freed = preview.cleanup()

        assert (removed, freed) == (1, len(MARKDOWN))
        assert not old.exists()
        assert fresh.exists()

    def test_the_size_cap_removes_oldest_first(self, preview_root, monkeypatch):
        old = store_doc("b", preview.new_doc_id(), "markdown", b"a" * 100)
        fresh = store_doc("b", preview.new_doc_id(), "markdown", b"b" * 100)
        stale = time.time() - 2 * 86400
        os.utime(old, (stale, stale))
        monkeypatch.setattr(config, "PREVIEW_TTL_DAYS", 0)

        removed, _ = preview.cleanup(max_bytes=150)

        assert removed == 1
        assert not old.exists()
        assert fresh.exists()

    def test_cleanup_on_an_absent_store_is_a_no_op(self, preview_root):
        assert preview.cleanup() == (0, 0)
