#!/usr/bin/env python
"""End-to-end smoke test for pi-chat on a machine with no usable model.

Starts the real server (uvicorn, real auth, real WebSocket handler), points the
pi subprocess at `tools/fake_rpc_pi.py`, then drives the browser protocol and
asserts the pipeline works. Nothing here touches an inference engine or the
network, so it is safe to run while another pi session owns the GPU.

    uv run python tools/smoke_test_ws.py            # exit 0 == pass
    PI_CHAT_SMOKE_PORT=9030 uv run python tools/smoke_test_ws.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
FAKE_PI = ROOT / "tools" / "fake_rpc_pi.py"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PI_CHAT_SMOKE_PORT", "9011"))
BASE = f"http://{HOST}:{PORT}"
TEST_PASSWORD = "test-only"
EXPECTED_TEXT = "Fake assistant reply from the RPC stub: alpha beta gamma."

failures: list[str] = []
checks = 0

# PI_CHAT_SMOKE_LIVE=1 swaps the stub for a real pi (set PI_CHAT_PI_ARGS to pick a provider,
# e.g. '--model google:gemini-3.8-flash --no-extensions --no-skills --no-context-files').
# Use it to prove protocol drift or absence of one; never needed for a normal test run, and
# never against a local engine that another pi session is already holding.
# A prompt the RPC double holds open, so there is a real running call to stop, and the log line the
# server writes each time it starts a pi child. Together these let the suite prove that stopping a turn
# does not restart the assistant: Ben could not tell what the Stop button reached for, and a tooltip is not
# evidence. See `check_stop_leaves_the_child_running`.
CANCEL_PROMPT = "hold a tool open please FAKE_RPC_HOLD_TOOL"
SPAWN_LOG_MARK = "Spawning pi --mode rpc"

LIVE = os.environ.get("PI_CHAT_SMOKE_LIVE", "") == "1"
LIVE_PROMPT = "Reply with exactly one word and nothing else: PI-CHAT-OK"

# Reopening a session is the one preview path a browser stage cannot reach without a model call, so
# the smoke test owns the fixture: a real session JSONL under the account's work directory, plus one
# stored document that exists and one that does not.
SMOKE_ACCOUNT = "b"
SMOKE_SCOPE = "smoke0000"
SMOKE_LIVE_DOC = "e" * 32
SMOKE_GONE_DOC = "f" * 32
SMOKE_PREVIEW_BASE = Path(os.environ.get("TEMP", "/tmp")) / "pi-chat-smoke-previews"


def write_smoke_session() -> Path:
    """Create a session file carrying two recorded previews, and the one document still on disk."""
    work_dir = ROOT / "sessions" / SMOKE_ACCOUNT
    work_dir.mkdir(parents=True, exist_ok=True)
    store = SMOKE_PREVIEW_BASE / SMOKE_ACCOUNT / SMOKE_SCOPE
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{SMOKE_LIVE_DOC}.md").write_text("# Replayed\n", encoding="utf-8")

    def entry(data: dict, entry_id: str) -> str:
        return json.dumps({"type": "custom", "customType": "pichat_preview", "data": data,
                           "id": entry_id, "parentId": "root", "timestamp": "2026-11-09T22:39:24.923Z"})

    lines = [
        json.dumps({"type": "session", "cwd": str(work_dir), "timestamp": "2026-11-09T22:37:41.869Z"}),
        json.dumps({"type": "message", "message": {"role": "user",
                   "content": [{"type": "text", "text": "make me a document"}]}}),
        entry({"v": 1, "op": "open", "docId": SMOKE_LIVE_DOC, "kind": "markdown", "title": "Replayed",
               "revision": 1, "file": f"{SMOKE_LIVE_DOC}.md"}, "e1"),
        entry({"v": 1, "op": "open", "docId": SMOKE_GONE_DOC, "kind": "html", "title": "Cleaned up",
               "revision": 1, "file": f"{SMOKE_GONE_DOC}.html"}, "e2"),
    ]
    path = work_dir / f"{time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime())}_smoke-preview-replay.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def remove_smoke_session(path: Path | None) -> None:
    for target in (path, SMOKE_PREVIEW_BASE):
        if target is None:
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        except OSError:
            pass


def check(condition: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


def wait_for_port(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.4)
            if sock.connect_ex((HOST, PORT)) == 0:
                return True
        time.sleep(0.25)
    return False


def login() -> str:
    request = urllib.request.Request(
        f"{BASE}/api/login",
        data=json.dumps({"account": "b", "password": TEST_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        cookie = response.headers.get("Set-Cookie", "")
        body = json.loads(response.read().decode())
    check(body.get("account") == "b", "login returns the account", str(body))
    check("pi_chat_session=" in cookie, "login sets the session cookie", cookie[:80])
    return cookie.split(";")[0]


def spawn_count(server_log: Path) -> int:
    """How many pi children the server has started, read from its own log.

    Counting spawn lines is the only honest way to answer "did that stop kill the process?" from outside:
    a restarted child logs again, and a child that survived a cancelled turn does not.
    """
    try:
        return server_log.read_text(encoding="utf-8", errors="replace").count(SPAWN_LOG_MARK)
    except OSError:
        return -1


async def check_stop_leaves_the_child_running(
    ws, pump, got: dict, pi_events: dict, server_log: Path
) -> None:
    """S4: Stop ends the turn, and stops nothing else.

    The order the server uses matters here and is not obvious: `cancel_result` goes to the browser before
    the `abort` is written to pi, because pi acknowledges an abort only once the session goes idle and the
    tool being escaped is exactly what keeps it busy. So the browser is told first, and the settled turn is
    what the checks below wait for.
    """
    started_before = pi_events.get("tool_execution_start", 0)
    ended_before = pi_events.get("tool_execution_end", 0)
    settled_before = pi_events.get("agent_settled", 0)
    spawns = spawn_count(server_log)
    check(spawns >= 1, "a pi child was spawned and logged", f"spawn lines: {spawns}")

    await ws.send(json.dumps({"type": "prompt", "message": CANCEL_PROMPT}))
    await pump(20.0, lambda _e: pi_events.get("tool_execution_start", 0) > started_before)
    check(pi_events.get("tool_execution_start", 0) > started_before,
          "the held call started, so there is something to stop", str(pi_events))

    cancel_before = got.get("cancel_result", 0)
    await ws.send(json.dumps({"type": "cancel_tool"}))
    await pump(20.0, lambda _e: got.get("cancel_result", 0) > cancel_before)
    check(got.get("cancel_result", 0) > cancel_before,
          "cancel_result reaches the browser before the run settles", str(got))

    await pump(20.0, lambda _e: pi_events.get("agent_settled", 0) > settled_before)
    check(pi_events.get("agent_settled", 0) > settled_before,
          "the aborted turn settles instead of hanging", str(pi_events))
    check(pi_events.get("tool_execution_end", 0) == ended_before,
          "the stopped call never reports a result",
          f"tool_execution_end went {ended_before} -> {pi_events.get('tool_execution_end', 0)}")

    settled_mid = pi_events.get("agent_settled", 0)
    await ws.send(json.dumps({"type": "prompt", "message": "second prompt after a stop"}))
    await pump(25.0, lambda _e: pi_events.get("agent_settled", 0) > settled_mid)
    check(pi_events.get("agent_settled", 0) > settled_mid,
          "the next prompt is answered, on the child that was already running", str(pi_events))
    check(spawn_count(server_log) == spawns,
          "stopping a turn spawned no new pi child",
          f"spawn lines went {spawns} -> {spawn_count(server_log)}")


async def drive_session(
    cookie: str, replay_session: Path | None = None, server_log: Path | None = None
) -> None:
    headers = {"Cookie": cookie}
    try:
        ws_context = websockets.connect(f"ws://{HOST}:{PORT}/ws", additional_headers=headers)
    except TypeError:  # websockets < 13
        ws_context = websockets.connect(f"ws://{HOST}:{PORT}/ws", extra_headers=headers)
    async with ws_context as ws:
        got: dict[str, int] = {}
        deltas: list[str] = []
        pi_events: dict[str, int] = {}
        queue_states: list[dict] = []
        queue_actions: list[dict] = []
        # (text, how many runs had started when pi delivered it) - the position a queued message took
        # in the session file, which is the thing the browser has to render faithfully.
        user_deliveries: list[tuple[str, int]] = []

        async def note(raw: str) -> None:
            message = json.loads(raw)
            mtype = message.get("type", "?")
            got[mtype] = got.get(mtype, 0) + 1
            if mtype == "queue_state":
                queue_states.append(message)
            elif mtype == "queue_action":
                queue_actions.append(message)
            if mtype == "pi_event":
                event = message.get("event", {})
                etype = event.get("type", "?")
                pi_events[etype] = pi_events.get(etype, 0) + 1
                if etype == "message_start" and (event.get("message") or {}).get("role") == "user":
                    text = "".join(
                        block.get("text", "")
                        for block in (event["message"].get("content") or [])
                        if block.get("type") == "text"
                    )
                    user_deliveries.append((text, pi_events.get("agent_start", 0)))
                if etype == "message_update":
                    sub = event.get("assistantMessageEvent", {})
                    if sub.get("type") == "text_delta":
                        deltas.append(sub.get("delta", ""))

        async def pump(seconds: float, stop_when) -> None:
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    await note(await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time())))
                except asyncio.TimeoutError:
                    return
                if stop_when(pi_events):
                    return

        # ping/pong proves the browser command loop and the pi child are alive.
        await ws.send(json.dumps({"type": "ping"}))
        await pump(5.0, lambda e: got.get("pong", 0) > 0)
        check(got.get("pong", 0) >= 1, "ping answered with pong", str(got))

        # First prompt spawns the pi subprocess lazily.
        prompt_text = LIVE_PROMPT if LIVE else "smoke test prompt"
        await ws.send(json.dumps({"type": "prompt", "message": prompt_text}))
        await pump(90.0 if LIVE else 25.0, lambda e: e.get("agent_settled", 0) > 0)

        check(got.get("session_started", 0) >= 1, "session_started emitted", str(got))
        check(pi_events.get("agent_start", 0) >= 1, "agent_start relayed", str(pi_events))
        check(bool(deltas) and len(deltas) > 1, "streamed text deltas relayed", f"{len(deltas)} deltas")
        assembled = "".join(deltas)
        if LIVE:
            check(len(assembled.strip()) > 0, "assistant produced text", assembled[:80])
            check("pi-chat-ok" in assembled.lower(), "assistant echoed the marker word", assembled[:120])
        else:
            check(assembled == EXPECTED_TEXT, "assembled stream matches full text", assembled[:80])
            check(pi_events.get("tool_execution_start", 0) >= 1, "tool card event relayed", str(pi_events))
            check(pi_events.get("tool_execution_end", 0) >= 1, "tool completion event relayed", str(pi_events))
        check(pi_events.get("message_end", 0) >= 1, "message_end relayed", str(pi_events))
        check(pi_events.get("agent_end", 0) >= 1, "agent_end relayed", str(pi_events))
        check(pi_events.get("agent_settled", 0) >= 1, "agent_settled relayed", str(pi_events))
        check("error" not in got, "no error message to the browser", str(got.get("error")))

        # Request/response commands need the id-matched future in PiProcess.
        await ws.send(json.dumps({"type": "get_messages"}))
        await pump(10.0, lambda e: got.get("messages_retrieved", 0) > 0)
        check(got.get("messages_retrieved", 0) >= 1, "get_messages round-trips", str(got))

        if LIVE:
            print("  skip  mid-run queue checks: a real turn is not predictable enough to time")
        else:
            await check_queue_delivery(ws, pump, got, pi_events, queue_states, queue_actions, user_deliveries)

        if LIVE or server_log is None:
            print("  skip  stop-the-turn checks: they need the RPC double and the server log")
        else:
            await check_stop_leaves_the_child_running(ws, pump, got, pi_events, server_log)

        await ws.send(json.dumps({"type": "new_session"}))
        await pump(10.0, lambda e: got.get("session_started", 0) > 1)
        check(got.get("session_started", 0) >= 2, "new_session emits a fresh session_started", str(got))

        if replay_session is not None:
            await check_preview_replay(ws, note, got, replay_session)


async def check_preview_replay(ws, note, got, session_path: Path) -> None:
    """Load a session that recorded previews and check the pane can be rebuilt from it.

    pi's `get_messages` does not return the persisted `pichat_preview` entries, so the server reads them
    out of the JSONL and puts them on `session_loaded`. A session that never had a preview must produce
    an empty list rather than a missing field, and a document cleanup took must arrive marked gone: the
    pane keeps the tab and says the file is missing instead of fetching a 404.
    """
    await ws.send(json.dumps({"type": "load_session", "sessionPath": str(session_path)}))
    loaded: list[dict] = []

    async def note_loaded(raw: str) -> None:
        await note(raw)
        message = json.loads(raw)
        if message.get("type") == "session_loaded":
            loaded.append(message)

    deadline = time.time() + 15.0
    while time.time() < deadline and not loaded:
        try:
            await note_loaded(await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time())))
        except asyncio.TimeoutError:
            break

    check(bool(loaded), "load_session answers with session_loaded", str(got))
    if not loaded:
        return
    previews = loaded[0].get("previews")
    check(isinstance(previews, list) and len(previews) == 2, "session_loaded carries the recorded previews", str(previews)[:160])
    if not isinstance(previews, list) or len(previews) != 2:
        return
    check([entry.get("op") for entry in previews] == ["open", "open"], "previews keep transcript order", str(previews)[:160])
    check(previews[0].get("available") is True and previews[0].get("bytes", 0) > 0,
          "a document still on disk is reported available", str(previews[0])[:160])
    check(previews[1].get("available") is False and previews[1].get("title") == "Cleaned up",
          "a cleaned-up document is reported gone with its recorded title", str(previews[1])[:160])


async def check_queue_delivery(ws, pump, got, pi_events, queue_states, queue_actions, user_deliveries) -> None:
    """Send at a running agent and follow the message through pi's queue.

    The stub keeps reading stdin while it streams and delivers steering at a turn boundary like the
    real thing, so this is the actual sequence: a prompt that lands mid-run is queued by its
    streamingBehavior, the browser sees pi's own queue announcements, the promote action clears and
    re-pushes rather than duplicating, and each message comes back as a user message at the point pi
    took it. Waiting on counters instead of sleeps keeps it honest on a loaded machine.
    """
    started = pi_events.get("agent_start", 0)
    settled = pi_events.get("agent_settled", 0)
    errors_before = got.get("error", 0)
    states_before = len(queue_states)

    await ws.send(json.dumps({"type": "prompt", "message": "queue smoke turn"}))
    await pump(25.0, lambda e: pi_events.get("agent_start", 0) > started)
    check(pi_events.get("agent_start", 0) > started, "queue turn started", str(pi_events))

    await ws.send(json.dumps({"type": "prompt", "message": "queued instruction"}))
    await pump(8.0, lambda _e: len(queue_states) > states_before)
    check(len(queue_states) > states_before, "queue_state announced to the browser", str(queue_states[-1:]))
    check(queue_states[-1].get("followUp") == ["queued instruction"], "default delivery waits in the queue", str(queue_states[-1]))

    await ws.send(json.dumps({"type": "prompt", "message": "steering instruction", "delivery": "now"}))
    await pump(
        8.0,
        lambda _e: len(queue_states) > states_before and queue_states[-1].get("steering"),
    )
    check(queue_states[-1].get("steering") == ["steering instruction"], "send-now steers instead of waiting", str(queue_states[-1]))

    actions_before = len(queue_actions)
    states_before_promote = len(queue_states)
    await ws.send(json.dumps({
        "type": "steer_now",
        "kind": "followUp",
        "index": 0,
        "text": "queued instruction",
    }))
    await pump(8.0, lambda _e: len(queue_actions) > actions_before)
    check(len(queue_actions) > actions_before, "promote acked", str(queue_actions[-1:]))
    # The ack only means the server finished pushing; the announcements for those pushes are still in
    # flight, and pi empties the queue as it delivers. So wait until the queue is *seen* holding both,
    # and assert on what was observed rather than on the newest state.
    def promoted_held() -> bool:
        return any(
            sorted(state.get("steering", [])) == ["queued instruction", "steering instruction"]
            and state.get("followUp") == []
            for state in queue_states[states_before_promote:]
        )

    await pump(6.0, lambda _e: promoted_held())
    check(
        promoted_held(),
        "promoted entry joined the queue without duplicating the other",
        str(queue_states[states_before_promote:][-3:]),
    )

    await pump(40.0, lambda _e: pi_events.get("agent_settled", 0) >= settled + 2)
    check(
        pi_events.get("agent_settled", 0) >= settled + 2,
        "both queued messages were delivered before the session went idle",
        f"settled {pi_events.get('agent_settled', 0) - settled} times",
    )
    delivered = [entry for entry in user_deliveries if entry[0] in {"queued instruction", "steering instruction"}]
    check(
        delivered == [("queued instruction", started + 1), ("steering instruction", started + 2)],
        "the promoted message took this turn's boundary and the other followed in the next run",
        f"deliveries {delivered} (runs started: {pi_events.get('agent_start', 0) - started})",
    )
    check(got.get("error", 0) == errors_before, "no error reached the browser mid-run", str(got.get("error")))


def main() -> int:
    if not FAKE_PI.exists():
        print(f"missing {FAKE_PI}")
        return 2

    sys.path.insert(0, str(ROOT))
    from pi_chat.auth import hash_password

    env = {
        **os.environ,
        "PI_CHAT_HOST": HOST,
        "PI_CHAT_PORT": str(PORT),
        "PI_CHAT_B_PASSWORD_HASH": hash_password(TEST_PASSWORD),
        "PI_CHAT_DEV": "",
        "PI_CHAT_LOG_LEVEL": "INFO",
        # The replay fixture writes one document into the store; keep it out of the checkout's tree.
        "PI_CHAT_PREVIEW_BASE_DIR": str(SMOKE_PREVIEW_BASE),
    }
    if LIVE:
        print(f"LIVE mode: real pi, PI_CHAT_PI_ARGS={os.environ.get('PI_CHAT_PI_ARGS', '')!r}")
    else:
        env["PI_CHAT_PI_BIN"] = str(FAKE_PI)
        env["PI_CHAT_PI_ARGS"] = ""
        # Slow enough that a message can land mid-run, which is what the queue checks need.
        env["FAKE_RPC_TURN_MS"] = os.environ.get("PI_CHAT_SMOKE_TURN_MS", "600")
    env.pop("PI_CHAT_R_PASSWORD_HASH", None)

    log_path = Path(os.environ.get("TEMP", "/tmp")) / "pi-chat-smoke-server.log"
    log_file = log_path.open("w", encoding="utf-8")
    print(f"server log: {log_path}")

    server = None
    session_path: Path | None = None
    try:
        server = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=str(ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        if not wait_for_port():
            print("server never came up; last 40 log lines:")
            log_file.flush()
            print(log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
            return 2
        print(f"server up on {BASE} (pid {server.pid}) with PI_CHAT_PI_BIN={FAKE_PI.name}")

        cookie = login()
        session_path = write_smoke_session()
        asyncio.run(drive_session(cookie, session_path, log_path))
    finally:
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        log_file.close()
        remove_smoke_session(session_path)

    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    spawn_lines = [line for line in tail if "Spawning pi" in line]
    check(bool(spawn_lines), "server logged the resolved pi command", "")
    if spawn_lines:
        print(f"       {spawn_lines[-1].strip()[:160]}")
    check(
        any("Pi process killed" in line for line in tail),
        "pi subprocess reaped on disconnect",
        "",
    )

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
