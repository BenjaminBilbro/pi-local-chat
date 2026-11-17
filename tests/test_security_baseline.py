"""Baseline security expectations for the web surface.

These exist because the audit of 2026-11-09 found two S1s that no test would have caught:
assistant HTML reaching innerHTML unsanitised, and unsalted SHA-256 profile hashes accepted as
a default. The render side of the first is covered by `npm test` (compare_render's sanitisation
check); what is covered here is the server half: the headers, and a credential store that fails
closed instead of falling back.
"""

import importlib
import re

import pytest
from fastapi.testclient import TestClient

from pi_chat import auth as auth_module
from pi_chat.auth import AuthManager, hash_password, require_configured_accounts, verify_password

LEGACY_SHA256_OF_TEST = "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"  # sha256("password")


class TestCredentialFormats:
    def test_scrypt_hash_round_trips(self):
        encoded = hash_password("correct horse")
        assert encoded.startswith("scrypt$")
        assert verify_password("correct horse", encoded) is True
        assert verify_password("wrong horse", encoded) is False

    def test_legacy_unsalted_sha256_is_rejected(self):
        # The fallback that accepted these is what let real credentials end up in the repo.
        assert verify_password("password", LEGACY_SHA256_OF_TEST) is False

    def test_manager_disables_a_profile_with_a_non_scrypt_hash(self, caplog):
        manager = AuthManager(password_hashes={"b": LEGACY_SHA256_OF_TEST})
        assert "b" not in manager.password_hashes
        assert manager.login("b", "password") is None

    def test_manager_keeps_a_scrypt_profile(self):
        manager = AuthManager(password_hashes={"b": hash_password("s-word")})
        assert manager.login("b", "s-word") is not None


class TestFailClosedStartup:
    def test_no_hashes_configured_refuses_to_start(self, monkeypatch):
        monkeypatch.setattr(auth_module, "ACCOUNT_PASSWORD_HASHES", {})
        with pytest.raises(RuntimeError, match="refusing to start"):
            require_configured_accounts()

    def test_legacy_hash_does_not_count_as_configured(self, monkeypatch):
        monkeypatch.setattr(auth_module, "ACCOUNT_PASSWORD_HASHES", {"b": LEGACY_SHA256_OF_TEST})
        with pytest.raises(RuntimeError, match="refusing to start"):
            require_configured_accounts()

    def test_scrypt_hash_counts_as_configured(self, monkeypatch):
        monkeypatch.setattr(
            auth_module, "ACCOUNT_PASSWORD_HASHES", {"b": hash_password("x")}
        )
        assert require_configured_accounts() == ["b"]


class TestEnvironmentFile:
    def test_dotenv_fills_gaps_without_overriding_the_environment(self, tmp_path, monkeypatch):
        config = importlib.import_module("pi_chat.config")
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# a comment\n"
            "PI_CHAT_TEST_ONLY=from-file\n"
            'PI_CHAT_TEST_QUOTED="quoted value"\n'
            "export PI_CHAT_TEST_EXPORTED=from-export\n"
            "PI_CHAT_TEST_PRESET=from-file\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PI_CHAT_TEST_PRESET", "from-environment")
        monkeypatch.delenv("PI_CHAT_TEST_ONLY", raising=False)

        config.load_dotenv(env_file)

        import os

        assert os.environ["PI_CHAT_TEST_ONLY"] == "from-file"
        assert os.environ["PI_CHAT_TEST_QUOTED"] == "quoted value"
        assert os.environ["PI_CHAT_TEST_EXPORTED"] == "from-export"
        assert os.environ["PI_CHAT_TEST_PRESET"] == "from-environment"

        for key in ("PI_CHAT_TEST_ONLY", "PI_CHAT_TEST_QUOTED", "PI_CHAT_TEST_EXPORTED", "PI_CHAT_TEST_PRESET"):
            os.environ.pop(key, None)

    def test_repository_ships_no_credential_defaults(self):
        config = importlib.import_module("pi_chat.config")
        source = (config.PROJECT_ROOT / "pi_chat" / "config.py").read_text(encoding="utf-8")
        # A 64-hex literal in the config module is the shape the leaked defaults had.
        assert re.search(r"[0-9a-f]{64}", source) is None


