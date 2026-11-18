"""WebSocket command handling between the browser and pi RPC."""

import asyncio
import json
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from .config import DEV_MODE
from .process import PiProcess, parse_queue_snapshot
from .preview import describe_preview_entries
from .sessions import (
    parse_jsonl_messages,
    parse_jsonl_preview_entries,
    resolve_session_path,
    session_belongs_to_account,
)
from .stt_session import AudioPacketError, STTSession, decode_audio_packet
from .voice_session import VoiceSession

log = logging.getLogger("pi-chat")

# Browser delivery word to pi's streamingBehavior value. "queue" is the default: the message waits
# for the run to finish. "now" is the send-now arrow: pi folds it in after the current turn's tool
# calls, which is the only point during a run at which a message can still be delivered.
_DELIVERY_BEHAVIOURS = {"queue": "followUp", "now": "steer"}


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
    if stt_service is not None:
        stt = _make_stt_session(stt_service, pi, config_)

    # Wire STT to Voice for AEC reference audio (if both available)
    if stt is not None and voice is not None:
        stt.set_voice_session(voice)

    await _load_dev_session(websocket, pi)

    try:
        while True:
            raw = await websocket.receive()
            if "bytes" in raw and raw["bytes"] is not None:
                # Binary audio packet for STT
                if stt is not None and stt.enabled:
                    try:
                        packet = decode_audio_packet(raw["bytes"])
                        stt.ingest_audio_packet(packet)
                    except AudioPacketError as e:
                        log.warning("Rejected an audio packet: %s", e)
                        await pi.send_browser_json({
                            "type": "stt_error",
                            "code": "invalid_packet",
                            "message": str(e),
                            "recoverable": True,
                        })
            else:
                # Text command
                message = await _parse_text_command(raw)
                if message is not None:
                    await _dispatch_command(pi, account, message, voice, stt)
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


async def _load_dev_session(websocket: WebSocket, pi: PiProcess) -> None:
    if not DEV_MODE:
        return

    session_path = websocket.query_params.get("session")
    if not session_path:
        return

    # DEV_MODE skips the per-profile isolation elsewhere, which is exactly why this read still gets
    # the containment check: `?session=` used to be handed straight to the parser, and it is the only
    # path in the app that reads a caller-supplied file.
    if not session_belongs_to_account(session_path, pi.project_root, pi.account or ""):
        log.warning("[DEV] Refusing an out-of-scope session path: %s", session_path)
        await _send_error(pi, "Session not found")
        return

    log.info("[DEV] Loading session from %s", session_path)
    messages = parse_jsonl_messages(session_path)
    if messages:
        await pi.send_browser_json(
            {
                "type": "session_loaded",
                "messages": messages,
                "sessionId": "dev",
                "messageCount": len(messages),
            }
        )
        return

    log.warning("[DEV] Failed to parse session file: %s", session_path)
    await _send_error(pi,
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
    pi: PiProcess,
    account: str,
    message: dict,
    voice=None,
    stt=None,
) -> None:
    command_type = message.get("type")

    # A malformed or failing command must not kill the connection (the outer
    # loop would propagate the exception to handle_websocket's finally block,
    # which kills the pi subprocess). Report and keep serving the client.
    try:
        if command_type == "prompt":
            await _handle_prompt(pi, message)
        elif command_type == "steer_now":
            await _handle_queue_action(pi, message, action="steer")
        elif command_type == "retract_queued":
            await _handle_queue_action(pi, message, action="retract")
        elif command_type == "new_session":
            if voice is not None:
                await voice.stop_current(reason="superseded")
            await _start_new_session(pi)
        elif command_type == "abort":
            if voice is not None:
                await voice.stop_current(reason="stopped")
            await pi.send({"type": "abort"})
        elif command_type == "cancel_tool":
            if voice is not None:
                await voice.stop_current(reason="stopped")
            await _cancel_run(pi)
        elif command_type == "ping":
            await pi.send_browser_json({"type": "pong"})
        elif command_type == "load_session":
            if voice is not None:
                await voice.stop_current(reason="superseded")
            await _load_session(pi, account, message.get("sessionPath", ""))
        elif command_type == "get_messages":
            await _get_messages(pi)
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
        elif command_type == "voice_playback_end":
            # Browser confirmed all TTS audio for a stream has finished playing
            # (or was stopped). Closes the server-side AEC window for STT.
            if voice is not None:
                stream_id = message.get("streamId", 0)
                if isinstance(stream_id, int):
                    voice.on_playback_end(stream_id)
        elif command_type == "voice_prepare":
            if voice is not None:
                await _handle_voice_prepare(pi, voice, message.get("settings", {}))
        # STT commands
        elif command_type == "stt_enable":
            if stt is not None:
                await stt.enable(message.get("settings"))
            else:
                await pi.send_browser_json({
                    "type": "stt_error",
                    "code": "stt_not_configured",
                    "message": "STT is not configured",
                    "recoverable": False,
                })
        elif command_type == "stt_disable":
            if stt is not None:
                await stt.disable()
        else:
            log.warning("WebSocket: unknown command type %r", command_type)
    except Exception as error:
        # exc_info keeps the traceback: this used to log only the message, which is how a real defect
        # in one command became an unreadable line, and the delivery used the raw socket, which is how
        # an error could arrive before the events it was about.
        log.error("WebSocket: command %r failed: %s", command_type, error, exc_info=True)
        await _send_error(pi, f"Command failed: {error}")


