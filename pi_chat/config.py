"""Shared application paths and environment settings."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DEV_MODE = os.environ.get("PI_CHAT_DEV", "").lower() in {"1", "true", "yes"}
SESSION_TTL_SECONDS = int(os.environ.get("PI_CHAT_SESSION_TTL", 7 * 24 * 60 * 60))

# Legacy defaults preserve the existing profile passwords while moving their
# verification off the client. Override these with scrypt hashes before
# exposing the app outside the local machine.
ACCOUNT_PASSWORD_HASHES = {
    "b": os.environ.get(
        "PI_CHAT_B_PASSWORD_HASH",
        "69b881c987b2c1a324b5c6139bc6e874b1787823baaf2e15307b3f950391c5db",
    ),
    "r": os.environ.get(
        "PI_CHAT_R_PASSWORD_HASH",
        "6e45bdd24c7f96268677a186aa2a4245acd58a1a02d76649c070d306f5461ab8",
    ),
}
