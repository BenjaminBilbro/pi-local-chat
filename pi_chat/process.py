"""Lifecycle management for the persistent ``pi --mode rpc`` process."""

import asyncio
import json
import logging
import os
import shlex
import shutil
import sys
import uuid
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import WebSocket

from . import preview as preview_store
from .config import CHILD_CONTEXT_FILES, PROJECT_ROOT

log = logging.getLogger("pi-chat")

# The preview pane's model-facing tool ships with the server, not with the user's pi extensions:
# it is loaded only for children this file spawns.
PREVIEW_EXTENSION = Path(__file__).resolve().parent / "extension" / "preview.ts"

# Grace period between SIGTERM and the escalation to SIGKILL when shutting the rpc child
# down. Without it, a child that installs its own SIGTERM handler (or is wedged in an
# uninterruptible call) hangs whatever is waiting on shutdown, and on POSIX terminate()
# is not immediate the way TerminateProcess() is on Windows.
_TERMINATE_GRACE_SECONDS = 5.0

# Path inside the npm prefix to pi's real JS entry point (package.json -> bin.pi).
_PI_CLI_JS = ("node_modules", "@earendil-works", "pi-coding-agent", "dist", "bundle", "cli.js")

# How many browser messages may be waiting for the tab. The browser is the only consumer of this
# pipe, and a tab that stopped reading must never become the reason pi's stdout stops draining:
# the reader enqueues and moves on, so pi is never blocked by a slow or dead client. Overflow means
# the client cannot keep up at all, and at that size (minutes of streaming) it is gone in every
# case that matters, so the honest answer is to close the socket rather than grow the buffer or
# drop events in silence. Invariant 5 then takes the child down with the connection.
_BROWSER_OUT_MAX = 4096

# kill() drains the outbox for this long before cancelling the writer, so the tail of a finished
# run (agent_settled and its last tool results) reaches a browser that is still there.
_BROWSER_DRAIN_SECONDS = 1.0


# Flags whose value is prose rather than a path or a switch. For profile `r` the spawn line used to
# carry `--append-system-prompt` twice per connection: the whole of APPEND_SYSTEM.md and the whole of that
# profile's persona file, printed into the log forever. Nothing in a log line needs that text; the length and the
# file name are what anyone debugging a prompt actually wants.
_PROMPT_FLAGS = {"--append-system-prompt", "--prompt", "-p"}
_ARG_VALUE_MAX_CHARS = 120


def _loggable_command(command: list[str]) -> list[str]:
    """`command` with prompt text replaced by its size.

    A token that is merely long gets truncated too, so a future `PI_CHAT_PI_ARGS` cannot reintroduce
    the problem through a flag this list has never heard of.
    """
    logged: list[str] = []
    redact_next = False
    for token in command:
        if redact_next:
            redact_next = False
            logged.append(f"<{len(token)} chars of prompt>")
            continue
        if token in _PROMPT_FLAGS:
            redact_next = True
            logged.append(token)
            continue
        if len(token) > _ARG_VALUE_MAX_CHARS:
            logged.append(f"{token[:_ARG_VALUE_MAX_CHARS]}... <{len(token) - _ARG_VALUE_MAX_CHARS} more chars>")
            continue
        logged.append(token)
    return logged


def _argv_for_pi_path(path: str) -> list[str]:
    """Turn a resolved pi location into an argv prefix that CreateProcess can execute.

    On Windows npm installs `pi` as `pi` + `pi.cmd` + `pi.ps1` shims next to the package;
    CreateProcess cannot run a `.cmd` without a shell, so `create_subprocess_exec("pi")`
    fails there. Resolve the shim to its JS entry and run it with node instead.

    The path is made absolute here because the child runs with cwd set to the profile's session
    directory: a relative PI_CHAT_PI_BIN such as `tools/fake_rpc_pi.py` would otherwise be
    resolved against a directory that does not contain it.
    """
    resolved = Path(path).expanduser().resolve()
    suffix = resolved.suffix.lower()

    if suffix in {".js", ".mjs", ".cjs"}:
        return ["node", str(resolved)]

    if suffix == ".py":
        # Dev/test stubs such as tools/fake_rpc_pi.py; CreateProcess cannot run a .py either.
        return [sys.executable, str(resolved)]

    if suffix in {".cmd", ".bat"}:
        cli = resolved.parent.joinpath(*_PI_CLI_JS)
        if cli.exists():
            return ["node", str(cli)]
        raise RuntimeError(
            f"Found pi shim at {resolved} but not the JS entry it wraps "
            f"(looked for {cli}). Set PI_CHAT_PI_BIN to pi's cli.js or the pi binary."
        )

    if suffix and not resolved.exists():
        raise RuntimeError(f"PI_CHAT_PI_BIN points at a missing file: {path}")

    return [str(resolved)]


