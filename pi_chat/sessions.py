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
        tool_errors = _collect_tool_errors(content)

        # Check for native session format (type:message records)
        has_message_records = False
        for line in content:
            try:
                entry = json.loads(line)
                if entry.get("type") == "message":
                    has_message_records = True
                    break
            except json.JSONDecodeError:
                continue

        if not has_message_records:
            # RPC capture format — collect from ALL agent_end events
            all_messages = []
            for line in content:
                try:
                    record = json.loads(line)
                    event = record.get("event", record)
                    if event.get("type") == "agent_end" and "messages" in event:
                        all_messages.extend(event["messages"])
                except json.JSONDecodeError:
                    continue
            if all_messages:
                _attach_subagent_details(all_messages, subagent_tool_calls)
                _attach_tool_errors(all_messages, tool_errors)
                return [
                    message
                    for message in all_messages
                    if message.get("role") in ("user", "assistant")
                ]

        # Native session fallback
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
        _attach_tool_errors(messages, tool_errors)
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


def _collect_tool_errors(content: list[str]) -> dict[str, bool]:
    """Collect final tool error state before toolResult records are filtered."""
    tool_errors: dict[str, bool] = {}

    for line in content:
        try:
            record = json.loads(line)
            event = record.get("event", record)

            if event.get("type") == "tool_execution_end":
                tool_call_id = event.get("toolCallId", "")
                if tool_call_id:
                    tool_errors[tool_call_id] = bool(event.get("isError"))

            if event.get("type") == "message":
                message = event.get("message", {})
                tool_call_id = message.get("toolCallId", "")
                if message.get("role") == "toolResult" and tool_call_id:
                    tool_errors[tool_call_id] = bool(message.get("isError"))
        except json.JSONDecodeError:
            continue

    return tool_errors


def _record_subagent_update(subagent_tool_calls: dict[str, dict], event: dict) -> None:
    tool_call_id = event.get("toolCallId", "")
    result_items = (
        event.get("partialResult", {})
        .get("details", {})
        .get("results", [])
    )
    if not result_items or not tool_call_id:
        return

    result = result_items[0]
    usage = result.get("usage", {})

    entry = subagent_tool_calls.setdefault(tool_call_id, {
        "name": event.get("args", {}).get("name", "sub-agent"),
    })
    entry["timelineMessages"] = result.get("messages", [])
    entry["turns"] = usage.get("turns")
    entry["maxTurns"] = result.get("maxTurnsLimit")
    _apply_structured_receipt(
        entry,
        result.get("receipt"),
        is_error=False,
    )


def _record_subagent_result(
    subagent_tool_calls: dict[str, dict],
    message: dict,
) -> None:
    tool_call_id = message.get("toolCallId", "")
    result_items = message.get("details", {}).get("results", [])
    is_error = message.get("isError", False)

    # Always register the toolCallId from the toolResult, even without results
    subagent_tool_calls.setdefault(tool_call_id, {})

    if not result_items:
        _extract_receipt_status_from_text(
            subagent_tool_calls,
            tool_call_id,
            _first_text(message.get("content", [])),
            is_error,
        )
        return

    result = result_items[0]
    usage = result.get("usage", {})

    entry = subagent_tool_calls[tool_call_id]
    entry["isError"] = is_error
    entry["timelineMessages"] = result.get("messages", [])
    entry["turns"] = usage.get("turns")
    entry["maxTurns"] = result.get("maxTurnsLimit")
    if not _apply_structured_receipt(
        entry,
        result.get("receipt"),
        is_error,
    ):
        _extract_receipt_status_from_text(
            subagent_tool_calls,
            tool_call_id,
            _first_text(message.get("content", [])),
            is_error,
        )


def _extract_receipt_status(
    subagent_tool_calls: dict[str, dict],
    tool_call_id: str,
    result: dict,
    is_error: bool,
) -> None:
    if not tool_call_id:
        return

    entry = subagent_tool_calls.setdefault(tool_call_id, {})
    result_items = result.get("details", {}).get("results", [])
    if result_items:
        final_result = result_items[0]
        entry["isError"] = is_error
        entry["timelineMessages"] = final_result.get("messages", [])
        entry["turns"] = final_result.get("usage", {}).get("turns")
        entry["maxTurns"] = final_result.get("maxTurnsLimit")
        if _apply_structured_receipt(
            entry,
            final_result.get("receipt"),
            is_error,
        ):
            return

    _extract_receipt_status_from_text(
        subagent_tool_calls,
        tool_call_id,
        _first_text(result.get("content", [])),
        is_error,
    )


def _apply_structured_receipt(
    entry: dict,
    receipt: dict | None,
    is_error: bool,
) -> bool:
    """Apply authoritative receipt fields when pi provides them as a dict."""
    if not isinstance(receipt, dict):
        return False

    status = receipt.get("status") or (
        "failed" if is_error else "completed"
    )
    entry["isError"] = is_error or status in ("failed", "error")
    entry["status"] = status
    entry["summary"] = (
        receipt.get("summary", "")
        or receipt.get("error", "")
        or receipt.get("cause", "")
    )
    return True


def _extract_receipt_status_from_text(
    subagent_tool_calls: dict[str, dict],
    tool_call_id: str,
    text: str,
    is_error: bool,
) -> None:
    if not tool_call_id:
        return

    subagent_tool_calls.setdefault(tool_call_id, {})
    entry = subagent_tool_calls[tool_call_id]
    entry["isError"] = is_error
    if is_error:
        entry["status"] = "failed"
    if not text:
        return

    if "PI_SUBAGENT_FAILURE_V1" in text:
        entry["isError"] = True
        entry["status"] = "failed"
        payload = _parse_embedded_json(text)
        if payload:
            entry["summary"] = (
                payload.get("error", "") or payload.get("cause", "")
            )
        return

    if "PI_SUBAGENT_RECEIPT_V1" in text:
        payload = _parse_embedded_json(text)
        if payload:
            status = payload.get(
                "status",
                "completed",
            )
            entry["status"] = status
            if status in ("failed", "error"):
                entry["isError"] = True
            entry["summary"] = payload.get(
                "summary",
                "",
            )
        return

    if "PI_SUBAGENT_" not in text:
        entry["fallbackText"] = text


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
            content["_status"] = details.get("status")
            content["_summary"] = details.get("summary")
            content["_fallbackText"] = details.get("fallbackText")
            content["_isError"] = details.get("isError", False)
            content["_timelineMessages"] = details.get("timelineMessages", [])
            content["_turns"] = details.get("turns")
            content["_maxTurns"] = details.get("maxTurns")


def _attach_tool_errors(
    messages: list[dict],
    tool_errors: dict[str, bool],
) -> None:
    for message in messages:
        if message.get("role") != "assistant":
            continue

        for content in message.get("content", []):
            tool_call_id = content.get("id", "")
            if content.get("type") == "toolCall" and tool_call_id in tool_errors:
                content["isError"] = tool_errors[tool_call_id]
