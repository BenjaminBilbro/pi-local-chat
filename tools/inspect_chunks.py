#!/usr/bin/env python3
"""Interactive chunk inspector for StreamingSpeechChunker.

Usage:
    uv run python tools/inspect_chunks.py              # interactive mode
    uv run python tools/inspect_chunks.py "Your text here."  # single-shot mode
    uv run python tools/inspect_chunks.py --rpc-capture FILE  # replay real RPC deltas
    uv run python tools/inspect_chunks.py --simulate-stream file.txt  # simulate streaming deltas

Shows exactly what chunks get emitted before they reach OmniVoice.
"""

import json
import sys
import time

from pi_chat.tts_chunking import StreamingSpeechChunker


def single_shot(text: str) -> None:
    """Feed all text at once and show chunks."""
    print(f"Input ({len(text)} chars):\n{text}\n")
    print("-" * 60)

    chunker = StreamingSpeechChunker()
    chunks = chunker.feed(text)
    chunks.extend(chunker.finish())

    print(f"Emitted {len(chunks)} chunk(s):\n")
    for i, chunk in enumerate(chunks, 1):
        print(f"  [{i}] ({len(chunk)} chars): {chunk}")


def interactive() -> None:
    """Interactive REPL-style input."""
    print("Interactive chunk inspector")
    print("- Type text and press Enter to feed it")
    print("- Type '.flush' to flush the text block")
    print("- Type '.finish' to finish and reset")
    print("- Type '.quit' to exit")
    print()

    chunker = StreamingSpeechChunker()
    now = 0.0

    while True:
        try:
            line = input("feed> ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if line == ".quit":
            break
        elif line == ".finish":
            chunks = chunker.finish()
            if chunks:
                print(f"finish -> {len(chunks)} chunk(s):")
                for i, c in enumerate(chunks, 1):
                    print(f"  [{i}] ({len(c)} chars): {c}")
            else:
                print("finish -> (no chunks)")
            chunker.reset()
            now = 0.0
        elif line == ".flush":
            chunks = chunker.flush_text_block(now=now)
            if chunks:
                print(f"flush -> {len(chunks)} chunk(s):")
                for i, c in enumerate(chunks, 1):
                    print(f"  [{i}] ({len(c)} chars): {c}")
            else:
                print("flush -> (no chunks)")
            now = 0.0
        elif line:
            now += 0.05  # simulate small time between feeds
            chunks = chunker.feed(line, now=now)
            if chunks:
                print(f"feed -> {len(chunks)} chunk(s):")
                for i, c in enumerate(chunks, 1):
                    print(f"  [{i}] ({len(c)} chars): {c}")
            else:
                print("feed -> (no chunks yet)")
            print(f"  pending: {chunker.pending_characters} chars")
        print()


def _ts_to_seconds(ts_str: str) -> float:
    """Convert ISO timestamp from RPC capture to seconds since first event."""
    # Parse ISO 8601: "2026-08-02T14:23:19.119"
    base = "2000-01-01T00:00:00.000"
    from datetime import datetime
    dt = datetime.fromisoformat(ts_str)
    base_dt = datetime.fromisoformat(base)
    return (dt - base_dt).total_seconds()


def replay_rpc_capture(path: str) -> None:
    """Replay text_delta events from an RPC capture file.

    Mirrors exactly how voice_session.py handles events in a live run:
    - Uses actual timestamps from the RPC capture for timing
    - Arms hold timers when pending chars >= 72 (matching _feed_delta behavior)
    - Processes hold timer flushes at their scheduled time (matching _hold_timer)
    - text_end calls flush_text_block() and cancels hold timer
    - agent_settled calls finish()
    """
    print(f"Replaying RPC capture: {path}\n")
    print("-" * 60)

    chunker = StreamingSpeechChunker()
    all_chunks: list[str] = []
    event_count = 0
    first_ts: float | None = None

    # Hold timer state (mirrors _arm_hold_timer / _hold_timer)
    hold_fire_time: float | None = None

    def _process_hold_timer(now: float) -> None:
        """Process hold timer if it should fire by now."""
        nonlocal hold_fire_time
        while hold_fire_time is not None and now >= hold_fire_time:
            fire_time = hold_fire_time
            hold_fire_time = None
            # Feed empty string to trigger max-hold flush (matches _flush_pending_on_hold)
            chunks = chunker.feed("", now=fire_time)
            if chunks:
                for c in chunks:
                    all_chunks.append(c)
                    print(f"[hold_timer {fire_time:.3f}s] -> chunk [{len(all_chunks):2d}] ({len(c):3d} chars): {c}")

    def _arm_hold_timer(now: float, hold_ms: float = 450.0) -> None:
        """Arm the hold timer (matches _arm_hold_timer)."""
        nonlocal hold_fire_time
        hold_fire_time = now + hold_ms / 1000.0

    def _cancel_hold_timer() -> None:
        """Cancel the hold timer (matches _flush_text_block cleanup)."""
        nonlocal hold_fire_time
        hold_fire_time = None

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            event = record.get("event", {})
            event_type = event.get("type")
            ts_str = record.get("ts", "")

            # Compute now from actual timestamp
            if ts_str:
                if first_ts is None:
                    first_ts = _ts_to_seconds(ts_str)
                now = _ts_to_seconds(ts_str) - first_ts
            else:
                now = 0.0

            # Process any pending hold timer before this event
            _process_hold_timer(now)

            if event_type == "message_update":
                ame = event.get("assistantMessageEvent", {})
                sub_type = ame.get("type")

                if sub_type == "text_delta":
                    delta = ame.get("delta", "") or ame.get("content", "")
                    if delta:
                        event_count += 1
                        chunks = chunker.feed(delta, now=now)
                        if chunks:
                            for c in chunks:
                                all_chunks.append(c)
                                print(f"[delta {event_count:4d}] -> chunk [{len(all_chunks):2d}] ({len(c):3d} chars): {c}")

                        # Arm hold timer if enough pending text (matches _feed_delta)
                        if chunker.pending_characters >= 72:
                            _arm_hold_timer(now)

                elif sub_type == "text_end":
                    # Flush current text block and cancel hold timer (matches _flush_text_block)
                    _cancel_hold_timer()
                    chunks = chunker.flush_text_block(now=now)
                    if chunks:
                        for c in chunks:
                            all_chunks.append(c)
                            print(f"[text_end] -> chunk [{len(all_chunks):2d}] ({len(c):3d} chars): {c}")

            elif event_type == "agent_settled":
                # Cancel hold timer, then finish (matches _on_agent_settled)
                _cancel_hold_timer()
                chunks = chunker.finish()
                if chunks:
                    for c in chunks:
                        all_chunks.append(c)
                        print(f"[agent_settled] -> chunk [{len(all_chunks):2d}] ({len(c):3d} chars): {c}")

    print("-" * 60)
    print(f"Total: {len(all_chunks)} chunks from {event_count} text_delta events")
    total_chars = sum(len(c) for c in all_chunks)
    print(f"Total chunk characters: {total_chars}")


def simulate_stream(text: str, chunk_size: int = 15, delay_ms: float = 10) -> None:
    """Simulate streaming deltas from text."""
    print(f"Simulating stream ({len(text)} chars, chunk_size={chunk_size}, delay={delay_ms}ms)\n")
    print("Input text:\n{text}\n")
    print("-" * 60)

    chunker = StreamingSpeechChunker()
    now = 0.0
    all_chunks: list[str] = []
    pos = 0

    while pos < len(text):
        delta = text[pos : pos + chunk_size]
        pos += chunk_size
        now += delay_ms / 1000.0
        chunks = chunker.feed(delta, now=now)
        if chunks:
            for c in chunks:
                all_chunks.append(c)
                print(f"[{len(all_chunks):2d}] ({len(c):3d} chars): {c}")

    # Flush remaining
    chunks = chunker.flush_text_block(now=now)
    if chunks:
        for c in chunks:
            all_chunks.append(c)
            print(f"[{len(all_chunks):2d}] ({len(c):3d} chars): {c}")

    chunks = chunker.finish()
    if chunks:
        for c in chunks:
            all_chunks.append(c)
            print(f"[{len(all_chunks):2d}] ({len(c):3d} chars): {c}")

    print("-" * 60)
    print(f"Total: {len(all_chunks)} chunks")


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1].startswith("--"):
        print("Usage:")
        print("  uv run python tools/inspect_chunks.py                    # interactive")
        print('  uv run python tools/inspect_chunks.py "Your text here."  # single-shot')
        print("  uv run python tools/inspect_chunks.py --rpc-capture FILE    # replay RPC deltas")
        print("  uv run python tools/inspect_chunks.py --simulate-stream FILE # streaming sim")
        sys.exit(0)

    if len(sys.argv) >= 3 and sys.argv[1] == "--rpc-capture":
        path = sys.argv[2]
        try:
            replay_rpc_capture(path)
        except FileNotFoundError:
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) >= 3 and sys.argv[1] == "--simulate-stream":
        path = sys.argv[2]
        try:
            text = open(path, "r", encoding="utf-8").read()
        except FileNotFoundError:
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        simulate_stream(text)
    elif len(sys.argv) == 2:
        single_shot(sys.argv[1])
    else:
        interactive()


if __name__ == "__main__":
    main()
