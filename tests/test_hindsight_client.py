"""Tests for spike/hindsight_client.py's recall(). Regression coverage for
the 2026-07-12 bug: recall() parsed a "chunks" key the live hindsight-api
response never populates -- the real shape is {"results": [...]}. Confirmed
recall() had never actually seen real memory content before that fix (see
docs/FINDINGS.md).

2026-07-29 (GitHub issue #1): recall() now returns (memory_id, document_id,
text) triples instead of (document_id, text) pairs. RecallResult's "id"
field is the individual fact/memory-level identifier accepted by
PATCH /memories/{memory_id} (contradiction_resolution.invalidate_memory());
"document_id" is a separate, optional field naming the source document. The
prior 2-tuple design conflated the two, which is why the old delete_document()
path operated on the whole document instead of just the conflicting fact.
"""
from __future__ import annotations

import json
import time

import hindsight_client as hc


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _urlopen_returning(payload: dict):
    def fake_urlopen(req, timeout=60):
        return FakeResponse(payload)
    return fake_urlopen


class TestRecall:
    def test_parses_results_key_into_memory_id_document_id_text_triples(self, monkeypatch):
        monkeypatch.setattr(hc, "urlopen", _urlopen_returning({
            "results": [
                {"id": "mem-1", "document_id": "doc-1", "text": "first memory"},
                {"id": "mem-2", "document_id": "doc-2", "text": "second memory"},
            ]
        }))
        triples = hc.recall("cursor-memory", "some query")
        assert triples == [
            ("mem-1", "doc-1", "first memory"),
            ("mem-2", "doc-2", "second memory"),
        ]

    def test_regression_ignores_chunks_key_even_when_present(self, monkeypatch):
        """The exact bug fixed 2026-07-12: a "chunks" key must never be read,
        even if populated -- only "results" is real."""
        monkeypatch.setattr(hc, "urlopen", _urlopen_returning({
            "chunks": [{"id": "wrong-id", "document_id": "wrong-doc", "text": "should not be used"}],
            "results": [{"id": "mem-1", "document_id": "doc-1", "text": "correct memory"}],
        }))
        triples = hc.recall("cursor-memory", "some query")
        assert triples == [("mem-1", "doc-1", "correct memory")]

    def test_missing_results_key_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(hc, "urlopen", _urlopen_returning({"chunks": {}}))
        assert hc.recall("cursor-memory", "some query") == []

    def test_skips_entries_missing_text_or_id(self, monkeypatch):
        monkeypatch.setattr(hc, "urlopen", _urlopen_returning({
            "results": [
                {"id": "mem-1", "document_id": "doc-1", "text": "ok"},
                {"id": "mem-2", "document_id": "doc-2"},          # missing text
                {"document_id": "doc-3", "text": "no id"},        # missing id (required per RecallResult)
                {"id": "mem-4", "document_id": "doc-4", "text": ""},  # empty text is falsy
                "not-a-dict",
            ]
        }))
        assert hc.recall("cursor-memory", "q") == [("mem-1", "doc-1", "ok")]

    def test_document_id_is_optional_and_defaults_to_none(self, monkeypatch):
        """RecallResult.document_id is optional in the schema -- a result
        with an id/text but no document_id must still be usable (the memory
        is still invalidatable even if it has no known source document)."""
        monkeypatch.setattr(hc, "urlopen", _urlopen_returning({
            "results": [{"id": "mem-1", "text": "ok, no document_id"}],
        }))
        assert hc.recall("cursor-memory", "q") == [("mem-1", None, "ok, no document_id")]

    def test_respects_max_results(self, monkeypatch):
        monkeypatch.setattr(hc, "urlopen", _urlopen_returning({
            "results": [{"id": f"mem-{i}", "document_id": str(i), "text": f"text{i}"} for i in range(10)]
        }))
        triples = hc.recall("cursor-memory", "q", max_results=3)
        assert len(triples) == 3

    def test_retries_on_transient_error_then_succeeds(self, monkeypatch):
        from urllib.error import URLError

        attempts = {"count": 0}

        def flaky_urlopen(req, timeout=60):
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise URLError("connection refused")
            return FakeResponse({"results": [{"id": "mem-1", "document_id": "doc-1", "text": "ok"}]})

        monkeypatch.setattr(hc, "urlopen", flaky_urlopen)
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        assert hc.recall("cursor-memory", "q", retries=2) == [("mem-1", "doc-1", "ok")]
        assert attempts["count"] == 2

    def test_returns_empty_list_after_exhausting_retries(self, monkeypatch):
        from urllib.error import URLError

        def always_fails(req, timeout=60):
            raise URLError("connection refused")

        monkeypatch.setattr(hc, "urlopen", always_fails)
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        assert hc.recall("cursor-memory", "q", retries=2) == []
