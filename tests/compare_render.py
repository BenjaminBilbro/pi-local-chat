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
from pi_chat.sessions import parse_jsonl_messages  # noqa: E402


def render_result(
    mode: str,
    payload: list[dict],
    **options: object,
) -> dict:
    result = subprocess.run(
        ["node", str(PROJECT_ROOT / "tools" / "render_message.js")],
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
        # Windows defaults pipes to the console codepage (cp1252), which corrupts any non-ASCII
        # session content in either direction. Be explicit: this protocol is UTF-8 JSON.
        encoding="utf-8",
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
    """Verify JS enrichToolCalls handles nested structured receipts correctly."""
    native_record = json.loads(
        (DATA / "full-nested-sub-agent-tool-result.json").read_text()
    )
    rpc_record = json.loads(
        (
            DATA / "full-nested-sub-agent-tool-result-from-rpc.json"
        ).read_text()
    )

    # Native fixture: single toolResult message — wrap with a dummy assistant
    native_tool_result = native_record["message"]
    native_tool_call_id = native_tool_result.get("toolCallId", "")
    native_assistant = {
        "role": "assistant",
        "content": [{
            "type": "toolCall",
            "id": native_tool_call_id,
            "name": "subagent",
            "arguments": {"name": "outer", "task": "test"},
        }],
    }
    native_messages = [native_assistant, native_tool_result]

    # RPC fixture: agent_end event already has the full message array
    rpc_messages = rpc_record["event"]["messages"]

    # Render both through JS
    native_result = render_result("history", native_messages)
    rpc_result = render_result("history", rpc_messages)

    native_html = native_result["assistantHtml"]
    rpc_html = rpc_result["assistantHtml"]

    if not native_html or not rpc_html:
        print("FAIL could not render native or RPC structured receipt")
        return False

    # Both should render a sub-agent card with completed status and summary
    if 'subagent-summary-status' not in native_html[0]:
        print("FAIL native structured receipt did not render status badge")
        return False
    if 'subagent-summary-status' not in rpc_html[0]:
        print("FAIL RPC structured receipt did not render status badge")
        return False

    # Both should contain the summary text
    if "completed 3 bash commands" not in native_html[0]:
        print("FAIL native structured receipt summary not rendered")
        return False
    if "completed 3 bash commands" not in rpc_html[0]:
        print("FAIL RPC structured receipt summary not rendered")
        return False

    print("PASS nested structured receipts render correctly in JS")
    return True


def verify_final_subagent_snapshot() -> bool:
    """Verify JS enrichToolCalls replaces partial snapshot with final result."""
    partial_message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "partial"}],
    }
    final_message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "final-only"}],
    }
    tool_result = {
        "role": "toolResult",
        "toolCallId": "tool-1",
        "toolName": "subagent",
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
    assistant = {
        "role": "assistant",
        "content": [{
            "type": "toolCall",
            "id": "tool-1",
            "name": "subagent",
            "arguments": {"name": "test", "task": "test task"},
        }],
    }
    messages = [assistant, tool_result]

    result = render_result("history", messages)
    html = result["assistantHtml"][0]

    # Should render the sub-agent timeline with final message
    if "final-only" not in html:
        print("FAIL final sub-agent message not rendered")
        return False
    if "completed" not in html:
        print("FAIL completed status not rendered")
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
    """Verify JS enrichToolCalls normalizes generic sub-agent failures."""
    tool_result = {
        "role": "toolResult",
        "toolName": "subagent",
        "toolCallId": "failed-subagent",
        "isError": True,
        "content": [{"type": "text", "text": "plain failure"}],
    }
    assistant = {
        "role": "assistant",
        "content": [{
            "type": "toolCall",
            "id": "failed-subagent",
            "name": "subagent",
            "arguments": {"name": "test", "task": "test task"},
        }],
    }
    messages = [assistant, tool_result]

    result = render_result("history", messages)
    html = result["assistantHtml"][0]

    if "plain failure" not in html:
        print("FAIL generic failure text not rendered")
        return False
    if "is-error" not in html:
        print("FAIL error state not rendered")
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


def verify_failed_model_call_leaves_an_error_bubble() -> bool:
    """A turn where the provider gave up must not settle to an empty timeline.

    pi retries transient provider failures (503/529) itself and can end the assistant message
    with stopReason 'error' and no errorMessage. The settled timeline used to be empty, so a
    failed turn looked like a successful one with no answer.
    """
    failed_message = {"role": "assistant", "content": [], "stopReason": "error"}
    events = [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": failed_message},
        {"type": "message_end", "message": failed_message},
        {"type": "agent_end", "messages": [failed_message]},
        {"type": "agent_settled"},
    ]
    html = "".join(render_result("live", events)["assistantHtml"])

    if "error-bubble" not in html:
        print("FAIL failed model call settled with no error bubble")
        return False

    print("PASS failed model call leaves an error bubble")
    return True


def verify_assistant_html_is_sanitized() -> bool:
    """Raw HTML in assistant text must not reach the DOM with its handlers attached.

    marked has shipped no sanitizer since v5, so HTML that arrives inside model output used to
    go straight to innerHTML. The realistic source is not a hostile model but a prompt-injected
    PDF or web page the agent faithfully echoed: an onerror attribute then fires in the page,
    same-origin with both profiles' session and voice endpoints.
    """
    hostile_text = (
        'Reading it back: <img src=x onerror=alert(1)> '
        '<a href="javascript:alert(2)">link</a> <script>alert(3)</script>'
    )
    assistant = {"role": "assistant", "content": [{"type": "text", "text": hostile_text}]}
    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": assistant},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "contentIndex": 0, "content": hostile_text},
        },
        {"type": "message_end", "message": assistant},
        {"type": "agent_settled"},
    ]

    history_html = render("history", [assistant])
    rendered = "".join(
        [
            "".join(render_result("live", events)["assistantHtml"]),
            "".join(history_html) if isinstance(history_html, list) else history_html,
        ]
    )

    for marker in ("onerror", "javascript:", "<script"):
        if marker in rendered.lower():
            print(f"FAIL assistant html kept an executable marker: {marker}")
            return False

    # A sanitizer that blanked the message would also pass the assertions above, so require the
    # benign content and a surviving img element (stripped, not deleted).
    if "Reading it back" not in rendered or "<img" not in rendered:
        print("FAIL assistant html was not rendered at all, so sanitisation proved nothing")
        return False

    print("PASS assistant html is sanitized before it reaches the DOM")
    return True


