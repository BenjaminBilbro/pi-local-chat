#!/usr/bin/env python3
"""Compare subagent data between session.jsonl and rpc_capture.jsonl formats."""

import json
import sys
from collections import Counter
from pathlib import Path

SESSION_FILE = Path("/home/bbilbro/pi-chat/data-samples/nested_subagent_session.jsonl")
RPC_FILE = Path("/home/bbilbro/pi-chat/data-samples/nested_subagent_rpc_capture.jsonl")


def parse_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    records = []
    for i, line in enumerate(lines, 1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"  [WARN] Failed to parse line {i}: {e}")
    return records


def find_subagent_events(records: list[dict]) -> list[tuple[int, dict]]:
    """Find all events mentioning 'subagent' in any field."""
    results = []
    for idx, record in enumerate(records):
        text = json.dumps(record).lower()
        if "subagent" in text:
            results.append((idx, record))
    return results


def find_toolcalls_in_messages(records: list[dict]) -> list[tuple[int, dict]]:
    """Find all messages with toolCall content items."""
    results = []
    for idx, record in enumerate(records):
        event = record.get("event", record)
        # Check in message records
        if event.get("type") == "message":
            msg = event.get("message", {})
            content = msg.get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("type") == "toolCall":
                    results.append((idx, record, item))
        # Check in agent_end messages
        if event.get("type") == "agent_end":
            messages = event.get("messages", [])
            for msg in messages:
                content = msg.get("content", [])
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "toolCall":
                        results.append((idx, record, item))
    return results


def print_section(title: str):
    print(f"\n{'=' * 80}")
    print(f" {title}")
    print(f"{'=' * 80}\n")


def print_subsection(title: str):
    print(f"\n--- {title} ---\n")


def analyze_rpc_capture(records: list[dict]):
    print_section("RPC CAPTURE ANALYSIS")

    # Count event types
    event_types = Counter()
    for record in records:
        event = record.get("event", record)
        event_types[event.get("type", "unknown")] += 1

    print("Event type counts:")
    for etype, count in event_types.most_common():
        print(f"  {etype}: {count}")

    # Find all subagent-related events
    subagent_events = find_subagent_events(records)
    print(f"\nFound {len(subagent_events)} events mentioning 'subagent':")

    for idx, event in subagent_events:
        raw = json.dumps(event, indent=2, ensure_ascii=False)
        print(f"\n[Line {idx}] Event type: {event.get('event', event).get('type')}")

        # For tool_execution_update, extract key info
        inner = event.get("event", event)
        etype = inner.get("type")

        if etype == "tool_execution_start":
            print(f"  toolCallId: {inner.get('toolCallId')}")
            print(f"  toolName: {inner.get('toolName')}")
            print(f"  args: {json.dumps(inner.get('args'), ensure_ascii=False)}")

        elif etype == "tool_execution_update":
            print(f"  toolCallId: {inner.get('toolCallId')}")
            print(f"  toolName: {inner.get('toolName')}")
            partial = inner.get("partialResult", {})
            content = partial.get("content", [])
            details = partial.get("details", {})
            results = details.get("results", [])
            print(f"  partialResult.content ({len(content)} items):")
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text", "")
                    print(f"    text: {text[:200]}...")
            if results:
                result = results[0]
                print(f"  partialResult.details.results[0]:")
                print(f"    activeToolExecutions: {json.dumps(result.get('activeToolExecutions', [])[:1], ensure_ascii=False)}")
                print(f"    usage: {json.dumps(result.get('usage', {}), ensure_ascii=False)}")
                print(f"    messages count: {len(result.get('messages', []))}")
                # Look for tool calls in the messages
                for msg in result.get("messages", []):
                    if msg.get("role") == "assistant":
                        for item in msg.get("content", []):
                            if isinstance(item, dict) and item.get("type") == "toolCall":
                                print(f"    -> toolCall: {item.get('name')} id={item.get('id', '')[:20]} args={json.dumps(item.get('arguments', {}))[:100]}")

        elif etype == "tool_execution_end":
            print(f"  toolCallId: {inner.get('toolCallId')}")
            print(f"  toolName: {inner.get('toolName')}")
            print(f"  isError: {inner.get('isError')}")
            result = inner.get("result", {})
            content = result.get("content", [])
            print(f"  result.content ({len(content)} items):")
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text", "")
                    if "PI_SUBAGENT_RECEIPT" in text or "PI_SUBAGENT_FAILURE" in text:
                        # Extract the JSON part
                        json_start = text.find("{")
                        if json_start >= 0:
                            try:
                                receipt = json.loads(text[json_start:])
                                print(f"    RECEIPT: {json.dumps(receipt, indent=2, ensure_ascii=False)[:500]}")
                            except json.JSONDecodeError:
                                print(f"    text (has RECEIPT marker but invalid JSON): {text[:200]}...")
                    else:
                        print(f"    text: {text[:200]}...")

        elif etype == "message_update":
            ame = inner.get("assistantMessageEvent", {})
            if ame.get("type") == "toolcall_end":
                tc = ame.get("toolCall", {})
                if tc.get("name") == "subagent":
                    print(f"  toolcall_end for subagent:")
                    print(f"    id: {tc.get('id')}")
                    print(f"    name: {tc.get('name')}")
                    print(f"    arguments: {json.dumps(tc.get('arguments'), ensure_ascii=False)[:300]}")

        elif etype == "message":
            msg = inner.get("message", {})
            if msg.get("role") == "toolResult" and msg.get("toolName") == "subagent":
                print(f"  toolResult message for subagent:")
                print(f"    toolCallId: {msg.get('toolCallId')}")
                print(f"    isError: {msg.get('isError')}")
                content = msg.get("content", [])
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text = c.get("text", "")
                        if "PI_SUBAGENT_RECEIPT" in text or "PI_SUBAGENT_FAILURE" in text:
                            json_start = text.find("{")
                            if json_start >= 0:
                                try:
                                    receipt = json.loads(text[json_start:])
                                    print(f"    RECEIPT: {json.dumps(receipt, indent=2, ensure_ascii=False)[:500]}")
                                except json.JSONDecodeError:
                                    print(f"    text (RECEIPT marker): {text[:200]}...")
                        else:
                            print(f"    text: {text[:200]}...")
                details = msg.get("details", {})
                results = details.get("results", [])
                if results:
                    print(f"    details.results[0]:")
                    r = results[0]
                    print(f"      usage: {json.dumps(r.get('usage', {}), ensure_ascii=False)}")
                    print(f"      messages count: {len(r.get('messages', []))}")
                    # Extract inner tool calls
                    for msg2 in r.get("messages", []):
                        if msg2.get("role") == "assistant":
                            for item in msg2.get("content", []):
                                if isinstance(item, dict) and item.get("type") == "toolCall" and item.get("name") != "subagent":
                                    print(f"      -> INNER toolCall: {item.get('name')} args={json.dumps(item.get('arguments', {}))[:100]}")

        else:
            print(f"  Full event (first 500 chars): {raw[:500]}...")


