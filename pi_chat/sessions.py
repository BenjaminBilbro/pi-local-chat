"""Session discovery, previewing, and JSONL message extraction."""

import json
import logging
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger("pi-chat")

# Summary cache, keyed by path and validated by (mtime_ns, size). Session files are append-only and
# mostly historical: this store holds 109 files and 103 MB across 16 project directories, and pi-chat
# re-lists on connect, after `new_session`, and after every loaded session. Reading all of it each time
# is what made the sidebar take seconds, and cold (first read after boot, Defender opening every file)
# it was worse. An unchanged file is now one `stat`.
_SUMMARY_CACHE: "OrderedDict[str, tuple[int, int, dict | None]]" = OrderedDict()
_SUMMARY_CACHE_LIMIT = 512


def _derived_session_dir(work_dir: Path, pi_sessions_dir: Path) -> Path:
    """The directory pi names after a work directory, under the scheme this store uses.

    Observed here: work dir `C:\\Users\\me\\pi-chat\\sessions\\b` keeps its sessions in
    `--C--Users-me-pi-chat-sessions-b--`. Deriving the name turns a 103 MB walk into a 1 MB one. It is
    an optimization only; the caller falls back to the full walk, so a change in pi's naming costs
    speed and never correctness.
    """
    slug = str(work_dir).replace(":", "-").replace("\\", "-").replace("/", "-")
    return pi_sessions_dir / f"--{slug}--"


def list_sessions(project_root: Path, account_label: str | None) -> list[dict]:
    """Return pi sessions associated with one local account."""
    if not account_label:
        return []

    work_dir = (project_root / "sessions" / account_label).resolve()
    pi_sessions_dir = Path.home() / ".pi" / "agent" / "sessions"
    if not pi_sessions_dir.exists():
        return []

    derived = _derived_session_dir(work_dir, pi_sessions_dir)
    search_root = derived if derived.is_dir() else pi_sessions_dir

    sessions = [
        summary
        for summary in (
            _summarize_session(f, expected_work_dir=work_dir)
            for f in search_root.rglob("*.jsonl")
        )
        if summary
    ]

    if search_root is not pi_sessions_dir and not sessions:
        # The derived name is a shortcut, not a promise about where pi writes. Finding nothing there
        # means the assumption stopped holding, so do the full walk once rather than serve an empty
        # sidebar to an account that demonstrably has sessions.
        sessions = [
            summary
            for summary in (
                _summarize_session(f, expected_work_dir=work_dir)
                for f in pi_sessions_dir.rglob("*.jsonl")
            )
            if summary
        ]

    sessions.sort(key=lambda session: session.get("timestamp", ""), reverse=True)
    return sessions


def session_belongs_to_account(
    session_path: str,
    project_root: Path,
    account_label: str,
) -> bool:
    """Return whether a pi session file belongs to the account work directory.

    The header is the whole question, and the header is the first line. This used to read the entire
    file, which made it the most expensive thing on the fastest path: every `load_session` click pays
    it before anything reaches the screen, and these files run to tens of megabytes.

    Worth stating plainly, because the docs did not: ownership comes from the `cwd` field inside the
    file, so any authored `.jsonl` that claims this account's work dir passes. A pi child already has
    unrestricted file access, so profile scoping here is presentation, not isolation. The login
    password is the boundary.
    """
    try:
        session_file = Path(session_path)
        if not session_file.is_file() or session_file.suffix != ".jsonl":
            return False

        header_line = _read_jsonl_header(session_file)
        if not header_line:
            return False

        header = json.loads(header_line)
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

        with _open_jsonl(session_file) as handle:
            header_line = handle.readline().strip()
            if not header_line:
                return {"error": "Empty session file"}
            header = json.loads(header_line)
            first_message, message_count = _message_summary(handle, preview_limit=500)
        return {
            "header": header,
            "firstMessage": first_message,
            "messageCount": message_count,
        }
    except json.JSONDecodeError as error:
        return {"error": f"Invalid JSON in session file: {error}"}
    except OSError as error:
        log.error("Preview error for %s: %s", session_path, error)
        return {"error": str(error)}


def parse_jsonl_messages(session_path: str) -> list[dict] | None:
    """Extract raw messages from either a pi session or an RPC capture.

    Returns all message records (user, assistant, toolResult, etc.) without
    sub-agent enrichment. The frontend (history.js / subagent.js) performs
    all sub-agent parsing via enrichToolCalls().

    One pass, streamed. This path used to read the file into memory twice (once to work out which
    format it was, once to pull the messages out of it) and to materialize every line of a file that
    can be tens of megabytes, all to answer a question the browser is waiting on.
    """
    try:
        session_file = Path(session_path)
        if not session_file.exists():
            return None

        # Both shapes are collected as we go, because telling them apart used to cost a second read.
        # A native session has `type: "message"` records and a capture has none, so preferring the
        # native reading keeps the previous behavior for a file that somehow carries both.
        native: list[dict] = []
        captured: list[dict] = []

        with _open_jsonl(session_file) as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue

                if entry.get("type") == "message":
                    native.append(entry.get("message", {}))
                    continue

                event = entry.get("event", entry)
                if isinstance(event, dict) and event.get("type") == "agent_end" and "messages" in event:
                    captured.extend(event["messages"])

        return native or captured or None
    except OSError as error:
        log.error("Failed to read JSONL %s: %s", session_path, error)
        return None
    except Exception as error:
        log.error("Failed to parse JSONL %s: %s", session_path, error, exc_info=True)
        return None