def resolve_pi_command() -> list[str]:
    """Argv prefix for launching pi, portable across Linux/macOS/Windows.

    Override with PI_CHAT_PI_BIN (a pi binary, or the package's cli.js to run under node).
    """
    override = os.environ.get("PI_CHAT_PI_BIN", "").strip()
    if override:
        return _argv_for_pi_path(override)

    found = shutil.which("pi")
    if found:
        return _argv_for_pi_path(found)

    log.warning("No pi on PATH; attempting bare 'pi' (spawn will fail unless it exists)")
    return ["pi"]


def pi_extra_args() -> list[str]:
    """Extra argv for every spawned pi, from PI_CHAT_PI_ARGS (shlex-split).

    Intended for test/privacy runs, e.g. PI_CHAT_PI_ARGS='--model google:gemini-3.8-flash
    --no-extensions --no-skills' to keep a smoke test off the local inference engine and off
    any extension that injects personal context.
    """
    raw = os.environ.get("PI_CHAT_PI_ARGS", "").strip()
    if not raw:
        return []
    tokens = shlex.split(raw, posix=os.name != "nt")
    return [token.strip('"') if os.name == "nt" else token for token in tokens]



class PiProcess:
    """Manage a persistent pi RPC subprocess and its pending requests."""

    def __init__(self, project_root: Path = PROJECT_ROOT):
        self.proc: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        # Last lines pi wrote to stderr. Nobody else reads that pipe, and an unread pipe can
        # fill its OS buffer and wedge the child; keeping the tail also explains silent exits.
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self.ws: WebSocket | None = None
        self.session_id = self._new_session_id()
        self.account: str | None = None
        # Debug-mode audit log (`pi_chat/audit.py`), attached by `websocket.handle_websocket` when
        # PI_CHAT_AUDIT is set. Everything the browser receives passes through this class, which makes it the
        # only place that can tell "never synthesized" apart from "synthesized and never delivered".
        self.audit = None
        self.work_dir: Path | None = None
        self.project_root = project_root
        self._pending_requests: dict[str, asyncio.Future] = {}

        # Voice observer hook
        self.event_observer: Callable[[dict], Awaitable[None]] | None = None

        # Everything the browser receives goes through one queue and one writer task, in the order it
        # was enqueued. Two consequences worth knowing before changing this: the browser can never see
        # an error overtake the events it explains, and the voice piece contract (static/voice.js drops
        # any frame whose sequence is not greater than the last it saw) holds by construction rather
        # than by lock ordering.
        self._browser_out: asyncio.Queue[tuple[str, object]] | None = None
        self._browser_writer_task: asyncio.Task | None = None
        # True once a send failed or the outbox overflowed. Every later send is a no-op, and the
        # socket is closed so the disconnect path can do its job.
        self.browser_dead = False
        self._browser_send_failures = 0
        # True only while kill() is running. An EOF on pi's stdout during kill() is the expected
        # ending, not a crash, and must not be reported to the browser as one.
        self._closing = False

        # Queue mirror. pi owns the steering/follow-up queues and announces every change with a
        # queue_update carrying the complete queue, so the browser renders that announcement rather
        # than guessing. The server keeps the same copy because a "send now" or retract click names
        # an entry the browser saw one round trip ago, and because pi rejects a bare prompt while it
        # is streaming - knowing whether a run is live is what makes that rejection unreachable.
        self.queue: dict[str, list[str]] = {"steering": [], "followUp": []}
        self.is_streaming = False

    @staticmethod
    def _new_session_id() -> str:
        return str(uuid.uuid4())[:8]

    async def spawn(self) -> None:
        """Replace the current process with a new pi RPC instance."""
        await self.kill()
        self.session_id = self._new_session_id()
        self.queue = {"steering": [], "followUp": []}
        self.is_streaming = False

        account_label = self.account or "default"
        self.work_dir = self.project_root / "sessions" / account_label
        self.work_dir.mkdir(parents=True, exist_ok=True)
        log.info("Work dir: %s", self.work_dir)

        command = resolve_pi_command() + ["--mode", "rpc", "--approve"] + pi_extra_args()
        if not CHILD_CONTEXT_FILES:
            command.append("--no-context-files")

        # Every child gets its own preview directory, which is also the only place the browser is
        # allowed to read stored documents from. It is created before the spawn so the extension
        # never sees a path that does not exist.
        environment = dict(os.environ)
        preview_dir = preview_store.connection_scope_dir(self.account, self.session_id)
        environment["PI_CHAT_PREVIEW_DIR"] = str(preview_dir)
        if self.account:
            # The one thing in the child's environment that says which profile it is talking to. Profile
            # `r`'s turns arrive as plain text, so nothing in the transcript identifies the person, and the
            # memory extension's rule for that profile's voice note matches the Telegram bridge prefix
            # instead. This is the key that rule fires on for the chat UI (see the persona injection rule in
            # ~/.pi/agent/lib/memory-core.ts), and the reason the persona lives in memory rather than in a
            # file appended to this command line.
            environment["PI_CHAT_ACCOUNT"] = self.account
        if PREVIEW_EXTENSION.is_file():
            command.extend(["--extension", str(PREVIEW_EXTENSION)])
        else:
            log.warning(
                "Preview extension missing at %s; this session has no preview tool",
                PREVIEW_EXTENSION,
            )

        # No `--append-system-prompt` for either profile. pi discovers `<agent-dir>/APPEND_SYSTEM.md` on its
        # own, and `--no-context-files` only turns off AGENTS.md and CLAUDE.md, so the memory protocol
        # reaches every child without a flag. That was not obvious, and it is why this used to be a
        # special case: a CLI-provided append source *replaces* the discovered one, so passing only one
        # profile's persona silently deleted the protocol from its sessions (the bug the old comment
        # describes). That persona now arrives through the memory extension, keyed on `PI_CHAT_ACCOUNT`, and
        # this whole branch went with it. See docs/system-prompt-composition.md.

        log.info(
            "Spawning pi --mode rpc (session %s, account %s, previews %s): %s",
            self.session_id,
            self.account or "none",
            preview_dir,
            _loggable_command(command),
        )
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.work_dir),
            env=environment,
            # Agent-end events can include full message history and base64 images.
            limit=32 * 1024 * 1024,
        )
        self.reader_task = asyncio.create_task(self._read_stdout())
        self.stderr_task = asyncio.create_task(self._read_stderr())

    async def kill(self) -> None:
        """Terminate the process while preserving account session directories."""
        # Set first: it is how _notice_child_exit distinguishes "we ended pi" from "pi ended us".
        self._closing = True

        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass

        if self.stderr_task and not self.stderr_task.done():
            self.stderr_task.cancel()
            try:
                await self.stderr_task
            except asyncio.CancelledError:
                pass

        if self.proc:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass  # already exited, nothing to escalate
            else:
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    log.warning(
                        "Pi process ignored SIGTERM for %ss, escalating to kill",
                        _TERMINATE_GRACE_SECONDS,
                    )
                    try:
                        self.proc.kill()
                        await asyncio.wait_for(self.proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
                    except asyncio.TimeoutError:
                        log.error("Pi process survived SIGKILL; the child is now orphaned")
                    except ProcessLookupError:
                        pass
                    else:
                        log.info("Pi process reaped after SIGKILL")
            self.proc = None

        # Give the writer a moment to flush what it already has, then stop it. Cancelling without the
        # drain would drop the end of a run that just finished cleanly.
        if self._browser_writer_task and not self._browser_writer_task.done():
            if self._browser_out is not None:
                try:
                    await asyncio.wait_for(
                        self._browser_out.join(), timeout=_BROWSER_DRAIN_SECONDS
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "Browser outbox did not drain in %ss; dropping %d queued message(s)",
                        _BROWSER_DRAIN_SECONDS,
                        self._browser_out.qsize(),
                    )
                except Exception as error:
                    # Not the timeout, so the outbox itself is in a state nobody predicted. Teardown has to
                    # finish regardless, but this is the shutdown path of the subsystem whose entire failure
                    # mode is "pi answered and nothing appeared", and the send paths next door used to be
                    # `except Exception: pass` for exactly that reason (see `_note_browser_failure`). Silence
                    # here would be that same bug in a quieter place.
                    log.warning(
                        "Browser outbox drain raised %s: %s; cancelling the writer",
                        type(error).__name__,
                        error,
                    )
            self._browser_writer_task.cancel()
            try:
                await self._browser_writer_task
            except asyncio.CancelledError:
                pass

        self.reader_task = None
        self.stderr_task = None
        self._browser_writer_task = None
        self._browser_out = None
        # A fresh connection starts with a clean send ledger, even if the last one ended in failures.
        self.browser_dead = False
        self._browser_send_failures = 0
        self._closing = False
        self.is_streaming = False
        self.queue = {"steering": [], "followUp": []}
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(RuntimeError("Pi process killed"))
        self._pending_requests.clear()

        previous_work_dir = self.work_dir
        self.work_dir = None
        log.info("Pi process killed (sessions preserved in %s)", previous_work_dir)

    async def send(self, message: dict) -> None:
        """Send a fire-and-forget JSON command to pi.

        Raises when there is no child to receive it. Returning quietly here meant a command could be
        dropped with nothing said to anyone, which is the same silence the EOF path used to produce
        (see `_notice_child_exit`); the dispatch handler already reports this to the browser.
        """
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("Pi process not running")

        line = (json.dumps(message) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, ValueError) as error:
            # The child died between the guard at the call site and this write. Treat the pipe closing
            # as the exit it is, so the browser hears about it once and the next command respawns.
            await self._notice_child_exit(detail=f"stdin closed on {message.get('type')}: {error}")
            raise RuntimeError("Pi process exited while receiving a command") from error

    async def send_and_wait(
        self,
        command: dict,
        request_id: str,
        timeout: float = 30.0,
    ) -> dict:
        """Send a command and wait for the response with the matching request ID."""
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("Pi process not running")

        future = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future

        command_with_id = {**command, "id": request_id}
        line = (json.dumps(command_with_id) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, ValueError) as error:
            self._pending_requests.pop(request_id, None)
            await self._notice_child_exit(
                detail=f"stdin closed on {command.get('type')}: {error}"
            )
            raise RuntimeError("Pi process exited while receiving a command") from error

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise

    async def send_browser_json(self, payload: dict) -> None:
        """Queue a JSON message for the browser. Never blocks on the client."""
        await self._enqueue_browser("json", payload)

    async def send_browser_bytes(self, payload: bytes) -> None:
        """Queue a binary message for the browser. Never blocks on the client."""
        await self._enqueue_browser("bytes", payload)

    async def _enqueue_browser(self, kind: str, payload: object) -> None:
        """Put one browser message on the outbox, starting the writer on first use.

        Enqueue-only is the point: the stdout reader used to `await` a client send for every pi event,
        so a tab that stopped reading stopped the reader, which stopped draining pi's stdout, which
        could wedge the child on a full pipe. Here the reader returns immediately and the writer task
        absorbs a slow client up to `_BROWSER_OUT_MAX` messages.
        """
        if self.ws is None or self.browser_dead:
            # A dropped message is the finding, not the absence of one. This is the branch where TTS bytes
            # were produced and never reached a tab, which reads to a person as "it went silent".
            if self.audit is not None and self.browser_dead:
                self.audit.event("browser_dropped", kind=kind,
                                 size=len(payload) if isinstance(payload, (bytes, bytearray)) else 0)
            return

        if self._browser_out is None:
            self._browser_out = asyncio.Queue(maxsize=_BROWSER_OUT_MAX)
            self._browser_writer_task = asyncio.create_task(self._write_browser_outbox())

        if self.audit is not None:
            if kind == "json" and isinstance(payload, dict):
                self.audit.json_out(payload)
            elif isinstance(payload, (bytes, bytearray)):
                self.audit.bytes_out(len(payload))
            else:
                self.audit.json_out({"type": type(payload).__name__})

        try:
            self._browser_out.put_nowait((kind, payload))
        except asyncio.QueueFull:
            self._note_browser_failure(
                f"browser outbox full at {_BROWSER_OUT_MAX} messages",
                level=logging.ERROR,
                audit_token="outbox_full",
            )
            await self._abandon_browser()

    async def _write_browser_outbox(self) -> None:
        """Single sender: whatever order messages were enqueued in is the order the browser sees."""
        out = self._browser_out
        if out is None:
            return
        while True:
            kind, payload = await out.get()
            try:
                if kind == "json":
                    await self.ws.send_json(payload)  # type: ignore[union-attr]
                else:
                    await self.ws.send_bytes(payload)  # type: ignore[union-attr]
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._note_browser_failure(f"send failed: {type(error).__name__}: {error}",
                                           audit_token=type(error).__name__)
                await self._abandon_browser()
                out.task_done()
                return
            out.task_done()

    def _note_browser_failure(self, reason: str, level: int = logging.WARNING, audit_token: str = "failure") -> None:
        """Say once, loudly, that the browser stopped taking messages.

        Both send paths used to be `except Exception: pass`, which made "pi answered but nothing
        appeared" impossible to diagnose. Counting keeps the log honest without turning a dead tab
        into a per-event wall of text.

        The audit record carries `audit_token` rather than `reason`: the audit shapes any string with spaces
        into a length and a digest, which is the right policy for a transcript and the wrong one for a
        sentence this file wrote itself. So the caller names the failure in one word, and the log line keeps
        the full sentence for the console.
        """
        self._browser_send_failures += 1
        if self._browser_send_failures == 1:
            log.log(level, "Browser delivery broke (%s); further failures will only be counted", reason)
            if self.audit is not None:
                self.audit.event("browser_send_failed", error=audit_token, failures=self._browser_send_failures)
        elif self._browser_send_failures % 500 == 0:
            log.log(level, "Browser still undeliverable: %d failures", self._browser_send_failures)
            if self.audit is not None:
                self.audit.event("browser_send_failed", error=audit_token, failures=self._browser_send_failures)

    async def _abandon_browser(self) -> None:
        """Close the socket so the disconnect path tears the connection down properly.

        Closing rather than continuing is what keeps the session honest: a client that cannot receive
        is not a client, and letting it stay would leave the UI showing a transcript the server has
        moved past. `handle_websocket`'s finally block does the teardown, including killing the child.
        """
        if self.browser_dead:
            return
        self.browser_dead = True
        ws, self.ws = self.ws, None
        try:
            await ws.close(code=1011)  # type: ignore[union-attr]
        except Exception as error:
            log.debug("Closing a broken browser socket raised: %s", error)

    def _observe_run_event(self, event: dict) -> bool:
        """Track the run and the queue from pi's own events; report whether the queue moved."""
        event_type = event.get("type")
        if event_type == "agent_start":
            self.is_streaming = True
        elif event_type == "agent_settled":
            # pi settles only after it has drained both queues, so an empty queue is implied here.
            self.is_streaming = False
        elif event_type == "queue_update":
            self.queue = parse_queue_snapshot(event)
            return True
        elif event_type == "response" and event.get("command") in {"new_session", "switch_session"}:
            # A different conversation underneath us: whatever pi had queued belongs to the old one.
            self.queue = {"steering": [], "followUp": []}
            self.is_streaming = False
        return False

    def clear_queue_state(self) -> None:
        """Forget the mirror when the session underneath it changes."""
        self.queue = {"steering": [], "followUp": []}
        self.is_streaming = False

    async def _read_stdout(self) -> None:
        """Forward parsed pi events to the connected browser."""
        try:
            while self.proc and self.proc.stdout and not self.proc.stdout.at_eof():
                line_bytes = await self.proc.stdout.readline()
                if not line_bytes:
                    break

                text = line_bytes.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    continue

                request_id = event.get("id")
                if request_id and request_id in self._pending_requests:
                    future = self._pending_requests.pop(request_id)
                    if not future.done():
                        if event.get("success"):
                            future.set_result(event)
                        else:
                            future.set_exception(
                                RuntimeError(event.get("error", "RPC command failed"))
                            )

                queue_changed = self._observe_run_event(event)

                # Send pi_event to browser using locked send
                await self.send_browser_json({"type": "pi_event", "event": event})

                if queue_changed:
                    # A second, smaller message: the browser's queue strip wants the current queue,
                    # not the event log. Same outbox as the event above, so it cannot overtake it.
                    await self.send_browser_json(
                        {"type": "queue_state", "account": self.account, **self.queue}
                    )

                # Call voice observer (if registered)
                if self.event_observer is not None:
                    try:
                        await self.event_observer(event)
                    except Exception as obs_err:
                        log.debug("Voice observer error (non-fatal): %s", obs_err)

        except asyncio.CancelledError:
            # Only kill() cancels this task, and kill() already knows the child is going away.
            return
        except Exception as error:
            log.error("stdout reader error: %s", error, exc_info=True)

        # Both non-cancel ways out of that loop are pi going away on its own: EOF on stdout, or a
        # failure reading it. Reaching here is what makes the `if not pi.proc` respawn guards in
        # websocket.py mean something again.
        await self._notice_child_exit()

    async def _notice_child_exit(self, detail: str | None = None) -> None:
        """Record an unexpected pi exit, release the pending callers, and tell the browser.

        Until now the reader loop simply stopped: `self.proc` stayed non-None, so every guard that
        meant to respawn was dead, and each later command failed as a broken pipe with no explanation.
        Engine OOM is routine on a single-GPU box, so this was the normal shape of a crash, not an
        edge case.
        """
        if self._closing or self.proc is None:
            return

        proc, self.proc = self.proc, None
        code = proc.returncode
        if code is None:
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                code = None

        log.warning(
            "pi exited unexpectedly (code %s)%s; stderr tail: %s",
            code,
            f" after {detail}" if detail else "",
            " | ".join(list(self._stderr_tail)[-3:]) or "(none)",
        )

        self.is_streaming = False
        self.clear_queue_state()
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(RuntimeError(f"Pi process exited (code {code})"))
        self._pending_requests.clear()

        # Deliberately the same `error` type the browser already renders, so the reader sees the
        # reason without a frontend change. `code` is there for a later UI that wants to offer a
        # retry distinctly from an ordinary command failure.
        await self.send_browser_json(
            {
                "type": "error",
                "code": "pi_exited",
                "message": (
                    "pi stopped unexpectedly. Your next message will start a fresh session; "
                    "open a saved conversation to continue where this one left off."
                ),
                "exitCode": code,
            }
        )

    async def _read_stderr(self) -> None:
        """Drain pi's stderr so the pipe cannot fill, and keep the tail for diagnostics."""
        try:
            while self.proc and self.proc.stderr and not self.proc.stderr.at_eof():
                line_bytes = await self.proc.stderr.readline()
                if not line_bytes:
                    break
                text = line_bytes.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                log.warning("pi stderr: %s", text[:500])
        except asyncio.CancelledError:
            return
        except Exception as error:
            log.error("stderr reader error: %s", error, exc_info=True)


def parse_queue_snapshot(payload: dict) -> dict[str, list[str]]:
    """Read a steering/followUp pair off a pi payload, in the one place that knows both shapes.

    pi carries the queue two ways: a `queue_update` event has the lists at the top level, and a
    `clear_queue` response has them under `data`. The server mirrors the queue, restores it after a
    clear, and hands cleared text back to the browser, so all three used to open it with their own
    comprehension and a pi-side field rename would have broken two of them.
    """
    source = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    source = source or {}
    return {
        "steering": [str(text) for text in (source.get("steering") or [])],
        "followUp": [str(text) for text in (source.get("followUp") or [])],
    }
