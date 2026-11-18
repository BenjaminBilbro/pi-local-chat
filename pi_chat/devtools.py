"""Dev-mode hot reload: what the running process was built from, and a way to change it from the page.

Two kinds of change live here, and they behave differently for a reason worth saying out loud. Front-end
files are read from disk on every request and already ship `Cache-Control: no-cache`, so a refresh applies
them; what is missing is not a cache bust but the *knowledge* that something changed. Back-end files are read
once, at import, so a refresh only reconnects you to the process holding the old code. Applying those needs a
new process, which a page cannot get by asking politely, so `server.py` runs a supervisor while `PI_CHAT_DEV`
is set and the page asks for a restart instead of guessing at a command line.

Chosen deliberately over the quieter option (Ben, 2026-11-18): the banner could have reported "back-end
changed, restart yourself" and stopped there. The button won because the thing being optimized is how fast a
change goes from edited to used while a person is watching, and a step that lives in another window is where
that loop dies.

The endpoint is therefore narrow: it exists only when `PI_CHAT_DEV` is set, it only answers to loopback, it
still needs a logged-in cookie, and it does exactly one thing, which is ask the server to shut down and the
supervisor to bring it back. `DEV_MODE` already refuses to bind anything but loopback unless
`PI_CHAT_ALLOW_DEV_REMOTE=1` says otherwise, so the two guards stack rather than relying on each other.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

# `sysexits` EX_TEMPFAIL, borrowed for its meaning: "this failed for a reason that will go away if you try
# again". The supervisor restarts on this code and only this code, so a crash is still a crash and does not
# become a respawn loop against a syntax error.
RESTART_EXIT_CODE = 75

# Set on the child only. Without it the relaunched `server.py` would start another supervisor and grow one
# process per restart forever.
CHILD_ENV = "PI_CHAT_CHILD"

ROOT = Path(__file__).resolve().parent.parent
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# One identity per process, so the page can tell "the stamp moved" from "the process I was talking to is gone
# and a new one answered". A stamp alone cannot make that distinction: a restart and a file edit look alike.
BOOT_ID = uuid.uuid4().hex[:8]
STARTED_AT = time.time()

_server = None                  # the uvicorn Server, attached by server.py
_restart_requested = False


def is_child() -> bool:
    return (os.environ.get(CHILD_ENV) or "").lower() in {"1", "true", "yes"}


def _files(root: Path, suffixes: tuple[str, ...]):
    try:
        if not root.is_dir():
            return
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in suffixes:
                yield path
    except OSError:
        # A tree that cannot be walked is reported as an empty stamp rather than raising inside a route that
        # has nothing to do with the filesystem.
        return


def _stamp(paths) -> dict:
    """A stamp for a set of files: enough to notice a change, and enough to name the file that caused it.

    The newest path is in the payload because a banner that says only "something changed" sends a person off
    to check their own edits, which is the work the banner exists to remove.
    """
    newest_ns = 0
    newest = ""
    files = 0
    total = 0
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        files += 1
        total += info.st_size
        if info.st_mtime_ns > newest_ns:
            newest_ns = info.st_mtime_ns
            try:
                newest = path.relative_to(ROOT).as_posix()
            except ValueError:
                newest = path.name
    # Size belongs in the stamp because two saves can land inside one clock tick, and an edit that keeps the
    # byte count identical is rare enough that mtime plus size ends the argument.
    return {"stamp": f"{newest_ns}-{files}-{total}", "files": files, "newest": newest}


def frontend_stamp(static_dir: Path) -> dict:
    return _stamp(_files(Path(static_dir), (".js", ".css", ".html", ".svg")))


def backend_stamp() -> dict:
    """Everything the running process imports, plus the entry point that starts it.

    `server.py` is folded into the same stamp rather than reported beside it: one restart decision should
    read one number, not two with a rule about which wins.
    """
    return _stamp([*(ROOT / "pi_chat").rglob("*.py"), ROOT / "server.py"])


# What this process imported. Taken at import rather than on the first request, because a request-time answer
# would call files edited in the seconds after startup "already loaded", and a banner that says "all caught
# up" about a stale process is worse than one that never appears.
BOOT_BACKEND = backend_stamp()["stamp"]


def snapshot(static_dir: Path, dev: bool) -> dict:
    """What `/api/version` answers: two stamps the page compares against, and who is answering."""
    return {
        "bootId": BOOT_ID,
        "pid": os.getpid(),
        "startedAt": STARTED_AT,
        "dev": bool(dev),
        "restartable": bool(dev) and _server is not None,
        "frontend": frontend_stamp(static_dir),
        "backend": backend_stamp(),
        # The stamp this process was built from. "Is a restart owed" is that compared against `backend` and
        # nothing else, so it survives a reload: reloading the page does not reload Python.
        "bootBackend": BOOT_BACKEND,
    }


def attach_server(server) -> None:
    """Hand over the uvicorn server so the dev restart route can ask it to stop."""
    global _server
    _server = server


def request_restart() -> bool:
    """Ask for a restart. False when there is no server to ask, which is every test and every import.

    Setting `should_exit` rather than raising or `os._exit` is the point: uvicorn then closes the websockets,
    which runs the disconnect path that kills each `pi` child and closes its audit log. An exit that skips
    that leaves orphaned RPC processes behind, and they are invisible from the page that asked.
    """
    global _restart_requested
    if _server is None:
        return False
    _restart_requested = True
    _server.should_exit = True
    return True


def take_restart_request() -> bool:
    """True once, after `server.run()` returns, if the shutdown was a restart rather than a Ctrl+C."""
    global _restart_requested
    requested, _restart_requested = _restart_requested, False
    return requested


def supervise(log) -> int:
    """Run the server as a child and bring it back when it asks to be restarted.

    The child is a fresh `server.py`, not a re-import: on Windows a process cannot replace its own image, so
    the only honest supervisor is a parent that waits. Children inherit this console, which is what makes
    Ctrl+C reach both of them instead of stranding a server with no terminal.

    It respawns on the restart code and nothing else. A syntax error exits non-zero, so a broken edit prints
    its traceback and stops, rather than restarting into a loop that fills a terminal.
    """
    env = {**os.environ, CHILD_ENV: "1"}
    while True:
        log.info("dev supervisor: starting server (restart-on-demand is on)")
        child = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=ROOT, env=env)
        code = child.wait()
        if code != RESTART_EXIT_CODE:
            return code
        # Let the listening socket finish closing before the next bind. Short enough to feel instant, long
        # enough that the common "address already in use" race stops being a thing someone has to retry.
        log.info("dev supervisor: restart requested, bringing the server back")
        time.sleep(0.4)
