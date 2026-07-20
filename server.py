"""FastAPI server bridging a web UI to pi --mode rpc."""

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from contextlib import asynccontextmanager
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

    async def spawn(self):
        """Kill any existing process and spawn a new pi RPC instance."""
        await self.kill()
        log.info("Spawning pi --mode rpc (session %s)", self.session_id)
        self.proc = await asyncio.create_subprocess_exec(
            "pi", "--mode", "rpc", "--no-session", "--approve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
        log.info("Pi process killed")

    async def send(self, message: dict):
        """Send a JSON command to pi's stdin."""
        if self.proc and self.proc.stdin:
            line = (json.dumps(message) + "\n").encode("utf-8")
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()

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


# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    pi.ws = websocket

    # If no pi process is running, spawn one
    if not pi.proc:
        await pi.spawn()
        await websocket.send_json({"type": "session_started", "sessionId": pi.session_id})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            cmd_type = msg.get("type")

            if cmd_type == "prompt":
                payload = {"type": "prompt", "message": msg["message"]}
                if "images" in msg:
                    payload["images"] = msg["images"]
                await pi.send(payload)

            elif cmd_type == "new_session":
                await pi.spawn()
                await websocket.send_json({"type": "session_started", "sessionId": pi.session_id})

            elif cmd_type == "abort":
                await pi.send({"type": "abort"})

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
