"""WebSocket command handling between the browser and pi RPC."""

import asyncio
import json
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from .config import DEV_MODE
from .process import PiProcess
from .sessions import parse_jsonl_messages, session_belongs_to_account
from .stt_session import AudioPacketError, STTSession, decode_audio_packet
from .voice_session import VoiceSession

log = logging.getLogger("pi-chat")


def _make_voice_session(tts_service, pi, config_):
    """Factory for VoiceSession to allow test injection."""
    return VoiceSession(
        tts_service=tts_service,
        send_json=pi.send_browser_json,
        send_bytes=pi.send_browser_bytes,
        config_=config_,
    )


def _make_stt_session(stt_service, pi, config_):
    """Factory for STTSession to allow test injection."""
    return STTSession(
        stt_service=stt_service,
        send_json=pi.send_browser_json,
        config_=config_,
    )


async def handle_websocket(
    websocket: WebSocket,
    pi: PiProcess,
    account: str,
    tts_service=None,
    stt_service=None,
    config_=None,
    voice_session_factory=None,
) -> None:
    """Run one browser connection until it disconnects."""
    await websocket.accept()
    pi.ws = websocket
    pi.account = account

    # Create voice session if TTS service is available
    voice = None
    if tts_service is not None:
        factory = voice_session_factory or _make_voice_session
        voice = factory(tts_service, pi, config_)
        pi.event_observer = voice.observe_pi_event

    # Create STT session if STT service is available
    stt = None
    log.info("[STT DEBUG] handle_websocket: stt_service=%s", stt_service)
    if stt_service is not None:
        stt = _make_stt_session(stt_service, pi, config_)
        log.info("[STT DEBUG] STTSession created: %s", stt)

    # Wire STT to Voice for AEC reference audio (if both available)
    if stt is not None and voice is not None:
        stt.set_voice_session(voice)

    await _load_dev_session(websocket)

    try:
        while True:
            raw = await websocket.receive()
            if "bytes" in raw and raw["bytes"] is not None:
                # Binary audio packet for STT
                log.debug("[STT DEBUG] received binary audio packet, stt=%s, enabled=%s",
                         stt, stt.enabled if stt else None)
                if stt is not None and stt.enabled:
                    try:
                        packet = decode_audio_packet(raw["bytes"])
                        stt.ingest_audio_packet(packet)
                    except AudioPacketError as e:
                        log.warning("[STT DEBUG] AudioPacketError: %s", e)
                        await websocket.send_json({
                            "type": "stt_error",
                            "code": "invalid_packet",
                            "message": str(e),
                            "recoverable": True,
                        })
            else:
                # Text command
                message = await _parse_text_command(raw)
                if message is not None:
                    await _dispatch_command(websocket, pi, account, message, voice, stt)
    except WebSocketDisconnect:
        log.info("Client disconnected — killing pi subprocess")
    except Exception as error:
        log.error("WebSocket error: %s", error)
    finally:
        pi.event_observer = None
        if voice is not None:
            await voice.close()
        if stt is not None:
            await stt.close()
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


async def _parse_text_command(raw) -> dict | None:
    """Parse a text WebSocket message as a JSON command."""
    if "text" not in raw or raw["text"] is None:
        return None
    try:
        return json.loads(raw["text"])
    except json.JSONDecodeError:
        return None


async def _dispatch_command(
    websocket: WebSocket,
    pi: PiProcess,
    account: str,
    message: dict,
    voice=None,
    stt=None,
) -> None:
    command_type = message.get("type")

    if command_type == "prompt":
        await _handle_prompt(websocket, pi, message)
    elif command_type == "new_session":
        if voice is not None:
            await voice.stop_current(reason="superseded")
        await _start_new_session(websocket, pi)
    elif command_type == "abort":
        if voice is not None:
            await voice.stop_current(reason="stopped")
        await pi.send({"type": "abort"})
    elif command_type == "ping":
        await pi.send_browser_json({"type": "pong"})
    elif command_type == "load_session":
        if voice is not None:
            await voice.stop_current(reason="superseded")
        await _load_session(
            websocket,
            pi,
            account,
            message.get("sessionPath", ""),
        )
    elif command_type == "get_messages":
        await _get_messages(websocket, pi)
    # Voice commands
    elif command_type == "voice_enable":
        if voice is not None:
            await voice.enable(message.get("settings", {}))
    elif command_type == "voice_disable":
        if voice is not None:
            await voice.disable()
    elif command_type == "voice_settings":
        if voice is not None:
            await voice.update_settings(message.get("settings", {}))
    elif command_type == "voice_stop":
        if voice is not None:
            await voice.stop_current(reason="stopped")
    elif command_type == "voice_prepare":
        if voice is not None:
            await _handle_voice_prepare(websocket, voice, message.get("settings", {}))
    # STT commands
    elif command_type == "stt_enable":
        log.info("[STT DEBUG] stt_enable command received, stt=%s", stt)
        if stt is not None:
            log.info("[STT DEBUG] calling stt.enable()")
            await stt.enable(message.get("settings"))
        else:
            log.warning("[STT DEBUG] stt is None, sending error")
            await websocket.send_json({
                "type": "stt_error",
                "code": "stt_not_configured",
                "message": "STT is not configured",
                "recoverable": False,
            })
    elif command_type == "stt_disable":
        log.info("[STT DEBUG] stt_disable command received, stt=%s", stt)
        if stt is not None:
            log.info("[STT DEBUG] calling stt.disable()")
            await stt.disable()


async def _handle_voice_prepare(websocket: WebSocket, voice, settings: dict) -> None:
    """Prepare voice with timeout protection (CRIT-8)."""
    try:
        await asyncio.wait_for(voice.prepare(settings), timeout=60.0)
    except asyncio.TimeoutError:
        log.error("Voice preparation timed out")
        await _send_error(
            websocket,
            "Voice preparation timed out. Try again or use a shorter sample.",
        )


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
    await pi.send_browser_json(
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
        await pi.send_browser_json(
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

        await pi.send_browser_json(
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
        await pi.send_browser_json(
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
