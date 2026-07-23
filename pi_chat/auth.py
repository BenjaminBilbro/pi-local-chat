"""Server-side profile authentication and in-memory browser sessions."""

import getpass
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from .config import ACCOUNT_PASSWORD_HASHES, SESSION_TTL_SECONDS

COOKIE_NAME = "pi_chat_session"
SCRYPT_PREFIX = "scrypt"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_LENGTH = 32


@dataclass(frozen=True)
class AuthSession:
    account: str
    expires_at: float


class AuthManager:
    """Validate profile passwords and issue opaque, in-memory sessions."""

    def __init__(
        self,
        password_hashes: dict[str, str] | None = None,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
    ):
        self.password_hashes = password_hashes or ACCOUNT_PASSWORD_HASHES
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, AuthSession] = {}

    def login(self, account: str, password: str) -> str | None:
        encoded_hash = self.password_hashes.get(account)
        if not encoded_hash or not verify_password(password, encoded_hash):
            return None

        self._remove_expired_sessions()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = AuthSession(
            account=account,
            expires_at=time.time() + self.session_ttl_seconds,
        )
        return token

    def account_for_token(self, token: str | None) -> str | None:
        if not token:
            return None

        session = self._sessions.get(token)
        if not session:
            return None
        if session.expires_at <= time.time():
            self._sessions.pop(token, None)
            return None
        return session.account

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def _remove_expired_sessions(self) -> None:
        now = time.time()
        expired_tokens = [
            token
            for token, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for token in expired_tokens:
            self._sessions.pop(token, None)


def hash_password(password: str) -> str:
    """Return a salted scrypt password hash suitable for configuration."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_LENGTH,
    )
    return "$".join(
        [
            SCRYPT_PREFIX,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            salt.hex(),
            digest.hex(),
        ]
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify either a current scrypt hash or a legacy SHA-256 hash."""
    if encoded_hash.startswith(f"{SCRYPT_PREFIX}$"):
        return _verify_scrypt(password, encoded_hash)

    # Compatibility with the two hashes previously shipped in browser code.
    legacy_digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy_digest, encoded_hash)


def _verify_scrypt(password: str, encoded_hash: str) -> bool:
    try:
        _, n, r, p, salt_hex, digest_hex = encoded_hash.split("$", 5)
        expected_digest = bytes.fromhex(digest_hex)
        actual_digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_digest),
        )
        return hmac.compare_digest(actual_digest, expected_digest)
    except (TypeError, ValueError):
        return False


def main() -> None:
    """Interactively create a password hash without exposing the password."""
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    if not password:
        raise SystemExit("Password cannot be empty.")
    print(hash_password(password))


if __name__ == "__main__":
    main()