def verify_mid_run_message_is_queued() -> bool:
    """A message typed during a run queues, and the run in flight keeps rendering.

    This is the original break: pi refused a prompt with no streamingBehavior, and the failure path
    unlocked the composer by dropping the streaming timeline, so the rest of the turn rendered
    nowhere and every later delta threw.
    """
    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "still working"},
        },
    ]

    result = render_result(
        "live",
        events,
        submitAfterEventsText="stop and check this instead",
    )

    if len(result["commands"]) != 1 or result["commands"][0].get("delivery") != "queue":
        print(f"FAIL mid-run message did not ask to queue: {result['commands']}")
        return False
    if "still working" not in "".join(result["assistantHtml"]):
        print("FAIL queueing a message discarded the run that was streaming")
        return False
    # Not yet delivered means not yet in the transcript: the chip strip is where a waiting message
    # lives, so the flow must not claim the model has already seen it.
    if [block["role"] for block in result["flow"]] != ["assistant"]:
        print(f"FAIL an undelivered message was put in the flow: {result['flow']}")
        return False

    # A running agent must not lock a composer that has something in it. The submit above empties
    # the composer, which does disable it (nothing to send), so check the state before the send.
    composed = render_result("live", events, afterEventsComposerText="second thought")
    idle = render_result("live", [{"type": "agent_start"}, {"type": "agent_settled"}], afterEventsComposerText="second thought")
    if composed["sendDisabled"] or composed["steerHidden"]:
        print(
            "FAIL a running agent still locks the composer "
            f"(disabled={composed['sendDisabled']}, send-now hidden={composed['steerHidden']})"
        )
        return False
    if composed["composerPlaceholder"] == idle["composerPlaceholder"]:
        print("FAIL the composer never said the message would wait")
        return False

    print("PASS a mid-run message queues and leaves the running turn alone")
    return True


def steered_run_events() -> list[dict]:
    """pi's event order for a run that took a steered message between two turns.

    pi pulls the message out of its queue and delivers it in the same event, which is where the
    session file records it: after the tool result of the interrupted turn, before whatever the
    model said next.
    """
    turn_one = {
        "role": "assistant",
        "content": [{"type": "text", "text": "measuring the disk"}],
        "stopReason": "stop",
        "timestamp": 1,
    }
    steered = {"role": "user", "content": [{"type": "text", "text": "stop and check this"}]}
    turn_two = {
        "role": "assistant",
        "content": [{"type": "text", "text": "ok, checking the network instead"}],
        "stopReason": "stop",
        "timestamp": 2,
    }
    return [
        {"type": "agent_start"},
        {"type": "message_start", "message": turn_one},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "measuring the disk"},
        },
        {"type": "message_end", "message": turn_one},
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {}},
        {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash",
         "result": {"content": []}, "isError": False},
        {"type": "queue_state", "steering": ["stop and check this"], "followUp": []},
        {"type": "message_start", "message": steered},
        {"type": "message_end", "message": steered},
        {"type": "queue_state", "steering": [], "followUp": []},
        {"type": "message_start", "message": turn_two},
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "ok, checking the network instead",
            },
        },
        {"type": "message_end", "message": turn_two},
        {"type": "agent_end", "messages": [turn_one, steered, turn_two]},
        {"type": "agent_settled"},
    ]


def verify_a_steered_message_lands_where_pi_took_it() -> bool:
    """The live flow has to read like the session file: turn, the message, then the next turn.

    Before this, the browser drew the bubble the moment it was sent and then kept streaming the rest
    of the run into the assistant block above it, so the message sat at the bottom of the transcript
    for the rest of the session.
    """
    events = steered_run_events()
    delivery = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "message_start" and event["message"]["role"] == "user"
    )

    waiting = render_result(
        "live",
        events[:delivery],
        interleavedSubmits=[{"afterEvents": 5, "text": "stop and check this", "target": "steer"}],
    )
    if [block["role"] for block in waiting["flow"]] != ["assistant"]:
        print(f"FAIL the steered message reached the flow before pi took it: {waiting['flow']}")
        return False
    if not waiting["queueChips"]:
        print("FAIL the steered message was not in the chip strip while it waited")
        return False

    delivered = render_result("live", events)
    flow = [block["role"] for block in delivered["flow"]]
    if flow != ["assistant", "user", "assistant"]:
        print(f"FAIL the steered message is not between the two turns: {delivered['flow']}")
        return False
    before, message, after = delivered["flow"]
    if "measuring the disk" not in before["text"] or "checking the network" not in after["text"]:
        print(f"FAIL the turns around the message lost their content: {delivered['flow']}")
        return False
    if "stop and check this" not in message["text"]:
        print(f"FAIL the delivered message did not render: {delivered['flow']}")
        return False
    if delivered["queueChips"]:
        print(f"FAIL a delivered message left its chip behind: {delivered['queueChips']}")
        return False

    print("PASS a steered message renders between the turns it actually split")
    return True