async def _handle_voice_prepare(pi: PiProcess, voice, settings: dict) -> None:
    """Prepare voice with timeout protection (CRIT-8)."""
    try:
        await asyncio.wait_for(voice.prepare(settings), timeout=60.0)
    except asyncio.TimeoutError:
        log.error("Voice preparation timed out")
        await _send_error(pi,
            "Voice preparation timed out. Try again or use a shorter sample.",
        )


async def _handle_prompt(pi: PiProcess, message: dict) -> None:
    if not pi.proc:
        await _start_session(pi)

    text = message.get("message")
    if not isinstance(text, str) or not text.strip():
        await _send_error(pi, "Prompt message is required")
        return

    delivery = message.get("delivery", "queue")
    if delivery not in _DELIVERY_BEHAVIOURS:
        await _send_error(pi, f"Unknown delivery mode: {delivery!r}")
        return

    payload = {
        "type": "prompt",
        "message": text,
        # pi refuses a bare prompt while it is streaming ("Specify streamingBehavior"), and until
        # now that refusal came back as a failed prompt response which took the in-flight run's
        # timeline down with it. Declaring the behaviour on every prompt makes the refusal
        # unreachable: pi ignores the field when idle and queues to the named place when not.
        # "queue" waits for the run to end (followUp); "now" lands after the current turn's tool
        # calls, before the next model call (steer).
        "streamingBehavior": _DELIVERY_BEHAVIOURS[delivery],
    }
    if "images" in message:
        try:
            payload["images"] = [
                {
                    "type": "image",
                    "data": image["data"],
                    "mimeType": image.get("mimeType", "image/png"),
                }
                for image in message["images"]
            ]
        except (KeyError, TypeError) as e:
            await _send_error(pi, f"Invalid image payload: {e}")
            return
    await pi.send(payload)


async def _handle_queue_action(pi: PiProcess, message: dict, *, action: str) -> None:
    """Promote one queued message to steering ("steer"), or drop it ("retract").

    pi exposes no way to change the delivery of a single queued entry, so the only available
    primitive is: take the whole queue back with clear_queue, then put it back the way you want it.
    That is also the honest read of the queue - the browser's chip was rendered from an event that
    crossed the wire before this click, and clear_queue answers with what pi actually holds.
    """
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        await _send_error(pi, "A queue action needs the queued message text")
        return
    if not pi.proc:
        await _send_error(pi, "Pi process not running")
        return

    try:
        snapshot = await pi.send_and_wait({"type": "clear_queue"}, _request_id(), timeout=10.0)
    except Exception as error:
        log.error("clear_queue failed: %s", error)
        await _send_error(pi, f"Could not read the pi queue: {error}")
        return

    queues = parse_queue_snapshot(snapshot)

    kind, position = _locate_queued(queues, message.get("kind"), message.get("index"), text)
    if position is None:
        # Nothing to act on: pi delivered it, or never took it. Put back exactly what we found and
        # never resend - a message pi already consumed must not appear twice.
        await _restore_queues(pi, **queues)
        await _send_error(pi, "That message is no longer queued")
        return

    if action == "steer" and not pi.is_streaming:
        # No run to steer into. pi drains a non-empty queue as it settles, so the message still
        # goes next; restoring it keeps the order the user arranged.
        await _restore_queues(pi, **queues)
        await _send_error(pi, "Nothing is running to steer; your message stays queued")
        return

    target = queues[kind].pop(position)
    if action == "steer":
        await pi.send({"type": "steer", "message": target})
    await _restore_queues(pi, **queues)
    await pi.send_browser_json({"type": "queue_action", "action": action, "ok": True})


