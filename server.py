"""Compatibility entry point for running pi chat directly."""

import logging
import os

from pi_chat import devtools
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

LOOPBACK_HOSTS = devtools.LOOPBACK_HOSTS


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "yes"}


def main():
    import uvicorn

    log = logging.getLogger("pi-chat")

    # Dev mode runs a supervisor, and the supervisor is all it does. A process cannot replace its own image on
    # Windows, so "restart the server" has to mean "finish, and let the parent bring another one up". The
    # diagnostics and account checks below belong to the process that reads them, which is the child, so the
    # parent stays this short.
    if DEV_MODE and not devtools.is_child():
        raise SystemExit(devtools.supervise(log))

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
    config = uvicorn.Config(
        "server:app",
        host=HOST,
        port=PORT,
        reload=False,
        # A dev restart closes websockets, and an open chat tab is not an idle connection uvicorn drops on its
        # own: with no graceful timeout `should_exit` waits for a tab that has no intention of leaving, and the
        # restart the button promised never happens. Three seconds is long enough for the disconnect path to
        # kill each pi child and close its audit log.
        timeout_graceful_shutdown=3 if DEV_MODE else None,
    )
    server = uvicorn.Server(config)
    devtools.attach_server(server)
    if DEV_MODE:
        log.info(
            "hot reload is on: front-end edits apply on refresh, and the page can ask for a restart "
            "(POST /api/dev/restart, loopback only)"
        )
    server.run()

    # The supervisor watches exit codes rather than log lines: 75 means "bring me another", and anything else
    # is the end of the run, so a traceback stays on screen instead of restarting into a loop.
    if devtools.take_restart_request():
        raise SystemExit(devtools.RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