def verify_two_fast_sends_keep_their_order() -> bool:
    """A second Enter typed before agent_start is queued by pi, so it must render where it landed.

    The composer cannot know a run is coming, which is the one moment an optimistic bubble would be
    drawn in the wrong place. Waiting for pi's echo is what keeps the order honest here.
    """
    first = {"role": "user", "content": [{"type": "text", "text": "first"}]}
    second = {"role": "user", "content": [{"type": "text", "text": "second"}]}
    turn_a = {"role": "assistant", "content": [{"type": "text", "text": "on it"}],
              "stopReason": "stop", "timestamp": 1}
    turn_b = {"role": "assistant", "content": [{"type": "text", "text": "and now the second"}],
              "stopReason": "stop", "timestamp": 2}

    result = render_result("live", [
        {"type": "message_start", "message": first},
        {"type": "message_end", "message": first},
        {"type": "agent_start"},
        {"type": "message_start", "message": turn_a},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "on it"}},
        {"type": "message_end", "message": turn_a},
        {"type": "queue_state", "steering": [], "followUp": ["second"]},
        {"type": "message_start", "message": second},
        {"type": "message_end", "message": second},
        {"type": "queue_state", "steering": [], "followUp": []},
        {"type": "message_start", "message": turn_b},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "delta": "and now the second"}},
        {"type": "message_end", "message": turn_b},
        {"type": "agent_end", "messages": [first, turn_a, second, turn_b]},
        {"type": "agent_settled"},
    ], interleavedSubmits=[
        {"afterEvents": 0, "text": "first"},
        {"afterEvents": 1, "text": "second"},
    ])

    flow = [block["role"] for block in result["flow"]]
    if flow != ["user", "assistant", "user", "assistant"]:
        print(f"FAIL two fast sends did not keep their order: {result['flow']}")
        return False
    if [block["text"] for block in result["flow"] if block["role"] == "user"] != ["first", "second"]:
        print(f"FAIL a fast send was drawn twice or not at all: {result['flow']}")
        return False

    print("PASS a second send typed before agent_start still waits for its place in the transcript")
    return True


def verify_a_running_tool_is_not_marked_finished() -> bool:
    """A tool line earns its mark from what pi has said about it, not from existing.

    Every call used to render a green tick, so a bash command that ran for two minutes looked done
    while the turn was still waiting on it.
    """
    def call(tool_id, name):
        return {"type": "toolCall", "id": tool_id, "name": name, "arguments": {}}

    streamed = {"role": "assistant", "content": [], "timestamp": 1}
    with_calls = {"role": "assistant",
                  "content": [{"type": "text", "text": "running three things"},
                              call("t1", "bash"), call("t2", "read"), call("t3", "bash")],
                  "stopReason": "toolUse", "timestamp": 2}
    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": streamed},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "running three things"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_end",
                                                             "toolCall": call("t1", "bash")}},
        {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {}},
        {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash",
         "result": {"content": []}, "isError": False},
        {"type": "tool_execution_start", "toolCallId": "t2", "toolName": "read", "args": {}},
        {"type": "tool_execution_end", "toolCallId": "t2", "toolName": "read",
         "result": {"content": []}, "isError": True},
        {"type": "tool_execution_start", "toolCallId": "t3", "toolName": "bash", "args": {}},
        {"type": "message_end", "message": with_calls},
    ]

    stages = [
        (4, ["pending"], "arguments streamed, execution not announced"),
        (5, ["running"], "executing"),
        (6, ["ok"], "result returned"),
        (8, ["ok", "error"], "second call failed"),
        (9, ["ok", "error", "running"], "third call still executing"),
    ]
    for count, expected, note in stages:
        statuses = render_result("live", events[:count])["toolStatuses"]
        if statuses != expected:
            print(f"FAIL tool marks wrong while {note}: {statuses}, expected {expected}")
            return False

    # The turn ends with the third call unanswered (an abort), and the settled render has to agree with
    # what a reload would show: the results that came back decide the marks, and no result is not a
    # success. Those toolResult messages are part of pi's agent_end payload in real life.
    # An unanswered call that *is* the end of the run is a call the reader stopped, though, so the mark
    # is cancelled rather than the bare unknown an old capture gets.
    settled = render_result("live", events + [
        {"type": "agent_end", "messages": [
            with_calls,
            {"role": "toolResult", "toolCallId": "t1", "toolName": "bash", "content": [],
             "isError": False, "timestamp": 3},
            {"role": "toolResult", "toolCallId": "t2", "toolName": "read", "content": [],
             "isError": True, "timestamp": 4},
        ]},
        {"type": "agent_settled"},
    ])
    if settled["toolStatuses"] != ["ok", "error", "cancelled"]:
        print(f"FAIL the settled timeline mis-marked the abandoned call: {settled['toolStatuses']}")
        return False

    print("PASS a tool line shows a ring while it runs and only then a tick or a cross")
    return True


