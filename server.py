"""FastAPI server bridging a web UI to pi --mode rpc."""

import asyncio
import base64
import json
import logging
import os
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from glob import glob
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pi-chat")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    log.info("Shutting down — killing pi subprocess")
    await pi.kill()


app = FastAPI(title="pi-chat", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"

# ── Pi subprocess manager ────────────────────────────────────────────────

class PiProcess:
    """Manages a persistent pi --mode rpc subprocess."""

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.ws: WebSocket | None = None
        self.session_id = str(uuid.uuid4())[:8]
        self.account: str | None = None  # 'b' or 'r'
        self.work_dir: Path | None = None
        self.project_root: Path = Path(__file__).parent
        self._pending_requests: dict[str, asyncio.Future] = {}  # request_id -> Future for RPC responses

    async def spawn(self):
        """Kill any existing process and spawn a new pi RPC instance."""
        await self.kill()
        self.session_id = str(uuid.uuid4())[:8]

        # Persistent session dir per account (e.g. ./sessions/b/ or ./sessions/r/)
        account_label = self.account or "default"
        self.work_dir = self.project_root / "sessions" / account_label
        self.work_dir.mkdir(parents=True, exist_ok=True)
        log.info("Work dir: %s", self.work_dir)

        # Build CLI args
        cmd = ["pi", "--mode", "rpc", "--approve"]
        if self.account == "r":
            roxy_path = Path(__file__).parent / "roxy.md"
            if roxy_path.exists():
                cmd.extend(["--append-system-prompt", roxy_path.read_text()])
                log.info("Appending roxy.md system prompt (%d chars)", roxy_path.stat().st_size)
            else:
                log.warning("roxy.md not found at %s — skipping", roxy_path)

        log.info("Spawning pi --mode rpc (session %s, account %s)", self.session_id, self.account or "none")
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.work_dir),
            limit=32 * 1024 * 1024,  # 32 MB buffer — agent_end events echo full message history including base64 images
        )
        # Start reading stdout in background
        self.reader_task = asyncio.create_task(self._read_stdout())

    async def kill(self):
        """Terminate the existing pi process."""
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
        # Clear pending requests — they will never be resolved
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(Exception("Pi process killed"))
        self._pending_requests.clear()
        # Do NOT clean up work dir — sessions are persistent per account
        prev_work_dir = self.work_dir
        self.work_dir = None
        log.info("Pi process killed (sessions preserved in %s)", prev_work_dir)

    async def send(self, message: dict):
        """Send a JSON command to pi's stdin (fire-and-forget)."""
        if self.proc and self.proc.stdin:
            line = (json.dumps(message) + "\n").encode("utf-8")
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()

    async def _send_and_wait(self, command: dict, request_id: str, timeout: float = 30.0) -> dict:
        """Send a JSON-RPC command and wait for its response matched by request_id.

        The pi RPC protocol echoes the `id` field on response events.
        This method registers a Future, sends the command, and awaits the Future.
        """
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("Pi process not running")

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_requests[request_id] = future

        cmd_with_id = {**command, "id": request_id}
        line = (json.dumps(cmd_with_id) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise

    async def _read_stdout(self):
        """Continuously read pi's stdout and forward to connected WebSocket."""
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

                # Check if this is a response to a pending RPC request (id-matched)
                req_id = event.get("id")
                if req_id and req_id in self._pending_requests:
                    future = self._pending_requests.pop(req_id)
                    if not future.done():
                        if event.get("success"):
                            future.set_result(event)
                        else:
                            future.set_exception(Exception(event.get("error", "RPC command failed")))

                # Forward all events to WebSocket
                if self.ws:
                    try:
                        await self.ws.send_json({"type": "pi_event", "event": event})
                    except Exception:
                        break
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("stdout reader error: %s", e)

# Global pi process instance
pi = PiProcess()

# ── Dev-mode session loader ──────────────────────────────────────────────

DEV_MODE = os.environ.get("PI_CHAT_DEV", "").lower() in ("1", "true", "yes")


def parse_jsonl_messages(session_path: str) -> list[dict] | None:
    """Extract final messages from a JSONL file (pi session or RPC capture).

    Supports two formats:
    1. Pi session files: lines with {type: 'message', message: {role, content}}
    2. RPC capture files: lines with {event: {type: 'agent_end', messages: [...]}}

    Also extracts sub-agent nested tool calls from:
    - tool_execution_update events (RPC capture format)
    - toolResult messages with details.results (pi session format)
    and attaches them to the corresponding subagent toolCall in the messages.

    Returns the message list or None if parsing fails.
    """
    try:
        session_file = Path(session_path)
        if not session_file.exists():
            return None

        content = session_file.read_text(encoding="utf-8", errors="replace").strip().split("\n")
        if not content:
            return None

        # First pass: collect sub-agent tool call details
        subagent_tool_calls: dict[str, dict] = {}  # toolCallId -> {name, toolCalls: [...], status, summary}

        for line in content:
            try:
                record = json.loads(line)
                event = record.get("event", record)  # handle RPC capture wrapper

                # RPC capture: tool_execution_update events
                if event.get("type") == "tool_execution_update" and event.get("toolName") == "subagent":
                    tool_call_id = event.get("toolCallId", "")
                    args = event.get("args", {})
                    partial = event.get("partialResult", {})
                    details = partial.get("details", {})
                    results = details.get("results", [])
                    if results:
                        tool_calls = _extract_tool_calls_from_result(results[0])
                        if tool_calls and tool_call_id:
                            subagent_tool_calls[tool_call_id] = {
                                "name": args.get("name", "sub-agent"),
                                "toolCalls": tool_calls,
                            }

                # RPC capture: tool_execution_end for final status
                if event.get("type") == "tool_execution_end" and event.get("toolName") == "subagent":
                    tool_call_id = event.get("toolCallId", "")
                    _extract_receipt_status(subagent_tool_calls, tool_call_id, event.get("result", {}), event.get("isError", False))

                # Pi session format: toolResult messages with details
                if event.get("type") == "message":
                    msg = event.get("message", {})
                    if msg.get("role") == "toolResult" and msg.get("toolName") == "subagent":
                        tool_call_id = msg.get("toolCallId", "")
                        isError = msg.get("isError", False)
                        details = msg.get("details", {})
                        results = details.get("results", [])
                        if results:
                            tool_calls = _extract_tool_calls_from_result(results[0])
                            if tool_call_id:
                                entry_data = {
                                    "toolCalls": tool_calls,
                                    "isError": isError,
                                }
                                # Extract receipt status from content
                                text_content = ""
                                for c in msg.get("content", []):
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        text_content = c.get("text", "")
                                        break
                                _extract_receipt_status_from_text(subagent_tool_calls, tool_call_id, text_content, isError)
                                if tool_calls:
                                    subagent_tool_calls.setdefault(tool_call_id, {})
                                    subagent_tool_calls[tool_call_id]["toolCalls"] = tool_calls
                                    subagent_tool_calls[tool_call_id]["isError"] = isError
            except json.JSONDecodeError:
                continue

        # Try RPC capture format first: look for agent_end with messages
        for line in content:
            try:
                record = json.loads(line)
                event = record.get("event", record)
                if event.get("type") == "agent_end" and "messages" in event:
                    messages = event["messages"]
                    _attach_subagent_details(messages, subagent_tool_calls)
                    return messages
            except json.JSONDecodeError:
                continue

        # Fall back to pi session format: collect all messages
        messages = []
        for line in content:
            try:
                entry = json.loads(line)
                if entry.get("type") == "message":
                    msg = entry.get("message", {})
                    role = msg.get("role")
                    if role in ("user", "assistant"):
                        messages.append(msg)
            except json.JSONDecodeError:
                continue

        _attach_subagent_details(messages, subagent_tool_calls)
        return messages if messages else None
    except Exception as e:
        log.error("Failed to parse JSONL %s: %s", session_path, e)
        return None


def _extract_tool_calls_from_result(result: dict) -> list[dict]:
    """Extract tool calls from a sub-agent result's messages array."""
    tool_calls = []
    for msg in result.get("messages", []):
        if msg.get("role") == "assistant":
            for c in msg.get("content", []):
                if c.get("type") == "toolCall" and c.get("name") != "subagent":
                    tc_name = c.get("name", "")
                    tc_args = c.get("arguments", {})
                    arg_desc = ""
                    if isinstance(tc_args, dict):
                        for key in ("command", "prompt", "path", "query", "questions", "url"):
                            if key in tc_args:
                                arg_desc = str(tc_args[key])[:120]
                                break
                        if not arg_desc:
                            arg_desc = json.dumps(tc_args, ensure_ascii=False)[:120]
                    tool_calls.append({"name": tc_name, "args": arg_desc})
    return tool_calls


def _extract_receipt_status(subagent_tool_calls: dict, tool_call_id: str, result: dict, is_error: bool) -> None:
    """Extract receipt status from tool_execution_end result."""
    if not tool_call_id or tool_call_id not in subagent_tool_calls:
        return
    text_content = ""
    for c in result.get("content", []):
        if isinstance(c, dict) and c.get("type") == "text":
            text_content = c.get("text", "")
            break
    _extract_receipt_status_from_text(subagent_tool_calls, tool_call_id, text_content, is_error)


def _extract_receipt_status_from_text(subagent_tool_calls: dict, tool_call_id: str, text: str, is_error: bool) -> None:
    """Extract status and summary from receipt text."""
    if not tool_call_id or not text:
        return
    subagent_tool_calls.setdefault(tool_call_id, {})
    subagent_tool_calls[tool_call_id]["isError"] = is_error

    if "PI_SUBAGENT_FAILURE_V1" in text:
        subagent_tool_calls[tool_call_id]["status"] = "failed"
        json_start = text.find("{")
        if json_start >= 0:
            try:
                failure = json.loads(text[json_start:])
                subagent_tool_calls[tool_call_id]["summary"] = failure.get("error", "") or failure.get("cause", "")
            except json.JSONDecodeError:
                pass
        return

    if "PI_SUBAGENT_RECEIPT_V1" in text:
        json_start = text.find("{")
        if json_start >= 0:
            try:
                receipt = json.loads(text[json_start:])
                subagent_tool_calls[tool_call_id]["status"] = receipt.get("status", "completed")
                subagent_tool_calls[tool_call_id]["summary"] = receipt.get("summary", "")
            except json.JSONDecodeError:
                pass


def _attach_subagent_details(messages: list[dict], subagent_tool_calls: dict[str, dict]) -> None:
    """Attach extracted sub-agent tool call details to subagent toolCall entries in messages."""
    for msg in messages:
        if msg.get("role") == "assistant":
            for c in msg.get("content", []):
                if c.get("type") == "toolCall" and c.get("name") == "subagent":
                    tc_id = c.get("id", "")
                    if tc_id and tc_id in subagent_tool_calls:
                        details = subagent_tool_calls[tc_id]
                        c["_toolCalls"] = details.get("toolCalls", [])
                        c["_status"] = details.get("status")
                        c["_summary"] = details.get("summary")
                        c["_isError"] = details.get("isError", False)


# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sessions")
async def list_sessions():
    """List all session files for the active account's work directory.

    Scans ~/.pi/agent/sessions/ for .jsonl files, reads each header to get the cwd,
    and filters to only those matching the current account's work_dir.
    Returns sessions sorted by timestamp descending with message count and first user message preview.
    """
    # Compute work_dir from account even if pi hasn't spawned yet
    account_label = pi.account or (pi.work_dir.name if pi.work_dir else None)
    if not account_label:
        return {"sessions": []}

    work_dir = pi.project_root / "sessions" / account_label

    pi_sessions_dir = Path.home() / ".pi" / "agent" / "sessions"
    if not pi_sessions_dir.exists():
        return {"sessions": []}

    work_dir_resolved = work_dir.resolve()
    sessions = []

    for jsonl_file in pi_sessions_dir.rglob("*.jsonl"):
        try:
            content = jsonl_file.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            if not content:
                continue

            header = json.loads(content[0])
            if header.get("type") != "session":
                continue

            # Filter: only sessions whose cwd matches our work directory
            session_cwd = Path(header.get("cwd", ""))
            try:
                session_cwd = session_cwd.resolve()
            except Exception:
                continue

            if session_cwd != work_dir_resolved:
                continue

            # Count messages and find first user message text
            message_count = 0
            first_message = ""
            for line in content[1:]:
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "message":
                        msg = entry.get("message", {})
                        if msg.get("role") == "user":
                            message_count += 1
                            if not first_message:
                                for c in msg.get("content", []):
                                    if c.get("type") == "text" and c.get("text"):
                                        first_message = c["text"][:200]
                                        break
                        elif msg.get("role") == "assistant":
                            message_count += 1
                except (json.JSONDecodeError, KeyError):
                    continue

            sessions.append({
                "id": header.get("id", ""),
                "timestamp": header.get("timestamp", ""),
                "cwd": str(session_cwd),
                "path": str(jsonl_file),
                "messageCount": message_count,
                "firstMessage": first_message,
            })
        except Exception as e:
            log.warning("Failed to read session %s: %s", jsonl_file, e)
            continue

    # Sort by timestamp descending (newest first)
    sessions.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
    return {"sessions": sessions}


@app.get("/api/sessions/preview")
async def session_preview(session_path: str):
    """Preview a session file — returns header, first user message, and message count."""
    try:
        session_file = Path(session_path)
        if not session_file.exists():
            return {"error": "Session file not found"}

        content = session_file.read_text(encoding="utf-8", errors="replace").strip().split("\n")
        if not content:
            return {"error": "Empty session file"}

        header = json.loads(content[0])

        # Find first user message and count total messages
        first_message = None
        message_count = 0
        for line in content[1:]:
            try:
                entry = json.loads(line)
                if entry.get("type") == "message":
                    msg = entry.get("message", {})
                    role = msg.get("role")
                    if role in ("user", "assistant"):
                        message_count += 1
                    if role == "user" and first_message is None:
                        for c in msg.get("content", []):
                            if c.get("type") == "text" and c.get("text"):
                                first_message = c["text"][:500]
                                break
            except (json.JSONDecodeError, KeyError):
                continue

        return {
            "header": header,
            "firstMessage": first_message or "",
            "messageCount": message_count,
        }
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in session file: {e}"}
    except Exception as e:
        log.error("Preview error for %s: %s", session_path, e)
        return {"error": str(e)}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    pi.ws = websocket

    # Dev-mode: ?session=PATH loads a JSONL file directly (no subprocess)
    if DEV_MODE:
        session_param = websocket.query_params.get("session")
        if session_param:
            log.info("[DEV] Loading session from %s", session_param)
            messages = parse_jsonl_messages(session_param)
            if messages:
                await websocket.send_json({
                    "type": "session_loaded",
                    "messages": messages,
                    "sessionId": "dev",
                    "messageCount": len(messages),
                })
                # In dev mode with session param, don't spawn pi — just serve the static render
                # Still allow the client to send set_account and other commands
            else:
                log.warning("[DEV] Failed to parse session file: %s", session_param)
                await websocket.send_json({
                    "type": "error",
                    "message": f"Failed to parse session file: {session_param}",
                })

    # Don't spawn yet — wait for set_account + first prompt.

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            cmd_type = msg.get("type")

            if cmd_type == "set_account":
                pi.account = msg.get("account")
                log.info("Account set to: %s", pi.account)
                continue

            if cmd_type == "prompt":
                # Spawn on first prompt (set_account already processed)
                if not pi.proc:
                    await pi.spawn()
                    await websocket.send_json({"type": "session_started", "sessionId": pi.session_id})
                payload = {"type": "prompt", "message": msg["message"]}
                if "images" in msg:
                    # Pi expects images as { type: "image", data: base64, mimeType: "..." }
                    # (used directly in openai-completions.js to build image_url)
                    payload["images"] = [
                        {"type": "image", "data": img["data"], "mimeType": img.get("mimeType", "image/png")}
                        for img in msg["images"]
                    ]
                await pi.send(payload)

            elif cmd_type == "new_session":
                await pi.spawn()
                await websocket.send_json({"type": "session_started", "sessionId": pi.session_id})

            elif cmd_type == "abort":
                await pi.send({"type": "abort"})

            elif cmd_type == "load_session":
                # Load a previous session: switch pi to it, then fetch its messages
                session_path = msg.get("sessionPath", "")
                if not session_path:
                    await websocket.send_json({"type": "error", "message": "No session path provided"})
                    continue

                # Spawn pi if not running (reuses account-based work_dir)
                if not pi.proc:
                    await pi.spawn()
                    await websocket.send_json({"type": "session_started", "sessionId": pi.session_id})

                try:
                    # Step 1: Switch to the target session
                    switch_result = await pi._send_and_wait(
                        {"type": "switch_session", "sessionPath": session_path},
                        str(uuid.uuid4())[:8],
                        timeout=10.0,
                    )

                    if not switch_result.get("success"):
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Failed to switch session: {switch_result.get('error', 'unknown')}",
                        })
                        continue

                    cancelled = switch_result.get("data", {}).get("cancelled", False)
                    if cancelled:
                        await websocket.send_json({"type": "error", "message": "Session switch cancelled by extension"})
                        continue

                    # Step 2: Fetch all messages from the loaded session
                    messages_result = await pi._send_and_wait(
                        {"type": "get_messages"},
                        str(uuid.uuid4())[:8],
                        timeout=15.0,
                    )

                    if messages_result.get("success"):
                        messages = messages_result.get("data", {}).get("messages", [])
                        await websocket.send_json({
                            "type": "session_loaded",
                            "messages": messages,
                            "sessionId": pi.session_id,
                            "messageCount": len(messages),
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Failed to get messages: {messages_result.get('error', 'unknown')}",
                        })

                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "error", "message": "Timed out loading session"})
                except Exception as e:
                    log.error("load_session error: %s", e)
                    await websocket.send_json({"type": "error", "message": str(e)})

            elif cmd_type == "get_messages":
                # Fetch messages from the current session
                if not pi.proc:
                    await websocket.send_json({"type": "error", "message": "Pi process not running"})
                    continue

                try:
                    result = await pi._send_and_wait(
                        {"type": "get_messages"},
                        str(uuid.uuid4())[:8],
                        timeout=15.0,
                    )
                    await websocket.send_json({
                        "type": "messages_retrieved" if result.get("success") else "error",
                        "messages": result.get("data", {}).get("messages", []) if result.get("success") else [],
                        "message": result.get("error") if not result.get("success") else None,
                    })
                except Exception as e:
                    log.error("get_messages error: %s", e)
                    await websocket.send_json({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        pi.ws = None
        log.info("Client disconnected — killing pi subprocess")
        await pi.kill()
    except Exception as e:
        log.error("WebSocket error: %s", e)
        pi.ws = None
        await pi.kill()


# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main():
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=9000, reload=False)


if __name__ == "__main__":
    main()