def analyze_session_file(records: list[dict]):
    print_section("SESSION FILE ANALYSIS")

    # Count record types
    record_types = Counter()
    for record in records:
        record_types[record.get("type", "unknown")] += 1

    print("Record type counts:")
    for rtype, count in record_types.most_common():
        print(f"  {rtype}: {count}")

    # Find all messages
    messages = []
    for idx, record in enumerate(records):
        if record.get("type") == "message":
            msg = record.get("message", {})
            messages.append((idx, msg))

    print(f"\nFound {len(messages)} message records:")

    for idx, msg in messages:
        role = msg.get("role")
        content = msg.get("content", [])
        print(f"\n[Line {idx}] role={role}, content items={len(content)}")

        for i, item in enumerate(content):
            if not isinstance(item, dict):
                print(f"  [{i}] raw: {str(item)[:200]}")
                continue

            itype = item.get("type", "unknown")
            if itype == "text":
                text = item.get("text", "")
                print(f"  [{i}] text: {text[:200]}...")
            elif itype == "thinking":
                thinking = item.get("thinking", "")
                print(f"  [{i}] thinking: {thinking[:200]}...")
            elif itype == "toolCall":
                print(f"  [{i}] toolCall:")
                print(f"      id: {item.get('id')}")
                print(f"      name: {item.get('name')}")
                print(f"      arguments: {json.dumps(item.get('arguments'), ensure_ascii=False)[:300]}")
                # Check for attached metadata
                for key in item:
                    if key.startswith("_"):
                        print(f"      {key}: {json.dumps(item.get(key), ensure_ascii=False)[:500]}")
                # Print ALL keys
                all_keys = list(item.keys())
                if len(all_keys) > 3:
                    print(f"      ALL keys: {all_keys}")
            else:
                print(f"  [{i}] {itype}: {json.dumps(item, ensure_ascii=False)[:200]}")

    # Also check agent_end events (in case messages are there)
    print_subsection("Checking for agent_end events with messages")
    for idx, record in enumerate(records):
        event = record.get("event", record)
        if event.get("type") == "agent_end":
            msgs = event.get("messages", [])
            print(f"\n[Line {idx}] agent_end with {len(msgs)} messages")
            for mi, msg in enumerate(msgs):
                role = msg.get("role")
                content = msg.get("content", [])
                print(f"  msg[{mi}] role={role}, content={len(content)} items")
                for ci, item in enumerate(content):
                    if isinstance(item, dict) and item.get("type") == "toolCall":
                        tc = item
                        print(f"    [{ci}] toolCall: {tc.get('name')} id={tc.get('id', '')[:20]}")
                        for key in item:
                            if key.startswith("_"):
                                print(f"      {key}: {json.dumps(item.get(key), ensure_ascii=False)[:300]}")


