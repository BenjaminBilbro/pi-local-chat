"""Attachments: the bytes a browser attaches, where they land, and what the agent is told.

Before this module existed, an attachment reached the model as text and nothing else. A PDF was
transcribed into the prompt and the original bytes were dropped on the floor; an image was handed to
pi's `images` array as base64 and never written anywhere at all. That is enough to *see* a picture and
nowhere near enough to work on one: pi's `ImageContent` is `{type, data, mimeType}`
(`pi/docs/message-types.md`), with no field for a filename or a path, so an assistant asked to fix a
photo knew what it looked like and had no way to name the file to `edit_image`.

So the bytes go to disk under the account's own work directory, which is the child's `cwd`
(`pi_chat/process.py`), and the prompt carries the absolute path beside the content that was always
inlined. The path is the whole point of the module; everything else here is the plumbing that makes
handing out a path safe:

* the browser never names a path. It names a `ref` this module minted, and `resolve_ref()` refuses
  anything that does not resolve inside this account's upload root;
* a filename is a display string from an untrusted client, so it is reduced to `[A-Za-z0-9._-]` and
  prefixed with random hex, which also settles collisions between two files called `IMG_4913.jpg`;
* the inline copy of a document is capped. Up to the cap the agent gets the text as it always did,
  and above it the agent gets the path and is told to read it in chunks, because a 5 MB text file
  used to be dumped whole into the context window at roughly 1.2 million tokens.

Retention is a knob with a sweep rather than a promise: attachments are conversation output, and a
directory that only ever grows is a disk-full bug with a delay attached.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT

log = logging.getLogger("pi-chat")

# The largest image allowed to ride the WebSocket as base64. uvicorn's incoming frame cap is
# `ws_max_size`, 16 MiB by default, and base64 costs 4 bytes per 3 - so 12 MiB of image would fill a
# frame exactly, leaving no room for the rest of the JSON around it. 10 MiB is the largest round
# number under that, and the slack is the message text, the media types and the filenames.
#
# The number is a socket limit wearing an image limit, and the reason to care is the failure mode: a
# frame over `ws_max_size` is not answered with an error, the connection is closed. Past this, the
# browser says "that photo is too big" rather than appearing to fall over.
MAX_IMAGE_BYTES = int(os.environ.get("PI_CHAT_IMAGE_MAX_BYTES", "10485760"))  # 10 MiB

# Ceiling on the extracted text an attached document contributes to the prompt. Chosen to keep one
# attachment worth roughly 50k tokens, which is a document the model can actually hold in mind; the
# files above it are read from disk on demand instead, which is what the `read` tool is for.
INLINE_MAX_CHARS = int(os.environ.get("PI_CHAT_INLINE_MAX_CHARS", "200000"))

# Days an attachment survives. The sweep runs at startup, which is enough: the server restarts more
# often than a month in this deployment, and a scheduled task is another thing that can silently die.
RETENTION_DAYS = int(os.environ.get("PI_CHAT_UPLOAD_RETENTION_DAYS", "30"))

# Image kinds the pane and the image tools agree on. The suffix is what decides how a saved file
# reads, so an exotic `mimeType` from the browser does not get to invent one.
SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_KNOWN_IMAGE_SUFFIXES = frozenset(SUFFIX_BY_MIME.values()) | {".jpeg"}

# A scope is the browser tab's own directory name: a uuid it keeps in sessionStorage. It has to exist
# before any child does (invariant 3: no `pi` process until the first prompt), so it cannot be a
# server-side connection id, and a shape this tight is what keeps it out of the path-traversal list.
_SCOPE = re.compile(r"^[0-9a-f][0-9a-f-]{7,63}$")

# `<scope>/<name>` as handed back by the upload route. No separator but `/`, no `..` segment, and the
# name is already sanitized, so a ref that matches this cannot escape the root even before the check.
_REF = re.compile(r"^[0-9a-f][0-9a-f-]{7,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_SAFE_RUN = re.compile(r"[^A-Za-z0-9._-]+")
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_MAX_STEM = 60


def account_root(account: str) -> Path:
    """The account's upload root: `sessions/<account>/uploads`.

    Under the work directory on purpose. It is already gitignored, it is already the child's `cwd`, and
    it is already scoped per account by the same rule that owns sessions (invariant 6).
    """
    return PROJECT_ROOT / "sessions" / account / "uploads"


def is_valid_scope(raw: object) -> bool:
    return isinstance(raw, str) and _SCOPE.match(raw) is not None


def scope_dir(account: str, scope: str, *, create: bool = False) -> Path | None:
    """This tab's directory inside the account's upload root, or None if the scope is not a scope."""
    if not is_valid_scope(scope):
        return None
    path = account_root(account) / scope
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(original: str, *, fallback: str = "file") -> str:
    """Reduce a client-supplied filename to a display name that is safe as one path segment.

    The stem is kept because the agent quotes it back and a person recognizes `receipt.pdf` faster
    than `a31f9c02-file.pdf`. Everything unsafe collapses to a single dash, the stem is capped so a
    300-character download name cannot push the extension out of the path, and a suffix that is not a
    plausible extension is dropped rather than trusted.
    """
    stem = Path(str(original or "").replace("\\", "/")).name
    suffix = ""
    match = _SUFFIX.search(stem)
    if match:
        candidate = match.group(0).lower()
        if len(candidate) <= 9 and candidate not in {".", ".."}:
            suffix = candidate
            stem = stem[: match.start()]
    stem = _SAFE_RUN.sub("-", stem).strip("-._")[:_MAX_STEM]
    if not stem:
        stem = fallback
    return f"{stem}{suffix}" if suffix else stem


def image_suffix_for(name: str, mime_type: str) -> str:
    """The suffix a saved image gets: the real one when we know it, otherwise from the media type.

    A browser can name a file anything and label it any media type. The first one whose suffix we
    recognise wins, because the file's suffix is what decides whether `edit_image` will open it.
    """
    original = Path(str(name or "")).suffix.lower()
    if original in _KNOWN_IMAGE_SUFFIXES:
        return ".jpg" if original == ".jpeg" else original
    return SUFFIX_BY_MIME.get(str(mime_type or "").lower(), ".png")


@dataclass(frozen=True)
class Stored:
    """One attachment as it now exists on disk.

    `ref` is what the browser keeps and sends back on the next prompt; `path` is what the agent is
    told. They are the same file seen from two ends, and only one of them is a path.
    """

    ref: str
    path: Path
    name: str
    bytes: int
    mime_type: str

    def as_dict(self) -> dict:
        return {
            "ref": self.ref,
            "path": str(self.path),
            "name": self.name,
            "bytes": self.bytes,
            "mimeType": self.mime_type,
        }


def store(
    account: str,
    scope: str | None,
    *,
    original_name: str,
    data: bytes,
    mime_type: str = "",
    suffix: str | None = None,
) -> Stored | None:
    """Write one attachment into this account's tree and return what it became.

    Returns None when the scope is not a valid scope id, which is the caller's cue to refuse the
    upload rather than silently filing it somewhere the browser cannot name later.
    """
    directory = scope_dir(account, scope or "", create=True)
    if directory is None:
        return None

    name = safe_name(original_name)
    if suffix:
        # An image's suffix comes from what the bytes are, not from what the file was called.
        name = f"{name.rsplit('.', 1)[0] if '.' in name else name}{suffix}"
    final = directory / f"{secrets.token_hex(4)}-{name}"
    final.write_bytes(data)
    return Stored(
        ref=f"{scope}/{final.name}",
        path=final,
        name=name,
        bytes=len(data),
        mime_type=mime_type,
    )


def resolve_ref(account: str, ref: object) -> Path | None:
    """The file a `ref` names, or None when it names nothing this account may read.

    Two guards, both needed. The pattern says the shape cannot contain an escape; the resolved-path
    check says the file really is under this account's root, which is what still holds if a
    symlink or a junction is planted in there by something else running as this user.
    """
    if not isinstance(ref, str) or _REF.match(ref) is None:
        return None
    root = account_root(account).resolve()
    candidate = (root / ref).resolve()
    if not candidate.is_file():
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _attr(value: object) -> str:
    """Escape an attribute value. A filename with a quote in it must not close the tag early."""
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def file_tag(*, name: str, path: Path, bytes_: int, text: str | None, mime_type: str = "") -> str:
    """One `<file>` record: the path always, and the body only when it was small enough to inline.

    The tag keeps the shape the browser already strips for display (`static/utils.js`), so an
    attachment renders as a chip and a name rather than as 30 KB of PDF transcription in a bubble.
    """
    mime = f' mimeType="{_attr(mime_type)}"' if mime_type else ""
    head = f'<file name="{_attr(name)}" path="{_attr(path)}" bytes="{bytes_}"{mime}'
    if text is None:
        # No body, and a line saying so: silence here reads as an empty document, and an assistant
        # that believes the file is empty does not go and read it.
        return head + f">(not inlined: over the limit of {INLINE_MAX_CHARS:,} characters)</file>"
    return f"{head}>{text}</file>"


def image_tag(*, name: str, path: Path, bytes_: int, mime_type: str) -> str:
    """One `<image>` record. The picture itself travels in pi's `images` array; this is the address."""
    return (
        f'<image name="{_attr(name)}" path="{_attr(path)}" '
        f'bytes="{bytes_}" mimeType="{_attr(mime_type)}"/>'
    )


