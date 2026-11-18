"""S3: deleting a session deletes the literal file, and only a session that is yours.

Ben asked for this in one line: "I need to be able to delete sessions. Deleted sessions should delete the
literal pi session jsonl." There is no archive and no soft flag behind that, which is what makes the checks
worth having: a half-delete leaves the thing he asked to be gone sitting on disk, and an over-eager one
unlinks a file somebody named in a request body.

So the two things under test are containment and ownership, in that order. Containment first because
ownership is read *out of the file*: `session_belongs_to_account` trusts the `cwd` header inside the JSONL,
so a file anywhere on this machine that claims an account's work directory would pass it, and delete does not
get to ask the file's permission before unlinking.

`pi_sessions_dir` is patched rather than the real `~/.pi/agent/sessions`, so no test can touch a session this
house actually had. The WebSocket's `load_session` path calls the same `resolve_session_path` helper, and the
route-level tests here are the coverage for it: driving the full child handshake to re-prove one shared guard
would cost more than it shows.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pi_chat.sessions as sessions
from pi_chat.auth import AuthManager, hash_password
from pi_chat.config import PROJECT_ROOT

PASSWORDS = {"b": "profile-b-pw", "r": "profile-r-pw"}


def session_lines(cwd: Path) -> list[str]:
    """A session file small enough to read in a test and shaped well enough to be listed."""
    return [
        json.dumps({
            "type": "session",
            "id": "01TEST",
            "cwd": str(cwd),
            "timestamp": "2026-11-17T18:00:00.000Z",
        }),
        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}}),
    ]


def write_session(path: Path, cwd: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(session_lines(cwd)) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A sessions directory of our own, with nothing of Ben's anywhere near it."""
    root = tmp_path / "pi-sessions"
    root.mkdir()
    monkeypatch.setattr(sessions, "pi_sessions_dir", lambda: root)
    sessions.clear_summary_cache()
    yield root
    sessions.clear_summary_cache()


@pytest.fixture()
def client(store):
    manager = AuthManager(password_hashes={label: hash_password(pw) for label, pw in PASSWORDS.items()})
    from pi_chat.app import create_app

    return TestClient(create_app(auth_manager=manager))


@pytest.fixture()
def signed_in(client):
    response = client.post("/api/login", json={"account": "b", "password": PASSWORDS["b"]})
    assert response.status_code == 200, response.text
    return client


def work_dir(account: str) -> Path:
    return (PROJECT_ROOT / "sessions" / account).resolve()


def delete(client, path: Path):
    return client.request("DELETE", "/api/sessions", json={"path": str(path)})


def test_deleting_a_session_unlinks_the_file(signed_in, store):
    session = write_session(store / "01TEST.jsonl", work_dir("b"))

    response = delete(signed_in, session)

    assert response.status_code == 200, response.text
    assert response.json().get("deleted") is True
    assert not session.exists(), "a deleted session has to be gone from disk, not marked"
    listed = [row["path"] for row in response.json()["sessions"]]
    assert str(session) not in listed, "the refreshed list still offers the session the reader deleted"


def test_a_second_delete_of_the_same_file_is_not_found(signed_in, store):
    session = write_session(store / "01TEST.jsonl", work_dir("b"))

    assert delete(signed_in, session).status_code == 200

    second = delete(signed_in, session)

    assert second.status_code == 404, second.text
    assert second.json()["detail"] == "Session not found", "a missing file and a refused one read the same"


def test_a_file_outside_pi_sessions_is_refused_and_survives(signed_in, store, tmp_path):
    """Ownership alone would let this through: the header claims profile b's work directory."""
    elsewhere = write_session(tmp_path / "elsewhere" / "01TEST.jsonl", work_dir("b"))

    response = delete(signed_in, elsewhere)

    assert response.status_code == 404, response.text
    assert elsewhere.exists(), "this is the file the guard exists for"


