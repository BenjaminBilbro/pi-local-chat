"""SEC-04: what a wrong password costs, and what it costs to keep trying.

scrypt at N=2**14 is roughly 10 ms per verification, which slows an offline crack but is not a rate
limit: from the tailnet, or through the quick tunnel, a handful of parallel connections gets an
attacker hundreds of guesses a second against two profiles. A profile now gets a bounded number of
wrong answers per window, and while it is over that budget the password is not verified at all.
"""

import time

import pytest
from fastapi.testclient import TestClient

from pi_chat.auth import LOGIN_MAX_FAILURES, AuthManager, hash_password
from pi_chat.auth import verify_password as real_verify


@pytest.fixture
def auth():
    manager = AuthManager(password_hashes={"b": hash_password("test123"), "r": hash_password("profile-r-pw")})
    assert LOGIN_MAX_FAILURES == manager.max_failed_logins, "the test below follows the shipped policy"
    return manager


class TestFailedAttemptWindow:
    def test_wrong_answers_below_the_budget_stay_retryable(self, auth):
        for attempt in range(LOGIN_MAX_FAILURES - 1):
            assert auth.login("b", "wrong") is None
            assert auth.retry_after("b") == 0.0, f"attempt {attempt + 1} should not be locked out"

    def test_the_budget_is_spent_on_answers_not_on_connections(self, auth):
        for _ in range(LOGIN_MAX_FAILURES):
            assert auth.login("b", "wrong") is None

        assert auth.retry_after("b") > 0.0
        assert auth.retry_after("r") == 0.0, "locking one profile must not lock the other"

    def test_a_locked_profile_is_not_verified_even_with_the_right_password(self, auth, monkeypatch):
        """The verification is the only thing a brute-force run buys per request, so refusing to run
        it is the whole point. A lockout that still hashes is a lockout that still costs."""
        calls = []

        def counting(password, encoded_hash):
            calls.append(password)
            return real_verify(password, encoded_hash)

        monkeypatch.setattr("pi_chat.auth.verify_password", counting)
        for _ in range(LOGIN_MAX_FAILURES):
            auth.login("b", "wrong")
        calls.clear()

        assert auth.login("b", "test123") is None, "a locked profile does not get in"
        assert calls == [], "the lockout refused before spending the scrypt budget"

    def test_a_correct_answer_clears_the_history(self, auth):
        for _ in range(LOGIN_MAX_FAILURES - 1):
            auth.login("b", "wrong")

        assert auth.login("b", "test123") is not None
        assert auth.retry_after("b") == 0.0

        # A fresh set of mistakes should cost the full budget again, not the remainder.
        for _ in range(LOGIN_MAX_FAILURES - 1):
            assert auth.login("b", "wrong") is None
        assert auth.retry_after("b") == 0.0

    def test_the_window_slides_and_the_profile_is_free_again(self, monkeypatch):
        manager = AuthManager(
            password_hashes={"b": hash_password("test123")},
            failed_login_window=50.0,
        )
        clock = [1_000.0]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])

        for _ in range(manager.max_failed_logins):
            manager.login("b", "wrong")
        assert manager.retry_after("b") == pytest.approx(50.0)

        clock[0] += 25.0
        assert manager.retry_after("b") == pytest.approx(25.0), "it counts down, it does not reset"

        clock[0] += 26.0
        assert manager.retry_after("b") == 0.0
        assert manager._failed_logins == {}, "an aged-out profile stops costing memory"


class TestLockoutLog:
    def test_engaging_the_lockout_says_so_once(self, auth, caplog):
        with caplog.at_level("WARNING", logger="pi-chat"):
            for _ in range(LOGIN_MAX_FAILURES):
                auth.login("b", "wrong")

        warnings = [r for r in caplog.records if "failed logins" in r.getMessage()]
        assert len(warnings) == 1, "a fumble must not fill the log, but a campaign must be visible"
        assert "profile 'b'" in warnings[0].getMessage().lower() or "'b'" in warnings[0].getMessage()

    def test_trying_again_while_locked_does_not_log_again(self, auth, caplog):
        with caplog.at_level("WARNING", logger="pi-chat"):
            for _ in range(LOGIN_MAX_FAILURES + 3):
                auth.login("b", "wrong")

        assert len([r for r in caplog.records if "failed logins" in r.getMessage()]) == 1


@pytest.fixture
def client():
    manager = AuthManager(password_hashes={"b": hash_password("test123"), "r": hash_password("profile-r-pw")})
    from pi_chat.app import create_app

    return TestClient(create_app(auth_manager=manager))


class TestLoginRoute:
    def test_a_wrong_password_is_401_and_a_locked_profile_is_429(self, client):
        for _ in range(LOGIN_MAX_FAILURES):
            response = client.post("/api/login", json={"account": "b", "password": "wrong"})
            assert response.status_code == 401

        locked = client.post("/api/login", json={"account": "b", "password": "wrong"})

        assert locked.status_code == 429
        assert "Too many failed attempts" in locked.json()["detail"]
        retry_after = locked.headers.get("Retry-After")
        assert retry_after is not None and int(retry_after) > 0, (
            "without this the browser cannot tell the user how long to wait"
        )

    def test_the_right_password_is_also_refused_while_locked(self, client):
        """Otherwise 429 becomes an oracle for "is this profile currently guessable", and the
        correct password still gets one verification per attempt."""
        for _ in range(LOGIN_MAX_FAILURES):
            client.post("/api/login", json={"account": "b", "password": "wrong"})

        response = client.post("/api/login", json={"account": "b", "password": "test123"})

        assert response.status_code == 429

    def test_locking_one_profile_leaves_the_other_able_to_log_in(self, client):
        for _ in range(LOGIN_MAX_FAILURES):
            client.post("/api/login", json={"account": "b", "password": "wrong"})

        assert client.post("/api/login", json={"account": "r", "password": "profile-r-pw"}).status_code == 200