class TestCredentialHelper:
    """The CLI has to leave nothing to copy-paste, so it writes .env itself. That means the
    writer needs to be careful about the other lines in the file."""

    def test_variable_names_are_the_ones_config_reads(self):
        from pi_chat import config

        assert auth_module.env_variable_for("b") == "PI_CHAT_B_PASSWORD_HASH"
        assert auth_module.env_variable_for("r") == "PI_CHAT_R_PASSWORD_HASH"
        assert config.ACCOUNT_ENV_VARIABLES["b"] == auth_module.env_variable_for("b")

    def test_unknown_profile_names_the_known_ones(self):
        with pytest.raises(SystemExit, match="Known profiles: b, r"):
            auth_module.env_variable_for("ben")

    def test_write_creates_the_file_when_absent(self, tmp_path):
        target = tmp_path / ".env"
        auth_module.write_env_value("b", "scrypt$1$2$3$deadbeef", target)
        assert target.read_text(encoding="utf-8").splitlines()[-1] == "PI_CHAT_B_PASSWORD_HASH=scrypt$1$2$3$deadbeef"

    def test_write_replaces_in_place_and_keeps_other_lines(self, tmp_path):
        target = tmp_path / ".env"
        target.write_text(
            "# keep me\n"
            "PI_CHAT_B_PASSWORD_HASH=scrypt$old\n"
            "PI_CHAT_HOST=0.0.0.0\n"
            "export PI_CHAT_R_PASSWORD_HASH=scrypt$legacy-form\n",
            encoding="utf-8",
        )

        auth_module.write_env_value("b", "scrypt$new", target)
        auth_module.write_env_value("r", "scrypt$also-new", target)

        lines = target.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# keep me"
        assert lines[1] == "PI_CHAT_B_PASSWORD_HASH=scrypt$new"
        assert lines[2] == "PI_CHAT_HOST=0.0.0.0"
        assert lines[3] == "PI_CHAT_R_PASSWORD_HASH=scrypt$also-new"

    def test_main_writes_without_printing_the_hash(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / ".env"
        monkeypatch.setattr(auth_module, "ENV_FILE", target)
        answers = iter(["sunny-stable-42", "sunny-stable-42"])
        monkeypatch.setattr(auth_module.getpass, "getpass", lambda prompt="": next(answers))

        auth_module.main(["--account", "b"])

        assert "PI_CHAT_B_PASSWORD_HASH=scrypt$" in target.read_text(encoding="utf-8")
        printed = capsys.readouterr().out
        assert "scrypt$" not in printed, "the hash should not be echoed when it was written"
        assert "Restart the server" in printed

    def test_main_print_mode_leaves_the_file_alone(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / ".env"
        monkeypatch.setattr(auth_module, "ENV_FILE", target)
        answers = iter(["hunter2hunter2", "hunter2hunter2"])
        monkeypatch.setattr(auth_module.getpass, "getpass", lambda prompt="": next(answers))

        auth_module.main(["--account", "r", "--print"])

        assert not target.exists()
        assert capsys.readouterr().out.startswith("PI_CHAT_R_PASSWORD_HASH=scrypt$")

    def test_main_mismatch_refuses_to_write(self, tmp_path, monkeypatch):
        target = tmp_path / ".env"
        monkeypatch.setattr(auth_module, "ENV_FILE", target)
        answers = iter(["one-thing", "another-thing"])
        monkeypatch.setattr(auth_module.getpass, "getpass", lambda prompt="": next(answers))

        with pytest.raises(SystemExit, match="do not match"):
            auth_module.main(["--account", "b"])
        assert not target.exists()

    def test_check_verifies_against_the_loaded_hash(self, monkeypatch):
        monkeypatch.setattr(
            auth_module, "ACCOUNT_PASSWORD_HASHES", {"b": hash_password("right-one")}
        )
        assert auth_module.check_account_password("b", "right-one") is True
        assert auth_module.check_account_password("b", "wrong-one") is False

    def test_check_on_an_unconfigured_profile_explains_the_fix(self, monkeypatch):
        monkeypatch.setattr(auth_module, "ACCOUNT_PASSWORD_HASHES", {})
        with pytest.raises(SystemExit, match="--account b"):
            auth_module.check_account_password("b", "anything")

    def test_account_is_asked_when_not_passed(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": " R ")
        assert auth_module.choose_account(None) == "r"

    def test_a_typed_account_is_never_guessed(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "ben")
        with pytest.raises(SystemExit, match="Known profiles"):
            auth_module.choose_account(None)


class TestSecurityHeaders:
    @pytest.fixture()
    def client(self):
        from pi_chat.app import create_app

        auth = AuthManager(password_hashes={"b": hash_password("test-password")})
        return TestClient(create_app(auth_manager=auth))

    def test_page_serves_a_locking_content_security_policy(self, client):
        response = client.get("/")
        policy = response.headers.get("Content-Security-Policy", "")

        assert "script-src 'self'" in policy
        assert "unsafe-eval" not in policy
        assert "frame-ancestors 'none'" in policy
        assert "object-src 'none'" in policy

    def test_nosniff_and_referrer_policy_are_set(self, client):
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("Referrer-Policy") == "no-referrer"

    def test_modules_are_revalidated_never_heuristically_cached(self, client):
        """The frontend is served by name, not by content hash, so it must revalidate.

        With only `Last-Modified` and no `Cache-Control`, Chrome derives freshness itself and can keep
        running a bundle from before the last edit. On 2026-11-09 that turned a correct server into an
        app that looked broken, and it also makes a screenshot stage capable of passing on code that is
        no longer on disk. `no-cache` revalidates against the etag, so a normal load is a 304.
        """
        for path in ("/", "/static/app.js", "/static/preview.js", "/static/styles.css"):
            assert client.get(path).headers.get("cache-control") == "no-cache", path

    def test_index_html_has_no_inline_script_or_event_handler(self, client):
        # script-src 'self' only holds if nothing needs inline script. If this starts failing,
        # the fix is to move the handler into a module, not to widen the policy.
        body = client.get("/").text
        assert "<script>" not in body
        assert " onclick=" not in body
        assert " onerror=" not in body