def test_dot_dot_cannot_climb_out_of_the_store(signed_in, store, tmp_path):
    outside = write_session(tmp_path / "outside.jsonl", work_dir("b"))
    sneaky = store / "sub" / ".." / ".." / "outside.jsonl"

    response = delete(signed_in, sneaky)

    assert response.status_code == 404, response.text
    assert outside.exists(), "resolved containment has to happen before the unlink, not in the string"


def test_another_profiles_session_is_not_deleted(signed_in, store):
    theirs = write_session(store / "01ROXY.jsonl", work_dir("r"))

    response = delete(signed_in, theirs)

    assert response.status_code == 403, response.text
    assert theirs.exists(), "the header says this one belongs to profile r"


def test_only_jsonl_is_a_session(signed_in, store):
    note = store / "notes.md"
    note.write_text("# hello\n", encoding="utf-8")

    response = delete(signed_in, note)

    assert response.status_code == 404, response.text
    assert note.exists()


def test_a_body_that_is_not_json_or_has_no_path_is_handled(signed_in, store):
    anonymous = signed_in.request("DELETE", "/api/sessions", content="not json",
                                 headers={"content-type": "application/json"})
    missing = signed_in.request("DELETE", "/api/sessions", json={"session": "01TEST"})

    assert anonymous.status_code == 400, anonymous.text
    assert missing.status_code == 404, missing.text


def test_anonymous_requests_are_refused_before_any_path_is_read(store):
    manager = AuthManager(password_hashes={label: hash_password(pw) for label, pw in PASSWORDS.items()})
    from pi_chat.app import create_app

    anonymous = TestClient(create_app(auth_manager=manager))

    assert anonymous.request("DELETE", "/api/sessions", json={"path": "x"}).status_code == 401


def test_the_cached_summary_does_not_resurrect_the_row(signed_in, store):
    session = write_session(store / "01TEST.jsonl", work_dir("b"))
    summaries = sessions.list_sessions(project_root=PROJECT_ROOT, account_label="b")
    assert [row["path"] for row in summaries] == [str(session)], "the fixture should be listable first"

    assert delete(signed_in, session).status_code == 200

    cached_paths = [key for key, value in sessions._SUMMARY_CACHE.items() if value[2]]
    assert not [key for key in cached_paths if Path(key).name == session.name], (
        f"the deleted file is still cached under: {cached_paths}"
    )


def test_the_derived_directory_goes_when_it_is_left_empty(signed_in, store):
    """pi groups a work directory's sessions under a `--slug--` folder; an empty one is noise."""
    derived = sessions._derived_session_dir(work_dir("b"), store)
    session = write_session(derived / "01TEST.jsonl", work_dir("b"))

    assert delete(signed_in, session).status_code == 200

    assert not session.exists()
    assert not derived.exists(), "the folder held only this session, so nothing is left to keep"


def test_a_sibling_session_keeps_the_directory(signed_in, store):
    derived = sessions._derived_session_dir(work_dir("b"), store)
    first = write_session(derived / "01AAA.jsonl", work_dir("b"))
    write_session(derived / "01BBB.jsonl", work_dir("b"))

    assert delete(signed_in, first).status_code == 200

    assert derived.is_dir(), "rmdir is only allowed to remove an empty directory, and this one is not"


def test_preview_refuses_a_path_outside_the_store(signed_in, store, tmp_path):
    """The same containment on the route that has been reading paths from the browser all along."""
    elsewhere = write_session(tmp_path / "elsewhere" / "01TEST.jsonl", work_dir("b"))

    response = signed_in.get("/api/sessions/preview", params={"session_path": str(elsewhere)})

    assert response.status_code == 404, response.text


def test_preview_still_works_for_a_session_in_the_store(signed_in, store):
    session = write_session(store / "01TEST.jsonl", work_dir("b"))

    response = signed_in.get("/api/sessions/preview", params={"session_path": str(session)})

    assert response.status_code == 200, response.text
    assert response.json()["messageCount"] == 1, "the guard must not break the path the sidebar depends on"
