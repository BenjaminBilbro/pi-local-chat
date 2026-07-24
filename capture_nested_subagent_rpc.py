"""Capture nested sub-agent RPC events and the native pi session JSONL.

Prompts:
  1. \"What is the current date?\"
  2. Spawn a sub-agent that makes 3 simple tool calls, and have THAT sub-agent
     spawn another sub-agent that makes 3 more simple tool calls.

Outputs:
  data-samples/nested_subagent_rpc_capture.jsonl   (wrapped RPC format)
  data-samples/nested_subagent_session.jsonl       (native pi session)
"""

import asyncio
import json
import signal
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_RPC = PROJECT_ROOT / "data-samples" / "nested_subagent_rpc_capture.jsonl"
PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"

PROMPT_1 = "What is the current date? Tell me briefly."

PROMPT_2 = (
    "Spawn a sub-agent named 'outer' and ask it to do the following: "
    "make 3 simple tool calls (bash: `echo hello`, bash: `date`, bash: `whoami`), "
    "THEN have THAT sub-agent spawn another sub-agent named 'inner' that makes "
    "3 more simple tool calls (bash: `pwd`, bash: `uname -a`, bash: `echo done`). "
    "Finally report back all results from both sub-agents."
)


def find_latest_session(work_dir: Path) -> Path | None:
    """Find the newest .jsonl under PI_SESSIONS_DIR matching our work dir."""
    if not PI_SESSIONS_DIR.exists():
        return None
    resolved = work_dir.resolve()
    best = None
    for jsonl in PI_SESSIONS_DIR.rglob("*.jsonl"):
        try:
            header = json.loads(jsonl.read_text(errors="replace").splitlines()[0])
            if Path(header.get("cwd", "")).resolve() == resolved:
                if best is None or jsonl.stat().st_mtime > best.stat().st_mtime:
                    best = jsonl
        except Exception:
            continue
    return best


async def main():
    OUT_RPC.write_text("")  # truncate
    work_dir = PROJECT_ROOT / "sessions" / "b"
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Work dir: {work_dir}")
    print(f"Spawning pi --mode rpc (output → {OUT_RPC})")

    proc = await asyncio.create_subprocess_exec(
        "pi", "--mode", "rpc", "--approve",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir),
        limit=32 * 1024 * 1024,
    )

    # Graceful shutdown on Ctrl+C
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: proc.terminate())

    event_count = 0

    async def read_stdout():
        nonlocal event_count
        while not proc.stdout.at_eof():
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                print(f"  [non-json] {text[:120]}")
                continue

            event_count += 1
            ts = datetime.now().isoformat(timespec="milliseconds")
            record = {"seq": event_count, "ts": ts, "event": event}
            # Append efficiently
            with open(OUT_RPC, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

            etype = event.get("type", "?")
            extra = ""
            if etype == "message_update":
                aev = event.get("assistantMessageEvent", {})
                extra = f"  [{aev.get('type', '?')}]"
            elif etype == "message_start":
                extra = f"  [role={event.get('message', {}).get('role', '?')}]"
            elif etype == "message_end":
                extra = f"  [role={event.get('message', {}).get('role', '?')}]"
            elif etype in ("tool_execution_start", "tool_execution_end"):
                extra = f"  [tool={event.get('toolName', '?')}]"
            print(f"  #{event_count:03d} {etype}{extra}")

        print(f"\n  stdout closed. Total events: {event_count}")

    asyncio.create_task(read_stdout())

    # Wait for pi to initialize
    await asyncio.sleep(2)

    prompts = [
        ("Initial prompt", PROMPT_1, 60),
        ("Nested sub-agent prompt", PROMPT_2, 600),
    ]

    for label, prompt, max_checks in prompts:
        print(f"\n>>> [{label}] {prompt[:80]}...")
        msg = json.dumps({"type": "prompt", "message": prompt}) + "\n"
        proc.stdin.write(msg.encode("utf-8"))
        await proc.stdin.drain()

        # Wait for agent_settled
        settled = False
        for _ in range(max_checks):
            await asyncio.sleep(0.5)
            content = OUT_RPC.read_text().strip().split("\n")
            if content:
                last = json.loads(content[-1])
                if last.get("event", {}).get("type") == "agent_settled":
                    settled = True
                    break
        if not settled:
            print(f"  ⚠ agent did not settle within timeout ({max_checks * 0.5}s), continuing...")
        else:
            print("  ✓ agent settled")

    print(f"\nDone! {event_count} RPC events captured in {OUT_RPC}")

    # Copy the native session file
    native = find_latest_session(work_dir)
    if native:
        out_native = PROJECT_ROOT / "data-samples" / "nested_subagent_session.jsonl"
        shutil.copy2(native, out_native)
        print(f"Native session copied: {native} → {out_native}")
    else:
        print("⚠ No native session file found — pi may not have saved one (--no-session was not used)")

    # Terminate
    await asyncio.sleep(1)
    proc.terminate()
    await proc.wait()
    print("pi process terminated.")


if __name__ == "__main__":
    asyncio.run(main())
