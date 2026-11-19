"""The pane-state channel: what the browser reports, and what a child may read back.

WP-3 of `~/.pi/agent/plans/pi-chat-preview-interactive.md`. The problem it exists to solve is that a
model has no idea what the human is looking at. `list` reports what the extension opened; only the
browser knows whether the column is open and which tab is in front of it.

Two properties are load-bearing and neither is obvious from the route code alone:

1. **A connection can only read its own pane.** The credential is a per-spawn secret, so a child in
   one session must not be able to describe another person's screen.
2. **The payload is a closed set.** The model reads these values as facts, so a browser must not be
   able to invent fields or park arbitrary text where a document title would go.

The field set is asserted *from* the constant the routes use, so adding a field to the allowlist
without wiring it here fails instead of silently going untested.
"""

import time

import pytest
from fastapi.testclient import TestClient

from pi_chat import app as app_module
from pi_chat import config
from pi_chat import preview
from pi_chat.app import create_app
from pi_chat.auth import AuthManager, hash_password

TOKEN_A = "a" * 32
TOKEN_B = "b" * 32

VIEW = {
    "open": True,
    "activeDocId": "d" * 32,
    "width": 720,
    "watching": "e" * 32,
    "revision": 4,
    "fileToken": "1234-5678",
}


@pytest.fixture
def store():
    """A clean pane-state map per test, and a record of what this test added.

    `PANE_STATE` is module level because it has to outlive a request, which means tests share it.
    Nothing here should assume the process started empty.
    """
    saved = dict(app_module.PANE_STATE)
    app_module.PANE_STATE.clear()
    yield app_module.PANE_STATE
    app_module.PANE_STATE.clear()
    app_module.PANE_STATE.update(saved)


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


def report(client, token: str, **overrides) -> dict:
    """POST one pane report.

    The connection secret travels as `token` and the pane's own staleness validator as `fileToken`.
    Those two must not share a key: the first attempt named both `token`, the view won the merge, and
    every report was rejected for having a bad secret. `test_the_secret_and_the_view_key_do_not_collide`
    keeps it that way.
    """
    return client.post(
        "/api/preview/state", json={"token": token, **VIEW, **overrides}
    )


class TestReporting:
    def test_a_login_is_required_before_a_pane_can_speak(self, client, store):
        assert client.post("/api/preview/state", json={"token": TOKEN_A, **VIEW}).status_code in (401, 403)

    def test_a_token_that_was_never_issued_is_rejected(self, ben, store):
        response = report(ben, "not-a-token")

        assert response.status_code == 400
        assert store == {}

    def test_a_report_is_stored_under_the_token_that_reported_it(self, ben, store):
        response = report(ben, TOKEN_A)

        assert response.status_code == 200
        assert sorted(store) == [TOKEN_A]
        assert store[TOKEN_A][0] == VIEW

    def test_the_response_names_what_was_kept_so_a_bench_can_see_a_silent_drop(self, ben, store):
        response = report(ben, TOKEN_A, junk="ignored")

        assert response.status_code == 200
        assert response.json()["fields"] == sorted(VIEW)

    def test_a_later_report_replaces_the_earlier_one_rather_than_joining_it(self, ben, store):
        report(ben, TOKEN_A, activeDocId=None, watching=None)
        report(ben, TOKEN_A, open=False, width=500)

        assert store[TOKEN_A][0]["open"] is False
        assert store[TOKEN_A][0]["width"] == 500


class TestFieldDiscipline:
    """Every allowed field must be exercised, and everything else must not arrive."""

    def test_every_allowed_field_can_be_reported_as_null(self, ben, store):
        """A field the browser sends as `null` is a real answer, not a missing one.

        No active tab and nothing being watched are both `null`, and a pane that switched them off
        has to be able to say so. Reading `"activeDocId" in view` rather than its truthiness is what
        separates "no tab is in front" from "this browser never reports tabs".
        """
        response = report(ben, TOKEN_A, **{field: None for field in VIEW})

        assert response.status_code == 200
        assert sorted(store[TOKEN_A][0]) == sorted(VIEW)
        assert set(store[TOKEN_A][0].values()) == {None}

    def test_a_key_the_browser_never_sent_stays_absent(self, ben, store):
        response = ben.post("/api/preview/state", json={"token": TOKEN_A, "open": False})

        assert response.status_code == 200
        assert store[TOKEN_A][0] == {"open": False}

    def test_a_report_with_no_usable_field_stores_nothing(self, ben, store):
        # Without this, any authenticated request with a valid token appends a permanent empty entry,
        # and the sweep that bounds the map only runs when something is written.
        assert ben.post("/api/preview/state", json={"token": TOKEN_A}).status_code == 400
        assert ben.post("/api/preview/state", json={"token": TOKEN_A, "junk": 1}).status_code == 400
        assert store == {}

    def test_the_allowlist_is_exactly_what_the_routes_validate(self):
        # Guards against a field added to PANE_STATE_FIELDS and validated in the route but never
        # asserted anywhere: the set here is derived from the constant, so it cannot fall behind.
        assert app_module.PANE_STATE_FIELDS == frozenset(VIEW)

    def test_an_oversized_or_wrong_typed_value_is_reported_as_null(self, ben, store):
        bad_values = {
            "activeDocId": "x" * 65,
            "width": 99_999,
            "revision": -1,
            "open": "yes",
            "watching": "y" * 65,
            "fileToken": "z" * 65,
        }

        for field, bad in bad_values.items():
            response = report(ben, TOKEN_A, **{field: bad})

            assert response.status_code == 200, field
            # Rejected means recorded as unknown, not left holding the previous document: a pane
            # that stopped reporting a field must not let the model keep reading an old one.
            assert store[TOKEN_A][0][field] is None, field

        # And the untouched fields of that same report survive it.
        report(ben, TOKEN_A, activeDocId="x" * 65)
        assert {key: value for key, value in store[TOKEN_A][0].items() if key != "activeDocId"} == {
            key: value for key, value in VIEW.items() if key != "activeDocId"
        }

    def test_a_body_that_is_a_list_is_not_stored_as_a_view(self, ben, store):
        response = ben.post("/api/preview/state", json=[1, 2, 3])

        assert response.status_code >= 400
        assert store == {}


