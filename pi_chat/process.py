"""Lifecycle management for the persistent ``pi --mode rpc`` process."""

import asyncio
import json
import logging
import uuid
from pathlib import Path

from fastapi import WebSocket

from .config import PROJECT_ROOT

log = logging.getLogger("pi-chat")


class PiProcess:
    """Manage a persistent pi RPC subprocess and its pending requests."""

    def __init__(self, project_root: Path = PROJECT_ROOT):
        self.proc: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.ws: WebSocket | None = None
        self.session_id = self._new_session_id()
        self.account: str | None = None
        self.work_dir: Path | None = None
        self.project_root = project_root
        self._pending_requests: dict[str, asyncio.Future] = {}

    @staticmethod
    def _new_session_id() -> str:
        return str(uuid.uuid4())[:8]

    async def spawn(self) -> None:
        """Replace the current process with a new pi RPC instance."""
        await self.kill()
        self.session_id = self._new_session_id()

        account_label = self.account or "default"
        self.work_dir = self.project_root / "sessions" / account_label
        self.work_dir.mkdir(parents=True, exist_ok=True)
        log.info("Work dir: %s", self.work_dir)

        command = ["pi", "--mode", "rpc", "--approve"]
        if self.account == "r":
            roxy_path = self.project_root / "roxy.md"
            if roxy_path.exists():
                command.extend(["--append-system-prompt", roxy_path.read_text()])
                log.info("Appending roxy.md system prompt (%d chars)", roxy_path.stat().st_size)
            else:
                log.warning("roxy.md not found at %s — skipping", roxy_path)

        log.info(
            "Spawning pi --mode rpc (session %s, account %s)",
            self.session_id,
            self.account or "none",
        )
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.work_dir),
            # Agent-end events can include full message history and base64 images.
            limit=32 * 1024 * 1024,
        )
        self.reader_task = asyncio.create_task(self._read_stdout())

    async def kill(self) -> None:
        """Terminate the process while preserving account session directories."""
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass

        if self.proc:
            try:
                self.proc.terminate()
                await self.proc.wait()
            except ProcessLookupError:
                pass
            self.proc = None

        self.reader_task = None
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(RuntimeError("Pi process killed"))
        self._pending_requests.clear()

        previous_work_dir = self.work_dir
        self.work_dir = None
        log.info("Pi process killed (sessions preserved in %s)", previous_work_dir)

    async def send(self, message: dict) -> None:
        """Send a fire-and-forget JSON command to pi."""
        if not self.proc or not self.proc.stdin:
            return

        line = (json.dumps(message) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

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
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise

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

                if self.ws:
                    try:
                        await self.ws.send_json({"type": "pi_event", "event": event})
                    except Exception:
                        break
        except asyncio.CancelledError:
            return
        except Exception as error:
            log.error("stdout reader error: %s", error)