def verify_send_now_arrow_steers() -> bool:
    result = render_result(
        "live",
        [{"type": "agent_start"}],
        submitAfterEventsText="look here instead",
        submitAfterEventsTarget="steer",
    )

    if len(result["commands"]) != 1 or result["commands"][0].get("delivery") != "now":
        print(f"FAIL send-now did not ask to steer: {result['commands']}")
        return False

    # And it is only offered while there is something to interrupt.
    idle = render_result("live", [{"type": "agent_start"}, {"type": "agent_settled"}])
    running = render_result("live", [{"type": "agent_start"}])
    if idle["steerHidden"] is not True or running["steerHidden"] is not True:
        print(
            "FAIL the send-now arrow is not gated on a run with content "
            f"(idle hidden={idle['steerHidden']}, running-without-text hidden={running['steerHidden']})"
        )
        return False

    print("PASS the send-now arrow steers and stays hidden when there is nothing to interrupt")
    return True


def verify_rejected_prompt_keeps_the_running_timeline() -> bool:
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "half a thought"}],
        "stopReason": "stop",
        "timestamp": 1,
    }
    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": assistant},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "half a thought"},
        },
        {
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "Agent is already processing. Specify streamingBehavior",
        },
    ]

    result = render_result("live", events)
    rendered = "".join(result["assistantHtml"])

    if "half a thought" not in rendered:
        print("FAIL a refused prompt removed the turn that was still streaming")
        return False
    if "already processing" not in rendered:
        print("FAIL the refusal was swallowed instead of shown")
        return False

    print("PASS a refused prompt reports itself without discarding the live turn")
    return True


def verify_queue_chips_are_pi_and_only_pi() -> bool:
    """The strip is pi's queue and nothing else: it appears on the announcement, acts by name, and
    empties when pi says it is empty."""
    events = [{"type": "agent_start"}]
    announced = {"type": "queue_state", "steering": [], "followUp": ["wait for me"]}

    waiting = render_result(
        "live",
        events + [announced],
        interleavedSubmits=[{"afterEvents": 1, "text": "wait for me"}],
    )

    if [block["role"] for block in waiting["flow"]] != ["assistant"]:
        print(f"FAIL a message still in the queue was drawn in the flow: {waiting['flow']}")
        return False
    if len(waiting["queueChips"]) != 1 or waiting["queueChips"][0]["kind"] != "followUp":
        print(f"FAIL pi's queue did not reach the strip: {waiting['queueChips']}")
        return False
    # The arrow promotes, the cross retracts: two actions on a waiting chip, one on a steering chip.
    if waiting["queueChips"][0]["actions"] != 2:
        print("FAIL a waiting queue chip lost an action")
        return False
    if waiting["queueChips"][0]["label"] != "after run":
        print(f"FAIL the head of the queue did not say when it lands: {waiting['queueChips']}")
        return False

    # pi's queue mode is one-at-a-time for both queues, so only the head entry goes next and the rest
    # follow one boundary each. The chips have to say so or the order looks like a batch.
    stacked = render_result(
        "live",
        events + [{"type": "queue_state", "steering": [], "followUp": ["first", "then"]}],
    )
    if [chip["label"] for chip in stacked["queueChips"]] != ["after run", "then"]:
        print(f"FAIL a stacked queue did not say which entry goes first: {stacked['queueChips']}")
        return False

    promoted = render_result(
        "live",
        events + [announced],
        clickQueueAction={"chip": 0, "action": 0},
    )
    expected = {"type": "steer_now", "kind": "followUp", "index": 0, "text": "wait for me"}
    if promoted["commands"] != [expected]:
        print(f"FAIL the chip's send-now did not name the entry: {promoted['commands']}")
        return False

    delivered = render_result("live", events + [
        announced,
        {"type": "message_start", "message": {"role": "user",
                                              "content": [{"type": "text", "text": "wait for me"}]}},
        {"type": "queue_state", "steering": [], "followUp": []},
    ], interleavedSubmits=[{"afterEvents": 1, "text": "wait for me"}])
    if delivered["queueChips"] or delivered["queueStripHidden"] is not True:
        print(f"FAIL an emptied queue left chips behind: {delivered['queueChips']}")
        return False
    # The run had streamed nothing yet, and a message delivered into an empty turn closes that empty
    # block rather than leaving a bare "assistant" label above the message.
    if [block["role"] for block in delivered["flow"]] != ["user"] \
            or "wait for me" not in delivered["flow"][-1]["text"]:
        print(f"FAIL delivery did not put the message in the flow: {delivered['flow']}")
        return False

    print("PASS the chip strip mirrors pi's queue, promotes by name, and clears on delivery")
    return True