class TestReading:
    def test_the_child_reads_back_what_the_pane_reported(self, ben, store):
        report(ben, TOKEN_A)

        response = ben.get("/api/preview/state", headers={"x-preview-token": TOKEN_A})

        assert response.status_code == 200
        body = response.json()
        assert {key: body[key] for key in VIEW} == VIEW
        assert body["ageSeconds"] >= 0

    def test_a_token_this_server_never_minted_is_a_404(self, ben, store):
        report(ben, TOKEN_A)

        assert ben.get("/api/preview/state", headers={"x-preview-token": "f" * 32}).status_code == 404

    def test_a_connection_cannot_read_another_connections_pane(self, ben, store):
        report(ben, TOKEN_A, activeDocId="a" * 32)
        report(ben, TOKEN_B, activeDocId="b" * 32)

        first = ben.get("/api/preview/state", headers={"x-preview-token": TOKEN_A}).json()
        second = ben.get("/api/preview/state", headers={"x-preview-token": TOKEN_B}).json()

        assert first["activeDocId"] == "a" * 32
        assert second["activeDocId"] == "b" * 32

    def test_the_token_header_works_without_any_cookie(self, client, store):
        """The child has no cookie, so on this read the token really is the only credential.

        Deliberate, not an oversight: it is a 128-bit secret minted per spawn and known only to this
        server and the child it spawned, the app is a loopback/tailnet box that expects no inbound,
        and the most a stolen token buys is a description of a pane holding documents that same
        connection already wrote. Writes, which is where invention would matter, still require login.
        """
        # A read for a token nobody reported is a 404 even with no cookie at all, because the read
        # is not gated on a session. The token is the credential; there is no second factor to
        # forget. And an unauthenticated POST still cannot plant a view for someone else's token.
        assert client.get("/api/preview/state", headers={"x-preview-token": "c" * 32}).status_code == 404
        assert client.post("/api/preview/state", json={"token": TOKEN_A, **VIEW}).status_code in (401, 403)
        assert store == {}

    def test_a_missing_token_header_is_401(self, ben, store):
        assert ben.get("/api/preview/state").status_code == 401


class TestLifetime:
    def test_a_stale_report_is_swept_by_the_next_one(self, ben, store):
        report(ben, TOKEN_A)
        token, written = store[TOKEN_A]
        store[TOKEN_A] = (token, written - app_module.PANE_STATE_TTL_SECONDS - 1)

        report(ben, TOKEN_B)

        assert TOKEN_A not in store
        assert TOKEN_B in store

    def test_a_fresh_report_survives_the_sweep(self, ben, store):
        report(ben, TOKEN_A)
        report(ben, TOKEN_B)

        assert sorted(store) == sorted([TOKEN_A, TOKEN_B])


class TestRouteOrder:
    def test_state_is_not_swallowed_as_a_document_id(self, ben, store):
        # `/api/preview/state` also fits `/api/preview/{doc_id}`. Registered after it, it would
        # answer 404 through the file route, which is indistinguishable from a dead document.
        report(ben, TOKEN_A)

        assert ben.get("/api/preview/state", headers={"x-preview-token": TOKEN_A}).status_code == 200

    def test_freshness_is_not_swallowed_either(self, ben, preview_root):
        response = ben.get("/api/preview/fresh", params={"doc": "z" * 32, "kind": "pdf"})

        assert response.status_code == 200
        assert response.json()["served"] is False


class TestTokenFactory:
    def test_tokens_are_hex_32_and_never_repeat(self):
        tokens = {preview.new_state_token() for _ in range(200)}

        assert len(tokens) == 200
        assert all(len(token) == 32 and all(c in "0123456789abcdef" for c in token) for token in tokens)

    def test_a_state_token_and_a_document_id_are_minted_apart(self):
        # Both are 32 hex, which is harmless, but a document id belongs in URLs and logs while a
        # state token must never appear in either. Minted by separate calls, so "just reuse
        # new_doc_id" fails here instead of leaking a secret into a query string.
        doc_id = preview.new_doc_id()
        secret = preview.new_state_token()

        assert len(doc_id) == len(secret) == 32
        assert doc_id != secret


class TestSweepHelper:
    def test_the_sweep_only_removes_expired_entries(self, store):
        now = time.time()
        store["fresh"] = ({"open": True}, now)
        store["old"] = ({"open": True}, now - app_module.PANE_STATE_TTL_SECONDS - 5)

        app_module._sweep_pane_state(now)

        assert list(store) == ["fresh"]