def _locate_queued(
    queues: dict[str, list[str]],
    kind: object,
    index: object,
    text: str,
) -> tuple[str, int | None]:
    """Find the queued entry a chip meant, tolerating a queue that already moved.

    The chip carries both its position and its text. Position wins when the text still matches it
    (duplicates then resolve in the user's favour); otherwise the first equal text anywhere in the
    named queue, then in the other one, since a message pi re-queues can change buckets.
    """
    preferred = kind if kind in ("steering", "followUp") else "followUp"
    other = "followUp" if preferred == "steering" else "steering"

    bucket = queues[preferred]
    if isinstance(index, int) and 0 <= index < len(bucket) and bucket[index] == text:
        return preferred, index
    for name in (preferred, other):
        match = next((i for i, item in enumerate(queues[name]) if item == text), None)
        if match is not None:
            return name, match
    return preferred, None


async def _restore_queues(
    pi: PiProcess,
    steering: list[str],
    followUp: list[str],
) -> None:
    """Push a queue back into pi in order, after a clear_queue took it away."""
    for item in steering:
        await pi.send({"type": "steer", "message": item})
    for item in followUp:
        await pi.send({"type": "follow_up", "message": item})


async def _start_session(pi: PiProcess) -> None:
    await pi.spawn()
    await pi.send_browser_json(
        {
            "type": "session_started",
            "sessionId": pi.session_id,
        }
    )


