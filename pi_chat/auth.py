"""Server-side profile authentication and in-memory browser sessions."""

import argparse
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .config import (
    ACCOUNT_ENV_VARIABLES,
    ACCOUNT_PASSWORD_HASHES,
    DEV_MODE,
    DEV_SESSION_FILE,
    ENV_FILE,
    SESSION_TTL_SECONDS,
)

log = logging.getLogger("pi-chat")

COOKIE_NAME = "pi_chat_session"
SCRYPT_PREFIX = "scrypt"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_LENGTH = 32

# Online guessing has to cost more than it can return. scrypt at N=2**14 is about 10 ms per
# verification, so the ten-seconds-per-guess an attacker used to get from the hash alone is not a
# rate limit: a few parallel connections is hundreds of guesses a second, and this server is
# reachable from the tailnet and from a quick tunnel. A profile is now allowed
# LOGIN_MAX_FAILURES wrong answers per LOGIN_WINDOW_SECONDS, and while it is over that the password
# is not checked at all - the check is the thing the attacker is paying for.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300.0

# Cap on tracked profile names, so a spray of random account strings cannot grow memory without
# bound. Names whose oldest attempt has aged out are dropped.
_MAX_TRACKED_ACCOUNTS = 1024


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
        max_failed_logins: int = LOGIN_MAX_FAILURES,
        failed_login_window: float = LOGIN_WINDOW_SECONDS,
    ):
        requested = password_hashes or ACCOUNT_PASSWORD_HASHES
        # Anything that is not a scrypt hash is refused rather than honoured: the fallback that
        # used to live in verify_password() accepted unsalted SHA-256, which is what let real
        # credentials end up committed to the repository.
        self.password_hashes = {}
        for account, encoded_hash in requested.items():
            if encoded_hash and encoded_hash.startswith(f"{SCRYPT_PREFIX}$"):
                self.password_hashes[account] = encoded_hash
            elif encoded_hash:
                log.error(
                    "Password hash for profile %r is not a scrypt hash; that profile is disabled. "
                    "Regenerate it with: python -m pi_chat.auth",
                    account,
                )
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, AuthSession] = {}
        # Dev mode only: live sessions survive a restart, because "restart the server" from a button that then
        # throws you back at the login screen is not a restart, it is a logout with extra steps. Production
        # keeps sessions in memory alone, where a restart is a deliberate act and losing them is the point.
        self._session_file = Path(DEV_SESSION_FILE) if DEV_MODE else None
        if self._session_file is not None:
            self._load_sessions()
        self.max_failed_logins = max_failed_logins
        self.failed_login_window = failed_login_window
        # profile -> monotonic timestamps of recent wrong answers, oldest first
        self._failed_logins: dict[str, deque[float]] = {}

    def login(self, account: str, password: str) -> str | None:
        if self.retry_after(account) > 0:
            # Deliberately not verifying the password: verification is the expensive part, and it is
            # the only thing an attacker gets back per request. Same answer as a wrong password.
            return None

        encoded_hash = self.password_hashes.get(account)
        if not encoded_hash or not verify_password(password, encoded_hash):
            self._note_failed_login(account)
            return None

        self._failed_logins.pop(account, None)
        self._remove_expired_sessions()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = AuthSession(
            account=account,
            expires_at=time.time() + self.session_ttl_seconds,
        )
        self._save_sessions()
        return token

    def retry_after(self, account: str) -> float:
        """Seconds until this profile may answer a login again. 0.0 means it may try now.

        The window slides: an attempt log that has aged out costs nothing, so a legitimate user who
        mistypes twice and waits is never punished beyond the next attempt.
        """
        attempts = self._failed_logins.get(account)
        if not attempts:
            return 0.0
        self._prune_failed_logins(account)
        if len(attempts) < self.max_failed_logins:
            return 0.0
        return max(0.0, self.failed_login_window - (time.monotonic() - attempts[0]))

    def _note_failed_login(self, account: str) -> None:
        now = time.monotonic()
        attempts = self._failed_logins.setdefault(account, deque())
        attempts.append(now)
        self._prune_failed_logins(account)
        if len(attempts) == self.max_failed_logins:
            # Once, on the transition into lockout: enough to spot a campaign in the log without
            # turning a fumble into noise.
            log.warning(
                "Profile %r has had %d failed logins in %.0f s; logins for it are refused for up to "
                "%.0f s more.",
                account,
                self.max_failed_logins,
                self.failed_login_window,
                self.retry_after(account),
            )
        if len(self._failed_logins) > _MAX_TRACKED_ACCOUNTS:
            for name in [k for k, v in self._failed_logins.items() if not v]:
                self._failed_logins.pop(name, None)

    def _prune_failed_logins(self, account: str) -> None:
        attempts = self._failed_logins.get(account)
        if not attempts:
            return
        cutoff = time.monotonic() - self.failed_login_window
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._failed_logins.pop(account, None)

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
            self._save_sessions()

    def _load_sessions(self) -> None:
        """Read live sessions back, dev mode only.

        Every failure here is a warning rather than an exception: this runs inside a constructor, and a
        half-written or hand-edited file must cost a login, not the server.
        """
        if self._session_file is None:
            return
        try:
            payload = json.loads(self._session_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            log.warning("Ignoring the dev session file %s (%s); logins will not survive this restart", self._session_file, error)
            return

        now = time.time()
        restored = 0
        for entry in payload.get("sessions", []) if isinstance(payload, dict) else []:
            if not isinstance(entry, dict):
                continue
            token = entry.get("token")
            account = entry.get("account")
            expires_at = entry.get("expiresAt")
            if not isinstance(token, str) or not isinstance(account, str):
                continue
            if not isinstance(expires_at, (int, float)) or expires_at <= now:
                continue
            if account not in self.password_hashes:
                # A profile whose hash has gone away is a profile that cannot be logged in, whatever the file
                # says about a session it issued days ago.
                continue
            self._sessions[token] = AuthSession(account=account, expires_at=float(expires_at))
            restored += 1
        if restored:
            log.info("Dev mode: restored %d live session(s) from %s", restored, self._session_file.name)

    def _save_sessions(self) -> None:
        """Write the live sessions, dropping expired ones on the way out so the file cannot only grow."""
        if self._session_file is None:
            return
        now = time.time()
        payload = {
            "sessions": [
                {"token": token, "account": session.account, "expiresAt": session.expires_at}
                for token, session in self._sessions.items()
                if session.expires_at > now
            ]
        }
        try:
            self._session_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            if os.name != "nt":
                # The file holds live tokens. Windows answers this through ACLs the call cannot express, so it
                # is best-effort there and a real mode on everything else.
                os.chmod(self._session_file, 0o600)
        except OSError as error:
            log.warning("Could not write the dev session file %s (%s)", self._session_file, error)

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
    """Verify a salted scrypt hash. Any other format is rejected outright."""
    if not encoded_hash.startswith(f"{SCRYPT_PREFIX}$"):
        return False
    return _verify_scrypt(password, encoded_hash)


def configured_accounts() -> list[str]:
    """Profile names that have a usable scrypt hash in the environment."""
    return sorted(
        account
        for account, encoded_hash in ACCOUNT_PASSWORD_HASHES.items()
        if encoded_hash
        and encoded_hash.startswith(f"{SCRYPT_PREFIX}$")
    )


def require_configured_accounts() -> list[str]:
    """Fail closed: no profile with a usable hash means the app must not serve traffic.

    The previous behaviour fell back to hashes committed in config.py, so a fresh checkout ran
    with known credentials, and those credentials were crackable offline from git history.
    """
    accounts = configured_accounts()
    if accounts:
        return accounts
    raise RuntimeError(
        "No profile password is configured, refusing to start.\n"
        "  1. Generate a hash per profile:  python -m pi_chat.auth\n"
        "  2. Put them in an untracked .env at the repo root (see README):\n"
        "       PI_CHAT_B_PASSWORD_HASH=scrypt$...\n"
        "       PI_CHAT_R_PASSWORD_HASH=scrypt$...\n"
        "     or export them in the environment before launching server.py."
    )


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


def env_variable_for(account: str) -> str:
    """The environment variable carrying a profile's hash, or an error naming the profiles."""
    try:
        return ACCOUNT_ENV_VARIABLES[account]
    except KeyError:
        known = ", ".join(sorted(ACCOUNT_ENV_VARIABLES))
        raise SystemExit(f"Unknown profile {account!r}. Known profiles: {known}") from None


def configured_hash_for(account: str) -> str:
    """The hash this process loaded for a profile (real environment first, then .env)."""
    return ACCOUNT_PASSWORD_HASHES.get(account, "")


def write_env_value(account: str, encoded_hash: str, env_file: Path | None = None) -> Path:
    """Point a profile at a new hash in .env, leaving every other line alone."""
    variable = env_variable_for(account)
    path = env_file or ENV_FILE
    entry = f"{variable}={encoded_hash}"

    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, existing in enumerate(lines):
            stripped = existing.strip()
            if stripped.startswith(f"{variable}=") or stripped.startswith(f"export {variable}="):
                lines[index] = entry
                break
        else:
            lines.append(entry)
        text = "\n".join(lines) + "\n"
    else:
        text = (
            "# Local credentials for pi-chat. Gitignored: never commit this file.\n"
            f"{entry}\n"
        )

    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)  # best effort: NTFS maps this loosely, but it is not group-readable
    except OSError:
        pass
    return path


