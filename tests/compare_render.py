"""Fixture-driven checks for historical/live timeline parity.

Each RPC capture is rendered twice through production modules:

1. Parsed final messages -> static/history.js
2. Raw event sequence -> static/chat.js

The settled assistant DOM must match exactly. Native session fixtures also
verify that consecutive assistant turns collapse into one run.
"""

from __future__ import annotations

import difflib
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "data-samples"
DEFAULT_FIXTURES = [
    DATA / "rpc_capture.jsonl",
    DATA / "subagent_rpc_capture.jsonl",
    DATA / "2026-07-21T23-24-02-074Z_019f86fe-529a-7b98-97c8-0a06b7075767.jsonl",
]

sys.path.insert(0, str(PROJECT_ROOT))
from pi_chat.sessions import (  # noqa: E402
    _extract_receipt_status,
    _record_subagent_result,
    parse_jsonl_messages,
)


def render_result(
    mode: str,
    payload: list[dict],
    **options: object,
) -> dict:
    result = subprocess.run(
        ["node", str(PROJECT_ROOT / "tests" / "render_message.js")],
        input=json.dumps(
            {
                "mode": mode,
                "messages" if mode == "history" else "events": payload,
                **options,
            },
            ensure_ascii=False,
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Node {mode} render failed:\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def render(mode: str, payload: list[dict]) -> list[str]:
    return render_result(mode, payload)["assistantHtml"]


def load_rpc_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = record.get("event")
        if isinstance(event, dict):
            events.append(event)
    return events


def expected_run_count(messages: list[dict]) -> int:
    count = 0
    waiting_for_assistant = False
    for message in messages:
        if message.get("role") == "user":
            waiting_for_assistant = True
        elif message.get("role") == "assistant" and waiting_for_assistant:
            count += 1
            waiting_for_assistant = False
    return count


def readable_html(html: str) -> list[str]:
    return html.replace("><", ">\n<").splitlines()


def compare_fixture(path: Path) -> bool:
    messages = parse_jsonl_messages(str(path))
    if not messages:
        print(f"FAIL {path.name}: parser returned no messages")
        return False

    historical = render("history", messages)
    expected = expected_run_count(messages)
    if expected == 0:
        print(f"FAIL {path.name}: parser returned no assistant runs")
        return False

    if len(historical) != expected:
        print(
            f"FAIL {path.name}: history rendered {len(historical)} "
            f"assistant blocks for {expected} runs"
        )
        return False

    events = load_rpc_events(path)
    if not any(event.get("type") == "agent_start" for event in events):
        print(f"PASS {path.name}: {expected} historical assistant run")
        return True

    live = render("live", events)
    if historical == live:
        print(
            f"PASS {path.name}: {len(live)} settled live/history "
            f"assistant block(s) match"
        )
        return True

    print(
        f"FAIL {path.name}: history has {len(historical)} block(s), "
        f"live has {len(live)}"
    )
    for index, (history_html, live_html) in enumerate(
        zip(historical, live, strict=False)
    ):
        if history_html == live_html:
            continue
        print(f"  first mismatch: assistant block {index}")
        diff = difflib.unified_diff(
            readable_html(history_html),
            readable_html(live_html),
            fromfile="history",
            tofile="live",
            lineterm="",
        )
        for line in list(diff)[:80]:
            print(f"  {line}")
        break
    return False


def verify_structured_receipts() -> bool:
    native_record = json.loads(
        (DATA / "full-nested-sub-agent-tool-result.json").read_text()
    )
    rpc_record = json.loads(
        (
            DATA / "full-nested-sub-agent-tool-result-from-rpc.json"
        ).read_text()
    )

    native_message = native_record["message"]
    rpc_message = next(
        message
        for message in rpc_record["event"]["messages"]
        if message.get("role") == "toolResult"
        and message.get("toolName") == "subagent"
    )

    snapshots = []
    for message in (native_message, rpc_message):
        calls: dict[str, dict] = {}
        _record_subagent_result(calls, message)
        snapshots.append(next(iter(calls.values())))

    fields = (
        "status",
        "summary",
        "turns",
        "maxTurns",
        "timelineMessages",
    )
    if any(snapshots[0].get(field) != snapshots[1].get(field) for field in fields):
        print("FAIL nested structured receipts differ between native and RPC")
        return False

    snapshot = snapshots[0]
    if (
        snapshot.get("status") != "completed"
        or not snapshot.get("summary")
        or snapshot.get("turns") != 4
        or snapshot.get("maxTurns") != 20
    ):
        print("FAIL nested structured receipt fields were not preserved")
        return False

    print("PASS nested structured receipt overrides malformed marker text")
    return True


def verify_final_subagent_snapshot() -> bool:
    partial_message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "partial"}],
    }
    final_message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "final-only"}],
    }
    calls = {
        "tool-1": {
            "timelineMessages": [partial_message],
            "turns": 1,
            "maxTurns": 4,
        }
    }
    result = {
        "content": [],
        "details": {
            "results": [{
                "messages": [partial_message, final_message],
                "usage": {"turns": 2},
                "maxTurnsLimit": 4,
                "receipt": {
                    "status": "completed",
                    "summary": "done",
                },
            }]
        },
    }

    _extract_receipt_status(calls, "tool-1", result, is_error=False)
    snapshot = calls["tool-1"]
    if (
        snapshot.get("timelineMessages") != [partial_message, final_message]
        or snapshot.get("turns") != 2
        or snapshot.get("maxTurns") != 4
    ):
        print("FAIL final tool event did not replace the partial sub-agent snapshot")
        return False

    print("PASS final tool event replaces the partial sub-agent snapshot")
    return True


