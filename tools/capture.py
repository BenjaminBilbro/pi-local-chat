"""Unified RPC event capture CLI for pi --mode rpc.

Merges the functionality of capture_rpc.py, capture_subagent_rpc.py, and
capture_nested_subagent_rpc.py into a single parameterizable tool.

Usage:
    uv run python tests/capture.py --scenario general
    uv run python tests/capture.py --scenario subagent
    uv run python tests/capture.py --scenario nested
    uv run python tests/capture.py --prompt "custom prompt" --output data-samples/custom.jsonl
    uv run python tests/capture.py --prompt "custom prompt" --timeout 300
"""

import argparse
import asyncio
import json
import signal
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data-samples"
PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"

# Built-in scenarios
SCENARIOS = {
    "general": {
        "description": "General RPC events (thinking, tools, text)",
        "prompts": [
            ("Initial prompt", "What files are in the current directory? List them using a tool.", 120),
            ("Follow-up", "Read the server.py file and summarize what it does in 2 sentences.", 120),
        ],
        "pi_args": ["pi", "--mode", "rpc", "--no-session", "--approve"],
        "output": "data-samples/general_rpc_capture.jsonl",
        "copy_native": False,
    },
    "subagent": {
        "description": "Sub-agent spawn RPC events",
        "prompts": [
            ("Sub-agent prompt",
             "Spawn a sub-agent and have it do the following 3 simple tool calls: "
             "1) `ls -la /tmp` via bash, "
             "2) `python -c \"print(2+2)\"` via bash, "
             "3) `echo hello world` via bash. "
             "Then report back the results of all 3 commands.",
             600),
        ],
        "pi_args": ["pi", "--mode", "rpc", "--no-session", "--approve"],
        "output": "data-samples/subagent_rpc_capture.jsonl",
        "copy_native": False,
    },
    "nested": {
        "description": "Nested sub-agent RPC events + native session copy",
        "prompts": [
            ("Initial prompt", "What is the current date? Tell me briefly.", 60),
            ("Nested sub-agent prompt",
             "Spawn a sub-agent named 'outer' and ask it to do the following: "
             "make 3 simple tool calls (bash: `echo hello`, bash: `date`, bash: `whoami`), "
             "THEN have THAT sub-agent spawn another sub-agent named 'inner' that makes "
             "3 more simple tool calls (bash: `pwd`, bash: `uname -a`, bash: `echo done`). "
             "Finally report back all results from both sub-agents.",
             600),
        ],
        "pi_args": ["pi", "--mode", "rpc", "--approve"],
        "output": "data-samples/nested_subagent_rpc_capture.jsonl",
        "copy_native": True,
        "native_output": "data-samples/nested_subagent_session.jsonl",
    },
}


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


async def wait_for_settled(out_path: Path, max_checks: int) -> bool:
    """Poll output file for agent_settled event."""
    for _ in range(max_checks):
        await asyncio.sleep(0.5)
        content = out_path.read_text().strip().split("\n")
        if content:
            try:
                last = json.loads(content[-1])
                if last.get("event", {}).get("type") == "agent_settled":
                    return True
            except json.JSONDecodeError:
                continue
    return False


async def run_capture(pi_args: list[str], prompts: list[tuple[str, str, int]],
                      out_path: Path, use_work_dir: bool = False,
                      copy_native: bool = False, native_output: str | None = None):
    """Run a pi RPC capture session."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("")  # truncate

    work_dir = None
    if use_work_dir:
        work_dir = PROJECT_ROOT / "sessions" / "b"
        work_dir.mkdir(parents=True, exist_ok=True)
        print(f"Work dir: {work_dir}")

    print(f"Spawning {' '.join(pi_args)} (output → {out_path})")

    proc = await asyncio.create_subprocess_exec(
        *pi_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir) if work_dir else None,
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
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

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
            elif etype in ("tool_execution_start", "tool_execution_end"):
                extra = f"  [tool={event.get('toolName', '?')}]"
            print(f"  #{event_count:03d} {etype}{extra}")

        print(f"\n  stdout closed. Total events: {event_count}")

    asyncio.create_task(read_stdout())

    # Wait for pi to initialize
    await asyncio.sleep(2)

    # Send prompts
    for label, prompt, max_checks in prompts:
        print(f"\n>>> [{label}] {prompt[:80]}...")
        msg = json.dumps({"type": "prompt", "message": prompt}) + "\n"
        proc.stdin.write(msg.encode("utf-8"))
        await proc.stdin.drain()

        settled = await wait_for_settled(out_path, max_checks)
        if not settled:
            print(f"  ⚠ agent did not settle within timeout ({max_checks * 0.5}s), continuing...")
        else:
            print("  ✓ agent settled")

    print(f"\nDone! {event_count} RPC events captured in {out_path}")

    # Copy native session if requested
    if copy_native and work_dir:
        native = find_latest_session(work_dir)
        if native:
            dest = PROJECT_ROOT / native_output if native_output else PROJECT_ROOT / "data-samples" / "nested_subagent_session.jsonl"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(native, dest)
            print(f"Native session copied: {native} → {dest}")
        else:
            print("⚠ No native session file found")

    # Terminate
    await asyncio.sleep(1)
    proc.terminate()
    await proc.wait()
    print("pi process terminated.")


async def main_async(args):
    out_path = PROJECT_ROOT / args.output if args.output else None

    if args.scenario:
        if args.scenario not in SCENARIOS:
            print(f"Unknown scenario: {args.scenario}")
            print(f"Available scenarios: {', '.join(SCENARIOS.keys())}")
            sys.exit(1)

        scenario = SCENARIOS[args.scenario]
        prompts = scenario["prompts"]
        pi_args = scenario["pi_args"]
        output = scenario["output"]
        copy_native = scenario["copy_native"]
        native_output = scenario.get("native_output")

        # Use work dir for scenarios that don't use --no-session
        use_work_dir = "--no-session" not in pi_args

        if args.output:
            output = args.output

        await run_capture(
            pi_args=pi_args,
            prompts=prompts,
            out_path=PROJECT_ROOT / output,
            use_work_dir=use_work_dir,
            copy_native=copy_native,
            native_output=native_output,
        )

    elif args.prompt:
        # Custom prompt mode
        prompts = [("Custom", args.prompt, args.timeout)]
        await run_capture(
            pi_args=["pi", "--mode", "rpc", "--no-session", "--approve"],
            prompts=prompts,
            out_path=PROJECT_ROOT / (args.output or "data-samples/custom_rpc_capture.jsonl"),
            use_work_dir=False,
            copy_native=False,
        )
    else:
        print("Error: --scenario or --prompt is required")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Capture pi --mode rpc events to JSONL files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Built-in scenarios:
  general   General RPC events (thinking, tools, text)
  subagent  Sub-agent spawn RPC events
  nested    Nested sub-agent RPC events + native session copy

Examples:
  %(prog)s --scenario general
  %(prog)s --scenario subagent
  %(prog)s --scenario nested
  %(prog)s --prompt "custom prompt" --output data-samples/custom.jsonl
  %(prog)s --prompt "custom prompt" --timeout 300
"""
    )
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()),
                        help="Use a built-in capture scenario")
    parser.add_argument("--prompt", help="Custom prompt to send to pi")
    parser.add_argument("--output", help="Output JSONL path (relative to project root)")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Max checks to wait for agent_settled per prompt (default: 120)")

    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
