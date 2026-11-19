"""FastAPI application factory for pi chat."""

import asyncio
import io
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import preview
from . import devtools
from .auth import AuthManager, COOKIE_NAME
from .config import DEV_MODE, PROJECT_ROOT, STATIC_DIR
from .process import PiProcess
from .sessions import (
    delete_session,
    list_sessions,
    parse_jsonl_messages,
    preview_session,
    resolve_session_path,
    session_belongs_to_account,
)
import pdfplumber

from .config import STT_ENABLED
from . import config as app_config
from . import phonon_voices
from . import ui_relay
from . import uploads
from .tts_service import TTSService
from .voice_store import VoiceStore, MAX_FILE_SIZE as VOICE_MAX_FILE_SIZE
from .websocket import handle_websocket

logging.basicConfig(
    level=os.environ.get("PI_CHAT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pi-chat")


# ---------------------------------------------------------------------------
# Preview pane state, held in memory only
# (plan: ~/.pi/agent/plans/pi-chat-preview-interactive.md, WP-3)
# ---------------------------------------------------------------------------

# How long a pane report may sit here. It only has to outlive the gap between the browser saying
# something and the model asking about it, so a connection's lifetime is generous already and the
# sweep exists purely so the dictionary cannot grow across a long-running server.
PANE_STATE_TTL_SECONDS = 60 * 60

# Keyed by the per-spawn token, never by a name a browser chose. Value is (view, last_write).
PANE_STATE: dict[str, tuple[dict, float]] = {}

# What a browser may report about its own pane, and the whole of it. The model reads these strings
# as facts about the user's screen, so an unbounded field or an invented one would be a memory leak
# and a gullibility bug at the same time.
#
# `fileToken` rather than `token` on purpose: the credential that authenticates this report is also
# in the body, and one key cannot carry both. The first version named this field `token`, and the
# report silently failed its own secret check.
PANE_STATE_FIELDS = frozenset(
    {"open", "activeDocId", "width", "watching", "revision", "fileToken"}
)


def _sweep_pane_state(now: float) -> None:
    for token, (_, written) in list(PANE_STATE.items()):
        if now - written > PANE_STATE_TTL_SECONDS:
            PANE_STATE.pop(token, None)


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
        # Attached files are the other accumulating store, and the same argument applies: nothing else
        # deletes them, and an attachment's path outlives its usefulness by weeks.
        try:
            uploads.prune()
        except OSError as error:
            log.warning("Attachment prune failed: %s", error)
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
        # Containment first, then ownership: the header this check trusts is inside the file, so a file
        # outside pi's sessions directory must be refused before anything is read out of it.
        session_file = resolve_session_path(session_path)
        if session_file is None or not session_belongs_to_account(
            str(session_file),
            PROJECT_ROOT,
            account,
        ):
            raise HTTPException(status_code=404, detail="Session not found")
        return preview_session(str(session_file))

    @application.delete("/api/sessions")
    async def delete_session_route(request: Request):
        """Delete one session, file and all.

        Ben's requirement in his words: a deleted session should delete the literal pi session JSONL. So
        there is no archive and no soft flag: a row that is marked deleted while the file sits on disk is
        the half-measure he was asking to get rid of, and pi has no trash to move it to. Which is why the
        two checks run before the unlink: the path has to resolve to a `.jsonl` inside pi's own sessions
        directory, and that file's header has to say it belongs to the profile that is asking. Both answers
        are 404 when they fail, because which session paths exist on this machine is not the browser's
        business either.

        The sidebar gets the refreshed list back rather than a bare ok, so the row disappears without a
        second round trip and a list that was rebuilt from disk.
        """
        account = _require_account(request, auth)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Expected a JSON body naming a session path")

        raw = body.get("path") if isinstance(body, dict) else None
        session_file = resolve_session_path(raw if isinstance(raw, str) else "")
        if session_file is None:
            raise HTTPException(status_code=404, detail="Session not found")

        deleted = await run_in_threadpool(delete_session, session_file, PROJECT_ROOT, account)
        if not deleted:
            # The header named a different account's work directory, or Windows still has the file open in
            # something that is reading it. Nothing is retried behind the browser's back.
            log.warning("session delete refused or failed: %s (account %s)", session_file, account)
            raise HTTPException(status_code=403, detail="Session could not be deleted")

        log.info("deleted session %s (account %s)", session_file.name, account)
        sessions = await run_in_threadpool(
            list_sessions, project_root=PROJECT_ROOT, account_label=account
        )
        return {"deleted": True, "sessions": sessions}

    @application.post("/api/upload-file")
    async def upload_file(request: Request, file: UploadFile, scope: str | None = Form(None)):
        account = _require_account(request, auth)

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

        # The scope is the tab's own attachment directory. It comes from the browser because no child
        # exists yet to key it on (invariant 3), and it is validated rather than trusted because it
        # becomes a path segment.
        if not uploads.is_valid_scope(scope):
            raise HTTPException(
                status_code=400,
                detail="This tab is missing its attachment scope; reload the page and try again.",
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

        # Keep the bytes. The transcription is what the model reads now; the file is what it opens
        # later, and without this line there was no file to open.
        stored = uploads.store(
            account,
            scope,
            original_name=filename,
            data=content_bytes,
            mime_type=mime_type,
        )
        if stored is None:
            raise HTTPException(status_code=400, detail="Invalid attachment scope")

        # Inline it only while it is small enough to be worth the context window. Above the cap the
        # response carries no body and the path does the work: the agent reads it in chunks.
        inlined = len(text) <= uploads.INLINE_MAX_CHARS
        if not inlined:
            log.info(
                "attachment %s is %d characters, over the inline limit: the agent gets the path only",
                stored.name,
                len(text),
            )

        return {
            "filename": stored.name,
            "ref": stored.ref,
            "path": str(stored.path),
            "mimeType": mime_type,
            "bytes": stored.bytes,
            # A null content is the browser's cue to send no body back on the prompt. The path is in
            # `path` either way, so the attachment is reachable at every size.
            "content": text if inlined else None,
            "inlined": inlined,
            "inlineLimitChars": uploads.INLINE_MAX_CHARS,
        }

    @application.post("/api/questionnaire")
    async def questionnaire_ask(
        request: Request,
        body: dict,
        x_preview_token: str | None = None,
    ):
        """Ask the human a set of questions, on behalf of the child that owns a connection.

        This exists because `ctx.ui.custom()` returns `undefined` in RPC mode (`pi/docs
        /rpc-extension-ui.md`), which is the one UI method a multi-question form needs - it is why a
        questionnaire asked through this application fails with "Cannot read properties of undefined
        (reading 'cancelled')" while working fine in a terminal. The child has the questions; only the
        browser has a person in front of it.

        Identity is the same per-spawn token the preview pane uses: a child has no cookie, and it must
        not be able to interrupt somebody else's tab. The wait is the answer, so the reply is the
        person's response, hours later or never - which is why the future is released when the tab or
        the run goes away (`pi_chat/ui_relay.py`).
        """
        token = (x_preview_token or request.headers.get("x-preview-token") or "").strip()
        pi = ui_relay.connection_for_token(token)
        if pi is None:
            # 410 rather than 404: the token was real once and the connection is simply gone, which is
            # the difference between "retry later" and "nobody is going to answer this".
            raise HTTPException(
                status_code=410,
                detail="No browser is connected for this session, so nobody can answer",
            )
        try:
            questions = ui_relay.normalise_questions(body)
        except ui_relay.QuestionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        result = await ui_relay.ask(pi, "questionnaire", {"questions": questions})
        if result.get("cancelled"):
            return JSONResponse({"cancelled": True, "reason": result.get("reason") or "cancelled"})
        return JSONResponse({
            "cancelled": False,
            "answers": ui_relay.normalise_answers(questions, result.get("answers")),
        })

    # The pane is the only witness to what the human can actually see: which tab is in front,
    # whether the column is open at all. The browser POSTs that here; the preview child reads it.
    # Identity is the per-spawn token in `x-preview-token` rather than a cookie, because a child
    # process has no cookie to present and must not be able to read another session's pane. That
    # is a 128-bit secret minted per spawn, which is the same class of credential the session
    # bearer already is, and the payload it unlocks is display metadata about documents this
    # connection already wrote.
    @application.get("/api/preview/state")
    async def preview_state_read(
        request: Request,
        x_preview_token: str | None = None,
    ):
        """Report what a connection's preview pane is showing, to the child that owns it."""
        token = (x_preview_token or request.headers.get("x-preview-token") or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="Preview state needs the connection token")
        entry = PANE_STATE.get(token)
        if entry is None:
            # Same shape as the freshness route's denial: nothing to report, and no clue about which
            # tokens exist.
            raise HTTPException(status_code=404, detail="No pane state recorded")
        view, _ = entry
        return JSONResponse({**view, "ageSeconds": round(time.time() - entry[1], 1)})

    @application.post("/api/preview/state")
    async def preview_state_write(
        request: Request,
        body: dict,
    ):
        """Record what a pane is showing. The field set is closed on purpose.

        Two rules that are easy to get wrong and cheap to get right here:

        - A key is stored when the body carried it and the value passed, or when the body carried it
          and the value is legitimately ``None`` (no active tab, nothing being watched). A key the
          body never sent stays absent, so a reader can tell "nothing is open in the pane" from
          "this browser does not report widths".
        - Nothing is stored unless at least one field was recognised. The alternative is that any
          authenticated request with a valid token can append a permanent empty entry per token, and
          the sweep that bounds the map only runs on a write that stores something.
        """
        _require_account(request, auth)
        token = str(body.get("token") or "") if isinstance(body, dict) else ""
        token = token.strip()
        if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
            raise HTTPException(status_code=400, detail="Preview state needs the connection token")

        def reported(name: str) -> bool:
            return isinstance(body, dict) and name in body

        def accept(name: str, value, *, kind: str, limit: int | None = None) -> None:
            """Store one reported field under the one rule that applies to all of them.

            A field the body carried is always recorded. What passes is the value; what does not is
            recorded as ``None``, which reads as "unknown" rather than silently keeping whatever the
            last report said. A pane that stops sending a field, or starts sending a bad one, must
            not leave the model reading yesterday's document id.

            `kind` is explicit per field rather than inferred from the value. An earlier version
            inferred it from which limits were passed, and a field declared with no limit then
            accepted anything: `{"open": "yes"}` arrived as a truthy string and the pane reported
            itself open to the model.
            """
            if not reported(name):
                return
            if kind == "bool":
                ok = isinstance(value, bool)
            elif kind == "id":
                ok = isinstance(value, str) and 0 < len(value) <= (limit or 64)
            elif kind == "count":
                ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value < (limit or 0)
            else:
                ok = False
            view[name] = value if ok else None

        view: dict = {}
        accept("open", body.get("open"), kind="bool")
        for name in ("activeDocId", "watching", "fileToken"):
            accept(name, body.get(name), kind="id", limit=64)
        accept("width", body.get("width"), kind="count", limit=8192)
        accept("revision", body.get("revision"), kind="count", limit=1_000_000)

        if not view:
            raise HTTPException(status_code=400, detail="Preview state needs at least one field")

        now = time.time()
        _sweep_pane_state(now)
        PANE_STATE[token] = (view, now)
        return JSONResponse({"ok": True, "fields": sorted(view)})

    # Registration order is load-bearing: Starlette matches in order, so a literal path that also
    # fits `/api/preview/{doc_id}` must be declared first. Below it, `/api/preview/fresh` gets
    # swallowed as `doc_id="fresh"` and answers a 404 the pane cannot tell from a dead document
    # (measured 2026-11-19: `test_it_is_routed_ahead_of_the_document_id_route`).
    @application.get("/api/preview/fresh")
    async def preview_freshness(
        request: Request,
        doc: str | None = Query(None, description="Stored document id, as returned by action=open"),
        path: str | None = Query(None, description="Referenced file, exactly as the pane is showing it"),
        kind: str | None = Query(None, description="Kind the pane expects; narrows a stored doc id"),
    ):
        """Has the file behind a preview changed? Answers with a validator, and never with bytes.

        One call is one ``stat``: the pane polls this for the active document only, and re-fetches
        from ``/api/preview/{doc_id}`` only when the returned ``token`` moves (plan:
        ``~/.pi/agent/plans/pi-chat-preview-interactive.md``). The response is small and JSON, so it
        is never framed and the site-wide CSP is correct for it as it stands.

        This is deliberately not a wider read than the serving route: it resolves through
        ``preview.head``, which calls the same allowlist and scope functions with the same preference
        order, so anything it can report on is already fetchable by this caller. "Nothing there" and
        "not allowed" share one answer for the same reason the route answers 404 rather than 403.
        """
        account = _require_account(request, auth)
        if kind is not None and kind not in preview.KIND_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Unsupported preview kind")
        if not (doc or path):
            # A malformed request, not a missing document: 404 would tell a caller that an empty
            # query is a real answer about a file.
            raise HTTPException(status_code=400, detail="Preview freshness needs doc or path")

        return JSONResponse(
            preview.head(account, doc_id=doc, kind=kind, path=path),
            headers={"Cache-Control": "no-store"},
        )

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
            description="Kind the pane expects (markdown|html|pdf|image); a mismatch is a 404",
        ),
    ):
        """Serve one preview-pane document: markdown, HTML, PDF, or an image.

        The store is scoped per profile, so a doc id from another profile's session is a 404. A
        ``path`` request is additionally confined to the roots in ``preview.allowed_roots``, and
        every denial is the same 404: this endpoint must not confirm which paths exist.

        ``kind`` is a contract, not a hint. The renderer and the CSP are chosen from the kind, so
        being handed a different kind than was asked for would put, say, an HTML document through
        the markdown path, or a stored PNG through the frame. Anything that disagrees is refused.
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

    @application.get("/api/limits")
    async def limits(request: Request):
        """The size ceilings the composer has to respect, so the browser can refuse before the socket does.

        Not decoration. An image larger than uvicorn's incoming frame cap does not come back as a
        413: the frame is dropped and the websocket closes, which a person reads as the chat falling
        over rather than as "that photo was too big". The number lives on the server, so the client
        cannot drift from it.
        """
        _require_account(request, auth)
        return {
            "imageMaxBytes": uploads.MAX_IMAGE_BYTES,
            "documentMaxBytes": MAX_DOCUMENT_SIZE,
            "inlineMaxChars": uploads.INLINE_MAX_CHARS,
            "retentionDays": uploads.RETENTION_DAYS,
        }

    @application.get("/api/version")
    async def version(request: Request):
        """What this process was built from, for the hot-reload banner.

        Behind authentication with the rest of the API rather than public: the payload is a pid, a boot id and
        the name of the most recently edited file, which is nothing to a stranger and a small map of this
        machine to one. The banner only ever needs it while someone is logged in.
        """
        _require_account(request, auth)
        # Two directory walks per poll, and the page polls every few seconds. On a thread, because this route
        # shares its event loop with every chat websocket on the box.
        return await run_in_threadpool(devtools.snapshot, STATIC_DIR, DEV_MODE)

    if DEV_MODE:
        # Registered only in dev mode, so everywhere else it is a 404 rather than a forbidden route that
        # advertises itself. This is the one endpoint in the app that can end the process.
        @application.post("/api/dev/restart")
        async def dev_restart(request: Request):
            """Ask the supervisor for a new process, so a back-end change applies without a terminal."""
            account = _require_account(request, auth)
            peer = request.client.host if request.client else ""
            if peer not in devtools.LOOPBACK_HOSTS:
                # 404, not 403: `PI_CHAT_ALLOW_DEV_REMOTE=1` exists, and the difference between "not allowed
                # from there" and "nothing here" is worth keeping off the wire.
                raise HTTPException(status_code=404, detail="Not found")
            if not devtools.request_restart():
                raise HTTPException(status_code=503, detail="No server to restart (running under a test client?)")
            log.warning("Dev restart requested by profile %s from %s", account, peer)
            return {"ok": True, "restarting": True, "bootId": devtools.BOOT_ID}

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
