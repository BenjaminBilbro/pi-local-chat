#!/usr/bin/env python3
"""Read a debug-mode audit log as a timeline instead of as JSONL.

`PI_CHAT_AUDIT=1` writes the record; this turns it into something to argue with. The point is not prettier
output, it is that a bug report is a *sequence* - "I pressed mute, it kept talking, then nothing went through"
- and a wall of one-object-per-line JSON makes you reconstruct the sequence by hand every single time, which
is how twenty minutes goes into reading a log that says what happened in four lines.

    .venv/Scripts/python.exe tools/audit.py                     # newest log in logs/
    .venv/Scripts/python.exe tools/audit.py --latest --grep mute
    .venv/Scripts/python.exe tools/audit.py logs/audit-b-20261118-214401-ab12cd.jsonl --tail 40
    .venv/Scripts/python.exe tools/audit.py --json              # machine-readable, for diffing two runs

The checks at the bottom are deliberately few and each one is a claim about a fault, not a statistic: a voice
stream that produced no audio bytes, an echo window that opened and never closed, messages dropped to a dead
tab. Those three are the difference between "the model did not answer" and "the answer existed and never
arrived", which is the question a person in the middle of a dead microphone cannot answer for themselves.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")

# Data keys worth a column. Anything else is noise unless --verbose asks for the whole dict.
INTERESTING = (
    "delivery", "reason", "state", "streamId", "open", "why", "error", "failures",
    "kind", "id", "phase", "muted", "enabled", "code", "engine", "command", "window_open",
    "eventKind", "subKind", "chars", "account", "files",
)


def load(path: str) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"{path}:{number}: not JSON, skipped", file=sys.stderr)
    return events


def latest() -> str | None:
    files = glob.glob(os.path.join(LOG_DIR, "audit-*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None


def describe(event: dict, verbose: bool = False) -> str:
    data = event.get("data") or {}
    bits = [f"{key}={data[key]}" for key in INTERESTING if key in data]
    extra = {key: value for key, value in data.items() if key not in INTERESTING}
    if extra and verbose:
        # Off by default because the shaped leftovers are mostly `message=str(84 ab12cd34)`, which is one
        # fixed-width token per line of noise until someone is hunting for a specific field.
        bits.append(json.dumps(extra, separators=(",", ":"), default=str))
    elif extra:
        bits.append(f"+{len(extra)} field{'s' if len(extra) > 1 else ''}")
    return " ".join(bits)


def render(events: list[dict], tail: int | None, grep: str | None, verbose: bool) -> list[str]:
    lines = []
    for event in events:
        kind = str(event.get("type", "?"))
        src = event.get("src", "")
        direction = event.get("dir", "")
        stamp = event.get("dt") or 0
        if direction == "totals":
            totals = event.get("data") or {}
            body = ", ".join(f"{key} {value}" for key, value in totals.items() if value)
            if not body:
                continue
            text = f"{stamp:>9.3f}  ....  {kind:<18} {body}"
        else:
            arrow = {"in": "  <", "out": "  >", "event": "  #", "dom": "  @"}.get(direction, "  ?")
            who = "client" if src == "client" else "server"
            text = f"{stamp:>9.3f}{arrow} {kind:<22} {describe(event, verbose)} [{who}]"
        if grep and grep.lower() not in text.lower():
            continue
        lines.append(text)
    return lines[-tail:] if tail else lines


def checks(events: list[dict]) -> list[tuple[str, str]]:
    """Named faults, each one a sentence a person can act on.

    Kept short on purpose. A check that fires on a healthy session is worse than no check, because the first
    false alarm teaches everyone to skim the block.
    """
    found: list[tuple[str, str]] = []
    severity = {"fault": "FAULT", "note": "note "}

    window_open_at = None
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "aec_window":
            if data.get("open"):
                window_open_at = event.get("dt")
            else:
                window_open_at = None
    if window_open_at is not None:
        found.append(("fault", f"echo window opened at {window_open_at:.2f}s and never closed: voice mode "
                               f"treats your microphone as the assistant's own voice from that point on"))

    started = [e for e in events if e.get("type") == "voice_stream_start"]
    if started:
        last = started[-1]
        after = [e for e in events if (e.get("dt") or 0) >= (last.get("dt") or 0)]
        audio = sum(((e.get("data") or {}).get("bytes_out") or 0)
                    for e in after if e.get("type") == "totals")
        ended = any(e.get("type") == "voice_stream_end" for e in after)
        if audio == 0 and ended:
            found.append(("fault", f"the last voice stream opened and ended with zero audio bytes: the reply "
                                   f"was written and never synthesized or delivered"))

    dropped = sum((e.get("data") or {}).get("failures", 1)
                  for e in events if e.get("type") == "browser_send_failed")
    if dropped:
        found.append(("fault", f"{dropped} browser delivery failure(s): messages were produced and not sent"))

    dead = sum((e.get("data") or {}).get("size", 0) for e in events if e.get("type") == "browser_dropped")
    if dead:
        found.append(("fault", f"{dead} bytes were dropped because the browser was already marked dead"))

    if any(e.get("type") == "truncated" for e in events):
        found.append(("note", "the log hit its rotation ceiling; the tail of the session is not in this file"))
    elif any(e.get("type") == "rotating" for e in events):
        found.append(("note", "this file rolled over; the rest of the session is in the next -part file"))

    if not found:
        found.append(("note", "no fault matched the checks: if something felt wrong, read the timeline around "
                              "the moment you noticed it"))
    return [(severity[kind], text) for kind, text in found]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", help="an audit JSONL file; defaults to the newest in logs/")
    parser.add_argument("--latest", action="store_true", help="ignore any path and read the newest log")
    parser.add_argument("--grep", help="keep only lines containing this text (case-insensitive)")
    parser.add_argument("--tail", type=int, help="show only the last N matching lines")
    parser.add_argument("--verbose", action="store_true", help="show every data field, not just the ones that explain state")
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON instead of prose")
    args = parser.parse_args(argv)

    path = latest() if args.latest or not args.path else args.path
    if not path or not os.path.isfile(path):
        print(f"No audit log found. Enable it with PI_CHAT_AUDIT=1 and reload the page, or pass a path.\n"
              f"Looked in {LOG_DIR}", file=sys.stderr)
        return 2

    events = load(path)
    if args.json:
        print(json.dumps({"path": path, "events": len(events),
                          "checks": [text for _, text in checks(events)],
                          "types": dict(Counter(str(e.get("type")) for e in events))}))
        return 0

    counts = Counter(str(event.get("type")) for event in events)
    span = [event.get("dt") for event in events if isinstance(event.get("dt"), (int, float))]
    print(path)
    print(f"{len(events)} events over {max(span) - min(span):.1f}s" if span else f"{len(events)} events")
    print("busiest: " + ", ".join(f"{name} x{count}" for name, count in counts.most_common(6)))
    print()
    for line in render(events, args.tail, args.grep, args.verbose):
        print(line)
    print()
    for label, text in checks(events):
        print(f"[{label}] {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
