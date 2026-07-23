"""Session discovery, previewing, and JSONL message extraction."""

import json
import logging
from pathlib import Path

log = logging.getLogger("pi-chat")


def list_sessions(project_root: Path, account_label: str | None) -> list[dict]:
    """Return pi sessions associated with one local account."""
    if not account_label:
        return []

    work_dir = (project_root / "sessions" / account_label).resolve()
    pi_sessions_dir = Path.home() / ".pi" / "agent" / "sessions"
    if not pi_sessions_dir.exists():
        return []

    sessions = []
    for jsonl_file in pi_sessions_dir.rglob("*.jsonl"):
        session = _summarize_session(jsonl_file, expected_work_dir=work_dir)
        if session:
            sessions.append(session)

    sessions.sort(key=lambda session: session.get("timestamp", ""), reverse=True)
    return sessions


def session_belongs_to_account(
    session_path: str,
    project_root: Path,
    account_label: str,
) -> bool:
    """Return whether a pi session file belongs to the account work directory."""
    try:
        session_file = Path(session_path)
        if not session_file.is_file() or session_file.suffix != ".jsonl":
            return False

        content = _read_jsonl_lines(session_file)
        if not content:
            return False

        header = json.loads(content[0])
        if header.get("type") != "session":
            return False

        session_cwd = Path(header.get("cwd", "")).resolve()
        account_work_dir = (
            project_root / "sessions" / account_label
        ).resolve()
        return session_cwd == account_work_dir
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError):
        return False


def preview_session(session_path: str) -> dict:
    """Return lightweight metadata for a session file."""
    try:
        session_file = Path(session_path)
        if not session_file.exists():
            return {"error": "Session file not found"}

        content = _read_jsonl_lines(session_file)
        if not content:
            return {"error": "Empty session file"}

        header = json.loads(content[0])
        first_message, message_count = _message_summary(content[1:], preview_limit=500)
        return {
            "header": header,
            "firstMessage": first_message,
            "messageCount": message_count,
        }
    except json.JSONDecodeError as error:
        return {"error": f"Invalid JSON in session file: {error}"}
    except Exception as error:
        log.error("Preview error for %s: %s", session_path, error)
        return {"error": str(error)}


def parse_jsonl_messages(session_path: str) -> list[dict] | None:
    """Extract final messages from either a pi session or an RPC capture."""
    try:
        session_file = Path(session_path)
        if not session_file.exists():
            return None

        content = _read_jsonl_lines(session_file)
        if not content:
            return None

        subagent_tool_calls = _collect_subagent_tool_calls(content)

        for line in content:
            try:
                record = json.loads(line)
                event = record.get("event", record)
                if event.get("type") == "agent_end" and "messages" in event:
                    messages = event["messages"]
                    _attach_subagent_details(messages, subagent_tool_calls)
                    return messages
            except json.JSONDecodeError:
                continue

        messages = []
        for line in content:
            try:
                entry = json.loads(line)
                if entry.get("type") != "message":
                    continue
                message = entry.get("message", {})
                if message.get("role") in ("user", "assistant"):
                    messages.append(message)
            except json.JSONDecodeError:
                continue

        _attach_subagent_details(messages, subagent_tool_calls)
        return messages or None
    except Exception as error:
        log.error("Failed to parse JSONL %s: %s", session_path, error)
        return None


def _read_jsonl_lines(session_file: Path) -> list[str]:
    return session_file.read_text(
        encoding="utf-8",
        errors="replace",
    ).strip().splitlines()


def _summarize_session(
    jsonl_file: Path,
    expected_work_dir: Path,
) -> dict | None:
    try:
        content = _read_jsonl_lines(jsonl_file)
        if not content:
            return None

        header = json.loads(content[0])
        if header.get("type") != "session":
            return None

        try:
            session_cwd = Path(header.get("cwd", "")).resolve()
        except Exception:
            return None

        if session_cwd != expected_work_dir:
            return None

        first_message, message_count = _message_summary(content[1:], preview_limit=200)
        return {
            "id": header.get("id", ""),
            "timestamp": header.get("timestamp", ""),
            "cwd": str(session_cwd),
            "path": str(jsonl_file),
            "messageCount": message_count,
            "firstMessage": first_message,
        }
    except Exception as error:
        log.warning("Failed to read session %s: %s", jsonl_file, error)
        return None


def _message_summary(lines: list[str], preview_limit: int) -> tuple[str, int]:
    first_message = ""
    message_count = 0

    for line in lines:
        try:
            entry = json.loads(line)
            if entry.get("type") != "message":
                continue

            message = entry.get("message", {})
            role = message.get("role")
            if role in ("user", "assistant"):
                message_count += 1

            if role == "user" and not first_message:
                first_message = _first_text(message.get("content", []))[:preview_limit]
        except (json.JSONDecodeError, KeyError):
            continue

    return first_message, message_count