def _note(count: int) -> str:
    """The one sentence that tells the model what the paths are for.

    Worth its tokens: without it the tags are decoration, and the whole reason this module exists is
    that an assistant with a picture in context had no way to find out where the picture was.
    """
    if count == 1:
        return (
            "The attached file on disk is at this exact path. Pass the path straight to a file or "
            "image tool (read, edit_image, generate_image) instead of asking for the attachment again."
        )
    return (
        f"The {count} attached files on disk are at these exact paths. Pass a path straight to a file "
        "or image tool (read, edit_image, generate_image) instead of asking for an attachment again."
    )


def attachment_block(tags: "list[str]") -> str:
    """The whole attachment section, wrapped.

    The wrapper is what lets the browser take the section back out for display with one rule
    (`static/utils.js` renders an attachment as a chip and a name, never as 30 KB of transcription),
    which is also why the note lives inside it rather than beside it: prose outside a tag is prose a
    renderer has to recognize by its wording, and wording changes.
    """
    if not tags:
        return ""
    return "<attachments>" + _note(len(tags)) + "\n" + "\n".join(tags) + "</attachments>"


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def accounts_with_uploads() -> list[str]:
    """Every account directory that has an upload root, configured or not.

    Deliberately read off the disk rather than from `ACCOUNT_ENV_VARIABLES`: a profile whose passphrase
    was removed still has attachments on disk, and a sweep that only visits profiles that still log in
    is how that directory grows forever.
    """
    sessions = PROJECT_ROOT / "sessions"
    if not sessions.is_dir():
        return []
    return sorted(
        path.name
        for path in sessions.iterdir()
        if path.is_dir() and (path / "uploads").is_dir()
    )


def prune(*, now: float | None = None) -> int:
    """Delete attachments older than the retention window. Returns how many files went.

    Failures are counted and logged rather than raised: a file the OS still has open (a preview pane,
    an editor) is a normal sight on Windows, and a startup that cannot delete one old upload has no
    business refusing to serve chat.
    """
    if RETENTION_DAYS <= 0:
        return 0
    cutoff = (now if now is not None else time.time()) - RETENTION_DAYS * 86400
    removed = 0
    for account in accounts_with_uploads():
        root = account_root(account)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError as error:
                log.debug("upload prune skipped %s: %s", path.name, error)
        _drop_empty_scopes(root)
    if removed:
        log.info(
            "pruned %d attachment%s older than %d day%s",
            removed,
            "" if removed == 1 else "s",
            RETENTION_DAYS,
            "" if RETENTION_DAYS == 1 else "s",
        )
    return removed


def _drop_empty_scopes(root: Path) -> None:
    for directory in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        try:
            next(directory.iterdir())
        except StopIteration:
            try:
                directory.rmdir()
            except OSError:
                pass
        except OSError:
            pass
