"""FastAPI application factory for pi chat."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import AuthManager, COOKIE_NAME
from .config import PROJECT_ROOT, STATIC_DIR
from .process import PiProcess
from .sessions import (
    list_sessions,
    preview_session,
    session_belongs_to_account,
)
from .websocket import handle_websocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pi-chat")


class LoginCredentials(BaseModel):
    account: str
    password: str


def create_app(
    process_factory: Callable[[], PiProcess] | None = None,
    auth_manager: AuthManager | None = None,
) -> FastAPI:
    """Create the web app with isolated pi processes per browser connection."""
    make_process = process_factory or PiProcess
    auth = auth_manager or AuthManager()
    active_processes: set[PiProcess] = set()

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

    application = FastAPI(title="pi-chat", lifespan=lifespan)
    application.state.auth = auth
    application.state.active_processes = active_processes

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
            await handle_websocket(websocket, pi, account)
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
