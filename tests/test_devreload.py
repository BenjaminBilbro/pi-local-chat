"""The hot-reload affordances: who is allowed to ask for a new process, and what a stamp has to reveal.

`/api/version` is read-only and mostly harmless, so its test is about whether it is *useful*: does an edit on
disk move the number the page compares, and does it say which file moved. `/api/dev/restart` is the opposite
kind of test. It is the one route in this app that ends the process, so it has to be absent when the server is
not in dev mode, unreachable from anywhere but loopback when it is, and the supervisor that brings the process
back has to restart on a request and stop on a crash. A supervisor that respawns on any exit is a machine that
hides a syntax error behind a loop.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import pi_chat.app as app_module
import pi_chat.auth as auth_module
import pi_chat.devtools as devtools
from pi_chat.app import create_app
from pi_chat.auth import AuthManager, hash_password


@pytest.fixture
def app(tmp_path, monkeypatch):
    auth = AuthManager()
    auth.password_hashes["b"] = hash_password("test123")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(app_module, "DEV_MODE", False)
    (tmp_path / "sessions" / "b").mkdir(parents=True)
    return create_app(auth_manager=auth)


@pytest.fixture
def client(app):
    client = TestClient(app)
    assert client.post("/api/login", json={"account": "b", "password": "test123"}).status_code == 200
    return client


# ── /api/version ─────────────────────────────────────────────────────


def test_version_needs_a_login(client):
    anonymous = TestClient(create_app(auth_manager=_auth()))
    assert anonymous.get("/api/version").status_code == 401


def test_version_names_the_newest_file_and_moves_when_it_changes(client, monkeypatch, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.js").write_text("export const a = 1;\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "STATIC_DIR", static)

    before = client.get("/api/version").json()
    # The name, not the absolute path: the banner has a few centimeters and a person who just edited the file
    # does not need their own directory repeated to them. Absolute paths also have no business in a response.
    assert before["frontend"]["newest"].endswith("app.js")
    assert before["dev"] is False
    assert before["bootId"] and before["pid"]

    (static / "app.js").write_text("export const a = 2;\n", encoding="utf-8")
    after = client.get("/api/version").json()
    assert after["frontend"]["stamp"] != before["frontend"]["stamp"]
    # The boot identity is the process, not the code: a stamp moving must not look like a restart.
    assert after["bootId"] == before["bootId"]


def test_a_file_in_the_repo_is_named_relative_to_the_repo():
    """`pi_chat/audit.py`, not `C:\\Users\\...\\pi_chat\\audit.py`.

    The banner names the file, and the only form worth naming is the one in an editor tab. Outside the repo
    there is no sensible prefix, which is why the fallback is a bare name rather than a path.
    """
    backend = devtools.backend_stamp()
    assert backend["newest"], "the real tree always has a newest file"
    assert not backend["newest"].startswith("C:"), backend["newest"]


def test_boot_backend_is_what_the_process_imported_not_what_disk_holds(client, monkeypatch):
    """The one fact the page cannot derive for itself.

    It can see the files on disk; it cannot see which bytes the server was started from. Without
    `bootBackend` a front-end reload cleared the restart prompt while the old Python kept serving, which is
    the banner saying "all caught up" about a process that is not.
    """
    live = client.get("/api/version").json()["backend"]["stamp"]
    monkeypatch.setattr(devtools, "BOOT_BACKEND", "0-0-0")
    reported = client.get("/api/version").json()
    assert reported["bootBackend"] == "0-0-0"
    assert reported["backend"]["stamp"] == live
    assert reported["bootBackend"] != reported["backend"]["stamp"], "stale, and the payload says so"


def test_backend_stamp_covers_the_entry_point(client):
    backend = client.get("/api/version").json()["backend"]
    assert backend["files"] > 10, "the stamp has to cover pi_chat/, not just the file you happened to edit"


# ── /api/dev/restart ─────────────────────────────────────────────────


def test_the_restart_route_does_not_exist_outside_dev_mode(client):
    """404 because the route is never registered, not 403 because it was refused.

    A production run should not advertise that a restart endpoint exists and is merely guarded.
    """
    assert client.post("/api/dev/restart").status_code == 404


def test_restart_refuses_a_non_loopback_peer(monkeypatch):
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    monkeypatch.setattr(devtools, "_server", object())
    try:
        auth = _auth()
        client = TestClient(create_app(auth_manager=auth))
        assert client.post("/api/login", json={"account": "b", "password": "test123"}).status_code == 200
        response = client.post("/api/dev/restart")
        assert response.status_code == 404, "the default test peer is not loopback"
    finally:
        monkeypatch.setattr(devtools, "_server", None)


def test_restart_asks_the_server_to_stop_and_tells_the_parent(monkeypatch):
    class FakeServer:
        should_exit = False

    server = FakeServer()
    monkeypatch.setattr(devtools, "_server", server)
    monkeypatch.setattr(devtools, "_restart_requested", False)

    assert devtools.request_restart() is True
    assert server.should_exit is True, "an exit that skips uvicorn's shutdown leaves every pi child running"
    assert devtools.take_restart_request() is True
    assert devtools.take_restart_request() is False, "a restart request is consumed once"


def test_restart_without_a_server_reports_rather_than_lies(monkeypatch):
    monkeypatch.setattr(devtools, "_server", None)
    assert devtools.request_restart() is False


# ── a restart must not become a logout ───────────────────────


def _manager(tmp_path, monkeypatch, dev=True, **kwargs):
    monkeypatch.setattr(auth_module, "DEV_MODE", dev)
    monkeypatch.setattr(auth_module, "DEV_SESSION_FILE", tmp_path / ".dev-sessions.json")
    return AuthManager(password_hashes={"b": hash_password("test123")}, **kwargs)


def test_a_restart_keeps_you_logged_in(tmp_path, monkeypatch):
    """The whole reason this exists.

    Sessions are in memory by design, and the button restarts the process, so without this the click that
    applies a change also throws the person back at the login screen. Found by running the restart for real
    rather than by reading it: the respawn worked and the browser was simply signed out.
    """
    first = _manager(tmp_path, monkeypatch)
    token = first.login("b", "test123")
    assert token and first.account_for_token(token) == "b"

    second = _manager(tmp_path, monkeypatch)
    assert second.account_for_token(token) == "b"


def test_production_writes_no_token_file_at_all(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, dev=False)
    assert manager.login("b", "test123")
    assert not (tmp_path / ".dev-sessions.json").exists(), (
        "a file of live tokens must be a dev-mode artifact, not something a server always leaves behind"
    )


def test_a_logged_out_token_does_not_come_back(tmp_path, monkeypatch):
    first = _manager(tmp_path, monkeypatch)
    token = first.login("b", "test123")
    first.logout(token)
    second = _manager(tmp_path, monkeypatch)
    assert second.account_for_token(token) is None


def test_an_expired_or_ghost_session_in_the_file_is_not_restored(tmp_path, monkeypatch):
    file = tmp_path / ".dev-sessions.json"
    file.write_text(json.dumps({"sessions": [
        {"token": "old", "account": "b", "expiresAt": 1_000_000_000},
        {"token": "ghost", "account": "whoever", "expiresAt": 9e12},
        {"token": "junk"},
        "not even a dict",
    ]}), encoding="utf-8")

    manager = _manager(tmp_path, monkeypatch)
    assert manager.account_for_token("old") is None, "the file is not a grant of access past its own expiry"
    assert manager.account_for_token("ghost") is None, "a profile with no password hash cannot be logged in"
    assert manager.account_for_token("junk") is None


def test_a_half_written_file_costs_a_login_and_not_the_server(tmp_path, monkeypatch):
    (tmp_path / ".dev-sessions.json").write_text('{"sessions": [{"tok', encoding="utf-8")
    manager = _manager(tmp_path, monkeypatch)
    assert manager.login("b", "test123"), "the constructor survived it, and login still works"


# ── the supervisor loop ──────────────────────────────────────────────


class _FakePopen:
    """Successive child exit codes, so the loop can be tested without starting interpreters."""

    calls = []
    codes = []

    def __init__(self, command, cwd=None, env=None):
        type(self).calls.append({"command": command, "cwd": str(cwd), "env": env})

    def wait(self):
        return type(self).codes.pop(0)


def test_supervisor_restarts_on_the_request_code_and_stops_on_anything_else(monkeypatch, tmp_path):
    _FakePopen.calls = []
    _FakePopen.codes = [devtools.RESTART_EXIT_CODE, 3]
    monkeypatch.setattr(devtools.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(devtools.time, "sleep", lambda *_: None)

    log = _RecordingLog()
    assert devtools.supervise(log) == 3
    assert len(_FakePopen.calls) == 2, "one restart, then the crash ends the run"


def test_supervisor_hands_the_child_the_marker_that_keeps_it_from_nesting(monkeypatch):
    _FakePopen.calls = []
    _FakePopen.codes = [0]
    monkeypatch.setattr(devtools.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(devtools.time, "sleep", lambda *_: None)
    monkeypatch.setenv("PI_CHAT_AUDIT", "1")

    devtools.supervise(_RecordingLog())

    env = _FakePopen.calls[0]["env"]
    assert env[devtools.CHILD_ENV] == "1", "a child without the marker spawns a supervisor of its own"
    assert env["PI_CHAT_AUDIT"] == "1", "the child must inherit the run's own environment"
    assert str(devtools.ROOT) in _FakePopen.calls[0]["command"][-1]


def test_child_marker_is_only_the_env_var(monkeypatch):
    monkeypatch.delenv(devtools.CHILD_ENV, raising=False)
    assert devtools.is_child() is False
    monkeypatch.setenv(devtools.CHILD_ENV, "1")
    assert devtools.is_child() is True


def test_a_missing_static_tree_is_a_zero_and_not_a_crash(tmp_path):
    stamp = devtools.frontend_stamp(tmp_path / "nope")
    assert stamp == {"stamp": "0-0-0", "files": 0, "newest": ""}


class _RecordingLog:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


def _auth() -> AuthManager:
    auth = AuthManager()
    auth.password_hashes["b"] = hash_password("test123")
    return auth


def test_snapshot_reports_restartable_only_when_a_server_can_be_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(devtools, "_server", None)
    offline = devtools.snapshot(tmp_path, True)
    assert offline["restartable"] is False, "dev mode without a supervisor should not offer a dead button"

    monkeypatch.setattr(devtools, "_server", object())
    online = devtools.snapshot(tmp_path, True)
    assert online["restartable"] is True
    assert json.dumps(online), "the payload has to be serializable, or the route 500s on every poll"
