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
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import preview
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
from . import config as app_config
from . import phonon_voices
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
    log.debug("Speech input startup state: STT_ENABLED=%s injected=%s", STT_ENABLED, stt_service)
    if _stt is None and STT_ENABLED:
        try:
            from .stt_service import STTService
            _stt = STTService()
            log.debug("Speech input service created")
        except ImportError as error:
            # The one state where the microphone is on the screen and cannot possibly work: speech input is
            # enabled by default, the package is not installed, and nothing else on the page says so. This line
            # used to carry the prefix of an old debugging session, which is how a warning gets skimmed.
            log.warning(
                "Speech input is enabled but RealtimeSTT is not installed, so the microphone will not start. "
                "Install it with: uv sync --extra stt (%s)",
                error,
            )
    elif _stt is None:
        log.debug("Speech input disabled (PI_CHAT_STT_ENABLED is off)")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Documents accumulate as conversations produce them, and nothing else ever deletes them.
        # Expired scopes and an over-cap store are reaped here rather than by a scheduled task, so
        # a long-lived server is no worse off than a restarted one.
        try:
            preview.cleanup()
        except OSError as error:
            log.warning("Preview cleanup failed: %s", error)
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

    # Content Security Policy. script-src is strict and there is no inline script anywhere in
    # the frontend; the assistant-output path is covered by DOMPurify instead (SEC-01). styles
    # keeps 'unsafe-inline' because index.html carries a handful of style attributes and the
    # header's job here is script execution, not stylesheet hygiene. connect-src allows the
    # ws:// upgrade the client uses on the tailnet.
    content_security_policy = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' data: blob:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", content_security_policy)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # The modules are served by name, not by content hash, so `index.html` and every `.js` next to
        # it need revalidation. Without `Cache-Control` Chrome falls back to heuristic freshness from
        # `Last-Modified` and happily runs a bundle from before the last edit; on 2026-11-09 that made a
        # fixed app look broken while its own files on disk were correct. `no-cache` still gets a 304 on
        # the etag, so the cost is one conditional request, not a re-download.
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

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
        """Log in a profile.

        Answers 429 while that profile is inside its failed-attempt window (see `auth.retry_after`),
        so a brute-force run is handed a retry time instead of a fresh password hash per request. The
        profile, not the client address, is the unit being limited: there is no proxy in front of this
        server to make an address meaningful, and it is reached both over the tailnet and over a
        tunnel.
        """
        retry_after = auth.retry_after(credentials.account)
        if retry_after > 0:
            raise HTTPException(
                status_code=429,
                detail="Too many failed attempts. Try again in a few minutes.",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

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
        # Threaded on purpose. Cached or not, this stats every session file of the account and, on the
        # first call after boot, reads the whole tree. Awaiting it here stalled the WebSocket of anyone
        # who was streaming while the sidebar refreshed, because the event loop had nothing else to run.
        sessions = await run_in_threadpool(
            list_sessions,
            project_root=PROJECT_ROOT,
            account_label=account,
        )
        return {"sessions": sessions}

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

        # Read file content, refusing it the moment it goes over rather than after buffering it all
        content_bytes = await _read_capped(
            file,
            MAX_DOCUMENT_SIZE,
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

    @application.get("/api/preview/{doc_id}")
    async def preview_document(
        request: Request,
        doc_id: str,
        path: str | None = Query(
            None,
            description="Absolute path of an existing file to preview instead of a stored document",
        ),
        kind: str | None = Query(
            None,
            description="Kind the pane expects (markdown|html|pdf); a mismatch is a 404",
        ),
    ):
        """Serve one preview-pane document: markdown, HTML, or PDF.

        The store is scoped per profile, so a doc id from another profile's session is a 404. A
        ``path`` request is additionally confined to the roots in ``preview.allowed_roots``, and
        every denial is the same 404: this endpoint must not confirm which paths exist.

        ``kind`` is a contract, not a hint. The renderer and the CSP are chosen from the kind, so
        being handed a different kind than was asked for would put, say, an HTML document through
        the markdown path. Anything that disagrees is refused.
        """
        account = _require_account(request, auth)
        if kind is not None and kind not in preview.KIND_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Unsupported preview kind")
        try:
            target = (
                preview.resolve_external_path(path, account)
                if path
                else preview.find_stored_document(account, doc_id, kind)
            )
        except preview.PreviewError:
            raise HTTPException(status_code=400, detail="Invalid preview request")

        if target is None:
            raise HTTPException(status_code=404, detail="Preview not found")

        media_type = preview.content_type_for(target)
        if media_type is None or (kind is not None and preview.kind_for(target) != kind):
            raise HTTPException(status_code=404, detail="Preview not found")

        return FileResponse(
            target,
            media_type=media_type,
            # Inline so the pane frames it, with a filename so "save" from the browser keeps a
            # usable name instead of "preview", "x", or the doc id alone.
            content_disposition_type="inline",
            filename=target.name,
            headers=preview.document_headers(target),
        )

    # ------------------------------------------------------------------
    # Custom voice API routes
    # ------------------------------------------------------------------

    @application.get("/api/tts/capabilities")
    async def tts_capabilities(request: Request):
        """What the voice settings dialog can offer for the engine that is actually running.

        The engine decides the shape of a voice: OmniVoice designs one from gender/age/pitch/
        accent/style/speed, or clones uploaded audio; Phonon picks a named catalog entry and
        one of five rate steps. The dialog asks instead of guessing, so a Phonon install does
        not render a wall of controls that would be silently ignored.
        """
        _require_account(request, auth)
        engine = app_config.resolve_tts_engine(app_config)
        payload: dict = {
            "engine": engine,
            "design": engine == "omnivoice",
            "customVoices": engine == "omnivoice" and bool(app_config.TTS_CUSTOM_VOICES_ENABLED),
            "rates": [],
            "voices": [],
            "defaultVoice": None,
            "defaultRate": None,
        }
        if engine == "phonon":
            measured = getattr(app_config, "TTS_PHONON_RATE_MEASURED", {})
            payload["rates"] = [
                {"label": label, "multiplier": measured.get(label)}
                for label in app_config.TTS_PHONON_RATE_LABELS
            ]
            payload["defaultRate"] = app_config.TTS_PHONON_RATE
            payload["defaultVoice"] = app_config.TTS_PHONON_VOICE
            # A snapshot read, not a Hub call, unless PI_CHAT_TTS_PHONON_LIVE_CATALOG=1.
            catalog = await run_in_threadpool(phonon_voices.catalog)
            payload["voices"] = [
                {
                    "name": v.get("name"),
                    "voiceId": v.get("voice_id"),
                    "language": v.get("language"),
                    "locale": v.get("locale"),
                    "gender": v.get("gender"),
                    "age": v.get("age"),
                    "description": v.get("description"),
                }
                for v in catalog
            ]
        return payload

    @application.post("/api/voices")
    async def upload_voice(request: Request, file: UploadFile, display_name: str | None = Form(None)):
        """Upload a custom voice sample."""
        _require_account(request, auth)

        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided")

        # Read it while enforcing the cap. The Content-Length check this replaced was a hint from the
        # client, and the store re-checks the decoded size anyway; neither buffered the body safely.
        content = await _read_capped(
            file,
            VOICE_MAX_FILE_SIZE,
            status_code=400,
            detail="File too large. Maximum is 10MB.",
        )

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

        wav_path = voice_store.audio_path(voice_id)
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

        # node renders a whole session in one subprocess, which can take seconds. Off the event loop:
        # this route shares it with every chat websocket, and `subprocess.run` here stalls input for
        # every profile while it waits.
        try:
            render_result = await run_in_threadpool(
                lambda: subprocess.run(
                    ["node", str(PROJECT_ROOT / "tools" / "render_message.js")],
                    input=json.dumps({"mode": "history", "messages": messages}),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            )
        except subprocess.TimeoutExpired:
            log.error("Debug render timed out after 30s for %s", session_path)
            raise HTTPException(
                status_code=500, detail="Failed to render session: node timed out"
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


# Documents are read into memory to extract text, so the size cap has to be enforced while reading.
MAX_DOCUMENT_SIZE = 5 * 1024 * 1024
_UPLOAD_CHUNK = 64 * 1024


async def _read_capped(
    upload: UploadFile,
    limit: int,
    *,
    status_code: int = 413,
    detail: str = "File too large.",
) -> bytes:
    """Read an upload, stopping as soon as it is clear it is too big.

    Both upload routes used to `await file.read()` and only then compare `len()` against the limit,
    which meant the limit described the response and not the memory: a two-gigabyte body was fully
    buffered before the request was refused. A Content-Length check is not a substitute either - that
    header is what the client says it sent, and it says nothing about the body that follows.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=status_code, detail=detail)
        chunks.append(chunk)
    return b"".join(chunks)


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