def verify_a_streamed_tool_call_shows_up_before_its_arguments_arrive() -> bool:
    """A tool line appears when pi names the call, not when its argument JSON finishes streaming.

    What pi 1.1.0 puts on the wire, measured by tools/capture.py and asserted by
    tests/test_fake_rpc_stream.py: `toolcall_start` carries {contentIndex, id, toolName}, every
    `toolcall_delta` carries {contentIndex, delta} and nothing else - no id, and no cumulative
    `partial` snapshot anywhere, because pi's JSON transform strips those to keep a stream linear.
    The first version of this check fed the browser the snapshot shape instead, so it passed while the
    renderer was reading a field that never arrived and a `write` stayed invisible for the whole time
    its file body streamed. Same events, same assertion, honest wire.
    """
    # Deliberately free of the word "streamed": the hint is asserted to stop
    # carrying a byte count by looking for that word.
    target = "notes/deploy-checklist.md"
    body = "a line of the file being written\n" * 40
    raw_args = json.dumps({"path": target, "content": body}, ensure_ascii=False)
    thinking = {"type": "thinking", "thinking": "I will write the file."}
    completed = {"type": "toolCall", "id": "w1", "name": "write",
                 "arguments": {"path": target, "content": body}}

    def with_blocks(blocks):
        return {"role": "assistant", "content": blocks, "stopReason": "toolUse", "timestamp": 1}

    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": {"role": "assistant", "content": [], "timestamp": 1}},
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
        {"type": "message_update", "assistantMessageEvent":
            {"type": "thinking_delta", "delta": "I will write the file."}},
        {"type": "message_update", "assistantMessageEvent":
            {"type": "thinking_end", "content": "I will write the file."}},
    ]

    # The provider names the call; nothing about the arguments has arrived yet.
    events.append({"type": "message_update", "assistantMessageEvent": {
        "type": "toolcall_start", "contentIndex": 1, "id": "w1", "toolName": "write"}})
    announced = len(events)

    marks = {}
    prefix = ""
    for chunk in [raw_args[i:i + 64] for i in range(0, len(raw_args), 64)]:
        prefix += chunk
        events.append({"type": "message_update", "assistantMessageEvent": {
            "type": "toolcall_delta", "contentIndex": 1, "delta": chunk}})
        marks.setdefault("third", len(events))
        if prefix == raw_args:
            marks["last"] = len(events)

    events.append({"type": "message_update",
                   "assistantMessageEvent": {"type": "toolcall_end", "contentIndex": 1,
                                             "toolCall": completed}})
    marks["end"] = len(events)
    events.append({"type": "tool_execution_start", "toolCallId": "w1", "toolName": "write",
                   "args": {"path": target, "content": body}})
    marks["exec"] = len(events)
    events.append({"type": "tool_execution_end", "toolCallId": "w1", "toolName": "write",
                   "result": {"content": []}, "isError": False})
    marks["done"] = len(events)
    events.append({"type": "message_end", "message": with_blocks([thinking, completed])})

    def line_at(count):
        # The hint is coalesced to one DOM write per animation frame, so a snapshot taken in the same
        # tick as the last delta would show a stale hint rather than a broken one. Wait for the flush.
        lines = render_result("live", events[:count], waitAfterEventsMs=60)["toolLines"]
        if len(lines) != 1:
            raise AssertionError(f"expected one tool line at {count} events, got {lines}")
        return lines[0]

    # Announced: named and ringed, with no arguments at all yet.
    first = line_at(announced)
    if first["name"] != "write" or first["status"] != "pending":
        print(f"FAIL a call is not shown at toolcall_start: {first}")
        return False

    # Mid-arguments: still pending, and the hint says what it is pointed at and how much has landed.
    streaming = line_at(marks["third"])
    if streaming["status"] != "pending" or "streamed" not in streaming["hint"]:
        print(f"FAIL a call mid-arguments lost its hint: {streaming}")
        return False
    if target not in streaming["hint"]:
        print(f"FAIL the hint waited for parsed arguments instead of reading the stream: {streaming}")
        return False

    for count, expected in ((marks["last"], "pending"), (marks["end"], "pending"),
                            (marks["exec"], "running"), (marks["done"], "ok")):
        line = line_at(count)
        if line["status"] != expected:
            print(f"FAIL wrong status at {count} events: {line}, expected {expected}")
            return False
    if target not in line_at(marks["end"])["hint"] or "streamed" in line_at(marks["end"])["hint"]:
        print("FAIL the byte count outlived the argument stream")
        return False

    # Settlement must not leave a second line, and must not keep a live-only hint: the settled
    # timeline is rebuilt by history.js, and a reload has never seen a byte counter.
    settled = render_result("live", events + [
        {"type": "agent_end", "messages": [
            with_blocks([thinking, completed]),
            {"role": "toolResult", "toolCallId": "w1", "toolName": "write", "content": [],
             "isError": False, "timestamp": 2},
        ]},
        {"type": "agent_settled"},
    ])
    if settled["toolLines"] != [{"name": "write", "status": "ok", "hint": ""}]:
        print(f"FAIL the settled timeline differs from a reload: {settled['toolLines']}")
        return False

    reloaded = render_result("history", [
        {"role": "user", "content": "write the file", "timestamp": 0},
        with_blocks([thinking, completed]),
        {"role": "toolResult", "toolCallId": "w1", "toolName": "write", "content": [],
         "isError": False, "timestamp": 2},
    ])["toolLines"]
    if reloaded != settled["toolLines"]:
        print(f"FAIL live settlement differs from history: {reloaded}")
        return False

    print("PASS a tool line shows its name and a ring while its arguments are still streaming")
    return True