def verify_retry_parity() -> bool:
    user = {
        "role": "user",
        "content": [{"type": "text", "text": "retry"}],
    }
    first = {
        "role": "assistant",
        "content": [{"type": "text", "text": "first attempt"}],
    }
    second = {
        "role": "assistant",
        "content": [{"type": "text", "text": "second attempt"}],
    }
    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": first},
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_end",
                "content": "first attempt",
            },
        },
        {"type": "message_end", "message": first},
        {"type": "agent_end", "messages": [user, first]},
        {"type": "agent_start"},
        {"type": "message_start", "message": second},
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_end",
                "content": "second attempt",
            },
        },
        {"type": "message_end", "message": second},
        {"type": "agent_end", "messages": [second]},
        {"type": "agent_settled"},
    ]

    historical = render("history", [user, first, second])
    live = render("live", events)
    if historical != live or len(live) != 1:
        print("FAIL retry lifecycle did not settle to one historical run")
        return False

    print("PASS retry lifecycle settles to one assistant run")
    return True


def verify_multiple_content_blocks() -> bool:
    assistant = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "first block"},
            {"type": "text", "text": "second block"},
        ],
    }
    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": assistant},
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_end",
                "contentIndex": 0,
                "content": "first block",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_end",
                "contentIndex": 1,
                "content": "second block",
            },
        },
        {"type": "message_end", "message": assistant},
        {"type": "agent_settled"},
    ]

    historical = render("history", [assistant])
    live = render("live", events)
    if historical != live:
        print("FAIL multiple streamed text blocks did not retain their order")
        return False

    print("PASS multiple streamed text blocks retain their order")
    return True


def verify_generic_subagent_failure() -> bool:
    message = {
        "role": "toolResult",
        "toolName": "subagent",
        "toolCallId": "failed-subagent",
        "isError": True,
        "content": [{"type": "text", "text": "plain failure"}],
    }
    calls: dict[str, dict] = {}
    _record_subagent_result(calls, message)
    snapshot = calls["failed-subagent"]

    if (
        snapshot.get("status") != "failed"
        or snapshot.get("isError") is not True
        or snapshot.get("fallbackText") != "plain failure"
    ):
        print("FAIL generic sub-agent failure was not normalized")
        return False

    print("PASS generic sub-agent failure is normalized")
    return True


def verify_prompt_failure_unlocks_composer() -> bool:
    result = render_result(
        "live",
        [{
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "model unavailable",
        }],
        submitText="first attempt",
        afterEventsComposerText="retry",
    )
    if result["sendDisabled"] or "model unavailable" not in result["assistantHtml"][0]:
        print("FAIL failed prompt response left the composer locked")
        return False

    print("PASS failed prompt response unlocks the composer")
    return True


def verify_disconnect_unlocks_composer() -> bool:
    result = render_result(
        "live",
        [{"type": "agent_start"}],
        submitText="first attempt",
        disconnect=True,
        afterEventsComposerText="retry",
    )
    if result["sendDisabled"]:
        print("FAIL disconnect left the composer locked")
        return False

    print("PASS disconnect unlocks the composer")
    return True


def verify_handled_prompt_unlocks_composer() -> bool:
    result = render_result(
        "live",
        [{
            "type": "response",
            "command": "prompt",
            "success": True,
        }],
        submitText="/handled-command",
        afterEventsComposerText="next prompt",
        waitAfterEventsMs=2100,
    )
    if result["sendDisabled"]:
        print("FAIL handled prompt without an agent lifecycle stayed locked")
        return False

    print("PASS handled prompt without an agent lifecycle unlocks")
    return True


def verify_subagent_interaction_survives_settlement() -> bool:
    events = load_rpc_events(DATA / "subagent_rpc_capture.jsonl")
    result = render_result(
        "live",
        events,
        collapseBeforeEventType="agent_end",
    )
    html = result["assistantHtml"][0]
    if (
        'aria-expanded="false"' not in html
        or not result["focusedToolCallId"]
    ):
        print("FAIL sub-agent collapse/focus state was lost at settlement")
        return False

    print("PASS sub-agent collapse/focus state survives settlement")
    return True


def main() -> int:
    fixtures = (
        [Path(argument).resolve() for argument in sys.argv[1:]]
        if len(sys.argv) > 1
        else DEFAULT_FIXTURES
    )

    missing = [path for path in fixtures if not path.is_file()]
    if missing:
        for path in missing:
            print(f"Missing fixture: {path}", file=sys.stderr)
        return 2

    checks = [compare_fixture(path) for path in fixtures]
    checks.append(verify_structured_receipts())
    checks.append(verify_final_subagent_snapshot())
    checks.append(verify_retry_parity())
    checks.append(verify_multiple_content_blocks())
    checks.append(verify_generic_subagent_failure())
    checks.append(verify_prompt_failure_unlocks_composer())
    checks.append(verify_disconnect_unlocks_composer())
    checks.append(verify_handled_prompt_unlocks_composer())
    checks.append(verify_subagent_interaction_survives_settlement())
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