def main():
    print("Parsing JSONL files...\n")

    session_records = parse_jsonl(SESSION_FILE)
    rpc_records = parse_jsonl(RPC_FILE)

    print(f"Session file: {len(session_records)} records")
    print(f"RPC capture: {len(rpc_records)} records")

    analyze_rpc_capture(rpc_records)
    analyze_session_file(session_records)

    print_section("COMPARISON SUMMARY")

    # Extract subagent data from RPC capture
    print("RPC Capture — subagent data available:")
    subagent_events = find_subagent_events(rpc_records)
    for idx, event in subagent_events:
        inner = event.get("event", event)
        etype = inner.get("type")
        if etype == "tool_execution_end":
            result = inner.get("result", {})
            content = result.get("content", [])
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text", "")
                    if "PI_SUBAGENT_RECEIPT" in text:
                        json_start = text.find("{")
                        if json_start >= 0:
                            try:
                                receipt = json.loads(text[json_start:])
                                print(f"  RECEIPT: status={receipt.get('status')}, summary={receipt.get('summary', '')[:100]}")
                                print(f"    changedFiles: {receipt.get('changedFiles', [])}")
                            except:
                                pass
                    elif "PI_SUBAGENT_FAILURE" in text:
                        json_start = text.find("{")
                        if json_start >= 0:
                            try:
                                failure = json.loads(text[json_start:])
                                print(f"  FAILURE: error={failure.get('error', '')[:100]}")
                            except:
                                pass
            # Check for details/results with inner tool calls
            details_key = None
            for c in content:
                if isinstance(c, dict) and "details" in c:
                    details_key = c.get("details")
                    break
            # Check toolResult message format
            if inner.get("type") == "message" and inner.get("message", {}).get("role") == "toolResult":
                msg = inner.get("message", {})
                details = msg.get("details", {})
                results = details.get("results", [])
                if results:
                    r = results[0]
                    usage = r.get("usage", {})
                    print(f"  Inner usage: turns={usage.get('turns')}, maxTurns={usage.get('maxTurnsLimit')}")

    print("\nSession File — subagent toolCall data available:")
    for idx, record in enumerate(session_records):
        if record.get("type") != "message":
            continue
        msg = record.get("message", {})
        if msg.get("role") != "assistant":
            continue
        for item in msg.get("content", []):
            if isinstance(item, dict) and item.get("type") == "toolCall" and item.get("name") == "subagent":
                print(f"  toolCall id={item.get('id', '')[:20]}")
                print(f"    arguments: {json.dumps(item.get('arguments'), ensure_ascii=False)[:200]}")
                for key in item:
                    if key.startswith("_"):
                        print(f"    {key}: {json.dumps(item.get(key), ensure_ascii=False)[:300]}")
                if not any(k.startswith("_") for k in item):
                    print(f"    NO attached metadata (_toolCalls, _summary, _status, etc.)")

    print("\n\nKEY FINDING: The session file toolCall items may NOT have the _toolCalls, _summary, _status fields")
    print("that the history.js renderer expects. These are supposed to be attached by sessions.py")
    print("via _attach_subagent_details(), which cross-references with _collect_subagent_tool_calls().")
    print("\nThe _collect_subagent_tool_calls() function looks for tool_execution_update events in the JSONL,")
    print("but the native session format stores 'type:message' records, NOT wrapped 'event' records.")
    print("This means the subagent detail collection may not work for native session format!")


if __name__ == "__main__":
    main()