def check_account_password(account: str, password: str) -> bool:
    """Whether a passphrase matches the hash this process loaded for that profile."""
    encoded = configured_hash_for(account)
    if not encoded:
        raise SystemExit(
            f"Profile {account!r} has no hash configured, so it cannot log in. "
            f"Set one with: python -m pi_chat.auth --account {account}"
        )
    return verify_password(password, encoded)


def choose_account(argv_account: str | None) -> str:
    """Use --account when given, otherwise ask. Never guess: b and r differ by one character."""
    if argv_account:
        return argv_account

    known = sorted(ACCOUNT_ENV_VARIABLES)
    print("Which profile? " + " / ".join(known))
    answer = input("> ").strip().lower()
    if answer not in ACCOUNT_ENV_VARIABLES:
        raise SystemExit(f"Unknown profile {answer!r}. Known profiles: {', '.join(known)}")
    return answer


def prompt_for_new_password(account: str) -> str:
    password = getpass.getpass(f"New passphrase for profile {account}: ")
    if not password:
        raise SystemExit("Password cannot be empty.")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")
    return password


def main(argv: list[str] | None = None) -> None:
    """Set or verify a profile passphrase, writing .env so nothing has to be copy-pasted."""
    parser = argparse.ArgumentParser(
        prog="python -m pi_chat.auth",
        description=(
            "Set or verify a pi-chat profile passphrase. Writes the hash into .env directly; a "
            "running server only picks it up on restart."
        ),
    )
    parser.add_argument(
        "-a",
        "--account",
        help="profile to act on (" + ", ".join(sorted(ACCOUNT_ENV_VARIABLES)) + "); asked if omitted",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--check",
        action="store_true",
        help="test a passphrase against the configured hash instead of setting a new one",
    )
    action.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the variable assignment instead of writing .env (for a launcher or systemd unit)",
    )
    args = parser.parse_args(argv)
    account = choose_account(args.account)
    variable = env_variable_for(account)

    if args.check:
        attempt = getpass.getpass(f"Passphrase to test for profile {account}: ")
        if check_account_password(account, attempt):
            print(f"Profile {account}: matches the configured hash.")
            return
        raise SystemExit(
            f"Profile {account}: does NOT match the configured hash. "
            f"Set a new passphrase with: python -m pi_chat.auth --account {account}"
        )

    password = prompt_for_new_password(account)
    encoded = hash_password(password)

    if args.print_only:
        print(f"{variable}={encoded}")
        print("Export that wherever the server starts, then restart it.")
        return

    path = write_env_value(account, encoded)
    print(f"Profile {account}: new passphrase written to {path} ({variable}).")
    print("Restart the server, then log in with the new passphrase.")
    if not args.account:
        print("To change another profile: python -m pi_chat.auth --account <profile>")


if __name__ == "__main__":
    main()