def _collect_subagent_tool_calls(content: list[str]) -> dict[str, dict]:
    subagent_tool_calls: dict[str, dict] = {}

    for line in content:
        try:
            record = json.loads(line)
            event = record.get("event", record)

            if (
                event.get("type") == "tool_execution_update"
                and event.get("toolName") == "subagent"
            ):
                _record_subagent_update(subagent_tool_calls, event)

            if (
                event.get("type") == "tool_execution_end"
                and event.get("toolName") == "subagent"
            ):
                _extract_receipt_status(
                    subagent_tool_calls,
                    event.get("toolCallId", ""),
                    event.get("result", {}),
                    event.get("isError", False),
                )

            if event.get("type") == "message":
                message = event.get("message", {})
                if (
                    message.get("role") == "toolResult"
                    and message.get("toolName") == "subagent"
                ):
                    _record_subagent_result(subagent_tool_calls, message)
        except json.JSONDecodeError:
            continue

    return subagent_tool_calls


def _record_subagent_update(subagent_tool_calls: dict[str, dict], event: dict) -> None:
    tool_call_id = event.get("toolCallId", "")
    result_items = (
        event.get("partialResult", {})
        .get("details", {})
        .get("results", [])
    )
    if not result_items or not tool_call_id:
        return

    tool_calls = _extract_tool_calls_from_result(result_items[0])
    if tool_calls:
        subagent_tool_calls[tool_call_id] = {
            "name": event.get("args", {}).get("name", "sub-agent"),
            "toolCalls": tool_calls,
        }


def _record_subagent_result(
    subagent_tool_calls: dict[str, dict],
    message: dict,
) -> None:
    tool_call_id = message.get("toolCallId", "")
    result_items = message.get("details", {}).get("results", [])
    if not result_items or not tool_call_id:
        return

    is_error = message.get("isError", False)
    _extract_receipt_status_from_text(
        subagent_tool_calls,
        tool_call_id,
        _first_text(message.get("content", [])),
        is_error,
    )

    tool_calls = _extract_tool_calls_from_result(result_items[0])
    if tool_calls:
        subagent_tool_calls.setdefault(tool_call_id, {})
        subagent_tool_calls[tool_call_id]["toolCalls"] = tool_calls
        subagent_tool_calls[tool_call_id]["isError"] = is_error


def _extract_tool_calls_from_result(result: dict) -> list[dict]:
    tool_calls = []
    for message in result.get("messages", []):
        if message.get("role") != "assistant":
            continue

        for content in message.get("content", []):
            if (
                content.get("type") != "toolCall"
                or content.get("name") == "subagent"
            ):
                continue

            arguments = content.get("arguments", {})
            argument_description = ""
            if isinstance(arguments, dict):
                for key in (
                    "command",
                    "prompt",
                    "path",
                    "query",
                    "questions",
                    "url",
                ):
                    if key in arguments:
                        argument_description = str(arguments[key])[:120]
                        break
                if not argument_description:
                    argument_description = json.dumps(
                        arguments,
                        ensure_ascii=False,
                    )[:120]

            tool_calls.append(
                {
                    "name": content.get("name", ""),
                    "args": argument_description,
                }
            )
    return tool_calls


def _extract_receipt_status(
    subagent_tool_calls: dict[str, dict],
    tool_call_id: str,
    result: dict,
    is_error: bool,
) -> None:
    if not tool_call_id or tool_call_id not in subagent_tool_calls:
        return

    _extract_receipt_status_from_text(
        subagent_tool_calls,
        tool_call_id,
        _first_text(result.get("content", [])),
        is_error,
    )


def _extract_receipt_status_from_text(
    subagent_tool_calls: dict[str, dict],
    tool_call_id: str,
    text: str,
    is_error: bool,
) -> None:
    if not tool_call_id or not text:
        return

    subagent_tool_calls.setdefault(tool_call_id, {})
    subagent_tool_calls[tool_call_id]["isError"] = is_error

    if "PI_SUBAGENT_FAILURE_V1" in text:
        subagent_tool_calls[tool_call_id]["status"] = "failed"
        payload = _parse_embedded_json(text)
        if payload:
            subagent_tool_calls[tool_call_id]["summary"] = (
                payload.get("error", "") or payload.get("cause", "")
            )
        return

    if "PI_SUBAGENT_RECEIPT_V1" in text:
        payload = _parse_embedded_json(text)
        if payload:
            subagent_tool_calls[tool_call_id]["status"] = payload.get(
                "status",
                "completed",
            )
            subagent_tool_calls[tool_call_id]["summary"] = payload.get(
                "summary",
                "",
            )


def _parse_embedded_json(text: str) -> dict | None:
    json_start = text.find("{")
    if json_start < 0:
        return None
    try:
        return json.loads(text[json_start:])
    except json.JSONDecodeError:
        return None


def _first_text(content: list[dict]) -> str:
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            return item.get("text", "")
    return ""


def _attach_subagent_details(
    messages: list[dict],
    subagent_tool_calls: dict[str, dict],
) -> None:
    for message in messages:
        if message.get("role") != "assistant":
            continue

        for content in message.get("content", []):
            if (
                content.get("type") != "toolCall"
                or content.get("name") != "subagent"
            ):
                continue

            tool_call_id = content.get("id", "")
            if not tool_call_id or tool_call_id not in subagent_tool_calls:
                continue

            details = subagent_tool_calls[tool_call_id]
            content["_toolCalls"] = details.get("toolCalls", [])
            content["_status"] = details.get("status")
            content["_summary"] = details.get("summary")
            content["_isError"] = details.get("isError", False)