def _open_jsonl(session_file: Path):
    """Open a session file the way every reader in this module wants it.

    `errors="replace"` is deliberate: a torn write from a killed pi leaves one bad byte, and one bad
    byte must not cost the whole conversation. Lines come back with their newline; the readers strip
    or hand the bytes to json.loads, which tolerates it.
    """
    return session_file.open("r", encoding="utf-8", errors="replace", newline="\n")


def _read_jsonl_header(session_file: Path, cap: int = 1 << 20) -> str | None:
    """Read the first line without reading the file.

    The cap exists so a file whose first "line" is really a megabyte of junk cannot pin a request
    handler; past the cap there is no usable header, which is the same answer as an empty file.
    """
    with _open_jsonl(session_file) as handle:
        line = handle.readline(cap)
    line = line.strip()
    return line or None


def _read_jsonl_lines(session_file: Path) -> list[str]:
    """Every line of a session file, in memory. Prefer `_open_jsonl`: this is what made large
    sessions expensive, and it survives only for callers that genuinely want a list."""
    with _open_jsonl(session_file) as handle:
        return [line.strip() for line in handle if line.strip()]


def clear_summary_cache() -> None:
    """Drop every cached summary. Tests and the `PI_CHAT_DEV` reload path use it."""
    _SUMMARY_CACHE.clear()


def _summarize_session(
    jsonl_file: Path,
    expected_work_dir: Path,
) -> dict | None:
    """Summarize one session file, from cache unless it changed.

    The cached value is the summary *before* the account filter, because the filter depends on who is
    asking and the cache is shared by both profiles. The filter is then a string compare against the
    resolved `cwd` that was read with the header, so a cache hit reads no file at all.
    """
    key = str(jsonl_file)
    try:
        info = jsonl_file.stat()
    except OSError:
        _SUMMARY_CACHE.pop(key, None)
        return None

    stamp = (info.st_mtime_ns, info.st_size)
    cached = _SUMMARY_CACHE.get(key)
    if cached is not None and cached[:2] == stamp:
        _SUMMARY_CACHE.move_to_end(key)
        summary = cached[2]
    else:
        summary = _read_summary(jsonl_file)
        _SUMMARY_CACHE[key] = (stamp[0], stamp[1], summary)
        while len(_SUMMARY_CACHE) > _SUMMARY_CACHE_LIMIT:
            _SUMMARY_CACHE.popitem(last=False)

    if not summary or summary["cwd"] != str(expected_work_dir):
        return None
    # A copy, so a caller editing the dict it got cannot write into the cache.
    return dict(summary)


def _read_summary(jsonl_file: Path) -> dict | None:
    """Read a session file and return its sidebar summary, unfiltered.

    Streamed, and with the header taken from the first line rather than from a materialized list:
    `list_sessions` runs this over the whole store on a cold start, and that is the read the sidebar
    wait used to be.
    """
    try:
        with _open_jsonl(jsonl_file) as handle:
            header_line = handle.readline().strip()
            if not header_line:
                return None

            header = json.loads(header_line)
            if header.get("type") != "session":
                return None

            try:
                session_cwd = Path(header.get("cwd", "")).resolve()
            except Exception as error:
                # Ownership is decided by comparing this resolved cwd against the account's work directory
                # (see `session_belongs_to_account`), so a cwd that will not resolve cannot be shown to belong to this
                # profile, and the session must not be listed. Dropping it is the right call; dropping it with
                # no trace is not, because "my session vanished from the list" then leaves no evidence
                # anywhere, while every other failure in this function is logged.
                log.warning(
                    "Skipping session %s: unreadable cwd %r (%s)", jsonl_file, header.get("cwd"), error
                )
                return None

            first_message, message_count = _message_summary(handle, preview_limit=200)
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


def _first_text(content):
    """Extract the first text string from a message content array."""
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            return item["text"]
    return ""


# The entry type `pi_chat/extension/preview.ts` appends with `pi.appendEntry`. The name is a wire
# contract between that extension and this server, so it appears in both and in the JSONL.
PREVIEW_CUSTOM_TYPE = "pichat_preview"


def parse_jsonl_preview_entries(session_path: str, limit: int = 400) -> list[dict]:
    """Return the preview-pane entries recorded in a session file, in transcript order.

    pi persists a preview as a `custom` message, which is what makes a reopened session rebuildable:
    the transcript holds every open, update, close and clear in the order it happened. Those entries
    never enter model context and `get_messages` does not return them (measured 2026-11-09 against a
    session holding two: 8 messages back, no custom entries), so reading the file here is the only
    way they reach a browser.

    Malformed lines and other extensions' entries are skipped, and an unreadable file yields an empty
    list rather than an error: a session with no previews is the common case, and a session list must
    not go red over one.
    """
    try:
        session_file = Path(session_path)
        if not session_file.is_file():
            return []
        handle = _open_jsonl(session_file)
    except OSError:
        return []

    entries: list[dict] = []
    try:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") != "custom" or record.get("customType") != PREVIEW_CUSTOM_TYPE:
                continue
            data = record.get("data")
            if isinstance(data, dict):
                entries.append(data)
                if len(entries) >= limit:
                    break
    finally:
        handle.close()
    return entries


def _message_summary(lines, preview_limit: int) -> tuple[str, int]:
    """Count user/assistant messages and grab the first user text.

    Takes any iterable of lines, including an open file, so the sidebar and the preview route stream
    a session instead of materializing it.
    """
    first_message = ""
    message_count = 0

    for line in lines:
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("type") != "message":
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