def verify_a_real_capture_streams_every_tool_call() -> bool:
    """Replay a genuine pi run and assert every call is on screen before its arguments finished.

    The synthetic event lists in this file are written by the same head that writes the renderer, so
    they agree with it - including on a wire shape pi 1.1.0 stopped sending. This check has no opinion
    about what pi sends: it feeds the browser `data-samples/markers_rpc_capture.jsonl`, recorded from
    a real `pi --mode rpc` by tools/capture.py, and asks what the reader would have seen partway
    through. It is the only check here that can fail because pi changed.
    """
    capture = DATA / "markers_rpc_capture.jsonl"
    if not capture.exists():
        print("SKIP no real capture in this checkout: uv run python tools/capture.py --scenario markers")
        return True

    events = []
    for line in capture.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line)["event"])

    def stream_type(event):
        if event.get("type") != "message_update":
            return ""
        return (event.get("assistantMessageEvent") or {}).get("type", "")

    starts = [i for i, event in enumerate(events) if stream_type(event) == "toolcall_start"]
    ends = [i for i, event in enumerate(events) if stream_type(event) == "toolcall_end"]
    calls = {event["assistantMessageEvent"]["toolCall"]["id"]
             for event in events if stream_type(event) == "toolcall_end"}
    if not starts or not ends:
        print("FAIL the capture has no streamed tool calls; re-capture it")
        return False

    # Everything up to the first completed call: every argument byte has arrived, nothing has executed.
    # A reader at this point should already see every line pi named.
    partway = render_result("live", events[:min(ends)], waitAfterEventsMs=80)
    announced = [line for line in partway["toolLines"] if line["status"] == "pending"]
    if not announced:
        print(f"FAIL no tool line was on screen before execution: {partway['toolLines']}")
        return False
    if not any("streamed" in line["hint"] for line in announced):
        print(f"FAIL a call with streamed arguments showed no byte count: {partway['toolLines']}")
        return False

    # The whole run: statuses must be the ones the capture's own results say, and a live-only hint
    # must not survive into the settled DOM.
    settled = render_result("live", events)
    expected = [
        "error" if event.get("isError") else "ok"
        for event in events if event.get("type") == "tool_execution_end"
    ]
    if len(settled["toolLines"]) != len(calls):
        print(f"FAIL the capture streamed {len(calls)} calls and the browser shows "
              f"{len(settled['toolLines'])}: {settled['toolLines']}")
        return False
    if sorted(settled["toolStatuses"]) != sorted(expected):
        print(f"FAIL statuses differ from the capture's results: {settled['toolStatuses']} "
              f"vs {expected}")
        return False
    if any(line["hint"] for line in settled["toolLines"]):
        print(f"FAIL a byte counter outlived the run: {settled['toolLines']}")
        return False

    # And a reload of the same run must agree with what the live stream settled on.
    reloaded = render_result("history", parse_jsonl_messages(
        capture.with_name("markers_session.jsonl")))["toolStatuses"]
    if sorted(reloaded) != sorted(expected):
        print(f"FAIL a reopened session disagrees with the live run: {reloaded} vs {expected}")
        return False

    print(f"PASS a real capture of {len(calls)} streamed calls renders on time, live and reopened")
    return True


def verify_a_thought_group_collapses_from_its_edge() -> bool:
    """Collapsing is a group action with two handles, and hovering the group is neither of them.

    Hover used to say "these lines are one thing" and clicking a thought folded the run away, which
    made it impossible to tell that a tool row was clickable: reaching for a row dimmed the whole run,
    and clicking one collapsed everything above it. Now the group owns a bracket down its right edge,
    and nothing inside it responds to a click except the row that opens its own detail view.
    """
    target = "out/report.txt"
    thinking = {"type": "thinking", "thinking": "look at the repo"}
    call = {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}
    message = {"role": "assistant", "content": [thinking, call], "stopReason": "toolUse",
               "timestamp": 1}
    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": {"role": "assistant", "content": [], "timestamp": 1}},
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta",
                                                             "delta": "look at the repo"}},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "toolcall_start", "contentIndex": 1, "id": "c1", "toolName": "bash"}},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "toolcall_delta", "contentIndex": 1,
            "delta": json.dumps(call["arguments"])}},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "thinking_end", "content": "look at the repo"}},
        {"type": "message_update", "assistantMessageEvent":
            {"type": "toolcall_end", "contentIndex": 1, "toolCall": call}},
        {"type": "message_end", "message": message},
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
         "args": call["arguments"]},
        {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
         "result": {"content": [{"type": "text", "text": "report.txt"}]}, "isError": False},
        {"type": "agent_end", "messages": [message, {"role": "toolResult", "toolCallId": "c1",
                                                     "toolName": "bash",
                                                     "content": [{"type": "text", "text": "report.txt"}],
                                                     "isError": False}]},
        {"type": "agent_settled"},
    ]

    plain = render_result("live", events)["groups"]
    if len(plain) != 1:
        print(f"FAIL expected one thought group, got {plain}")
        return False
    group = plain[0]
    # The handle is in the DOM, says what it would do, and says so in both directions.
    if group["edges"] != 1 or not group["edgeLabel"].lower().startswith("collapse"):
        print(f"FAIL the group has no usable right-edge handle: {group}")
        return False
    if group["collapsed"] or group["edgeExpanded"] != "true" or group["stubExpanded"] != "true":
        print(f"FAIL a fresh group does not read as expanded: {group}")
        return False

    from_edge = render_result("live", events,
                              collapseGroupEdgeBeforeEventType="agent_settled")["groups"][0]
    if not from_edge["collapsed"] or from_edge["edgeExpanded"] != "false":
        print(f"FAIL clicking the edge did not collapse the group: {from_edge}")
        return False
    if from_edge["stubExpanded"] != "false":
        print(f"FAIL the edge collapsed the group but left the header claiming otherwise: {from_edge}")
        return False

    from_stub = render_result("live", events,
                              collapseGroupBeforeEventType="agent_settled")["groups"][0]
    if not from_stub["collapsed"] or from_stub["edgeExpanded"] != "false":
        print(f"FAIL clicking the header did not collapse the group or update the edge: {from_stub}")
        return False

    # The negative half, which is the half that was the complaint.
    inside = render_result("live", events,
                           clickThinkingBeforeEventType="agent_settled")["groups"][0]
    if inside["collapsed"]:
        print(f"FAIL clicking a line of thinking still folds the run away: {inside}")
        return False

    print("PASS a group collapses from its right-edge bracket, and from nothing inside it")
    return True


