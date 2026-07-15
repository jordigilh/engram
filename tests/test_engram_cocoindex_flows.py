"""Smoke tests for engram-cocoindex-flows.py -- the CocoIndex ingestion flows
added during the 2026-07-15 Engram-onboarding-into-Hindsight+CocoIndex work
(docs/*.md -> engram-docs Hindsight bank, *.py -> engram_code_embeddings
pgvector table).

These are deliberately lighter than test_cocoindex_flows.py's full
contradiction-branching coverage -- this module has no contradiction
resolution (it retains docs/code directly, not correction transcripts) --
but they pin the two things most likely to silently break:
  1. The module actually imports/executes cleanly alongside cocoindex-flows.py
     in one process (the ContextKey collision regression below).
  2. process_doc_file()'s path -> document_id/tags/section derivation, since
     a bug there would misfile every future doc update into the wrong tag.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


class FakeDocFile:
    """Minimal stand-in for CocoIndex's localfs.File as used by
    process_doc_file(), which calls file.read_text() and file.file_path
    directly (no nested file_path.path wrapper, unlike cocoindex-flows.py's
    transcript File usage)."""

    def __init__(self, content: str, path: str):
        self._content = content
        self.file_path = Path(path)

    async def read_text(self) -> str:
        return self._content


def _run(coro):
    return asyncio.run(coro)


class TestModuleLoadsWithoutContextKeyCollision:
    """Regression guard: engram-cocoindex-flows.py originally reused
    ContextKey("pg_pool"), the exact same name cocoindex-flows.py registers
    for its own Postgres pool. CocoIndex registers ContextKeys
    process-globally and raises ValueError on a same-name second
    registration, so loading both modules in one process (as pytest does
    across the whole tests/ suite) crashed at collection time. Renamed to
    "engram_repo_pg_pool" to fix it -- this test fails loudly if that ever
    regresses back to a colliding name."""

    def test_fixture_loads_alongside_cocoindex_flows_fixture(self, cocoindex_flows, engram_cocoindex_flows):
        assert engram_cocoindex_flows is not None
        assert cocoindex_flows is not None

    def test_apps_are_defined(self, engram_cocoindex_flows):
        assert engram_cocoindex_flows.docs_app is not None
        assert engram_cocoindex_flows.code_app is not None


class TestSplitText:
    def test_short_text_is_not_split(self, engram_cocoindex_flows):
        text = "short content"
        assert engram_cocoindex_flows._split_text(text, chunk_size=800, chunk_overlap=200) == [text]

    def test_long_text_is_split_into_multiple_chunks(self, engram_cocoindex_flows):
        text = "line\n" * 500  # well over the 800-char chunk_size
        chunks = engram_cocoindex_flows._split_text(text, chunk_size=800, chunk_overlap=200)
        assert len(chunks) > 1
        assert all(len(c) > 0 for c in chunks)


class TestProcessDocFile:
    def test_root_level_doc_gets_root_section_tag(self, engram_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(engram_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(engram_cocoindex_flows.process_doc_file(
            FakeDocFile("# Hello\n\nSome content", "/fake/repo/docs/foo.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="engram",
        ))

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["bank_id"] == "engram-docs"
        assert call["document_id"] == "engram--foo"
        assert call["tags"] == ["root", "engram"]
        assert call["metadata"] == {"source": "cocoindex", "repo": "engram"}

    def test_nested_doc_gets_first_path_segment_as_section_tag(self, engram_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(engram_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(engram_cocoindex_flows.process_doc_file(
            FakeDocFile("design notes", "/fake/repo/docs/architecture/design.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="engram",
        ))

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["document_id"] == "engram--architecture--design"
        assert call["tags"] == ["architecture", "engram"]

    def test_empty_content_returns_without_retaining(self, engram_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(engram_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(engram_cocoindex_flows.process_doc_file(
            FakeDocFile("", "/fake/repo/docs/empty.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="engram",
        ))

        assert retain_calls == []

    def test_long_doc_produces_chunk_suffixed_document_ids(self, engram_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(engram_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))
        long_content = "line\n" * 500

        _run(engram_cocoindex_flows.process_doc_file(
            FakeDocFile(long_content, "/fake/repo/docs/long.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="engram",
        ))

        assert len(retain_calls) > 1
        assert retain_calls[0]["document_id"] == "engram--long"
        assert retain_calls[1]["document_id"] == "engram--long--chunk1"


class TestHindsightRetain:
    """Same retry-then-give-up contract as cocoindex-flows.py's own
    hindsight_retain() -- see test_cocoindex_flows.py::TestHindsightRetain."""

    def test_success_returns_parsed_json(self, engram_cocoindex_flows, monkeypatch):
        import json

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"success": True}).encode()

        monkeypatch.setattr(engram_cocoindex_flows, "urlopen", lambda req, timeout=60: FakeResponse())
        result = engram_cocoindex_flows.hindsight_retain(bank_id="engram-docs", content="x", document_id="doc-1")
        assert result == {"success": True}

    def test_retries_then_gives_up_returning_empty_dict(self, engram_cocoindex_flows, monkeypatch):
        from urllib.error import URLError

        def always_fails(req, timeout=60):
            raise URLError("connection refused")

        monkeypatch.setattr(engram_cocoindex_flows, "urlopen", always_fails)
        monkeypatch.setattr(engram_cocoindex_flows.time, "sleep", lambda *_: None)

        result = engram_cocoindex_flows.hindsight_retain(bank_id="engram-docs", content="x", document_id="doc-1")
        assert result == {}
