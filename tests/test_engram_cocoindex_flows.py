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


class FakeCodeFile:
    """Minimal stand-in for CocoIndex's localfs.File as used by
    process_code_file(), which reads file.file_path.resolve() (not the
    nested file_path.path FakeDocFile above deliberately omits)."""

    def __init__(self, content: str, path: str):
        self._content = content
        self.file_path = Path(path)

    async def read_text(self) -> str:
        return self._content


class FakeTable:
    """Captures declare_row() calls in insertion order, standing in for
    CocoIndex's postgres table target."""

    def __init__(self):
        self.rows = []

    def declare_row(self, row):
        self.rows.append(row)


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

    def test_regression_prepending_a_findings_style_section_does_not_change_older_sections_document_id(
        self, engram_cocoindex_flows, monkeypatch,
    ):
        """The exact 2026-08-03 cascade bug: FINDINGS.md grows by prepending
        a new dated ## entry above existing ones. An older entry's
        document_id (and retained content) must be unaffected by the
        prepend. See docs/FINDINGS.md."""
        before_content = (
            "# Research Findings\n\n"
            "## 2026-08-01: First entry\n\nOriginal body text for the first entry.\n"
        )
        after_content = (
            "# Research Findings\n\n"
            "## 2026-08-03: New entry\n\nBrand new body text.\n\n"
            "## 2026-08-01: First entry\n\nOriginal body text for the first entry.\n"
        )

        before_calls = []
        monkeypatch.setattr(engram_cocoindex_flows, "hindsight_retain", lambda **kwargs: before_calls.append(kwargs))
        _run(engram_cocoindex_flows.process_doc_file(
            FakeDocFile(before_content, "/fake/repo/docs/FINDINGS.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="engram",
        ))

        after_calls = []
        monkeypatch.setattr(engram_cocoindex_flows, "hindsight_retain", lambda **kwargs: after_calls.append(kwargs))
        _run(engram_cocoindex_flows.process_doc_file(
            FakeDocFile(after_content, "/fake/repo/docs/FINDINGS.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="engram",
        ))

        before_by_id = {c["document_id"]: c["content"] for c in before_calls}
        after_by_id = {c["document_id"]: c["content"] for c in after_calls}
        first_entry_doc_id = [d for d in before_by_id if d != "engram--FINDINGS"][0]

        assert first_entry_doc_id in after_by_id
        assert after_by_id[first_entry_doc_id] == before_by_id[first_entry_doc_id]


class TestProcessCodeFile:
    """process_code_file() batches embedding through chunking.embed_code_chunks()
    instead of calling a bare per-chunk _embed_text() in a loop (see
    docs/FINDINGS.md 2026-08-07). embed_code_chunks() itself is mocked here
    so these tests stay fast/deterministic and don't depend on loading a
    real sentence-transformers model -- that integration is covered
    directly by tests/test_chunking.py::TestEmbedCodeChunks."""

    def test_chunks_are_embedded_and_declared_as_rows_in_order(self, engram_cocoindex_flows, monkeypatch):
        from engram import chunking as chunking_module

        async def fake_embed_code_chunks(chunks):
            return [[float(i)] * 3 for i in range(len(chunks))]

        monkeypatch.setattr(chunking_module, "embed_code_chunks", fake_embed_code_chunks)

        table = FakeTable()
        _run(engram_cocoindex_flows.process_code_file(
            FakeCodeFile("def add(a, b):\n    return a + b\n", "/fake/repo/foo.py"),
            table=table,
            base_dir=Path("/fake/repo"),
            repo_tag="engram",
        ))

        assert len(table.rows) == 1
        row = table.rows[0]
        assert row.id == "engram/foo.py:0"
        assert row.filepath == "engram/foo.py"
        assert row.chunk_index == 0
        assert row.embedding == [0.0, 0.0, 0.0]
        assert "def add" in row.code
        assert row.search_text == f"engram/foo.py {row.code}"

    def test_multiple_chunks_get_matching_embeddings_by_position(self, engram_cocoindex_flows, monkeypatch):
        from engram import chunking as chunking_module

        captured_chunks = []

        async def fake_embed_code_chunks(chunks):
            captured_chunks.extend(chunks)
            return [[float(i)] for i in range(len(chunks))]

        monkeypatch.setattr(chunking_module, "embed_code_chunks", fake_embed_code_chunks)

        table = FakeTable()
        content = "".join(f"def add_{i}(a, b):\n    return a + b\n\n\n" for i in range(50))
        _run(engram_cocoindex_flows.process_code_file(
            FakeCodeFile(content, "/fake/repo/math.py"),
            table=table,
            base_dir=Path("/fake/repo"),
            repo_tag="engram",
        ))

        assert len(table.rows) > 1
        assert len(table.rows) == len(captured_chunks)
        for i, row in enumerate(table.rows):
            assert row.chunk_index == i
            assert row.embedding == [float(i)]
            assert row.code == captured_chunks[i]

    def test_empty_content_declares_no_rows(self, engram_cocoindex_flows, monkeypatch):
        from engram import chunking as chunking_module

        embed_calls = []

        async def fake_embed_code_chunks(chunks):
            embed_calls.append(chunks)
            return []

        monkeypatch.setattr(chunking_module, "embed_code_chunks", fake_embed_code_chunks)

        table = FakeTable()
        _run(engram_cocoindex_flows.process_code_file(
            FakeCodeFile("", "/fake/repo/empty.py"),
            table=table,
            base_dir=Path("/fake/repo"),
            repo_tag="engram",
        ))

        assert table.rows == []
        assert embed_calls == []


class TestHindsightRetainStrategyField:
    def test_regression_payload_does_not_include_dead_strategy_field(self, engram_cocoindex_flows, monkeypatch):
        """Same fix as cocoindex-flows.py's hindsight_retain(): strategy=
        "exact" was never registered in any bank's retain_strategies
        config, so it was pure log noise. See docs/FINDINGS.md 2026-08-03."""
        import json

        captured_requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"success": True}).encode()

        def fake_urlopen(req, timeout=60):
            captured_requests.append(json.loads(req.data))
            return FakeResponse()

        monkeypatch.setattr(engram_cocoindex_flows, "urlopen", fake_urlopen)
        engram_cocoindex_flows.hindsight_retain(bank_id="engram-docs", content="x", document_id="doc-1")

        assert "strategy" not in captured_requests[0]["items"][0]


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
