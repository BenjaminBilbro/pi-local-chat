"""Debug-mode audit log: one JSONL file per browser connection, both directions of the wire.

This exists because a report about what a person *did* cannot be checked against code that only knows what
the server *said*. The mute bug is the case that motivated it. That button sent a command which never touched
the microphone, so nothing in the server's own view was wrong, and the only evidence that capture stayed open
lived in the browser. Server-side wire events plus browser-reported events in one file on one clock is what
turns "it kept talking and then nothing went through" into a timeline.

**Shapes, not bytes.** `PI_CHAT_AUDIT=1` records what happened, never what was said. Text becomes a length
and a short hash, and only keys that carry machine state keep their values verbatim. That limit is deliberate
rather than a first-pass omission: the profiles here are family, the repository is public, and a log holding
transcripts is a second copy of the thing the 2026-11-17 history purge removed. Anything that smells like a
credential is written as the word `redacted` instead of a digest of it, because a digest of a low-entropy
password is a lookup table waiting for a volunteer.

Enable it with `PI_CHAT_AUDIT=1 uv run python server.py`. Files land in `logs/`, one per connection, named
for the account and the moment the connection opened, and `logs/` is gitignored. Read them with
`tools/audit.py`. See `docs/dev-workflow.md`.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

from . import config

# Keys whose values are machine state: keep them, including short strings, because `reason: "superseded"` is
# often the entire diagnostic. Anything not listed is shaped even when it is short, on the assumption that a
# bare word from an unknown field might be something a person said. The list is an allowlist on purpose - a
# new payload field is a stranger until someone decides which side of this line it is on.
SAFE_KEYS = frozenset({
    "type", "state", "reason", "action", "code", "kind", "engine", "mode", "role", "phase",
    "ok", "enabled", "available", "ready", "recoverable", "cancelled", "delivery", "muted",
    "streamId", "stream_id", "sequence", "seq", "index", "turn", "count", "chars", "bytes",
    "ms", "messageId", "toolCallId", "toolName", "cleared", "width", "height", "version", "id",
    "stateName", "side", "stage", "surface", "key", "event", "failures", "open", "error",
    # Written by this server about itself, never quoted from a conversation: `why` names which code path
    # moved the echo window, `account` is a profile letter, `size` is a byte count.
    "why", "account", "size",
})

# Written as the word `redacted`. Hashing these would be a claim that their secrecy is the problem.
SECRET_KEYS = frozenset({
    "password", "pass", "token", "secret", "authorization", "cookie", "api_key", "apikey",
})

# A state word: short, no whitespace, no separators a path or a sentence would need.
ENUM = re.compile(r"[A-Za-z][A-Za-z0-9_.:/+#-]{0,47}")


def _digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:8]


def shape_value(key: str, value: object, safe: bool | None = None) -> object:
    """Reduce one value to something safe to keep: state verbatim, content as a length plus a digest.

    The string rule is what makes this safe to leave running. A short token with no spaces on a
    state-carrying key stays, because it names a state. Anything longer, or on a key nobody vouched for,
    becomes `str(1834 a1b2c3d4)`, which is enough to tell a reply from its retry and not enough to read.
    """
    if key.lower() in SECRET_KEYS:
        return "redacted"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        keep_as_is = (safe if safe is not None else key in SAFE_KEYS) and ENUM.fullmatch(value)
        if keep_as_is:
            return value
        return "" if value == "" else f"str({len(value)} {_digest(value)})"
    if isinstance(value, bytes):
        return f"bytes({len(value)})"
    if isinstance(value, dict):
        return {str(k): shape_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        # A count plus one shaped element. The interesting list is "3 files" or "2 queue entries"; the
        # element is there so the shape of a list of objects survives instead of collapsing to a number.
        items = list(value)
        return {"len": len(items), "sample": shape_value(key, items[0], safe) if items else None}
    return type(value).__name__


def _with_event_kind(message: dict) -> dict:
    """Shape a payload, then pull pi's own event kind up to the top level.

    A streaming answer arrives as hundreds of `{"type": "pi_event", "event": {...}}` envelopes, and the shaped
    envelope alone says nothing but `pi_event`. The kind, the sub-kind and the length of a text delta are
    state words rather than conversation, and they are what turns "it flickered while streaming" into a rate
    you can compare against a code change.
    """
    shaped = shape(message)
    event = message.get("event")
    if not isinstance(event, dict):
        return shaped
    kind = event.get("type")
    if isinstance(kind, str) and kind:
        shaped["eventKind"] = kind[:48]
    inner = event.get("assistantMessageEvent")
    if isinstance(inner, dict):
        sub = inner.get("type")
        if isinstance(sub, str) and sub:
            shaped["subKind"] = sub[:48]
        delta = inner.get("delta")
        if isinstance(delta, str):
            shaped["chars"] = len(delta)
    return shaped


def shape(message: object) -> dict:
    """Shape a whole payload. Keys survive; only state-carrying values keep their bytes."""
    if not isinstance(message, dict):
        return {"payload": shape_value("payload", message)}
    return {str(k): shape_value(str(k), v) for k, v in message.items()}


class AuditLog:
    """One append-only JSONL file for one browser connection.

    Writes flush every line rather than buffering. The sessions this exists to diagnose are the ones that end
    in a crash or a closed laptop, and a log missing its last thirty events describes a different bug than
    the one being reported.
    """

    def __init__(self, path: Path, conn: str, account: str | None = None, max_bytes: int | None = None):
        self.path = path
        # Rotations are always named from where the connection started, never from the file currently open.
        # Deriving from the current name is the bug this line prevents: part2 becomes part2-part2.
        self._base = path
        self.conn = conn
        self.account = account
        self.max_bytes = max_bytes if max_bytes is not None else config.AUDIT_MAX_BYTES
        self._seq = 0
        self._t0 = time.monotonic()
        self._pending = {"bytes_in": 0, "bytes_out": 0}
        self._stopped = False
        self._rotations = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self.event("open", account=account or "anon")

    # ── the wire ─────────────────────────────────────────────────────

    def json_in(self, message: dict) -> None:
        """One command from the browser.

        No counter here, and none for `json_out` either. Each of these lines already *is* the record of one
        message, so counting them beside themselves wrote a second line per message: on a live streaming
        session that was half the file, and a log that is half noise is a log nobody reads.
        """
        self.record("server", "in", str(message.get("type", "unknown")), _with_event_kind(message))

    def json_out(self, payload: dict) -> None:
        """One message headed for the browser."""
        self.record("server", "out", str(payload.get("type", "unknown")), _with_event_kind(payload))

    def bytes_in(self, count: int) -> None:
        """Microphone bytes. Coalesced on purpose: a second of audio is dozens of identical packets, and a
        wall of them buries the interesting lines. What matters is the total and when it stopped."""
        self._pending["bytes_in"] += count
        if self._pending["bytes_in"] >= config.AUDIT_COALESCE_BYTES:
            self._flush_pending()

    def bytes_out(self, count: int) -> None:
        """Synthesized audio bytes headed for the browser. The absence of these lines is the evidence that
        speech never arrived, which is the difference between a synthesis failure and a playback failure."""
        self._pending["bytes_out"] += count
        if self._pending["bytes_out"] >= config.AUDIT_COALESCE_BYTES:
            self._flush_pending()

    # ── internal state ───────────────────────────────────────────────

    def event(self, kind: str, /, **data: object) -> None:
        """A server-side transition that is not a message, e.g. the AEC window opening or closing.

        `kind` is positional-only because the payload is arbitrary and `kind` is a perfectly ordinary field
        name to want in it; a keyword here would collide with the caller's own data and raise inside the
        recorder, which is the one thing a recorder may never do to the thing it is watching.
        """
        self.record("server", "event", kind, shape(data) if data else None)

    def client_event(self, kind: str, data: dict | None = None) -> None:
        """Something the browser saw: a click, a phase change, the AudioContext's state, frames received."""
        self.record("client", "event", kind, shape(data or {}))
    def close(self, reason: str = "closed") -> None:
        if not self._stopped:
            self.record("server", "event", "close", {"reason": reason})
        self._close_file()

    # ── writing ──────────────────────────────────────────────────────

    def record(self, src: str, direction: str, kind: str, data: dict | None = None) -> None:
        if self._stopped:
            return
        self._flush_pending()
        self._seq += 1
        line = {
            "seq": self._seq,
            "ts": round(time.time(), 3),
            "dt": round(time.monotonic() - self._t0, 3),
            "conn": self.conn,
            "src": src,
            "dir": direction,
            "type": kind,
        }
        if data:
            line["data"] = data
        self._write(line)
        # The cap is a dogfooding guard, not a rotation policy for its own sake: a session is longer than a
        # few megabytes of events, and the interesting moment is usually the one nearest the report, so a log
        # that stops writing has thrown away the part someone needed. Past the cap it rolls to a second file
        # and keeps going, up to a ceiling that exists only so a runaway loop cannot fill the disk.
        if self._seq % 50 == 0 and self.path.stat().st_size > self.max_bytes:
            if self._rotations >= config.AUDIT_MAX_ROTATIONS:
                self._stopped = True
                self._write({"seq": self._seq, "conn": self.conn, "type": "truncated",
                             "note": "audit log reached its rotation ceiling and stopped writing"}, force=True)
                return
            self._write({"seq": self._seq, "conn": self.conn, "type": "rotating",
                         "note": "continuing in the next file", "bytes": self.path.stat().st_size}, force=True)
            self._rotate()

    def _rotate(self) -> None:
        """Continue into `...-part2.jsonl`, then part3, keeping one connection readable per file."""
        try:
            self._fh.close()
        except OSError:
            pass
        stem = self._base.with_suffix("")
        self.path = Path(f"{stem}-part{self._rotations + 2}{self._base.suffix}")
        self._rotations += 1
        try:
            self._fh = self.path.open("a", encoding="utf-8")
        except OSError:
            self._stopped = True

    def _flush_pending(self) -> None:
        if self._stopped or not any(self._pending.values()):
            return
        snapshot = dict(self._pending)
        self._pending = dict.fromkeys(snapshot, 0)
        self._seq += 1
        self._write({
            "seq": self._seq,
            "ts": round(time.time(), 3),
            "dt": round(time.monotonic() - self._t0, 3),
            "conn": self.conn,
            "src": "server",
            "dir": "totals",
            "type": "totals",
            "data": snapshot,
        })

    def _write(self, line: dict, force: bool = False) -> None:
        if self._stopped and not force:
            return
        try:
            self._fh.write(json.dumps(line, separators=(",", ":"), default=str) + "\n")
            self._fh.flush()
        except (OSError, ValueError):
            # OSError is the disk; ValueError is a handle closed underneath us, which is what "the browser
            # went away mid-write" actually looks like from here. Either way: an audit log that cannot be
            # written must never be the reason a session fails.
            self._stopped = True

    def _close_file(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def open_log(account: str | None = None) -> AuditLog | None:
    """Open a log for a new connection, or return None when the audit flag is off.

    Off unless asked for, and `PI_CHAT_DEV` deliberately does not imply it. A forgotten flag that writes a
    record of a conversation to disk is its own finding, so this one costs a second variable to turn on.
    """
    if not config.AUDIT_ENABLED:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    token = f"{stamp}-{secrets.token_hex(3)}"
    safe_account = re.sub(r"[^A-Za-z0-9_.-]", "_", account or "anon")
    path = Path(config.AUDIT_DIR) / f"audit-{safe_account}-{token}.jsonl"
    return AuditLog(path, token, account)
