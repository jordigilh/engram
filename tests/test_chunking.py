"""Tests for chunking.py -- the shared, content-stable chunking helpers used
by all four *-cocoindex-flows.py scripts (cocoindex, engram, koku, dcm).

Primary regression target: the 2026-08-03 chunk-ID-cascade bug (see
docs/FINDINGS.md) where fixed-character-offset chunking + positional
document_ids meant any edit before the tail of a document (or any
state/label change on an issue) reindexed every downstream chunk, making
hindsight-api's delta-diff see "changed" content that was actually
byte-identical, just repositioned.
"""
from __future__ import annotations

import chunking


class TestSplitFixedWindow:
    def test_short_text_is_not_split(self):
        text = "short content"
        assert chunking.split_fixed_window(text, chunk_size=800, chunk_overlap=200) == [text]

    def test_long_text_is_split_into_multiple_chunks(self):
        text = "line\n" * 500
        chunks = chunking.split_fixed_window(text, chunk_size=800, chunk_overlap=200)
        assert len(chunks) > 1
        assert all(len(c) > 0 for c in chunks)


class TestSplitMarkdownSectionsNoHeadings:
    """Content with no markdown headings falls back to the original
    fixed-window behavior and naming exactly -- no regression for plain-text
    docs or docs whose whole body is one paragraph."""

    def test_short_no_heading_content_is_single_bare_key_chunk(self):
        result = chunking.split_markdown_sections("just some plain text", chunk_size=800, chunk_overlap=200)
        assert result == [("", "just some plain text")]

    def test_long_no_heading_content_uses_legacy_chunk_numbering(self):
        content = "line\n" * 500
        result = chunking.split_markdown_sections(content, chunk_size=800, chunk_overlap=200)
        keys = [key for key, _ in result]
        assert len(keys) > 1
        assert keys[0] == ""
        assert keys[1] == "chunk1"


class TestSplitMarkdownSectionsWithHeadings:
    def test_single_leading_heading_is_bare_key(self):
        content = "# Hello\n\nSome content"
        result = chunking.split_markdown_sections(content, chunk_size=800, chunk_overlap=200)
        assert result == [("", content)]

    def test_second_heading_gets_stable_hash_key_not_positional(self):
        content = "# Title\n\nIntro\n\n## Section A\n\nBody A\n\n## Section B\n\nBody B\n"
        result = chunking.split_markdown_sections(content, chunk_size=800, chunk_overlap=200)
        keys = [key for key, _ in result]
        assert keys[0] == ""
        assert keys[1] != "chunk1"
        assert keys[2] != "chunk2"
        assert len(keys[1]) == 16  # sha1[:16] digest, not a small integer index
        assert keys[1] != keys[2]

    def test_regression_prepending_a_new_section_does_not_change_existing_section_keys(self):
        """The exact FINDINGS.md failure mode: a new dated entry gets
        prepended above existing ones. Existing sections' keys (and thus
        their document_ids and stored content-hashes) must be identical
        before and after."""
        before = "# Findings\n\n## 2026-08-01: First entry\n\nFirst body text.\n"
        after = "# Findings\n\n## 2026-08-03: New entry\n\nNew body text.\n\n## 2026-08-01: First entry\n\nFirst body text.\n"

        before_result = chunking.split_markdown_sections(before, chunk_size=800, chunk_overlap=200)
        after_result = chunking.split_markdown_sections(after, chunk_size=800, chunk_overlap=200)

        before_by_key = dict(before_result)
        after_by_key = dict(after_result)
        first_entry_key = [k for k in before_by_key if k != ""][0]

        assert first_entry_key in after_by_key
        assert after_by_key[first_entry_key] == before_by_key[first_entry_key]

    def test_editing_one_section_does_not_change_other_sections_keys_or_text(self):
        content = "# Title\n\n## Section A\n\nOriginal A\n\n## Section B\n\nOriginal B\n"
        edited = "# Title\n\n## Section A\n\nEDITED A with more text\n\n## Section B\n\nOriginal B\n"

        result = dict(chunking.split_markdown_sections(content, chunk_size=800, chunk_overlap=200))
        edited_result = dict(chunking.split_markdown_sections(edited, chunk_size=800, chunk_overlap=200))

        section_b_key = _key_for_heading(content, "## Section B")
        assert result[section_b_key] == edited_result[section_b_key]

    def test_oversized_section_is_subsplit_with_part_suffix_scoped_to_that_section(self):
        big_body = "x" * 2000
        content = f"# Title\n\n## Big Section\n\n{big_body}\n"
        result = chunking.split_markdown_sections(content, chunk_size=800, chunk_overlap=200)
        keys = [key for key, _ in result]
        # title chunk ("") + at least 2 parts of the oversized section
        assert keys[0] == ""
        big_section_key = keys[1]
        assert any(k.startswith(f"{big_section_key}-part") for k in keys[2:])

    def test_prepending_new_section_bounds_cascade_to_new_section_only_even_when_oversized(self):
        """Regression: sub-splitting must chunk each section starting from
        that section's own offset 0, not the whole document's offset, so
        prepending new content never shifts an existing oversized section's
        internal part boundaries."""
        big_body = "y" * 2000
        before = f"# Title\n\n## Big Section\n\n{big_body}\n"
        after = f"# Title\n\n## New Section\n\nshort new text\n\n## Big Section\n\n{big_body}\n"

        before_result = dict(chunking.split_markdown_sections(before, chunk_size=800, chunk_overlap=200))
        after_result = dict(chunking.split_markdown_sections(after, chunk_size=800, chunk_overlap=200))

        big_section_key = _key_for_heading(before, "## Big Section")
        assert big_section_key in after_result
        # every "-part{n}" entry scoped to the big section is byte-identical
        for key, text in before_result.items():
            if key == big_section_key or key.startswith(f"{big_section_key}-part"):
                assert after_result[key] == text


