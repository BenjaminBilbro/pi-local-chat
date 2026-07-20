"""FastAPI server bridging a web UI to pi --mode rpc."""

import asyncio
import base64
import json
import logging
import os
import signal
import sys
import tempfile
import shutil
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
        self.account: str | None = None  # 'b' or 'r'
        self.work_dir: Path | None = None

    async def spawn(self):
        """Kill any existing process and spawn a new pi RPC instance."""
        await self.kill()
        self.session_id = str(uuid.uuid4())[:8]

        # ephemeral working dir in /tmp
        self.work_dir = Path(tempfile.mkdtemp(prefix="pi-chat-"))
        log.info("Work dir: %s", self.work_dir)

        # Build CLI args
        cmd = ["pi", "--mode", "rpc", "--no-session", "--approve"]
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
            limit=1024 * 1024,  # 1 MB buffer — long JSON events can exceed default 64 KB
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
        # Clean up ephemeral work dir
        if hasattr(self, "work_dir") and self.work_dir:
            try:
                shutil.rmtree(self.work_dir)
                log.info("Cleaned up work dir: %s", self.work_dir)
            except Exception as e:
                log.warning("Failed to clean up %s: %s", self.work_dir, e)
            self.work_dir = None
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
                    # Save base64 images to temp files — avoids blowing past
                    # asyncio's stdout readline buffer with large inline payloads.
                    # Pi reads the file directly from disk (no buffer limit concerns).
                    image_paths = []
                    for img in msg["images"]:
                        ext = (img.get("mimeType", "image/png").split("/")[1] or "png")
                        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="." + ext)
                        tmp.write(base64.b64decode(img["data"]))
                        tmp.close()
                        image_paths.append({"path": tmp.name})
                        log.info("Saved image to %s (%.1fKB)", tmp.name, os.path.getsize(tmp.name) / 1024)
                    payload["images"] = image_paths
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
