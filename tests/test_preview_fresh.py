"""The staleness primitive: `preview.head`, and the `/api/preview/fresh` route in front of it.

WP-1 of `~/.pi/agent/plans/pi-chat-preview-interactive.md`. The pane is going to poll this for the one
document it is showing, which makes two properties load-bearing and worth pinning in tests rather than
in prose:

1. **It reports on exactly what the serving route would serve.** Same pointers, same preference order
   (`path` before the stored copy), same allowlist. If they drifted, the pane would reload on a file it
   cannot fetch, or go stale on one it can.
2. **It is not a path oracle.** Every refusal is the same answer, for the same reason
   `resolve_external_path` returns None instead of the route raising 403.

The last test is a measurement, not an assertion of fact: it bounds a regression in a route the pane
will hit once a second, and prints the number so the next person can see what this box actually does.
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
GONE = {"served": False, "bytes": None, "mtime": None, "token": None}


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


@pytest.fixture
def outside(tmp_path):
    """A file outside every allowed root, so the denial path has something real to refuse."""
    path = tmp_path.parent / "not-a-root" / "secret.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MARKDOWN)
    return path


def store_doc(account: str, doc_id: str, kind: str, data: bytes, scope: str = "sess1234") -> Path:
    return preview.write_document(preview.scope_dir(account, scope, create=True), doc_id, kind, data)


def rewrite(path: Path, data: bytes) -> None:
    """Write past the timestamp granularity a same-size save could hide inside."""
    path.write_bytes(data)
    future = int(time.time()) + 10
    os.utime(path, (future, future))


class TestHeadOnStoredDocuments:
    def test_a_stored_document_reports_its_size_and_a_token(self, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        head = preview.head("b", doc_id=doc_id, kind="markdown")

        assert head["served"] is True
        assert head["bytes"] == len(MARKDOWN)
        assert head["token"]
        assert head["mtime"].endswith("+00:00")

    def test_the_same_bytes_answer_with_the_same_token_twice(self, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "html", MARKDOWN)

        first = preview.head("b", doc_id=doc_id, kind="html")
        second = preview.head("b", doc_id=doc_id, kind="html")

        assert first == second, "a polling pane would reload forever on a stable file"

    def test_a_change_of_bytes_moves_the_token(self, preview_root):
        doc_id = preview.new_doc_id()
        path = store_doc("b", doc_id, "markdown", MARKDOWN)

        before = preview.head("b", doc_id=doc_id, kind="markdown")
        rewrite(path, b"# Changed\n")
        after = preview.head("b", doc_id=doc_id, kind="markdown")

        assert before["token"] != after["token"]
        assert after["bytes"] == len(b"# Changed\n")

    def test_the_token_survives_a_delete_then_restore(self, preview_root):
        doc_id = preview.new_doc_id()
        path = store_doc("b", doc_id, "markdown", MARKDOWN)
        info = path.stat()
        original = f"{info.st_mtime_ns}-{info.st_size}"

        path.unlink()
        assert preview.head("b", doc_id=doc_id, kind="markdown") == GONE

        # A copy-back that preserves mtime and size is the same document to a validator. The pane
        # will not reload, and that is correct: the bytes it is holding are still the bytes on disk.
        rewrite(path, MARKDOWN)
        os.utime(path, ns=(info.st_mtime_ns, info.st_mtime_ns))
        assert preview.head("b", doc_id=doc_id, kind="markdown")["token"] == original

    def test_a_kind_that_is_not_asked_for_is_not_reported_on(self, preview_root):
        # The same one-doc-id-three-kinds bug that made `kind` a contract on the serving route
        # (docs/preview-pane-handoff.md): a freshness answer for a different kind would drive a
        # reload of bytes the pane was never given.
        doc_id = preview.new_doc_id()
        scope = preview.scope_dir("b", "sess1234", create=True)
        preview.write_document(scope, doc_id, "markdown", MARKDOWN)
        preview.write_document(scope, doc_id, "html", MARKDOWN + b"<!-- html -->")

        html = preview.head("b", doc_id=doc_id, kind="html")

        assert html["served"] is True
        assert html["bytes"] == len(MARKDOWN) + len(b"<!-- html -->")

    def test_an_unknown_kind_narrows_to_nothing_rather_than_to_any_file(self, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        assert preview.head("b", doc_id=doc_id, kind="exe") == GONE


class TestHeadOnReferencedPaths:
    def test_an_allowed_path_is_reported_on(self, preview_root, tmp_path):
        path = tmp_path / "sessions" / "b" / "report.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MARKDOWN)

        head = preview.head("b", path=str(path))

        assert head["served"] is True
        assert head["bytes"] == len(MARKDOWN)

    def test_path_wins_over_the_stored_copy_because_that_is_what_the_route_fetches(
        self, preview_root
    ):
        # `previewUrl` puts `path` on the request whenever the entry has one, so after an update wrote
        # a stored copy beside a referenced file the pane is still reading the referenced file.
        # Judging the other pointer here would report freshness for bytes nobody fetches.
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", b"stored copy, different size\n")
        referenced = preview_root / "b" / "sess1234" / "watched.md"
        referenced.write_bytes(MARKDOWN)

        head = preview.head("b", doc_id=doc_id, kind="markdown", path=str(referenced))

        assert head["bytes"] == len(MARKDOWN)

    def test_moving_a_file_moves_the_token_even_at_the_same_size(self, preview_root, tmp_path):
        path = tmp_path / "sessions" / "b" / "report.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MARKDOWN)
        before = preview.head("b", path=str(path))

        same_size = b"# Other\n\nBody **text**\n"
        assert len(same_size) == len(MARKDOWN), "this test is about the size-blind half of the token"
        path.write_bytes(same_size)

        # mtime_ns alone settles this only where the filesystem has sub-second stamps. Where it does
        # not, the byte count is the only witness and a same-size save is genuinely invisible; that
        # is the documented limit of the pairing, not a failure to fix.
        if Path(path).stat().st_mtime_ns % 1_000_000 == 0:
            pytest.skip("filesystem stamps to the second; a same-size save cannot be seen")
        assert preview.head("b", path=str(path))["token"] != before["token"]


class TestHeadIsNotAnOracle:
    def test_a_path_outside_the_roots_is_the_same_answer_as_nothing(self, preview_root, outside):
        assert preview.head("b", path=str(outside)) == GONE

    def test_another_profiles_document_is_the_same_answer_as_nothing(self, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("r", doc_id, "markdown", MARKDOWN)

        assert preview.head("b", doc_id=doc_id, kind="markdown") == GONE

    def test_an_unknown_profile_is_the_same_answer_as_nothing(self, preview_root):
        doc_id = preview.new_doc_id()

        assert preview.head("nobody", doc_id=doc_id, kind="markdown") == GONE

    def test_no_identifier_is_the_same_answer_as_nothing(self, preview_root):
        assert preview.head("b") == GONE
        assert preview.head("b", doc_id="not-hex", kind="markdown") == GONE

    def test_the_answer_is_always_the_same_shape_and_never_names_a_file(self, preview_root, outside):
        found = preview.head("b", path=str(preview_root))
        missing = preview.head("b", path=str(outside))

        assert set(found) == set(missing) == {"served", "bytes", "mtime", "token"}


class TestHeadForEntry:
    def test_it_follows_the_entry_pointers_the_same_way_the_pane_does(self, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        by_entry = preview.head_for_entry("b", {"docId": doc_id, "kind": "markdown"})
        by_args = preview.head("b", doc_id=doc_id, kind="markdown")

        assert by_entry == by_args

    def test_it_survives_junk_without_raising(self, preview_root):
        assert preview.head_for_entry("b", None) == GONE
        assert preview.head_for_entry("b", {}) == GONE
        assert preview.head_for_entry("b", {"docId": None, "kind": 42, "path": 7}) == GONE


class TestFreshRoute:
    def test_a_stored_document_answers_with_its_token(self, ben, preview_root):
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        response = ben.get(f"/api/preview/fresh", params={"doc": doc_id, "kind": "markdown"})

        assert response.status_code == 200
        body = response.json()
        assert body["served"] is True
        assert body["bytes"] == len(MARKDOWN)
        assert response.headers["cache-control"] == "no-store"

    def test_a_missing_document_is_an_answer_and_not_an_error(self, ben, preview_root):
        # 404 would be a lie here: the pane is asking "is what I show still current", and "no" is a
        # real answer, delivered the same way for every kind of absence.
        response = ben.get("/api/preview/fresh", params={"doc": preview.new_doc_id(), "kind": "pdf"})

        assert response.status_code == 200
        assert response.json() == GONE

    def test_an_outside_path_is_indistinguishable_from_a_missing_document(
        self, ben, preview_root, outside
    ):
        missing = ben.get("/api/preview/fresh", params={"doc": preview.new_doc_id(), "kind": "markdown"})
        refused = ben.get("/api/preview/fresh", params={"path": str(outside)})

        assert refused.status_code == missing.status_code == 200
        assert refused.json() == missing.json()

    def test_a_malformed_request_is_an_error_because_absence_is_already_the_answer(
        self, ben, preview_root
    ):
        assert ben.get("/api/preview/fresh").status_code == 400
        assert ben.get("/api/preview/fresh", params={"kind": "exe", "doc": "a" * 32}).status_code == 400

    def test_the_route_needs_the_same_login_as_the_route_it_shadows(self, client, preview_root):
        response = client.get("/api/preview/fresh", params={"doc": preview.new_doc_id()})

        assert response.status_code in (401, 403)

    def test_it_is_routed_ahead_of_the_document_id_route(self, ben, preview_root):
        # `/api/preview/fresh` must not be swallowed as `doc_id="fresh"`: that would answer 404 from
        # the serving route and the pane would read a 404 it cannot distinguish from a dead document.
        response = ben.get("/api/preview/fresh", params={"doc": preview.new_doc_id(), "kind": "md"})

        assert response.status_code == 400, "wrong kind: the request reached freshness, not the file route"
        assert "kind" in response.text.lower()


class TestFreshRouteCost:
    def test_two_hundred_warm_hits_stay_under_a_second_and_a_half(self, ben, preview_root):
        """Bound the cost of a pane that asks once a second about the document it is showing.

        The estimate in the plan is 0.3 to 1.5 ms per hit, and the argument for polling was never CPU
        but wakeups, so this catches an order-of-magnitude regression (a glob, a re-resolution of the
        roots list per request, a thread hop) rather than tuning a fine margin. TestClient adds its own
        per-call overhead, so the number printed here is an upper bound on the in-server cost.
        """
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)
        params = {"doc": doc_id, "kind": "markdown"}
        for _ in range(10):
            assert ben.get("/api/preview/fresh", params=params).status_code == 200

        started = time.perf_counter()
        for _ in range(200):
            assert ben.get("/api/preview/fresh", params=params).status_code == 200
        elapsed = time.perf_counter() - started

        print(f"\npreview fresh: {elapsed / 200 * 1000:.2f} ms per warm request")
        assert elapsed < 1.5, f"200 warm freshness reads took {elapsed:.2f}s"

    def test_a_path_lookup_never_walks_the_store(self, monkeypatch, preview_root, tmp_path):
        """The fast path is a join and a stat. A `**/` glob per second is the regression to catch."""
        calls = []
        real = preview.find_stored_document

        def spy(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(preview, "find_stored_document", spy)
        path = tmp_path / "sessions" / "b" / "report.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MARKDOWN)

        preview.head("b", path=str(path))

        assert calls == []

    def test_a_stored_lookup_does_use_the_store_resolver(self, monkeypatch, preview_root):
        # The other half: the fast path must not accidentally become "never look anything up", which
        # would report every stored document as missing and read as "nothing ever changes".
        calls = []
        real = preview.find_stored_document

        def spy(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(preview, "find_stored_document", spy)
        doc_id = preview.new_doc_id()
        store_doc("b", doc_id, "markdown", MARKDOWN)

        assert preview.head("b", doc_id=doc_id, kind="markdown")["served"] is True
        assert len(calls) == 1