async def _start_new_session(pi: PiProcess) -> None:
    """Start a clean session without replacing an existing RPC process."""
    if not pi.proc:
        await _start_session(pi)
        return

    try:
        result = await pi.send_and_wait(
            {"type": "new_session"},
            _request_id(),
            timeout=15.0,
        )
        if result.get("data", {}).get("cancelled", False):
            await _send_error(pi,
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
        await _send_error(pi, str(error))


async def _cancel_run(pi: PiProcess) -> None:
    """Stop a run and whatever tool it is waiting on: pi has no per-tool cancel, so this is
    ``clear_queue`` followed by ``abort``.

    The single lever pi offers for a call in flight is the run's abort signal - enough to kill a
    20-minute bash process tree, but it ends the turn with it. Anything still sitting in the queue
    would otherwise be delivered into the wreckage, so the queue is taken back first and handed to the
    browser as text. It is never resent: a message pi already delivered must not appear twice, and
    the reader may only want part of it back.
    """
    cleared: list[str] = []
    try:
        snapshot = await pi.send_and_wait({"type": "clear_queue"}, _request_id(), timeout=10.0)
        queues = parse_queue_snapshot(snapshot)
        cleared.extend(queues["steering"])
        cleared.extend(queues["followUp"])
    except Exception as error:  # a stuck clear_queue must not cost the reader the abort itself
        log.error("cancel_tool: clear_queue failed, aborting anyway: %s", error)

    # Before the abort, not after: pi answers abort only once the session goes idle, and the very tool
    # the reader is trying to escape is what holds that open.
    await pi.send_browser_json({"type": "cancel_result", "cleared": cleared})

    try:
        await pi.send({"type": "abort"})
    except Exception as error:
        log.error("cancel_tool: abort failed: %s", error)
        await _send_error(pi, f"Could not stop the run: {error}")


async def _load_session(pi: PiProcess, account: str, session_path: str) -> None:
    if not session_path:
        await _send_error(pi, "No session path provided")
        return

    # Same containment the HTTP routes apply, for the same reason: the ownership check underneath reads a
    # `cwd` header that lives inside the file it is vetting, so the path has to be inside pi's own sessions
    # directory before anything in the file is trusted.
    session_file = resolve_session_path(session_path)
    if session_file is None:
        await _send_error(pi, "Session not found")
        return
    session_path = str(session_file)

    if not session_belongs_to_account(
        session_path,
        pi.project_root,
        account,
    ):
        await _send_error(pi, "Session not found")
        return

    # Opening a session used to take ten seconds, and the order was the whole cause. The conversation
    # lives in the JSONL on disk, but the old code spawned pi, waited for `switch_session` (which does
    # not answer until the child has booted: extensions transpiled, provider resolved, engine
    # connected), then asked the child for messages, and only then read the file it was going to show
    # anyway. The render needs nothing from the child, so it goes out first.
    messages = parse_jsonl_messages(session_path)

    # pi's `get_messages` does not return the persisted preview entries, so the pane would come back
    # empty on every reopen while the transcript still holds them. Same pass over the file, in
    # transcript order, each pointer annotated for whether its file survived cleanup.
    previews = describe_preview_entries(account, parse_jsonl_preview_entries(session_path))

    if messages is None:
        # The one case where waiting is honest: an RPC capture or a file this parser cannot read, where
        # only pi knows the history. Everything else renders now and reconciles with pi afterwards.
        if not pi.proc:
            await _start_session(pi)
        messages_result = await pi.send_and_wait(
            {"type": "get_messages"},
            _request_id(),
            timeout=15.0,
        )
        messages = (
            messages_result.get("data", {}).get("messages", [])
            if messages_result.get("success")
            else []
        )

    if not pi.proc:
        await _start_session(pi)

    await pi.send_browser_json(
        {
            "type": "session_loaded",
            "messages": messages,
            "sessionId": pi.session_id,
            "messageCount": len(messages),
            "previews": previews,
        }
    )

    try:
        # Point pi at the same conversation. Commands on this socket are handled one at a time, so a
        # prompt typed during this window is dispatched after the switch completes and cannot land in
        # the conversation that was on screen before the click.
        switch_result = await pi.send_and_wait(
            {
                "type": "switch_session",
                "sessionPath": session_path,
            },
            _request_id(),
            # The old 10 s was a boot budget. Here the child is already spawning and this is a file
            # load; failing it leaves pi answering a different session than the one displayed, which is
            # worse than waiting for it.
            timeout=30.0,
        )

        if not switch_result.get("success"):
            await _send_error(pi,
                f"Failed to switch session: {switch_result.get('error', 'unknown')}. "
                "The conversation above was read from disk; pi is still on its previous session.",
            )
            return

        if switch_result.get("data", {}).get("cancelled", False):
            await _send_error(pi,
                "Session switch cancelled by extension",
            )
            return
    except asyncio.TimeoutError:
        await _send_error(pi,
            "Timed out switching session. The conversation above was read from disk; pi is still on the previous session.",
        )
    except Exception as error:
        log.error("load_session error: %s", error)
        await _send_error(pi, str(error))


async def _get_messages(pi: PiProcess) -> None:
    if not pi.proc:
        await _send_error(pi, "Pi process not running")
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
        await _send_error(pi, str(error))


async def _send_error(pi: PiProcess, message: str) -> None:
    """Report a failure to the browser.

    This goes through PiProcess rather than the raw socket on purpose: the outbox is one queue with one
    writer, so an error cannot overtake the events it is explaining, and a failure to deliver it is
    counted and logged in the same place as a failure to deliver a pi event.
    """
    await pi.send_browser_json({"type": "error", "message": message})


def _request_id() -> str:
    """A full uuid4: 8 hex chars is 32 bits, and a collision overwrites the pending future and
    orphans the first caller until its timeout with no clue why.
    """
    return str(uuid.uuid4())
