"""Capture raw pi --mode rpc events to a JSONL file for dataset inspection."""

import asyncio
import json
import signal
import sys
from datetime import datetime
from pathlib import Path

OUT = Path("rpc_capture.jsonl")

async def main():
    OUT.write_text("")  # truncate
    print(f"Spawning pi --mode rpc (output → {OUT})")

    proc = await asyncio.create_subprocess_exec(
        "pi", "--mode", "rpc", "--no-session", "--approve",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
            OUT.write_text(OUT.read_text() + json.dumps(record) + "\n")

            # Print summary to terminal
            etype = event.get("type", "?")
            extra = ""
            if etype == "message_update":
                aev = event.get("assistantMessageEvent", {})
                extra = f"  [{aev.get('type', '?')}]"
            elif etype == "message_start":
                extra = f"  [role={event.get('message', {}).get('role', '?')}]"
            elif etype == "message_end":
                extra = f"  [role={event.get('message', {}).get('role', '?')}]"
            print(f"  #{event_count:03d} {etype}{extra}")

        print(f"\n  stdout closed. Total events: {event_count}")

    reader = asyncio.create_task(read_stdout())

    # Wait a moment for pi to initialize
    await asyncio.sleep(2)

    # Send test prompts that trigger reasoning + tool use
    prompts = [
        "What files are in the current directory? List them using a tool.",
        "Read the server.py file and summarize what it does in 2 sentences.",
    ]

    for prompt in prompts:
        print(f"\n>>> Sending prompt: {prompt}")
        msg = json.dumps({"type": "prompt", "message": prompt}) + "\n"
        proc.stdin.write(msg.encode("utf-8"))
        await proc.stdin.drain()

        # Wait for agent to settle (watch for agent_settled)
        settled = False
        for _ in range(120):  # up to 60s
            await asyncio.sleep(0.5)
            # Check last line for agent_settled
            content = OUT.read_text().strip().split("\n")
            if content:
                last = json.loads(content[-1])
                if last.get("event", {}).get("type") == "agent_settled":
                    settled = True
                    break
        if not settled:
            print("  ⚠ agent did not settle within timeout, continuing anyway...")
        else:
            print("  ✓ agent settled")

    print(f"\nDone! {event_count} events captured in {OUT}")
    print("Press Ctrl+C to stop if pi is still running...")

    # Keep alive briefly so user can inspect
    await asyncio.sleep(2)
    proc.terminate()
    await proc.wait()
    print("pi process terminated.")

if __name__ == "__main__":
    asyncio.run(main())
