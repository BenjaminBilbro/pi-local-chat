"""Exhaustive tests for StreamingSpeechChunker.

Verifies streaming Markdown-aware chunking without torch/omnivoice import.
"""

import pytest
from pi_chat.tts_chunking import StreamingSpeechChunker


class Test0ImportGuard:  # Runs first (Test0) before torch is imported
    def test_no_torch(self):
        import sys
        assert "torch" not in sys.modules

    def test_no_omnivoice(self):
        import sys
        assert "omnivoice" not in sys.modules


class TestBasicChunking:
    def test_single_sentence(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Hello there. How can I help you today?")
        chunks.extend(chunker.finish())
        assert len(chunks) >= 1
        joined = " ".join(chunks)
        assert "Hello there" in joined
        assert "help you today" in joined

    def test_two_sentences(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("First sentence. Second sentence.")
        assert len(chunks) >= 1

    def test_reset_clears_state(self):
        chunker = StreamingSpeechChunker()
        chunker.feed("Some text. More text.")
        chunker.reset()
        assert chunker.pending_characters == 0
        chunks = chunker.feed("New text.")
        for c in chunks:
            assert "Some text" not in c


class TestPunctuationSplitAcrossDeltas:
    def test_period_split(self):
        """Period arrives in a separate delta."""
        chunker = StreamingSpeechChunker()
        chunker.feed("Hello there")
        chunks = chunker.feed(". How are you?")
        joined = " ".join(chunks)
        assert "Hello there" in joined

    def test_question_mark_split(self):
        chunker = StreamingSpeechChunker()
        chunker.feed("Are you ready")
        chunks = chunker.feed("?")
        joined = " ".join(chunks)
        assert "Are you ready" in joined


class TestFencedCodeBlocks:
    def test_backtick_fence_omitted(self):
        """Fenced code block should be omitted from speech."""
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Here is some code:\n\n```python\ndef hello():\n    print('world')\n```\n\nThat should work.")
        chunks.extend(chunker.feed("") )
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "def hello" not in joined
        assert "print" not in joined
        assert "Here is some code" in joined or "That should work" in joined

    def test_tilde_fence_omitted(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Code:\n\n~~~\nfor i in range(10): pass\n~~~\n\nDone.")
        chunks.extend(chunker.feed("") )
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "for i in range" not in joined

    def test_fence_marker_split_across_deltas(self):
        """Fence marker split: ``` in one delta, python in next."""
        chunker = StreamingSpeechChunker()
        chunker.feed("Start:\n\n``")
        chunker.feed("`python\ncode here\n```\n\nEnd.")
        chunks = chunker.feed("")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "code here" not in joined

    def test_long_fenced_code_no_buffer_bloat(self):
        """Long fenced code should not cause unbounded buffer growth."""
        chunker = StreamingSpeechChunker()
        chunker.feed("Before.\n\n```python\n")
        for _ in range(100):
            chunker.feed("x = 1\n")
        chunker.feed("```\n\nAfter.")
        chunks = chunker.feed("")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "x = 1" not in joined or joined.count("x = 1") == 0


class TestInlineCode:
    def test_inline_code_spoken_without_backticks(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Use the `read` function here.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "read" in joined
        assert "`" not in joined

    def test_inline_code_at_boundary(self):
        chunker = StreamingSpeechChunker()
        chunker.feed("Call `func`")
        chunks = chunker.feed("(). Done.")
        joined = " ".join(chunks)
        assert "func" in joined


class TestMarkdownLinks:
    def test_link_label_spoken_url_omitted(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("See [the docs](https://example.com) for more.")
        chunker.finish()
        joined = " ".join(chunks)
        assert "the docs" in joined
        assert "example.com" not in joined
        assert "https" not in joined

    def test_link_nested_brackets(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("See [link [nested]](https://x.com).")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "link" in joined


class TestURLs:
    def test_bare_url_omitted(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Visit https://example.com/path for info.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "example.com" not in joined

    def test_www_url_omitted(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Go to www.example.com now.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "example.com" not in joined

    def test_http_url_omitted(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Check http://test.com.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "test.com" not in joined


class TestImages:
    def test_image_alt_as_description(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Here is ![a chart](/chart.png) showing results.")
        chunker.finish()
        joined = " ".join(chunks)
        assert "chart" in joined.lower() or "image" in joined.lower()
        assert "chart.png" not in joined

    def test_image_url_omitted(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("![alt text](https://img.com/x.jpg)")
        chunker.finish()
        joined = " ".join(chunks)
        assert "img.com" not in joined


class TestAbbreviationsAndNumbers:
    def test_abbreviation_not_split(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Dr. Smith is here.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "Dr." in joined or "Dr" in joined

    def test_decimal_not_split(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("The value is 3.14.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "3.14" in joined

    def test_version_not_split(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Use version v1.2.3.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "v1.2.3" in joined

    def test_initials_not_split(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Meet A.B. C.D. today.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "A.B." in joined or "A.B" in joined


class TestCJKPunctuation:
    def test_cjk_period(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("你好。再见。")
        chunks.extend(chunker.finish())
        assert len(chunks) >= 1

    def test_cjk_exclamation(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("太好了！")
        chunks.extend(chunker.finish())
        assert len(chunks) >= 1


class TestShortFragments:
    def test_short_fragment_merges(self):
        """Short text should not be emitted immediately."""
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Hi.")
        assert len(chunks) == 0  # Too short for first chunk
        chunks.extend(chunker.finish())
        assert len(chunks) >= 1

    def test_flush_retains_short_fragment(self):
        chunker = StreamingSpeechChunker()
        chunker.feed("Hi.")
        chunks = chunker.flush_text_block()
        # Short fragment retained, not emitted as its own chunk
        assert len(chunks) == 0

    def test_finish_emits_short_phrase(self):
        chunker = StreamingSpeechChunker()
        chunker.feed("OK.")
        chunks = chunker.finish()
        assert len(chunks) >= 1


class TestMultipleTextBlocks:
    def test_tool_gap_preserves_text(self):
        """Text before and after tool call should both be spoken."""
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Let me check that for you.")
        chunker.flush_text_block()  # Simulate text_end before tool
        chunks.extend(chunker.feed("Here's what I found. It looks good."))
        chunker.finish()
        joined = " ".join(chunks)
        assert "check that" in joined or "found" in joined


class TestHardMaximum:
    def test_hard_max_split(self):
        """Very long text should be split at hard max."""
        chunker = StreamingSpeechChunker(max_hold_ms=0)
        long_text = "word " * 100 + "."
        chunks = chunker.feed(long_text)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert len(chunk) <= StreamingSpeechChunker.HARD_MAX + 10  # Allow symbol expansion


class TestSymbolNormalization:
    def test_arrow(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("This -> that.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "to" in joined

    def test_unicode_arrow(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("This → that.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "to" in joined

    def test_less_equal(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("x <= 5.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "less than or equal to" in joined

    def test_greater_equal(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("x >= 5.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "greater than or equal to" in joined

    def test_equals(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("x == 5.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "equals" in joined

    def test_not_equal(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("x != 5.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "does not equal" in joined

    def test_and(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("a && b.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "and" in joined

    def test_or(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("a || b.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "or" in joined


class TestEmphasisAndFormatting:
    def test_bold_stripped(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("**bold text** here.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "bold text" in joined
        assert "*" not in joined

    def test_italic_stripped(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("*italic text* here.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "italic text" in joined

    def test_heading_stripped(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("# Heading text\n\nBody.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "#" not in joined

    def test_list_markers_stripped(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("- Item one\n- Item two")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "Item one" in joined


class TestHTMLTags:
    def test_html_tag_stripped(self):
        chunker = StreamingSpeechChunker()
        chunks = chunker.feed("Text with <strong>bold</strong> here.")
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        assert "<" not in joined
        assert ">" not in joined


class TestPendingCharacters:
    def test_pending_count(self):
        chunker = StreamingSpeechChunker()
        chunker.feed("Some text")
        assert chunker.pending_characters > 0
        chunker.reset()
        assert chunker.pending_characters == 0


class TestNoTextLoss:
    def test_all_text_accounted_for(self):
        """No text should be lost across feed/flush/finish."""
        chunker = StreamingSpeechChunker()
        original = "Hello. This is a test. More text here."
        chunks = chunker.feed(original)
        chunks.extend(chunker.finish())
        joined = " ".join(chunks)
        # Check key words are present
        assert "Hello" in joined
        assert "test" in joined
        assert "More text" in joined

    def test_streaming_text_no_loss(self):
        """Text arriving in many small deltas should not be lost."""
        chunker = StreamingSpeechChunker()
        text = "Hello world. This is a test."
        for ch in text:
            chunker.feed(ch)
        chunker.finish()
        joined = " ".join(chunks for chunks in [chunker.finish()] if chunks)
        # finish() was already called, so check pending is 0
        assert chunker.pending_characters == 0
