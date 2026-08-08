"""FastAPI application factory for pi chat."""

import asyncio
import io
import json
import logging
import os
import subprocess
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import AuthManager, COOKIE_NAME
from .config import DEV_MODE, PROJECT_ROOT, STATIC_DIR
from .process import PiProcess
from .sessions import (
    list_sessions,
    parse_jsonl_messages,
    preview_session,
    session_belongs_to_account,
)
import pdfplumber

from .config import STT_ENABLED
from .tts_service import TTSService
from .voice_store import VoiceStore, MAX_FILE_SIZE as VOICE_MAX_FILE_SIZE
from .websocket import handle_websocket

logging.basicConfig(
    level=os.environ.get("PI_CHAT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pi-chat")


class LoginCredentials(BaseModel):
    account: str
    password: str


def create_app(
    process_factory: Callable[[], PiProcess] | None = None,
    auth_manager: AuthManager | None = None,
    tts_service=None,
    stt_service=None,
) -> FastAPI:
    """Create the web app with isolated pi processes per browser connection.

    Args:
        process_factory: Factory for PiProcess (for tests).
        auth_manager: AuthManager instance (for tests).
        tts_service: TTSService instance (for tests; production creates one).
        stt_service: STTService instance (for tests; production creates one if STT_ENABLED).
    """
    make_process = process_factory or PiProcess
    auth = auth_manager or AuthManager()
    active_processes: set[PiProcess] = set()

    # Create VoiceStore (shared across all profiles)
    voice_store = VoiceStore()

    # Create or use injected TTS service
    _tts = tts_service or TTSService(voice_store=voice_store)

    # Create or use injected STT service (conditionally enabled)
    _stt = stt_service
    log.info("[STT DEBUG] STT_ENABLED=%s, stt_service=%s", STT_ENABLED, stt_service)
    if _stt is None and STT_ENABLED:
        try:
            from .stt_service import STTService
            _stt = STTService()
            log.info("[STT DEBUG] STTService created")
        except ImportError as error:
            log.warning("[STT DEBUG] STT not available (RealtimeSTT not installed): %s", error)
    elif _stt is None:
        log.info("[STT DEBUG] STT disabled (STT_ENABLED=%s)", STT_ENABLED)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        log.info(
            "Shutting down — killing %d pi subprocess(es)",
            len(active_processes),
        )
        await asyncio.gather(
            *(pi.kill() for pi in active_processes),
            return_exceptions=True,
        )
        # Close STT service
        if _stt is not None:
            try:
                await _stt.close()
            except Exception as error:
                log.warning("STT close error during shutdown: %s", error)
        # Close TTS service last (after all processes are killed)
        try:
            await _tts.close()
        except Exception as error:
            log.warning("TTS close error during shutdown: %s", error)

    application = FastAPI(title="pi-chat", lifespan=lifespan)
    application.state.auth = auth
    application.state.active_processes = active_processes
    application.state.tts = _tts
    application.state.stt = _stt

    @application.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @application.post("/api/login")
    async def login(
        credentials: LoginCredentials,
        request: Request,
        response: Response,
    ):
        token = auth.login(credentials.account, credentials.password)
        if not token:
            raise HTTPException(
                status_code=401,
                detail="Incorrect profile or password",
            )

        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=auth.session_ttl_seconds,
            httponly=True,
            secure=_request_is_https(request),
            samesite="strict",
            path="/",
        )
        return {"account": credentials.account}

    @application.post("/api/logout")
    async def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(COOKIE_NAME))
        response.delete_cookie(
            key=COOKIE_NAME,
            path="/",
            samesite="strict",
        )
        return {"success": True}

    @application.get("/api/me")
    async def get_current_account(request: Request):
        return {"account": _require_account(request, auth)}

    @application.get("/api/sessions")
    async def get_sessions(request: Request):
        account = _require_account(request, auth)
        return {
            "sessions": list_sessions(
                project_root=PROJECT_ROOT,
                account_label=account,
            )
        }

    @application.get("/api/sessions/preview")
    async def get_session_preview(request: Request, session_path: str):
        account = _require_account(request, auth)
        if not session_belongs_to_account(
            session_path,
            PROJECT_ROOT,
            account,
        ):
            raise HTTPException(status_code=404, detail="Session not found")
        return preview_session(session_path)

    @application.post("/api/upload-file")
    async def upload_file(request: Request, file: UploadFile):
        _require_account(request, auth)

        filename = file.filename or ""
        if not filename:
            raise HTTPException(status_code=400, detail="No file provided")

        # Validate file extension
        ext = Path(filename).suffix.lower()
        if ext not in (".pdf", ".txt"):
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {ext}. Only .pdf and .txt are allowed.",
            )

        # Read file content and check size
        content_bytes = await file.read()
        max_size = 5 * 1024 * 1024  # 5MB
        if len(content_bytes) > max_size:
            raise HTTPException(
                status_code=413,
                detail="File too large. Maximum size is 5MB.",
            )

        # Extract text based on file type
        if ext == ".txt":
            try:
                text = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="File is not valid UTF-8 text.",
                )
            mime_type = "text/plain"
        else:  # .pdf
            try:
                text = ""
                with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
                    for page in pdf.pages:
                        page_text = page.extract_text()
                        if page_text:
                            text += page_text + "\n"
            except Exception as error:
                log.error("PDF extraction failed: %s", error)
                raise HTTPException(
                    status_code=400,
                    detail="Failed to extract text from PDF.",
                )
            mime_type = "application/pdf"

        return {
            "filename": filename,
            "content": text,
            "mimeType": mime_type,
        }

    # ------------------------------------------------------------------
    # Custom voice API routes
    # ------------------------------------------------------------------

    @application.post("/api/voices")
    async def upload_voice(request: Request, file: UploadFile, display_name: str | None = Form(None)):
        """Upload a custom voice sample."""
        _require_account(request, auth)

        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided")

        # (CRIT-4) Check Content-Length before reading
        content_length = file.size if hasattr(file, 'size') else None
        if content_length and content_length > VOICE_MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail="File too large. Maximum is 10MB."
            )

        content = await file.read()

        # (HIGH-15) Run upload in thread pool to avoid blocking event loop
        loop = asyncio.get_running_loop()
        try:
            sample = await loop.run_in_executor(
                None,
                lambda: voice_store.upload(content, file.filename, display_name)
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        return {
            "voiceId": sample.voice_id,
            "filename": sample.filename,
            "displayName": sample.display_name,
            "duration": round(sample.duration, 1),
        }

    @application.get("/api/voices")
    async def list_voices(request: Request):
        """List available custom voices."""
        _require_account(request, auth)
        samples = voice_store.list_voices()
        return {
            "voices": [
                {
                    "voiceId": s.voice_id,
                    "filename": s.filename,
                    "displayName": s.display_name,
                    "duration": round(s.duration, 1),
                    "uploadTime": int(s.upload_time),
                }
                for s in samples
            ]
        }

    @application.patch("/api/voices/{voice_id}")
    async def update_voice(request: Request, voice_id: str):
        """Update a voice sample's metadata (display name)."""
        _require_account(request, auth)

        # (HIGH-7) Validate JSON body properly
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        display_name = body.get("displayName")
        if not isinstance(display_name, str) or not display_name:
            raise HTTPException(status_code=400, detail="displayName must be a non-empty string")

        if not voice_store.get_voice(voice_id):
            raise HTTPException(status_code=404, detail="Voice not found")

        voice_store.update_display_name(voice_id, display_name)
        return {"success": True}

    @application.delete("/api/voices/{voice_id}")
    async def delete_voice(request: Request, voice_id: str):
        """Delete a custom voice sample."""
        _require_account(request, auth)
        if not voice_store.delete_voice(voice_id):
            raise HTTPException(status_code=404, detail="Voice not found")
        # Invalidate from TTS cache (T34/MED-8)
        _tts.invalidate_voice(voice_id)
        return {"success": True}

    @application.get("/api/voices/{voice_id}/audio")
    async def get_voice_audio(request: Request, voice_id: str):
        """Get the audio file for a voice sample (for preview playback)."""
        _require_account(request, auth)

        sample = voice_store.get_voice(voice_id)
        if not sample:
            raise HTTPException(status_code=404, detail="Voice not found")

        wav_path = voice_store._wav_path(voice_id)
        if not wav_path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found")

        return FileResponse(wav_path, media_type="audio/wav")

    @application.get("/api/debug/session")
    async def debug_session(
        request: Request,
        session_path: str = Query(..., description="Absolute path to session JSONL"),
    ):
        """Render a session through the full Python→JS pipeline for debugging."""
        account = _require_account(request, auth)
        if not DEV_MODE and not session_belongs_to_account(
            session_path,
            PROJECT_ROOT,
            account,
        ):
            raise HTTPException(status_code=404, detail="Session not found")

        messages = parse_jsonl_messages(session_path)
        if messages is None:
            raise HTTPException(status_code=400, detail="Failed to parse session file")

        render_result = subprocess.run(
            ["node", str(PROJECT_ROOT / "tools" / "render_message.js")],
            input=json.dumps({"mode": "history", "messages": messages}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if render_result.returncode != 0:
            log.error("Debug render failed: %s", render_result.stderr)
            raise HTTPException(
                status_code=500, detail="Failed to render session"
            )

        rendered = json.loads(render_result.stdout)
        return {
            "messageCount": len(messages),
            "messages": messages,
            "assistantHtml": rendered.get("assistantHtml", []),
        }

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        account = auth.account_for_token(
            websocket.cookies.get(COOKIE_NAME)
        )
        if not account:
            await websocket.accept()
            await websocket.close(code=4401)
            return

        pi = make_process()
        pi.account = account
        active_processes.add(pi)
        try:
            await handle_websocket(websocket, pi, account, tts_service=_tts, stt_service=_stt)
        finally:
            active_processes.discard(pi)

    application.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )
    return application


def _require_account(request: Request, auth: AuthManager) -> str:
    account = auth.account_for_token(request.cookies.get(COOKIE_NAME))
    if not account:
        raise HTTPException(status_code=401, detail="Authentication required")
    return account


def _request_is_https(request: Request) -> bool:
    forwarded_protocol = request.headers.get(
        "x-forwarded-proto",
        "",
    ).split(",", 1)[0].strip()
    return request.url.scheme == "https" or forwarded_protocol == "https"


app = create_app()
