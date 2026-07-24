"""WebSocket command handling between the browser and pi RPC."""

import asyncio
import json
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from .config import DEV_MODE
from .process import PiProcess
from .sessions import parse_jsonl_messages, session_belongs_to_account

log = logging.getLogger("pi-chat")


async def handle_websocket(
    websocket: WebSocket,
    pi: PiProcess,
    account: str,
) -> None:
    """Run one browser connection until it disconnects."""
    await websocket.accept()
    pi.ws = websocket
    pi.account = account
    await _load_dev_session(websocket)

    try:
        while True:
            message = await _receive_command(websocket)
            if message is not None:
                await _dispatch_command(websocket, pi, account, message)
    except WebSocketDisconnect:
        log.info("Client disconnected — killing pi subprocess")
    except Exception as error:
        log.error("WebSocket error: %s", error)
    finally:
        pi.ws = None
        await pi.kill()


async def _load_dev_session(websocket: WebSocket) -> None:
    if not DEV_MODE:
        return

    session_path = websocket.query_params.get("session")
    if not session_path:
        return

    log.info("[DEV] Loading session from %s", session_path)
    messages = parse_jsonl_messages(session_path)
    if messages:
        await websocket.send_json(
            {
                "type": "session_loaded",
                "messages": messages,
                "sessionId": "dev",
                "messageCount": len(messages),
            }
        )
        return

    log.warning("[DEV] Failed to parse session file: %s", session_path)
    await _send_error(
        websocket,
        f"Failed to parse session file: {session_path}",
    )


async def _receive_command(websocket: WebSocket) -> dict | None:
    raw_message = await websocket.receive_text()
    try:
        return json.loads(raw_message)
    except json.JSONDecodeError:
        return None


async def _dispatch_command(
    websocket: WebSocket,
    pi: PiProcess,
    account: str,
    message: dict,
) -> None:
    command_type = message.get("type")

    if command_type == "prompt":
        await _handle_prompt(websocket, pi, message)
    elif command_type == "new_session":
        await _start_new_session(websocket, pi)
    elif command_type == "abort":
        await pi.send({"type": "abort"})
    elif command_type == "ping":
        await websocket.send_json({"type": "pong"})
    elif command_type == "load_session":
        await _load_session(
            websocket,
            pi,
            account,
            message.get("sessionPath", ""),
        )
    elif command_type == "get_messages":
        await _get_messages(websocket, pi)


async def _handle_prompt(
    websocket: WebSocket,
    pi: PiProcess,
    message: dict,
) -> None:
    if not pi.proc:
        await _start_session(websocket, pi)

    payload = {
        "type": "prompt",
        "message": message["message"],
    }
    if "images" in message:
        payload["images"] = [
            {
                "type": "image",
                "data": image["data"],
                "mimeType": image.get("mimeType", "image/png"),
            }
            for image in message["images"]
        ]
    await pi.send(payload)


async def _start_session(websocket: WebSocket, pi: PiProcess) -> None:
    await pi.spawn()
    await websocket.send_json(
        {
            "type": "session_started",
            "sessionId": pi.session_id,
        }
    )


async def _start_new_session(
    websocket: WebSocket,
    pi: PiProcess,
) -> None:
    """Start a clean session without replacing an existing RPC process."""
    if not pi.proc:
        await _start_session(websocket, pi)
        return

    try:
        result = await pi.send_and_wait(
            {"type": "new_session"},
            _request_id(),
            timeout=15.0,
        )
        if result.get("data", {}).get("cancelled", False):
            await _send_error(
                websocket,
                "New session cancelled by extension",
            )
            return

        pi.session_id = _request_id()
        await websocket.send_json(
            {
                "type": "session_started",
                "sessionId": pi.session_id,
            }
        )
    except Exception as error:
        log.error("new_session error: %s", error)
        await _send_error(websocket, str(error))


async def _load_session(
    websocket: WebSocket,
    pi: PiProcess,
    account: str,
    session_path: str,
) -> None:
    if not session_path:
        await _send_error(websocket, "No session path provided")
        return
    if not session_belongs_to_account(
        session_path,
        pi.project_root,
        account,
    ):
        await _send_error(websocket, "Session not found")
        return

    if not pi.proc:
        await _start_session(websocket, pi)

    try:
        switch_result = await pi.send_and_wait(
            {
                "type": "switch_session",
                "sessionPath": session_path,
            },
            _request_id(),
            timeout=10.0,
        )

        if not switch_result.get("success"):
            await _send_error(
                websocket,
                f"Failed to switch session: {switch_result.get('error', 'unknown')}",
            )
            return

        if switch_result.get("data", {}).get("cancelled", False):
            await _send_error(
                websocket,
                "Session switch cancelled by extension",
            )
            return

        messages_result = await pi.send_and_wait(
            {"type": "get_messages"},
            _request_id(),
            timeout=15.0,
        )
        if not messages_result.get("success"):
            await _send_error(
                websocket,
                f"Failed to get messages: {messages_result.get('error', 'unknown')}",
            )
            return

        # Use enriched messages from sessions.py (has sub-agent timeline data)
        # rather than raw messages from pi RPC
        messages = parse_jsonl_messages(session_path)
        if not messages:
            # Fallback to raw messages from pi if parsing fails
            messages = messages_result.get("data", {}).get("messages", [])

        await websocket.send_json(
            {
                "type": "session_loaded",
                "messages": messages,
                "sessionId": pi.session_id,
                "messageCount": len(messages),
            }
        )
    except asyncio.TimeoutError:
        await _send_error(websocket, "Timed out loading session")
    except Exception as error:
        log.error("load_session error: %s", error)
        await _send_error(websocket, str(error))


async def _get_messages(websocket: WebSocket, pi: PiProcess) -> None:
    if not pi.proc:
        await _send_error(websocket, "Pi process not running")
        return

    try:
        result = await pi.send_and_wait(
            {"type": "get_messages"},
            _request_id(),
            timeout=15.0,
        )
        success = result.get("success")
        await websocket.send_json(
            {
                "type": "messages_retrieved" if success else "error",
                "messages": (
                    result.get("data", {}).get("messages", [])
                    if success
                    else []
                ),
                "message": result.get("error") if not success else None,
            }
        )
    except Exception as error:
        log.error("get_messages error: %s", error)
        await _send_error(websocket, str(error))


async def _send_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_json({"type": "error", "message": message})


def _request_id() -> str:
    return str(uuid.uuid4())[:8]
