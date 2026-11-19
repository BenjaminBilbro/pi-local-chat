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

# The preview tier. Below `PREVIEW_MIN_CHARS` a document is handed over whole, because a 2 KB note
# split into a head and a tail is worse than the note: two fragments, more tokens, less sense. Between
# that and the cap it is handed over as the first `PREVIEW_HEAD_CHARS` and the last `PREVIEW_TAIL_CHARS`
# with the omission stated in characters, because the body of a document is exactly the part a model can
# go and read, while the front (what this file is, its status) and the tail (what it concluded) are the
# parts it needs to decide whether to.
#
# The number that sold Ben on it was his own session: a 10,156-byte notes file went in whole at roughly
# 2.5k tokens and the assistant then spent a turn proving to itself that it had not been truncated.
# Above the cap the cost is not 2.5k but around 50k, riding in every later turn of that session and
# re-prefilled from scratch whenever the engine's prefix cache misses, which he has now watched happen.
PREVIEW_MIN_CHARS = int(os.environ.get("PI_CHAT_INLINE_PREVIEW_MIN_CHARS", "4000"))
PREVIEW_HEAD_CHARS = int(os.environ.get("PI_CHAT_INLINE_PREVIEW_HEAD_CHARS", "1500"))
PREVIEW_TAIL_CHARS = int(os.environ.get("PI_CHAT_INLINE_PREVIEW_TAIL_CHARS", "500"))

# Stable prefix of the marker line inside a previewed body. `static/utils.js` matches on it to label the
# chip "previewed", and it has to be stable for the same reason `(not inlined` is: the marker is present
# in the live turn and in the session file, so recognising it there rather than storing it somewhere
# else is what makes a reloaded session draw the same chip as a live one.
PREVIEW_MARKER = "[preview:"

# Days an attachment survives. The sweep runs at startup, which is enough: the server restarts more
# often than a month in this deployment, and a scheduled task is another thing that can silently die.
RETENTION_DAYS = int(os.environ.get("PI_CHAT_UPLOAD_RETENTION_DAYS", "30"))

# Documents that need no parser: they are decoded, kept as their own bytes, and handed over as text.
# `.md` was added on 2026-11-19 because Ben could attach a PDF of a licence but not his own notes, and
# the only thing standing in the way was this table plus the `accept` attribute in `static/index.html`.
# Anything that needs a parser (a .docx, a spreadsheet) is a different change and a different risk.
TEXT_SUFFIX_TO_MIME = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
}
PDF_SUFFIX = ".pdf"
PDF_MIME = "application/pdf"


def accepted_document_suffixes() -> tuple[str, ...]:
    """What `/api/upload-file` takes, in the order the error message lists it."""
    return (PDF_SUFFIX, *TEXT_SUFFIX_TO_MIME)

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


def inline_verdict(text: str | None) -> str:
    """One verdict for one document: `inlined`, `empty`, `over`, or `missing`.

    This function exists because two places used to decide separately and disagree. The upload route
    tested `len(text) <= INLINE_MAX_CHARS`, which a scanned PDF passes at length 0; the prompt path
    tested `0 < len(text) <= INLINE_MAX_CHARS`, which it fails. The document therefore arrived in the
    model's context blamed for a size it did not have, and the advice that follows a size is to read the
    file in chunks, which cannot work on a page with no text layer either. Ben's live test found it on
    a photo of a licence.

    Whitespace counts as empty on purpose: pdfplumber answers a scanned page with newlines, and a body
    of three `\n` is the same document as a body of nothing.
    """
    if text is None:
        return "missing"
    if not text.strip():
        return "empty"
    if len(text) > INLINE_MAX_CHARS:
        return "over"
    return "inlined"


