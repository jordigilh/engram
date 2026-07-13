"""Tests for contradiction_resolution.py's three-tier resolve() and its
delete_document()/env-parsing helpers.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

import contradiction_resolution as cr


@pytest.fixture(autouse=True)
def isolate_auto_resolved_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "AUTO_RESOLVED_LOG_PATH", tmp_path / "contradictions-auto-resolved.jsonl")
    monkeypatch.setattr(cr, "LOG_DIR", tmp_path)


def _raise(exc: Exception):
    raise exc


def _contradiction_result(
    contradicts: bool,
    conflicting_memory_index=None,
    explanation: str = "",
    confidence: float = 0.0,
    error: str | None = None,
):
    return SimpleNamespace(
        contradicts=contradicts,
        conflicting_memory_index=conflicting_memory_index,
        explanation=explanation,
        confidence=confidence,
        error=error,
    )


class TestCheckEnabled:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_CONTRADICTION_CHECK", raising=False)
        assert cr.check_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CONTRADICTION_CHECK", "off")
        assert cr.check_enabled() is False

    def test_anything_else_is_on(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CONTRADICTION_CHECK", "yes")
        assert cr.check_enabled() is True


class TestAutoThreshold:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_CONTRADICTION_AUTO_THRESHOLD", raising=False)
        assert cr.auto_threshold() == cr.DEFAULT_AUTO_THRESHOLD

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CONTRADICTION_AUTO_THRESHOLD", "0.75")
        assert cr.auto_threshold() == 0.75

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CONTRADICTION_AUTO_THRESHOLD", "not-a-float")
        assert cr.auto_threshold() == cr.DEFAULT_AUTO_THRESHOLD


class TestAutoMode:
    def test_default_shadow(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_CONTRADICTION_AUTO_MODE", raising=False)
        assert cr.auto_mode() == "shadow"

    def test_explicit_live(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CONTRADICTION_AUTO_MODE", "live")
        assert cr.auto_mode() == "live"

    def test_invalid_falls_back_to_shadow(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CONTRADICTION_AUTO_MODE", "bogus")
        assert cr.auto_mode() == "shadow"


class TestDeleteDocument:
    def test_success_returns_true(self, monkeypatch):
        monkeypatch.setattr(cr, "urlopen", lambda req, timeout=10: None)
        assert cr.delete_document("cursor-memory", "doc-1") is True

    def test_404_returns_false_without_retry(self, monkeypatch):
        calls = {"count": 0}

        def fake_urlopen(req, timeout=10):
            calls["count"] += 1
            raise HTTPError(req.full_url, 404, "not found", None, None)

        monkeypatch.setattr(cr, "urlopen", fake_urlopen)
        assert cr.delete_document("cursor-memory", "doc-1") is False
        assert calls["count"] == 1

    def test_transient_error_retries_then_succeeds(self, monkeypatch):
        calls = {"count": 0}

        def fake_urlopen(req, timeout=10):
            calls["count"] += 1
            if calls["count"] < 2:
                raise URLError("connection reset")
            return None

        monkeypatch.setattr(cr, "urlopen", fake_urlopen)
        monkeypatch.setattr(cr.time, "sleep", lambda *_: None)
        assert cr.delete_document("cursor-memory", "doc-1", retries=2) is True
        assert calls["count"] == 2

    def test_exhausts_retries_and_returns_false(self, monkeypatch):
        def always_fails(req, timeout=10):
            raise URLError("connection reset")

        monkeypatch.setattr(cr, "urlopen", always_fails)
        monkeypatch.setattr(cr.time, "sleep", lambda *_: None)
        assert cr.delete_document("cursor-memory", "doc-1", retries=2) is False

    def test_non_404_http_error_retries_then_fails(self, monkeypatch):
        def always_500(req, timeout=10):
            raise HTTPError(req.full_url, 500, "server error", None, None)

        monkeypatch.setattr(cr, "urlopen", always_500)
        monkeypatch.setattr(cr.time, "sleep", lambda *_: None)
        assert cr.delete_document("cursor-memory", "doc-1", retries=1) is False


class TestResolve:
    def test_disabled_returns_retain_without_calling_recall(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CONTRADICTION_CHECK", "off")
        monkeypatch.setattr(cr, "recall", lambda *a, **k: _raise(AssertionError("recall should not be called")))
        result = cr.resolve("cursor-memory", "some statement")
        assert result.action == "retain"

    def test_recall_exception_fails_open_to_retain(self, monkeypatch):
        monkeypatch.setattr(cr, "recall", lambda *a, **k: _raise(RuntimeError("network down")))
        result = cr.resolve("cursor-memory", "some statement")
        assert result.action == "retain"

    def test_no_existing_memories_returns_retain(self, monkeypatch):
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [])
        result = cr.resolve("cursor-memory", "some statement")
        assert result.action == "retain"

    def test_check_contradiction_error_returns_retain(self, monkeypatch):
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [("doc-1", "old memory")])
        monkeypatch.setattr(cr, "check_contradiction", lambda *a, **k: _contradiction_result(
            contradicts=False, error="timeout",
        ))
        result = cr.resolve("cursor-memory", "some statement")
        assert result.action == "retain"

    def test_no_contradiction_returns_retain(self, monkeypatch):
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [("doc-1", "old memory")])
        monkeypatch.setattr(cr, "check_contradiction", lambda *a, **k: _contradiction_result(contradicts=False))
        result = cr.resolve("cursor-memory", "some statement")
        assert result.action == "retain"

    def test_contradicts_with_null_index_returns_retain(self, monkeypatch):
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [("doc-1", "old memory")])
        monkeypatch.setattr(cr, "check_contradiction", lambda *a, **k: _contradiction_result(
            contradicts=True, conflicting_memory_index=None, confidence=0.99,
        ))
        result = cr.resolve("cursor-memory", "some statement")
        assert result.action == "retain"

    def test_contradicts_with_out_of_range_index_returns_retain(self, monkeypatch):
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [("doc-1", "old memory")])
        monkeypatch.setattr(cr, "check_contradiction", lambda *a, **k: _contradiction_result(
            contradicts=True, conflicting_memory_index=5, confidence=0.99,
        ))
        result = cr.resolve("cursor-memory", "some statement")
        assert result.action == "retain"

    def test_high_confidence_auto_resolves_in_shadow_mode_without_deleting(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_CONTRADICTION_AUTO_MODE", raising=False)  # default shadow
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [("doc-1", "old memory")])
        monkeypatch.setattr(cr, "check_contradiction", lambda *a, **k: _contradiction_result(
            contradicts=True, conflicting_memory_index=0, confidence=0.95, explanation="clear conflict",
        ))
        delete_calls = []
        monkeypatch.setattr(cr, "delete_document", lambda *a, **k: delete_calls.append(a) or True)

        result = cr.resolve("cursor-memory", "new statement")

        assert result.action == "auto_resolved"
        assert result.superseded_document_id == "doc-1"
        assert delete_calls == [], "shadow mode must never call delete_document"

        logged = [json.loads(line) for line in cr.AUTO_RESOLVED_LOG_PATH.read_text().splitlines()]
        assert len(logged) == 1
        assert logged[0]["mode"] == "shadow"
        assert logged[0]["deleted"] is False

    def test_high_confidence_auto_resolves_in_live_mode_and_deletes(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_CONTRADICTION_AUTO_MODE", "live")
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [("doc-1", "old memory")])
        monkeypatch.setattr(cr, "check_contradiction", lambda *a, **k: _contradiction_result(
            contradicts=True, conflicting_memory_index=0, confidence=0.95, explanation="clear conflict",
        ))
        delete_calls = []
        monkeypatch.setattr(cr, "delete_document", lambda *a, **k: delete_calls.append(a) or True)

        result = cr.resolve("cursor-memory", "new statement")

        assert result.action == "auto_resolved"
        assert delete_calls == [("cursor-memory", "doc-1")]

        logged = [json.loads(line) for line in cr.AUTO_RESOLVED_LOG_PATH.read_text().splitlines()]
        assert logged[0]["mode"] == "live"
        assert logged[0]["deleted"] is True

    def test_low_confidence_queues_for_human_review(self, monkeypatch):
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [("doc-1", "old memory")])
        monkeypatch.setattr(cr, "check_contradiction", lambda *a, **k: _contradiction_result(
            contradicts=True, conflicting_memory_index=0, confidence=0.5, explanation="ambiguous",
        ))
        queue_calls = []
        monkeypatch.setattr(cr.pending_queue, "append_pending", lambda **kwargs: queue_calls.append(kwargs))

        result = cr.resolve("cursor-memory", "new statement")

        assert result.action == "queued"
        assert result.superseded_document_id == "doc-1"
        assert len(queue_calls) == 1
        assert queue_calls[0]["new_statement"] == "new statement"
        assert queue_calls[0]["conflicting_memory"] == "old memory"
        assert queue_calls[0]["conflicting_memory_index"] == 0
        assert queue_calls[0]["document_id"] == "doc-1"

    def test_confidence_exactly_at_threshold_auto_resolves(self, monkeypatch):
        """confidence >= threshold, boundary case."""
        monkeypatch.setenv("ENGRAM_CONTRADICTION_AUTO_THRESHOLD", "0.9")
        monkeypatch.setattr(cr, "recall", lambda *a, **k: [("doc-1", "old memory")])
        monkeypatch.setattr(cr, "check_contradiction", lambda *a, **k: _contradiction_result(
            contradicts=True, conflicting_memory_index=0, confidence=0.9,
        ))
        result = cr.resolve("cursor-memory", "new statement")
        assert result.action == "auto_resolved"
