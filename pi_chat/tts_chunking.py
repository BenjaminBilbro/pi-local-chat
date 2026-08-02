"""Streaming Markdown-aware speech chunker for pi-chat voice mode.

Splits assistant text_delta events into natural speech chunks suitable for
TTS synthesis. Operates in two stages:

1. A streaming scanner identifies speakable text and safe break positions
   without splitting inside Markdown constructs.
2. A chunk selector emits bounded pieces at natural boundaries.

Designed to be stateful across text_delta calls and deterministic.
Never imports torch/omnivoice.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
import logging
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol normalization table (from plan)
# ---------------------------------------------------------------------------

_SYMBOL_REPLACEMENTS = {
    "->": " to ",
    "\u2192": " to ",                  # →
    "<=": " less than or equal to ",
    "\u2264": " less than or equal to ",  # ≤
    ">=": " greater than or equal to ",
    "\u2265": " greater than or equal to ",  # ≥
    "==": " equals ",
    "!=": " does not equal ",
    "\u2260": " does not equal ",       # ≠
    "&&": " and ",
    "||": " or ",
}

# Build regex pattern - sort by length descending to match longer patterns first
_SYMBOL_PATTERN = re.compile(
    "|".join(re.escape(k) for k in sorted(_SYMBOL_REPLACEMENTS.keys(), key=len, reverse=True))
)

# Strong sentence boundaries
_STRONG_BOUNDARY = re.compile(
    r"[.!?…\u3002\uFF01\uFF1F](?:\s+|$|[\"\')\]\u3001\u3002]*)"
)

# Clause boundaries
_CLAUSE_BOUNDARY = re.compile(
    r"[,;:\u2014\u2013\u3001](?:\s+|$)"
)

# Safe whitespace break
_SAFE_WHITESPACE = re.compile(r"\s+")

# URL pattern for stripping
_URL_PATTERN = re.compile(
    r"(?:https?://|www\.)[^\s<>\]\)]+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# OmniVoice expressive tags (must be preserved literally, including brackets)
# ---------------------------------------------------------------------------

_OMNIVOICE_TAGS = frozenset({
    "laughter",
    "sigh",
    "confirmation-en",
    "question-en",
    "question-ah",
    "question-oh",
    "question-ei",
    "question-yi",
    "surprise-ah",
    "surprise-oh",
    "surprise-wa",
    "surprise-yo",
    "dissatisfaction-hnn",
})

# Pattern: [tagname] where tagname is a known OmniVoice tag
_OMNIVOICE_TAG_PATTERN = re.compile(
    r"\[(" + "|".join(re.escape(t) for t in _OMNIVOICE_TAGS) + r")\]"
)


# ---------------------------------------------------------------------------
# Streaming scanner state
# ---------------------------------------------------------------------------

@dataclass
class _ScannerState:
    """Internal state for streaming Markdown/code scanning."""
    in_fence: bool = False
    fence_char: str = ""
    fence_marker: str = ""
    pending_fence_chars: str = ""
    in_inline_code: bool = False
    inline_code_bt: int = 0  # Backtick count for inline code delimiter
    pending_bt: int = 0      # Pending backticks before deciding inline vs fence
    pending_bt_at_line_start: bool = False  # Were pending bt at line start?
    pending_link_label: str = ""
    link_depth: int = 0
    pending_image_alt: str = ""
    in_image_alt: bool = False
    image_depth: int = 0
    # OmniVoice tag collection across deltas
    in_omni_tag: bool = False
    pending_omni_tag: str = ""
    speakable_buffer: str = ""

    def reset(self) -> None:
        self.in_fence = False
        self.fence_char = ""
        self.fence_marker = ""
        self.pending_fence_chars = ""
        self.in_inline_code = False
        self.inline_code_bt = 0
        self.pending_bt = 0
        self.pending_bt_at_line_start = False
        self.pending_link_label = ""
        self.link_depth = 0
        self.pending_image_alt = ""
        self.in_image_alt = False
        self.image_depth = 0
        self.in_omni_tag = False
        self.pending_omni_tag = ""
        self.speakable_buffer = ""


# ---------------------------------------------------------------------------
# StreamingSpeechChunker
# ---------------------------------------------------------------------------

class StreamingSpeechChunker:
    """Stateful streaming parser that splits text into speech chunks."""

    FIRST_CHUNK_PREFERRED_MIN = 24
    FIRST_CHUNK_PREFERRED_MAX = 120
    LATER_CHUNK_PREFERRED_MIN = 80
    LATER_CHUNK_PREFERRED_MAX = 160
    HARD_MAX = 220
    MAX_HOLD_CHARS = 72
    MIN_FLUSH_CHARS = 24
    MIN_EMIT_CHARS = 10

    def __init__(self, max_hold_ms: float = 450.0):
        self._scanner = _ScannerState()
        self._max_hold_ms = max_hold_ms
        self._last_emit_time: float | None = None
        self._first_chunk_emitted: bool = False
        self._pending_short_fragment: str = ""

    def feed(self, delta: str, now: float | None = None) -> list[str]:
        """Feed a text delta and return emitted speech chunks."""
        now = now or time.monotonic()
        if not delta:
            return self._try_emit_max_hold(now)

        speakable = self._scan_delta(delta)
        self._scanner.speakable_buffer += speakable
        return self._try_emit_chunks(now)

    def flush_text_block(self, now: float | None = None) -> list[str]:
        """Flush on text_end. Retain short fragments for merge."""
        now = now or time.monotonic()
        chunks = list(self._try_emit_chunks(now))

        remaining = self._scanner.speakable_buffer.strip()
        self._scanner.speakable_buffer = ""

        if remaining and len(remaining) >= self.MIN_FLUSH_CHARS:
            chunks.append(self._normalize_symbols(remaining))
        elif remaining:
            self._pending_short_fragment += remaining

        return chunks

    def finish(self) -> list[str]:
        """Final flush on agent_settled. Emit everything."""
        chunks = []

        if self._pending_short_fragment:
            frag = self._pending_short_fragment.strip()
            self._pending_short_fragment = ""
            if frag:
                chunks.append(self._normalize_symbols(frag))

        remaining = self._scanner.speakable_buffer.strip()
        self._scanner.speakable_buffer = ""

        if remaining:
            normalized = self._normalize_symbols(remaining)
            if normalized.strip():
                chunks.append(normalized)

        self._scanner.reset()

        for chunk in chunks:
            logger.info(f"Final chunk: {chunk}")

        return chunks

    def reset(self) -> None:
        """Clear all state for a new response."""
        self._scanner.reset()
        self._last_emit_time = None
        self._first_chunk_emitted = False
        self._pending_short_fragment = ""

    @property
    def pending_characters(self) -> int:
        return len(self._scanner.speakable_buffer)

    # -----------------------------------------------------------------------
    # Internal: scanning
    # -----------------------------------------------------------------------

    def _scan_delta(self, text: str) -> str:
        """Scan text delta and extract speakable portions."""
        result: list[str] = []
        i, n = 0, len(text)
        s = self._scanner

        while i < n:
            ch = text[i]

            # Pending fence chars (split across deltas)
            if s.pending_fence_chars and ch == s.pending_fence_chars[0]:
                s.pending_fence_chars += ch
                i += 1
                if len(s.pending_fence_chars) >= 3:
                    s.fence_char = s.pending_fence_chars[0]
                    s.fence_marker = s.pending_fence_chars
                    s.pending_fence_chars = ""
                    s.in_fence = True
                    # Skip language specifier to newline
                    while i < n and text[i] != "\n":
                        i += 1
                    if i < n:
                        i += 1
                continue
            elif s.pending_fence_chars:
                # Not continuing fence, emit as text
                result.append(s.pending_fence_chars)
                s.pending_fence_chars = ""

            # Inside fenced code block
            if s.in_fence:
                if text[i:i + len(s.fence_marker)] == s.fence_marker:
                    marker_len = len(s.fence_marker)
                    s.in_fence = False
                    s.fence_marker = ""
                    i += marker_len
                    if i < n and text[i] == "\n":
                        i += 1
                else:
                    i += 1
                continue

            # Fence open at line start
            if ch in ("`", "~") and (i == 0 or text[i - 1] == "\n"):
                run = 0
                while i + run < n and text[i + run] == ch:
                    run += 1
                if run >= 3:
                    s.fence_char = ch
                    s.fence_marker = text[i:i + run]
                    s.in_fence = True
                    i += run
                    while i < n and text[i] != "\n":
                        i += 1
                    if i < n:
                        i += 1
                    continue

            # Pending backticks for inline code
            if s.pending_bt > 0:
                if ch == "`":
                    s.pending_bt += 1
                    i += 1
                    continue
                # Not more backticks - decide: fence or inline code?
                if s.pending_bt >= 3 and s.pending_bt_at_line_start:
                    # Treat as fence
                    s.fence_char = "`"
                    s.fence_marker = "`" * s.pending_bt
                    s.in_fence = True
                    s.pending_bt = 0
                    s.pending_bt_at_line_start = False
                    # Skip to newline
                    while i < n and text[i] != "\n":
                        i += 1
                    if i < n:
                        i += 1
                    continue
                # Inline code
                s.in_inline_code = True
                s.inline_code_bt = s.pending_bt
                s.pending_bt = 0
                s.pending_bt_at_line_start = False

            # Inside inline code
            if s.in_inline_code:
                if ch == "`":
                    bt = 0
                    while i + bt < n and text[i + bt] == "`":
                        bt += 1
                    if bt >= s.inline_code_bt:
                        s.in_inline_code = False
                        s.inline_code_bt = 0
                        i += bt
                        continue
                    result.append("`" * bt)
                    i += bt
                    continue
                result.append(ch)
                i += 1
                continue

            # Image: ![alt](url)
            if ch == "!" and i + 1 < n and text[i + 1] == "[":
                s.in_image_alt = True
                s.pending_image_alt = ""
                s.image_depth = 1
                i += 2
                continue

            if s.in_image_alt:
                if ch == "[":
                    s.image_depth += 1
                    s.pending_image_alt += ch
                elif ch == "]":
                    s.image_depth -= 1
                    if s.image_depth == 0:
                        s.in_image_alt = False
                        alt = s.pending_image_alt
                        s.pending_image_alt = ""
                        if i + 1 < n and text[i + 1] == "(":
                            pd = 1
                            i += 2
                            while i < n and pd > 0:
                                if text[i] == "(":
                                    pd += 1
                                elif text[i] == ")":
                                    pd -= 1
                                i += 1
                            if alt.strip():
                                result.append(f"image: {alt}. ")
                        else:
                            result.append(alt)
                    else:
                        s.pending_image_alt += ch
                else:
                    s.pending_image_alt += ch
                i += 1
                continue

            # [ — could be a link [label](url) or an OmniVoice tag [tagname]
            if ch == "[" and s.link_depth == 0:
                # Look ahead in current delta for ]( to detect links
                j = i + 1
                depth = 1
                while j < n and depth > 0:
                    if text[j] == "[":
                        depth += 1
                    elif text[j] == "]":
                        depth -= 1
                    j += 1
                is_link = (j < n and text[j] == "(")

                if is_link:
                    # Link detected in current delta — use link handling
                    s.pending_link_label = ""
                    s.link_depth = 1
                    i += 1
                    continue
                else:
                    # Not confirmed as link — collect chars, may become link later
                    s.in_omni_tag = True
                    s.pending_omni_tag = ""
                    i += 1
                    continue

            # Collecting chars after [ (could become link or OmniVoice tag)
            if s.in_omni_tag:
                if ch == "]":
                    tag_name = s.pending_omni_tag
                    # Check if next char is '(' — if so, it's a link
                    if i + 1 < n and text[i + 1] == "(":
                        # Convert to link: skip ]( and URL
                        s.in_omni_tag = False
                        s.pending_omni_tag = ""
                        label = tag_name
                        i += 2
                        pd = 1
                        while i < n and pd > 0:
                            if text[i] == "(":
                                pd += 1
                            elif text[i] == ")":
                                pd -= 1
                            i += 1
                        if label.strip():
                            result.append(label)
                        continue
                    # Not a link — check if OmniVoice tag
                    s.in_omni_tag = False
                    s.pending_omni_tag = ""
                    if tag_name in _OMNIVOICE_TAGS:
                        result.append(f"[{tag_name}]")
                    else:
                        # Plain brackets
                        result.append("[")
                        result.append(tag_name)
                        result.append("]")
                    i += 1
                    continue
                elif ch == "(":
                    # [label( — treat as link with URL starting here
                    s.in_omni_tag = False
                    label = s.pending_omni_tag
                    s.pending_omni_tag = ""
                    i += 1
                    pd = 1
                    while i < n and pd > 0:
                        if text[i] == "(":
                            pd += 1
                        elif text[i] == ")":
                            pd -= 1
                        i += 1
                    if label.strip():
                        result.append(label)
                    continue
                elif ch == "[":
                    # Nested [ — not a simple tag, emit and start link handling
                    s.in_omni_tag = False
                    result.append("[")
                    result.append(s.pending_omni_tag)
                    s.pending_omni_tag = ""
                    s.pending_link_label = ""
                    s.link_depth = 1
                    i += 1
                    continue
                else:
                    s.pending_omni_tag += ch
                    i += 1
                    continue

            # Link: [label](url)
            if ch == "[" and s.link_depth == 0:
                s.pending_link_label = ""
                s.link_depth = 1
                i += 1
                continue

            if s.link_depth > 0:
                if ch == "[":
                    s.link_depth += 1
                    s.pending_link_label += ch
                elif ch == "]":
                    s.link_depth -= 1
                    if s.link_depth == 0:
                        label = s.pending_link_label
                        s.pending_link_label = ""
                        s.link_depth = 0
                        if i + 1 < n and text[i + 1] == "(":
                            pd = 1
                            i += 2
                            while i < n and pd > 0:
                                if text[i] == "(":
                                    pd += 1
                                elif text[i] == ")":
                                    pd -= 1
                                i += 1
                            if label.strip():
                                result.append(label)
                        else:
                            result.append(label)
                    else:
                        s.pending_link_label += ch
                else:
                    s.pending_link_label += ch
                i += 1
                continue

            # Inline code open
            if ch == "`":
                bt = 0
                while i + bt < n and text[i + bt] == "`":
                    bt += 1
                if bt == 1:
                    s.in_inline_code = True
                    s.inline_code_bt = 1
                    i += 1
                    continue
                s.pending_bt = bt
                s.pending_bt_at_line_start = (i == 0 or text[i - 1] == "\n")
                i += bt
                continue

            # HTML tags
            if ch == "<" and i + 1 < n and (text[i + 1].isalpha() or text[i + 1] == "/"):
                while i < n and text[i] != ">":
                    i += 1
                if i < n:
                    i += 1
                continue

            # Emphasis
            if ch in ("*", "_"):
                i += 1
                continue

            # Headings
            if ch == "#" and (i == 0 or text[i - 1] == "\n"):
                while i < n and text[i] == "#":
                    i += 1
                continue

            # List markers
            if ch in "-+*" and (i == 0 or text[i - 1] == "\n"):
                i += 1
                if i < n and text[i] == " ":
                    i += 1
                continue

            # Blockquote
            if ch == ">" and (i == 0 or text[i - 1] == "\n"):
                i += 1
                if i < n and text[i] == " ":
                    i += 1
                continue

            # Table pipes (but not ||)
            if ch == "|" and i + 1 < n and text[i + 1] == "|":
                result.append("||")
                i += 2
                continue
            if ch == "|":
                result.append(" ")
                i += 1
                continue

            # Newlines
            if ch == "\n":
                result.append(" ")
                i += 1
                continue

            # Normal char
            result.append(ch)
            i += 1

        speakable = "".join(result)
        speakable = _URL_PATTERN.sub("", speakable)
        return speakable

    # -----------------------------------------------------------------------
    # Internal: chunking
    # -----------------------------------------------------------------------

    def _try_emit_max_hold(self, now: float) -> list[str]:
        buf = self._scanner.speakable_buffer.strip()
        if len(buf) < self.MAX_HOLD_CHARS:
            return []
        if self._last_emit_time is None:
            return []
        if (now - self._last_emit_time) * 1000 < self._max_hold_ms:
            return []

        idx = self._find_clause_or_ws(buf, self.HARD_MAX)
        if idx > self.MIN_EMIT_CHARS:
            chunk = buf[:idx]
            self._scanner.speakable_buffer = buf[idx:]
            self._last_emit_time = now
            self._first_chunk_emitted = True
            return [self._normalize_symbols(chunk)]
        return []

    def _try_emit_chunks(self, now: float) -> list[str]:
        chunks: list[str] = []
        buf = self._scanner.speakable_buffer.strip()
        emitted = 0

        while len(buf) - emitted > 0:
            rem = buf[emitted:].strip()
            if not rem:
                break

            # Max-hold rule
            if (self._last_emit_time is not None
                    and len(rem) >= self.MAX_HOLD_CHARS
                    and (now - self._last_emit_time) * 1000 >= self._max_hold_ms):
                idx = self._find_clause_or_ws(rem, self.HARD_MAX)
                if idx > self.MIN_EMIT_CHARS:
                    chunks.append(self._normalize_symbols(rem[:idx]))
                    emitted += idx
                    self._last_emit_time = now
                    self._first_chunk_emitted = True
                    continue

            # Find boundary
            if not self._first_chunk_emitted:
                idx = self._find_boundary_first(rem)
            else:
                idx = self._find_boundary_later(rem)

            if idx > 0:
                chunks.append(self._normalize_symbols(rem[:idx]))
                emitted += idx
                self._last_emit_time = now
                self._first_chunk_emitted = True
                continue

            # Hard max
            if len(rem) >= self.HARD_MAX:
                idx = self._find_ws(rem, self.HARD_MAX)
                if idx > self.MIN_EMIT_CHARS:
                    chunks.append(self._normalize_symbols(rem[:idx]))
                    emitted += idx
                    self._last_emit_time = now
                    self._first_chunk_emitted = True
                    continue
                chunks.append(self._normalize_symbols(rem[:self.HARD_MAX]))
                emitted += self.HARD_MAX
                self._last_emit_time = now
                self._first_chunk_emitted = True
                continue

            break

        if emitted > 0:
            self._scanner.speakable_buffer = buf[emitted:]
        return chunks

    def _find_boundary_first(self, text: str) -> int:
        bounds = [m for m in _STRONG_BOUNDARY.finditer(text[:self.HARD_MAX])
                  if self._safe_boundary(text, m)]
        if not bounds:
            return 0
        for m in bounds:
            e = m.end()
            if self.FIRST_CHUNK_PREFERRED_MIN <= e <= self.FIRST_CHUNK_PREFERRED_MAX:
                return e
        if bounds[0].end() >= self.MIN_EMIT_CHARS:
            return bounds[0].end()
        return 0

    def _find_boundary_later(self, text: str) -> int:
        bounds = [m for m in _STRONG_BOUNDARY.finditer(text[:self.HARD_MAX])
                  if self._safe_boundary(text, m)]
        if not bounds:
            return 0
        for m in bounds:
            e = m.end()
            if self.LATER_CHUNK_PREFERRED_MIN <= e <= self.LATER_CHUNK_PREFERRED_MAX:
                return e
        if len(text) < self.LATER_CHUNK_PREFERRED_MIN:
            if bounds[0].end() >= self.MIN_EMIT_CHARS:
                return bounds[0].end()
        if bounds[0].end() >= self.MIN_EMIT_CHARS:
            return bounds[0].end()
        return 0

    def _safe_boundary(self, text: str, match) -> bool:
        pos = match.start()
        ch = match.group(0)[0]
        if ch != ".":
            return True

        before = text[:pos].rstrip()
        after = text[pos + 1:].lstrip()

        # Decimal: digit.digit
        if before and before[-1].isdigit() and after and after[0].isdigit():
            return False

        # Abbreviation: Dr., Mr., etc.
        if before:
            sp = before.rfind(" ")
            word = before[sp + 1:].rstrip() if sp >= 0 else before.rstrip()
            if 2 <= len(word) <= 4 and word[0].isupper():
                rest = word[1:]
                if rest.islower() and rest.isalpha():
                    return False

        # Initial: A.
        if before:
            sp = before.rfind(" ")
            word = before[sp + 1:].rstrip() if sp >= 0 else before.rstrip()
            if len(word) == 1 and word.isupper():
                return False

        # Email
        if before and "@" in before[-30:]:
            return False

        return True

    def _find_clause_or_ws(self, text: str, max_len: int) -> int:
        for m in _CLAUSE_BOUNDARY.finditer(text[:max_len]):
            if m.end() > self.MIN_EMIT_CHARS:
                return m.end()
        return self._find_ws(text, max_len)

    def _find_ws(self, text: str, max_len: int) -> int:
        last = 0
        for m in _SAFE_WHITESPACE.finditer(text[:max_len]):
            last = m.end()
        return last if last > self.MIN_EMIT_CHARS else 0

    def _normalize_symbols(self, text: str) -> str:
        result = _SYMBOL_PATTERN.sub(
            lambda m: " " + _SYMBOL_REPLACEMENTS.get(m.group(0), m.group(0)) + " ",
            text,
        )
        return re.sub(r"\s+", " ", result).strip()
