"""Document preview store: where preview-pane documents live, and who may read them.

Two sources can reach the pane (plan: ``~/.pi/agent/plans/pi-chat-preview-pane.md``):

``store``
    A file the preview extension wrote into the per-connection scope directory it was handed
    through ``PI_CHAT_PREVIEW_DIR``. The browser names it by ``doc_id`` and this module finds it
    under the requesting account's directory only.

``path``
    An existing file somewhere on disk that the assistant pointed at. Serving an arbitrary path
    would turn the pane into a file-read oracle for any browser that reaches the server, so a path
    is accepted only when it resolves (symlinks included) inside an allowed root for that account.

Large bytes stay here: the pi session file and the WebSocket carry a pointer, and the browser
fetches content from ``/api/preview/{doc_id}``. That keeps multi-megabyte documents out of the
JSONL history and out of the event stream.

Every helper reads ``config`` attributes at call time, so tests can monkeypatch the settings
without re-importing this module.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("pi-chat")

# The kinds Phase 1 renders. This is also the *only* set of suffixes the preview route will serve,
# which is what keeps an allowed root from becoming "read any file in this tree".
KIND_EXTENSIONS = {"markdown": ".md", "html": ".html", "pdf": ".pdf"}

# What a browser needs from a persisted preview entry, and nothing else. `file` is the stored copy the
# extension wrote, `path` an existing file the pane was pointed at; a doc has one, the other, or (after
# an update to a referenced file) both, and the pane prefers `path`.
ENTRY_FIELDS = ("v", "op", "docId", "kind", "title", "revision", "file", "path", "note")

_SUFFIX_KINDS = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
}

_CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".pdf": "application/pdf",
}

# doc ids are uuid4().hex, and the strict check also stops a doc_id from acting as a glob.
DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# CSP for an HTML preview document. Model output is untrusted, so isolation comes from the opaque
# origin that ``sandbox`` without ``allow-same-origin`` produces (no parent DOM, no cookies: the
# document is cross-site to a SameSite=strict cookie jar). ``script-src 'unsafe-inline'`` is what
# makes a self-contained generated document work *inside* that box, and the tight img/connect/form
# sources are what keep the box from being an exfiltration or tracking endpoint. Remote images do
# not load; inline SVG and data: URIs do.
HTML_DOCUMENT_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "media-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "sandbox allow-scripts; "
    "frame-ancestors 'self'"
)

# A PDF is framed by the browser's own viewer, so it needs framing permission and nothing else.
# No ``sandbox`` token here: applying it to a PDF response interferes with the built-in viewer.
# A PDF is framed by the browser's own viewer, so it needs framing permission and nothing else. No
# ``sandbox`` token: Chrome's viewer will not render a sandboxed PDF, which is why preview.js also
# takes the ``sandbox`` attribute off the frame for this kind alone (measured 2026-11-09, and
# ``--stage pdf`` is the standing check). That makes a framed PDF the one preview body read as an
# application body of this origin rather than in an opaque frame, which is worth accepting: the route
# refuses a body whose kind differs from what was asked, so this response is only ever
# ``application/pdf`` under ``default-src 'none'``, and an application body carries no script context
# for a same origin to be worth.
PDF_DOCUMENT_CSP = "default-src 'none'; frame-ancestors 'self'"

# Markdown is fetched as text and rendered in the parent document after DOMPurify, so it is never a
# framed document and must not be framable at all.
TEXT_DOCUMENT_CSP = "default-src 'none'; frame-ancestors 'none'"


class PreviewError(ValueError):
    """A preview document was rejected before anything was written or served."""


def base_dir() -> Path:
    """Root holding every account's preview scopes."""
    return Path(config.PREVIEW_BASE_DIR)