def verify_thinking_and_tool_runs_collapse_as_one_group() -> bool:
    """A run of thinking and tool lines is one unit, and prose is where the unit ends.

    Ten rounds of thinking -> tool -> thinking left forty lines in a column, which is unreadable and
    is what the reader asked to fold away. An actual assistant sentence has to break the group, and so
    does a failed model call: neither belongs behind a stub that says Thinking...
    """
    def call(tool_id, name):
        return {"type": "toolCall", "id": tool_id, "name": name, "arguments": {}}

    def thinking(text):
        return {"type": "thinking", "thinking": text}

    def streamed_thinking(text):
        return [
            {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
            {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": text}},
            {"type": "message_update", "assistantMessageEvent": {"type": "thinking_end", "content": text}},
        ]

    def streamed_text(text):
        return [
            {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": text}},
        ]

    def executed(tool_id, name, is_error=False):
        return [
            {"type": "tool_execution_start", "toolCallId": tool_id, "toolName": name, "args": {}},
            {"type": "tool_execution_end", "toolCallId": tool_id, "toolName": name,
             "result": {"content": []}, "isError": is_error},
        ]

    first = {"role": "assistant", "content": [thinking("look at the repo"), call("t1", "bash"),
                                              thinking("now I see it"),
                                              {"type": "text", "text": "here is what I found"}],
             "stopReason": "toolUse", "timestamp": 2}
    second = {"role": "assistant", "content": [thinking("one more check"), call("t2", "read")],
              "stopReason": "error", "errorMessage": "the provider gave up", "timestamp": 5}
    results = [
        {"role": "toolResult", "toolCallId": "t1", "toolName": "bash", "content": [],
         "isError": False, "timestamp": 3},
        {"role": "toolResult", "toolCallId": "t2", "toolName": "read", "content": [],
         "isError": False, "timestamp": 4},
    ]

    events = [{"type": "agent_start"},
              {"type": "message_start", "message": {"role": "assistant", "content": [], "timestamp": 1}}]
    events += streamed_thinking("look at the repo") + executed("t1", "bash")
    events += streamed_thinking("now I see it")
    events += streamed_text("here is what I found")
    events.append({"type": "message_end", "message": first})
    events.append({"type": "message_start", "message": {"role": "assistant", "content": [], "timestamp": 5}})
    events += streamed_thinking("one more check") + executed("t2", "read")
    events.append({"type": "message_end", "message": second})

    settled = render_result("live", events + [
        {"type": "agent_end", "messages": [first, second, *results]},
        {"type": "agent_settled"},
    ])

    shape = [(group["index"], group["steps"]) for group in settled["groups"]]
    if shape != [("0", 3), ("1", 2)]:
        print(f"FAIL thinking and tool lines did not group: {shape}")
        return False
    if any(group["collapsed"] for group in settled["groups"]):
        print("FAIL a group rendered collapsed before anyone asked")
        return False

    # The prose and the failure stay outside, in the right order, with exactly one spacer between
    # each neighbour: a group appended ahead of its own connector shows up here as a doubled one.
    expected_rail = [
        'group:0',
        'connector',
        'timeline-item.text-bubble',
        'connector',
        'group:1',
        'connector',
        'timeline-item.text-bubble.error-bubble',
    ]
    if settled["rail"] != expected_rail:
        print(f"FAIL the grouped timeline is out of order: {settled['rail']}")
        return False

    # A fold the reader made has to outlive the rebuild that happens at agent_end.
    folded = render_result("live", events + [
        {"type": "agent_end", "messages": [first, second, *results]},
        {"type": "agent_settled"},
    ], collapseGroupBeforeEventType="message_end")
    if [(group["index"], group["collapsed"]) for group in folded["groups"]] != [("0", True), ("1", False)]:
        print(f"FAIL a collapsed group was lost at settlement: {folded['groups']}")
        return False

    reloaded = render_result("history", [
        {"role": "user", "content": "check the repo", "timestamp": 0},
        first, second, *results,
    ])
    if reloaded["groups"] != settled["groups"] or reloaded["rail"] != settled["rail"]:
        print("FAIL the grouped timeline a reload builds differs from the live one")
        return False

    print("PASS a run of thinking and tool lines folds into one group that prose breaks")
    return True


def verify_a_cancelled_call_is_marked_cancelled_in_both_paths() -> bool:
    """A call the reader stopped says so, on reload too.

    pi's only lever for a call in flight is the run's abort signal, and it leaves the call with no
    result at all. "No result" is also what an old capture looks like, so the run's own
    ``stopReason: "aborted"`` is the only thing that can tell the two apart - and it has to be read in
    the history path, because that is what a reopened session has.
    """
    call = {"type": "toolCall", "id": "b1", "name": "bash", "arguments": {"command": "make test"}}
    thought = {"type": "thinking", "thinking": "running the suite"}
    aborted_message = {"role": "assistant", "content": [thought, call],
                       "stopReason": "aborted", "timestamp": 2}

    events = [
        {"type": "agent_start"},
        {"type": "message_start", "message": {"role": "assistant", "content": [], "timestamp": 1}},
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
        {"type": "message_update", "assistantMessageEvent":
            {"type": "thinking_delta", "delta": "running the suite"}},
        {"type": "message_update", "assistantMessageEvent":
            {"type": "thinking_end", "content": "running the suite"}},
        {"type": "tool_execution_start", "toolCallId": "b1", "toolName": "bash",
         "args": {"command": "make test"}},
        {"type": "message_end", "message": aborted_message},
    ]

    # While the abort is still landing, the row is honest about what it knows: still running.
    mid = render_result("live", events)["toolLines"]
    if [line["status"] for line in mid] != ["running"]:
        print(f"FAIL a call being cancelled lost its running mark: {mid}")
        return False

    settled = render_result("live", events + [
        {"type": "agent_end", "messages": [aborted_message]},
        {"type": "agent_settled"},
    ])
    if [line["status"] for line in settled["toolLines"]] != ["cancelled"]:
        print(f"FAIL settlement did not mark the cancelled call: {settled['toolLines']}")
        return False

    reloaded = render_result("history", [
        {"role": "user", "content": "run the tests", "timestamp": 0},
        aborted_message,
    ])
    if reloaded["toolLines"] != settled["toolLines"]:
        print(f"FAIL a reopened session describes the cancelled call differently: {reloaded['toolLines']}")
        return False

    # The shape pi really writes when a reader stops a tool that was already executing: the assistant
    # message was persisted before the tool ran, so it still says "toolUse" and only the missing result
    # at the end of the run shows. This is the case feature 2 exists for, and the browser caught that
    # the message-level signal alone misses it.
    mid_tool = {"role": "assistant", "content": [thought, dict(call)],
                "stopReason": "toolUse", "timestamp": 2}
    stopped_mid_tool = render_result("history", [
        {"role": "user", "content": "run the tests", "timestamp": 0},
        mid_tool,
    ])
    if [line["status"] for line in stopped_mid_tool["toolLines"]] != ["cancelled"]:
        print(f"FAIL a call stopped while executing was not marked cancelled: {stopped_mid_tool['toolLines']}")
        return False

    # The other half of the rule: a call with no result that is not where the run ended stays genuinely
    # unknown rather than being guessed at.
    earlier = {"role": "assistant", "content": [thought, dict(call)],
               "stopReason": "toolUse", "timestamp": 2}
    resumed = {"role": "assistant", "content": [{"type": "text", "text": "carried on without it"}],
               "stopReason": "stop", "timestamp": 9}
    unaccounted = render_result("history", [
        {"role": "user", "content": "run the tests", "timestamp": 0},
        earlier,
        {"role": "toolResult", "toolCallId": "b2", "content": [{"type": "text", "text": "ok"}],
         "isError": False, "timestamp": 5},
        resumed,
    ])["toolStatuses"]
    if unaccounted != ["unknown"]:
        print(f"FAIL an unanswered call away from the end of the run was guessed at: {unaccounted}")
        return False

    print("PASS a cancelled call reads as cancelled live and on reload")
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
    checks.append(verify_generic_subagent_failure())
    checks.append(verify_retry_parity())
    checks.append(verify_multiple_content_blocks())
    checks.append(verify_generic_subagent_failure())
    checks.append(verify_prompt_failure_unlocks_composer())
    checks.append(verify_disconnect_unlocks_composer())
    checks.append(verify_handled_prompt_unlocks_composer())
    checks.append(verify_subagent_interaction_survives_settlement())
    checks.append(verify_failed_model_call_leaves_an_error_bubble())
    checks.append(verify_assistant_html_is_sanitized())
    checks.append(verify_mid_run_message_is_queued())
    checks.append(verify_a_steered_message_lands_where_pi_took_it())
    checks.append(verify_a_running_tool_is_not_marked_finished())
    checks.append(verify_a_streamed_tool_call_shows_up_before_its_arguments_arrive())
    checks.append(verify_a_real_capture_streams_every_tool_call())
    checks.append(verify_a_thought_group_collapses_from_its_edge())
    checks.append(verify_thinking_and_tool_runs_collapse_as_one_group())
    checks.append(verify_a_cancelled_call_is_marked_cancelled_in_both_paths())
    checks.append(verify_two_fast_sends_keep_their_order())
    checks.append(verify_send_now_arrow_steers())
    checks.append(verify_rejected_prompt_keeps_the_running_timeline())
    checks.append(verify_queue_chips_are_pi_and_only_pi())
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
