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
    """Extract raw messages from either a pi session or an RPC capture.

    Returns all message records (user, assistant, toolResult, etc.) without
    sub-agent enrichment. The frontend (history.js / subagent.js) performs
    all sub-agent parsing via enrichToolCalls().
    """
    try:
        session_file = Path(session_path)
        if not session_file.exists():
            return None

        content = _read_jsonl_lines(session_file)
        if not content:
            return None

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
            # RPC capture format — collect messages from ALL agent_end events
            messages = []
            for line in content:
                try:
                    record = json.loads(line)
                    event = record.get("event", record)
                    if event.get("type") == "agent_end" and "messages" in event:
                        messages.extend(event["messages"])
                except json.JSONDecodeError:
                    continue
            return messages or None

        # Native session — return all message records (not just user/assistant)
        messages = []
        for line in content:
            try:
                entry = json.loads(line)
                if entry.get("type") != "message":
                    continue
                message = entry.get("message", {})
                messages.append(message)
            except json.JSONDecodeError:
                continue

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