def _key_for_heading(content: str, heading_line: str) -> str:
    """Test helper: compute the stable key chunking.py would assign to a
    given heading line, using the module's own hashing so this test doesn't
    hardcode a digest value that would break if the algorithm ever changes."""
    return chunking._stable_key(heading_line)


class TestSplitIssueSections:
    def test_short_header_with_no_comments_is_single_bare_key_chunk(self):
        result = chunking.split_issue_sections("short header text", [], chunk_size=1200, chunk_overlap=300)
        assert result == [("", "short header text")]

    def test_comments_get_ordinal_keys_not_character_offsets(self):
        result = chunking.split_issue_sections(
            "header", ["first comment", "second comment"], chunk_size=1200, chunk_overlap=300,
        )
        assert result == [
            ("", "header"),
            ("comment0", "first comment"),
            ("comment1", "second comment"),
        ]

    def test_regression_appending_a_new_comment_does_not_change_earlier_comment_keys_or_text(self):
        """The exact kubernaut-issues failure mode: a new comment arrives on
        an existing issue. Earlier comments' keys/content must be
        unaffected -- unlike single-fixed-offset chunking over the whole
        concatenated issue text."""
        before = chunking.split_issue_sections("header", ["first comment"], chunk_size=1200, chunk_overlap=300)
        after = chunking.split_issue_sections(
            "header", ["first comment", "second comment"], chunk_size=1200, chunk_overlap=300,
        )
        before_by_key = dict(before)
        after_by_key = dict(after)
        assert after_by_key["comment0"] == before_by_key["comment0"]
        assert after_by_key[""] == before_by_key[""]
        assert "comment1" in after_by_key
        assert "comment1" not in before_by_key

    def test_oversized_header_is_subsplit_with_positional_part_suffix(self):
        big_header = "z" * 3000
        result = chunking.split_issue_sections(big_header, [], chunk_size=1200, chunk_overlap=300)
        keys = [key for key, _ in result]
        assert keys[0] == ""
        assert "part1" in keys

    def test_oversized_comment_is_subsplit_scoped_to_that_comment(self):
        big_comment = "w" * 3000
        result = chunking.split_issue_sections("header", [big_comment], chunk_size=1200, chunk_overlap=300)
        keys = [key for key, _ in result]
        assert "comment0" in keys
        assert "comment0-part1" in keys
