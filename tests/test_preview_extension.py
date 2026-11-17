"""The preview extension, driven without spending a token.

`/preview` is a slash command, and pi dispatches a slash command over RPC with
`disposition: "handled"` and no model call. That makes the whole extension testable for free: pi
registers the tool and the command, runs the code, writes the file, and appends the entry, with
nothing sent to any engine. Spawning pi is the point of these tests, so they are skipped rather
than faked when pi is not installed.

The browser half is deliberately not exercised here; the entry payload is the contract between the
two, and `tests/test_preview_api.py` covers what the server does with it.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from pi_chat.process import resolve_pi_command

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "pi_chat" / "extension" / "preview.ts"
STARTUP_TIMEOUT = 60.0
EVENT_TIMEOUT = 30.0


def _pi_is_installed() -> object | None:
    """Where the real CLI lives, or None when there is none.

    `resolve_pi_command()` deliberately answers with a best-effort `["pi"]` when it finds nothing, because
    a server that is starting up should log a failed spawn rather than refuse to boot. That fallback is the
    wrong answer in a test: an unfindable binary here means "this check does not apply to this machine",
    and treating it as a failure is how a suite turns red for reasons that have nothing to do with the code.
    CI is the machine where this matters, and CI has no pi by design: every other step in the workflow runs
    against `tools/fake_rpc_pi.py`, and the point of *these* tests is that the real CLI loads the real
    extension, so there is nothing to substitute.
    """
    candidate = os.environ.get("PI_CHAT_PI_BIN", "").strip() or "pi"
    if candidate.lower().endswith((".js", ".mjs", ".cjs")):
        # `_argv_for_pi_path` runs a package entry point under node, so the script is the thing that must exist.
        return candidate if Path(candidate).is_file() else None
    return shutil.which(candidate)


if _pi_is_installed() is None:
    pytest.skip(
        "these tests spawn the real pi CLI to load pi_chat/extension/preview.ts; none is installed here",
        allow_module_level=True,
    )


class PiRpc:
    """Enough of an RPC client to send commands and collect events."""

    def __init__(self, cwd: Path, env: dict):
        argv = resolve_pi_command() + [
            "--mode", "rpc",
            "--approve",
            # Only the extension under test: no user extensions, no skills, no built-ins.
            "-ne",
            "-e", str(EXTENSION),
            "--no-skills",
            "--session-dir", str(cwd / "sessions"),
        ]
        self.process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.stderr_lines: list[str] = []
        self._stop = threading.Event()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self):
        # Drain continuously: a full stdout pipe stalls pi, and a test that waits on a wedged
        # child is indistinguishable from a broken extension.
        assert self.process.stdout is not None
        for raw in iter(self.process.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                self.events.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _pump_stderr(self):
        assert self.process.stderr is not None
        for raw in iter(self.process.stderr.readline, b""):
            self.stderr_lines.append(raw.decode("utf-8", errors="replace").strip())

    def send(self, payload: dict):
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def command(self, request_id: str, payload: dict):
        self.send({**payload, "id": request_id})

    def wait_for(self, predicate, timeout: float = EVENT_TIMEOUT, what: str = "event"):
        """Return the first collected event matching predicate, or fail with context."""
        deadline = timeout
        seen = []
        while True:
            try:
                event = self.events.get(timeout=0.25)
            except queue.Empty:
                deadline -= 0.25
                if deadline <= 0:
                    pytest.fail(
                        f"timed out waiting for {what}. "
                        f"stderr tail: {self.stderr_lines[-6:]!r}; events: {seen[-8:]!r}"
                    )
                continue
            seen.append(event.get("type"))
            if predicate(event):
                return event

    def preview_entry(self, match=None):
        return self.wait_for(
            lambda event: event.get("type") == "entry_appended"
            and event.get("entry", {}).get("customType") == "pichat_preview"
            and (match is None or match(event["entry"]["data"])),
            what="a pichat_preview entry",
        )["entry"]["data"]

    def close(self):
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp("preview-ext")
    (root / "sessions").mkdir()
    return root


@pytest.fixture(scope="module")
def documents(workspace):
    notes = workspace / "notes.md"
    notes.write_text("# Notes\n\n- from the extension test\n", encoding="utf-8")
    awkward = workspace / "secrets.md"
    awkward.write_text("do not commit\n", encoding="utf-8")
    return {"notes": notes, "secrets": awkward}


@pytest.fixture(scope="module")
def pi(workspace, tmp_path_factory):
    if not EXTENSION.is_file():
        pytest.skip(f"preview extension is missing at {EXTENSION}")
    preview_dir = tmp_path_factory.mktemp("preview-store")
    env = dict(os.environ)
    env["PI_CHAT_PREVIEW_DIR"] = str(preview_dir)
    env.pop("PI_CHAT_PI_ARGS", None)
    try:
        resolve_pi_command()
    except Exception as error:  # pragma: no cover - environment dependent
        pytest.skip(f"pi is not runnable here: {error}")

    client = PiRpc(workspace, env)
    try:
        # The command only works once the runtime is up, so wait for proof of that rather than
        # sleeping an arbitrary amount.
        client.command("boot", {"type": "get_commands"})
        client.wait_for(
            lambda event: event.get("id") == "boot" and event.get("type") == "response",
            timeout=STARTUP_TIMEOUT,
            what="pi to finish starting",
        )
        yield client, preview_dir
    finally:
        client.close()


class TestPreviewCommand:
    def test_a_file_is_referenced_without_being_copied(self, pi, documents):
        client, store = pi
        before = set(store.glob("*"))

        client.send({"type": "prompt", "message": f"/preview {documents['notes']}"})
        entry = client.preview_entry(lambda data: data.get("op") == "open")

        assert entry["v"] == 1
        assert entry["kind"] == "markdown"
        assert entry["path"] == str(documents["notes"])
        assert entry["title"] == "notes.md"
        assert entry["revision"] == 1
        assert "file" not in entry
        assert entry["docId"] and len(entry["docId"]) == 32
        # A referenced document is shown, not duplicated.
        assert set(store.glob("*")) == before

    def test_store_mode_copies_the_bytes_into_the_connection_scope(self, pi, documents):
        client, store = pi

        client.send({"type": "prompt", "message": f"/preview store {documents['secrets']}"})
        entry = client.preview_entry(
            lambda data: data.get("op") == "open" and data.get("title") == "secrets.md"
        )

        assert "path" not in entry
        copied = store / entry["file"]
        assert copied.is_file()
        assert copied.read_bytes() == documents["secrets"].read_bytes()
        assert entry["file"] == f"{entry['docId']}.md"

    def test_clear_closes_the_pane(self, pi):
        client, _ = pi

        client.send({"type": "prompt", "message": "/preview clear"})
        entry = client.preview_entry(lambda data: data.get("op") == "clear")

        assert entry["op"] == "clear"

    def test_a_missing_file_reports_without_opening_anything(self, pi, workspace):
        client, _ = pi
        missing = workspace / "nope.md"

        client.send({"type": "prompt", "message": f"/preview {missing}"})
        notification = client.wait_for(
            lambda event: event.get("type") == "extension_ui_request"
            and event.get("method") == "notify"
            and "No file at" in str(event.get("message", "")),
            what="a notify about the missing file",
        )

        assert notification["notifyType"] == "error"

    def test_entries_are_persisted_for_a_session_replay(self, pi):
        client, _ = pi
        request_id = "entries-1"

        client.command(request_id, {"type": "get_entries"})
        response = client.wait_for(
            lambda event: event.get("id") == request_id and event.get("type") == "response",
            what="get_entries",
        )

        assert response["success"] is True
        previews = [
            entry
            for entry in response["data"]["entries"]
            if entry.get("type") == "custom" and entry.get("customType") == "pichat_preview"
        ]
        ops = [entry["data"]["op"] for entry in previews]
        # Everything the extension did is in the session file, which is what lets a reopened
        # session rebuild the pane without a live pi.
        assert "open" in ops and "clear" in ops
        assert all(entry["data"]["v"] == 1 for entry in previews)


class TestRegistration:
    def test_the_extension_loads_without_errors(self, pi):
        client, _ = pi
        # Loading a malformed tool or command definition surfaces as extension_error. Nothing has
        # arrived so far, and nothing may: this is the only cheap check that pi actually accepted
        # the registration shape rather than the file merely parsing.
        client.command("probe-1", {"type": "get_state"})
        client.wait_for(
            lambda event: event.get("id") == "probe-1" and event.get("type") == "response",
            what="get_state",
        )

        errors = [event for event in drain(client) if event.get("type") == "extension_error"]
        assert errors == []

    def test_the_preview_command_is_discoverable(self, pi):
        client, _ = pi

        client.command("commands-1", {"type": "get_commands"})
        response = client.wait_for(
            lambda event: event.get("id") == "commands-1" and event.get("type") == "response",
            what="get_commands",
        )

        assert response["success"] is True
        names = [
            command.get("name")
            for command in response["data"].get("commands", [])
        ]
        assert "preview" in names


def drain(client: PiRpc) -> list:
    """Everything collected so far, without waiting for more."""
    events = []
    while True:
        try:
            events.append(client.events.get_nowait())
        except queue.Empty:
            return events