def preview_body(text: str) -> str:
    """Head, an honest statement of what is missing, tail. For documents between the preview floor and the cap.

    Both cuts land on whitespace where one is nearby, so the model is not shown half a word and does not
    infer a word boundary that is not there. The marker counts in characters, the same unit the cap is
    quoted in, so the two numbers mean the same thing.
    """
    total = len(text)
    head = text[:PREVIEW_HEAD_CHARS]
    cut = head.rfind(" ")
    if cut > PREVIEW_HEAD_CHARS // 2:
        head = text[:cut]

    tail_from = max(PREVIEW_HEAD_CHARS, total - PREVIEW_TAIL_CHARS)
    start = text.find(" ", tail_from)
    if start == -1 or start > total - PREVIEW_TAIL_CHARS // 2:
        start = tail_from
    else:
        # Begin on the next word rather than on the space that ended the last one, so the tail does not
        # open with whitespace and the first thing shown is content.
        start += 1
    tail = text[start:]

    shown = len(head) + len(tail)
    omitted = max(0, total - shown)
    # One bracketed line, no brackets inside it: `static/utils.js` and the tests both find the marker by
    # its prefix, and the shape has to stay as easy to spot as the `(not inlined` prefix beside it.
    marker = (
        f"\n\n{PREVIEW_MARKER} the first {len(head):,} and the last {len(tail):,} characters of "
        f"{total:,}. {omitted:,} characters are not shown. The file at this tag's path attribute has all "
        "of them: read it with offset and limit when the middle matters.]\n\n"
    )
    return head + marker + tail


def inline_preview(text: str | None) -> tuple[str | None, str]:
    """The body to send, and the state to report. One call, so the upload route and the prompt path ship
    the same bytes rather than two readings of the same document.

    `preview` is a *shape* decision, not an enforcement one: the result is still a body, it still obeys
    the cap, and the marker travels inside it. That is what keeps the second enforcement point honest
    without a second implementation - the browser sends back the string it was given, `file_tag` runs
    `inline_verdict` on it, and the model reads the same text either side of the wire.
    """
    verdict = inline_verdict(text)
    if verdict != "inlined":
        # No body, and the state that explains it. Returning `None` here rather than the original text is
        # what makes "there is a body" the same question as "`body is not None`" at both call sites.
        return None, verdict
    assert text is not None
    if len(text) <= PREVIEW_MIN_CHARS:
        return text, "inlined"
    return preview_body(text), "preview"


def not_inlined_sentence(verdict: str) -> str:
    """The line that replaces a body, for a document the model was not given.

    Built per call rather than at import: `INLINE_MAX_CHARS` is a knob, and a sentence frozen at import
    would quote a limit that is not in force. Every one of them begins with `(not inlined`, which is how
    `static/utils.js` tells an apology from a document (`parseAttachments`), and the chip in the
    transcript follows that. A new sentence here keeps the prefix or a file that was left out starts
    rendering as one that arrived.
    """
    limit = f"{INLINE_MAX_CHARS:,}"
    if verdict == "over":
        return (
            f"(not inlined: over the limit of {limit} characters. The file is at the path; read it from "
            "there in chunks.)"
        )
    if verdict == "empty":
        return (
            "(not inlined: nothing was extracted from this file, so it has no text layer. The bytes are at "
            "the path. Reading it as text will never yield anything: it is probably a scan or a photo of a "
            "page, so convert the page to an image and read the image instead.)"
        )
    return (
        f"(not inlined: this browser sent no body, and the document may be over the limit of {limit} "
        "characters. The file is at the path; read it from there.)"
    )


def file_tag(*, name: str, path: Path, bytes_: int, text: str | None, mime_type: str = "") -> str:
    """One `<file>` record: the path always, and the body only when `inline_verdict` allows it.

    The tag keeps the shape the browser already strips for display (`static/utils.js`), so an
    attachment renders as a chip and a name rather than as 30 KB of PDF transcription in a bubble.

    The verdict is computed here rather than passed in because this is the only place that puts a
    document in front of the model, and the sentence has to be the one true about the body it was given.
    An empty body is not a small document: it is a different failure, and it needs the actionable
    sentence, because "this file has no text layer" is the cue to rasterize the page.
    """
    mime = f' mimeType="{_attr(mime_type)}"' if mime_type else ""
    head = f'<file name="{_attr(name)}" path="{_attr(path)}" bytes="{bytes_}"{mime}'
    verdict = inline_verdict(text)
    if verdict == "inlined":
        return f"{head}>{text}</file>"
    return f"{head}>{not_inlined_sentence(verdict)}</file>"


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
