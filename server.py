"""Compatibility entry point for running pi chat directly."""

import logging
import os

from pi_chat.app import app
from pi_chat.auth import require_configured_accounts
from pi_chat.config import DEV_MODE, log_env_diagnostics

# All interfaces by default so the app is reachable from a phone over Tailscale. Two things
# keep that honest: the desktop's Ethernet sits on the Windows Public firewall profile (inbound
# LAN blocked) and Tailscale ACLs decide who else can connect. To pin to one interface instead,
# set PI_CHAT_HOST=100.x.y.z (the Tailscale address) or 127.0.0.1 for local-only. Put a tunnel
# with auth in front of it for anything outside your own tail (see README "Cloudflare Tunnel").
HOST = os.environ.get("PI_CHAT_HOST", "0.0.0.0")
PORT = int(os.environ.get("PI_CHAT_PORT", "9000"))

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "yes"}


def main():
    import uvicorn

    log = logging.getLogger("pi-chat")

    # Before anything can fail for a reason we can see: a `PI_CHAT_` variable nobody reads behaves exactly
    # like one that took effect, and this application keeps most of its interesting behavior behind knobs.
    log_env_diagnostics(log)

    # Fail closed. A missing profile hash used to fall back to a hash committed in the repo,
    # which meant a fresh checkout served traffic with credentials anyone could crack offline
    # out of git history.
    try:
        accounts = require_configured_accounts()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    # PI_CHAT_DEV skips the per-account session ownership checks. That is fine loopback-only
    # and wrong on a shared interface, so it has to be opted into explicitly.
    if DEV_MODE and HOST not in LOOPBACK_HOSTS and not _truthy(os.environ.get("PI_CHAT_ALLOW_DEV_REMOTE")):
        raise SystemExit(
            "PI_CHAT_DEV disables session ownership checks between profiles, and this server is "
            f"binding {HOST}. Run it on 127.0.0.1, or set PI_CHAT_ALLOW_DEV_REMOTE=1 if you mean it."
        )

    if DEV_MODE:
        log.warning("PI_CHAT_DEV is on: per-profile session isolation is NOT enforced")

    log.info(
        "pi-chat starting on %s:%s with profiles: %s",
        HOST,
        PORT,
        ", ".join(accounts),
    )
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