def account_dir(account: str, *, create: bool = False) -> Path:
    """Directory holding one account's preview scopes.

    Profile names come from config, so an unexpected account can never become a path segment.
    """
    if account not in config.ACCOUNT_ENV_VARIABLES:
        raise PreviewError(f"Unknown profile for previews: {account!r}")
    path = base_dir() / account
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def scope_dir(account: str, scope: str, *, create: bool = False) -> Path:
    """Per-connection directory the preview extension is told to write into.

    ``scope`` is the PiProcess session id. It is generated here, but is sanitized anyway because
    the child process controls the environment variable carrying it.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", scope or "")[:64]
    if not safe or safe in {".", ".."}:
        safe = "default"
    path = account_dir(account, create=create) / safe
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def connection_scope_dir(account: str | None, session_id: str) -> Path:
    """Preview scope for one live connection, created on the way out.

    A connection with no known profile (a test harness, a future non-profile route) gets a scope
    under ``default``, which no browser can read: the content route only ever looks inside the
    authenticated profile's own tree.
    """
    label = account if account in config.ACCOUNT_ENV_VARIABLES else "default"
    return scope_dir(label, session_id, create=True)


def new_doc_id() -> str:
    return uuid.uuid4().hex


def new_state_token() -> str:
    """Secret for one connection's pane-state channel.

    The preview extension runs as a child of the server and cannot present the browser's cookie, so
    it needs a credential of its own to ask "which document is the human looking at". Minting it per
    spawn keeps that channel closed to any other local process and to any other session: a child may
    read the pane state its own connection reported and nothing else. Handed to the child in the
    environment and to the browser in `session_loaded`, which is the same disclosure either way.
    """
    return secrets.token_hex(16)


def document_path(scope: Path, doc_id: str, kind: str) -> Path:
    """Where a document with this id and kind lives inside a scope directory."""
    if not DOC_ID_RE.match(doc_id):
        raise PreviewError("doc_id must be 32 hex characters")
    extension = KIND_EXTENSIONS.get(kind)
    if extension is None:
        raise PreviewError(f"Unsupported preview kind: {kind!r}")
    return Path(scope) / f"{doc_id}{extension}"


def write_document(scope: Path, doc_id: str, kind: str, data: bytes) -> Path:
    """Write a preview document atomically, overwriting the previous revision of that doc id.

    The extension normally writes its own bytes with node:fs; this is the same contract for
    server-side callers and for tests.
    """
    limit = int(config.PREVIEW_MAX_DOC_MB) * 1024 * 1024
    if len(data) > limit:
        raise PreviewError(
            f"Preview document is {len(data)} bytes, limit is {limit}"
        )
    target = document_path(Path(scope), doc_id, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return target


def find_stored_document(account: str, doc_id: str, kind: str | None = None) -> Path | None:
    """Resolve a stored doc id to a file inside this account's preview tree, if any.

    A document's kind is fixed when it is opened, so normally exactly one file carries an id. If
    more than one does (an assistant reused an id across kinds, or a stale revision survived), the
    newest wins rather than whatever the directory iteration happened to yield first: the pane must
    never be handed a document of a different kind than the CSP and renderer were chosen for. Pass
    ``kind`` to say what the client expects and only that kind is considered.
    """
    if not DOC_ID_RE.match(doc_id):
        return None
    if kind is not None and kind not in KIND_EXTENSIONS:
        return None
    try:
        root = account_dir(account).resolve()
    except PreviewError:
        return None
    if not root.is_dir():
        return None

    wanted = KIND_EXTENSIONS[kind] if kind else None
    # Two accounts can never read each other's documents, and the tree walk stays inside the
    # resolved root so a symlinked scope cannot widen the search.
    matches: list[Path] = []
    newest: tuple[float, Path] | None = None
    for candidate in root.glob(f"**/{doc_id}.*"):
        suffix = candidate.suffix.lower()
        if suffix not in _CONTENT_TYPES or (wanted and suffix != wanted):
            continue
        resolved = candidate.resolve()
        if not resolved.is_file() or not _is_within(resolved, root):
            continue
        matches.append(resolved)
        try:
            stamp = resolved.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest[0]:
            newest = (stamp, resolved)

    if newest is None:
        return None
    if len(matches) > 1:
        log.warning(
            "Preview doc id %s has %d documents; serving the newest (%s)",
            doc_id,
            len(matches),
            newest[1].name,
        )
    return newest[1]


def allowed_roots(account: str) -> list[Path]:
    """Trees a ``path`` preview may be served from for this account."""
    roots = [base_dir().resolve(), (config.PROJECT_ROOT / "sessions" / account).resolve()]
    for extra in getattr(config, "PREVIEW_EXTRA_ROOTS", []):
        candidate = Path(extra)
        if candidate.is_dir():
            roots.append(candidate.resolve())
    return roots


def resolve_external_path(raw: str, account: str) -> Path | None:
    """Resolve an assistant-supplied file path, or None when it may not be served.

    None is the only failure mode on purpose: 403 would confirm that a path exists.
    """
    if not raw or "\x00" in raw:
        return None
    try:
        candidate = Path(raw).expanduser()
    except (OSError, ValueError):
        return None
    if not candidate.is_absolute():
        return None

    resolved = candidate.resolve()
    if resolved.suffix.lower() not in _CONTENT_TYPES:
        return None
    if not resolved.is_file():
        return None

    # DEV_MODE already skips session ownership elsewhere and exists to render arbitrary local
    # files, so it relaxes this the same way. Never in production.
    if getattr(config, "DEV_MODE", False):
        return resolved

    roots = allowed_roots(account)
    for root in roots:
        if _is_within(resolved, root):
            return resolved
    log.info("Preview refused a path outside the allowed roots: %s", resolved)
    return None


def content_type_for(path: Path) -> str | None:
    return _CONTENT_TYPES.get(Path(path).suffix.lower())


def kind_for(path: Path) -> str | None:
    return _SUFFIX_KINDS.get(Path(path).suffix.lower())


# What a caller gets when there is nothing to watch. Identical for "outside the roots", "another
# profile's document", "wrong kind" and "deleted since the last poll": the staleness route must not be
# a path oracle any more than the serving route is, so those four cannot be told apart from here.
NOT_FOUND: dict = {"served": False, "bytes": None, "mtime": None, "token": None}


def head(
    account: str,
    *,
    doc_id: str | None = None,
    kind: str | None = None,
    path: str | None = None,
) -> dict:
    """Stat the bytes one pane would fetch, without fetching them.

    This is the whole staleness primitive behind ``GET /api/preview/fresh``: the pane compares the
    returned ``token`` against the one it rendered, and re-fetches only when it moves. It resolves
    through the same two functions as the serving route (``resolve_external_path`` for a referenced
    file, ``find_stored_document`` for a stored one) and in the same preference order as
    ``previewUrl`` and ``_entry_location``, because judging the other pointer would report freshness
    for bytes the browser never asks for. So this cannot name a file the caller could not already
    fetch: it is a cheaper read of the same permission, not a wider one.

    ``token`` is ``f"{st_mtime_ns}-{st_size}"``, the same pairing ``devtools._stamp`` settled on:
    two saves inside one clock tick are separated by the byte count, and an edit that preserves the
    byte count is rare enough that the mtime ends the argument. It is a validator, not a timestamp:
    it is compared for equality and never ordered.

    ``doc_id`` is a *stored* document id. A doc that was updated after being opened by ``path``
    carries both, and the caller passes the ``path`` it shows rather than the stored copy, which is
    the one the route would serve. Callers that hold an entry should go through ``head_for_entry``.
    """
    if isinstance(path, str) and path:
        location = resolve_external_path(path, account)
    else:
        # A kind that is present but unknown must not widen the search to "any kind": the pane asked
        # about one specific document, and answering for a sibling file with the same id would drive
        # a reload of bytes it was never given. Absence of `kind` is different and legitimate (a
        # replayed entry from before the field existed), and there the newest match is what the
        # serving route itself would hand back.
        if kind is not None and kind not in KIND_EXTENSIONS:
            return dict(NOT_FOUND)
        if not isinstance(doc_id, str) or not DOC_ID_RE.match(doc_id):
            return dict(NOT_FOUND)
        location = find_stored_document(account, doc_id, kind)

    if location is None:
        return dict(NOT_FOUND)
    try:
        if not location.is_file():
            return dict(NOT_FOUND)
        info = location.stat()
    except OSError:
        return dict(NOT_FOUND)
    return {
        "served": True,
        "bytes": info.st_size,
        "mtime": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "token": f"{info.st_mtime_ns}-{info.st_size}",
    }


def head_for_entry(account: str, entry: dict) -> dict:
    """``head`` for a persisted preview entry, using the entry's own pointers.

    Exists so the pane's two callers (a live entry, a replayed one) ask the same question the same
    way as ``_entry_location`` does, instead of each re-deriving which of ``path`` and ``file`` wins.
    """
    if not isinstance(entry, dict):
        return dict(NOT_FOUND)
    raw_path = entry.get("path")
    kind = entry.get("kind") if entry.get("kind") in KIND_EXTENSIONS else None
    return head(
        account,
        doc_id=str(entry.get("docId") or "") or None,
        kind=kind,
        path=raw_path if isinstance(raw_path, str) and raw_path else None,
    )


def document_headers(path: Path) -> dict[str, str]:
    """Headers that make a preview document safe to frame, per kind.

    The app middleware sets CSP with ``setdefault``, so setting it here is what overrides the
    site-wide ``frame-ancestors 'none'`` for the two kinds that legitimately get framed.
    """
    kind = kind_for(Path(path))
    if kind == "html":
        csp = HTML_DOCUMENT_CSP
    elif kind == "pdf":
        csp = PDF_DOCUMENT_CSP
    else:
        csp = TEXT_DOCUMENT_CSP
    return {
        "Content-Security-Policy": csp,
        # Documents are rewritten in place as the assistant edits them, and doc ids are reused
        # across revisions, so nothing here may be cached by a shared proxy or the browser.
        "Cache-Control": "no-store",
    }


def cleanup(
    ttl_days: int | None = None,
    max_bytes: int | None = None,
) -> tuple[int, int]:
    """Delete expired scopes, then the oldest scopes until under the size cap.

    Returns (removed_files, freed_bytes). Measured in files rather than scope directories because
    a scope is cheap and a stale 20 MB PDF is not.
    """
    root = base_dir()
    if not root.is_dir():
        return (0, 0)

    ttl_days = int(config.PREVIEW_TTL_DAYS) if ttl_days is None else int(ttl_days)
    max_bytes = (
        int(config.PREVIEW_MAX_MB) * 1024 * 1024
        if max_bytes is None
        else int(max_bytes)
    )
    now = time.time()
    removed = 0
    freed = 0

    files: list[tuple[Path, float, int]] = []
    for candidate in root.glob("*/*/*"):
        if not candidate.is_file():
            continue
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if ttl_days > 0 and now - stat.st_mtime > ttl_days * 86400:
            if _delete(candidate):
                removed += 1
                freed += stat.st_size
            continue
        files.append((candidate, stat.st_mtime, stat.st_size))

    total = sum(size for _, _, size in files)
    if max_bytes > 0 and total > max_bytes:
        for path, _, size in sorted(files, key=lambda item: item[1]):
            if total <= max_bytes:
                break
            if _delete(path):
                removed += 1
                freed += size
                total -= size

    if removed:
        log.info("Preview cleanup removed %d file(s), %d bytes", removed, freed)
    return (removed, freed)


def _delete(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def describe_preview_entries(account: str, entries: Iterable[dict]) -> list[dict]:
    """Turn persisted preview entries into the payload a browser replays, annotating availability.

    A session file records a pointer; only the disk knows whether the bytes are still there. Cleanup
    keeps a scope for ``preview_ttl_days`` and prunes the oldest scopes past the size cap, so a session
    reopened a month later points at documents that no longer exist. Answering that per entry costs one
    ``stat``, memoized per document, and it is what lets the pane say "this file is gone" instead of
    fetching forty times to discover it.

    Only the fields the pane uses are copied out. The rest of a persisted entry is pi's bookkeeping and
    has no business in a browser payload.
    """
    described: list[dict] = []
    stored: dict[tuple[str, str | None], Path | None] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        payload = {key: entry[key] for key in ENTRY_FIELDS if key in entry}
        if str(entry.get("op") or "open") in ("open", "update"):
            payload.update(_availability(_entry_location(account, entry, stored)))
        described.append(payload)
    return described


def _entry_location(
    account: str,
    entry: dict,
    cache: dict[tuple[str, str | None], Path | None],
) -> Path | None:
    """Where the pane would read this document from, using the same preference the url encodes.

    ``previewUrl`` puts ``path`` on the request whenever the entry has one, so a referenced file is the
    thing being fetched even after an update wrote a stored copy beside the original. Checking the other
    order would report availability for bytes the browser never asks for.
    """
    raw_path = entry.get("path")
    if isinstance(raw_path, str) and raw_path:
        return resolve_external_path(raw_path, account)

    doc_id = str(entry.get("docId") or "")
    if not doc_id:
        return None
    kind = entry.get("kind") if entry.get("kind") in KIND_EXTENSIONS else None
    key = (doc_id, kind)
    if key not in cache:
        cache[key] = find_stored_document(account, doc_id, kind)
    return cache[key]


def _availability(location: Path | None) -> dict:
    if location is None:
        return {"available": False, "bytes": None, "mtime": None}
    try:
        if not location.is_file():
            return {"available": False, "bytes": None, "mtime": None}
        info = location.stat()
    except OSError:
        return {"available": False, "bytes": None, "mtime": None}
    return {
        "available": True,
        "bytes": info.st_size,
        "mtime": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds"),
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
