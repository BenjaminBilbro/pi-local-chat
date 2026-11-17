"""WP-12 / QA-11: the fixture truth-check has to be able to say "green" about a green corpus.

`tools/capture.py --census "data-samples/*.jsonl"` exited 2 on the repo's own corpus for two reasons
that were tangled in one glob: pi session files sit in the same directory as RPC captures and were
graded against wire markers they have no business containing, and the per-file rule demanded that every
capture show every core marker when two of them have no tool call in them by design. Both are locked
here, along with the checks that are supposed to stay red.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("capture_tool", ROOT / "tools" / "capture.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def capture_tool():
    return _tool()


def write(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def rpc_corpus(with_tools: bool = True) -> list[dict]:
    """The smallest set of records that covers every core marker the census asks of a corpus."""
    records = [
        {"event": {"type": "agent_start"}},
        {"event": {"type": "turn_start"}},
        {"event": {"type": "message_start"}},
        {"event": {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}}},
        {"event": {"type": "message_update", "assistantMessageEvent": {"type": "text_delta",
                                                                       "delta": "hi"}}},
        {"event": {"type": "message_update", "assistantMessageEvent": {"type": "text_end"}}},
        {"event": {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}}},
        {"event": {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta",
                                                                       "delta": "hm"}}},
        {"event": {"type": "message_update", "assistantMessageEvent": {"type": "thinking_end"}}},
    ]
    if with_tools:
        records += [
            {"event": {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash"}},
            {"event": {"type": "message_update", "assistantMessageEvent": {
                "type": "toolcall_start", "id": "c1", "toolName": "bash"}}},
            {"event": {"type": "message_update", "assistantMessageEvent": {
                "type": "toolcall_delta", "delta": "{\"command\":"}}},
            {"event": {"type": "message_update", "assistantMessageEvent": {
                "type": "toolcall_end", "toolCall": {"id": "c1", "name": "bash"}}}},
            {"event": {"type": "tool_execution_end", "toolCallId": "c1", "isError": False}},
        ]
    records += [
        {"event": {"type": "message_end"}},
        {"event": {"type": "turn_end"}},
        {"event": {"type": "agent_end"}},
        {"event": {"type": "agent_settled"}},
    ]
    return records


SESSION_FILE = [
    {"type": "session", "id": "019f86fe", "cwd": "/tmp/whatever", "timestamp": "2026-07-21"},
    {"type": "message", "message": {"role": "user", "content": "hi"}},
    {"type": "message", "message": {"role": "assistant", "content": "hello"}},
    {"type": "model_change", "model": "some/model"},
]


def test_a_session_file_alongside_a_capture_is_skipped_and_the_set_is_green(capture_tool, tmp_path,
                                                                           capsys):
    """QA-11(a). The session file used to be graded as a wire capture, which reported 19 missing core
    markers and exited 2 on the repo's own corpus. Mixed sets are the case that matters, because
    `data-samples/` is one directory holding both kinds."""
    session = write(tmp_path / "2026-07-21T23-24-02-074Z_019f86fe.jsonl", SESSION_FILE)
    capture = write(tmp_path / "capture.jsonl", rpc_corpus())

    code = capture_tool.print_census([session, capture])
    out = capsys.readouterr().out

    assert code == 0
    assert "session)" in out
    assert "NEW" not in out
    assert "no core marker here" not in out
    assert "1 session fixture(s) skipped" in out
    assert "core markers across the corpus: covered" in out


def test_a_set_with_no_capture_at_all_says_so_instead_of_passing(capture_tool, tmp_path, capsys):
    """A glob that matches only session files is a mistake in the invocation, not a green corpus."""
    session = write(tmp_path / "session.jsonl", SESSION_FILE)

    code = capture_tool.print_census([session])
    out = capsys.readouterr().out

    assert code == 2
    assert "nothing graded" in out


def test_a_capture_with_no_tool_call_is_not_by_itself_a_failure(capture_tool, tmp_path, capsys):
    """`custom_closure_capture.jsonl` and `custom_rpc_capture.jsonl` legitimately have none."""
    closure = write(tmp_path / "closure.jsonl", rpc_corpus(with_tools=False))
    tools = write(tmp_path / "tools.jsonl", rpc_corpus(with_tools=True))

    code = capture_tool.print_census([closure, tools])
    out = capsys.readouterr().out

    assert code == 0
    assert "no core marker here" in out
    assert "core markers across the corpus: covered" in out


def test_a_corpus_that_cannot_show_a_tool_call_is_still_red(capture_tool, tmp_path, capsys):
    closure = write(tmp_path / "closure.jsonl", rpc_corpus(with_tools=False))

    code = capture_tool.print_census([closure])
    out = capsys.readouterr().out

    assert code == 2
    assert "MISSING tool_execution_start" in out


def test_a_file_that_is_neither_format_is_reported_and_fails(capture_tool, tmp_path, capsys):
    weird = write(tmp_path / "weird.jsonl", [{"type": "no_idea_what_this_is"}])

    code = capture_tool.print_census([weird])
    out = capsys.readouterr().out

    assert code == 2
    assert "unrecognizable file(s): weird.jsonl" in out


@pytest.mark.parametrize("event,expected", [
    ({"type": "toolcall_start", "id": "c1", "toolName": "bash"}, "1.1.0+"),
    ({"type": "toolcall_start", "partial": {"content": []}}, "pre-1.1.0"),
])
def test_the_wire_generation_is_named(capture_tool, tmp_path, event, expected):
    """1.1.0 put id/toolName on toolcall_start and stripped every `partial` snapshot. Five captures
    here predate that and still pass, which is how the July stub that lied about `partial` survived."""
    records = rpc_corpus(with_tools=True)
    for record in records:
        inner = record.get("event", {})
        if inner.get("type") == "message_update" and \
                inner.get("assistantMessageEvent", {}).get("type") == "toolcall_start":
            inner["assistantMessageEvent"] = event
    path = write(tmp_path / "shape.jsonl", records)

    assert capture_tool.census_file(path)["wire"] == expected


def test_the_repos_own_corpus_is_green(capture_tool, capsys):
    """QA-11, stated as a test rather than as an exit code nobody reads."""
    paths = capture_tool.expand([str(ROOT / "data-samples" / "*.jsonl")])
    assert paths, "no fixtures found to grade"

    code = capture_tool.print_census(paths)
    out = capsys.readouterr().out

    assert code == 0, out[-2000:]
    assert "RPC capture(s)" in out
