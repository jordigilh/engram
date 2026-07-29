"""Tests for fallback_extract.py: the no-LLM heuristic extraction + local
recovery buffer used when hindsight-api's retain call fails transiently.

hindsight-api's Haiku extraction is server-side (confirmed 2026-07-29 --
nightly-learn.py's retain_windows() only POSTs raw text; see docs/FINDINGS.md
and GitHub issue #5), so this module can't wrap a local Haiku call. Instead
it buffers the failed window locally (tagged fallback-extraction) with a
cheap heuristic extraction, for later reprocessing once hindsight-api's own
Vertex AI dependency recovers.
"""
from __future__ import annotations

import json

import pytest

import fallback_extract as fe


@pytest.fixture(autouse=True)
def isolate_fallback_log(tmp_path, monkeypatch):
    monkeypatch.setattr(fe, "FALLBACK_LOG_PATH", tmp_path / "fallback-retained.jsonl")


class TestExtract:
    def test_detects_capitalized_and_acronym_entities(self):
        result = fe.extract("CocoIndex and the Hindsight API both reference ENGRAM_CORRECTION_DETECTOR")

        assert "CocoIndex" in result["entities"]
        assert "ENGRAM_CORRECTION_DETECTOR" in result["entities"]

    def test_detects_known_topic_keywords(self):
        result = fe.extract("this window is about contradiction resolution and watermark handling")

        assert "contradiction-resolution" in result["topics"]
        assert "watermark" in result["topics"]

    def test_unrelated_text_has_no_topics(self):
        result = fe.extract("the weather today is sunny and warm")

        assert result["topics"] == []

    def test_high_salience_signal_words_raise_salience(self):
        result = fe.extract("this is mandatory, we always do it this way and never skip it")

        assert result["salience"] == "high"

    def test_plain_text_has_low_or_medium_salience(self):
        result = fe.extract("the sky is blue today and birds are singing")

        assert result["salience"] in ("low", "medium")

    def test_empty_text_returns_empty_result_without_raising(self):
        result = fe.extract("")

        assert result == {"entities": [], "topics": [], "salience": "low"}

    def test_short_text_returns_empty_result_without_raising(self):
        result = fe.extract("hi")

        assert result["entities"] == []
        assert result["salience"] == "low"

    def test_no_duplicate_entities(self):
        result = fe.extract("CocoIndex uses CocoIndex internally for CocoIndex flows")

        assert result["entities"].count("CocoIndex") == 1


class TestRecordFallback:
    def test_appends_tagged_json_line(self):
        fe.record_fallback(
            window="[CORRECTION] User: we don't use HAPI",
            transcript_id="tid-1",
            project="engram",
            extracted={"entities": ["HAPI"], "topics": [], "salience": "medium"},
            reason="Connection error",
        )

        lines = fe.FALLBACK_LOG_PATH.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["transcript_id"] == "tid-1"
        assert entry["project"] == "engram"
        assert entry["tags"] == ["fallback-extraction"]
        assert entry["window"] == "[CORRECTION] User: we don't use HAPI"
        assert entry["extracted"]["entities"] == ["HAPI"]
        assert entry["reason"] == "Connection error"
        assert "timestamp" in entry

    def test_multiple_calls_append_not_overwrite(self):
        fe.record_fallback("w1", "tid-1", None, {}, "reason1")
        fe.record_fallback("w2", "tid-2", None, {}, "reason2")

        lines = fe.FALLBACK_LOG_PATH.read_text().splitlines()
        assert len(lines) == 2

    def test_no_project_writes_null_project(self):
        fe.record_fallback("w1", "tid-1", None, {}, "reason1")

        entry = json.loads(fe.FALLBACK_LOG_PATH.read_text().splitlines()[0])
        assert entry["project"] is None


class TestLoadSaveCountBacklog:
    def test_load_backlog_empty_when_file_missing(self):
        assert fe.load_backlog() == []

    def test_load_backlog_returns_recorded_entries_in_order(self):
        fe.record_fallback("w1", "tid-1", "engram", {}, "reason1")
        fe.record_fallback("w2", "tid-2", "engram", {}, "reason2")

        backlog = fe.load_backlog()

        assert [e["window"] for e in backlog] == ["w1", "w2"]

    def test_save_backlog_overwrites_with_only_given_entries(self):
        fe.record_fallback("w1", "tid-1", "engram", {}, "reason1")
        fe.record_fallback("w2", "tid-2", "engram", {}, "reason2")
        remaining = [fe.load_backlog()[1]]

        fe.save_backlog(remaining)

        assert [e["window"] for e in fe.load_backlog()] == ["w2"]

    def test_save_backlog_empty_list_clears_file(self):
        fe.record_fallback("w1", "tid-1", "engram", {}, "reason1")

        fe.save_backlog([])

        assert fe.load_backlog() == []

    def test_count_backlog(self):
        assert fe.count_backlog() == 0

        fe.record_fallback("w1", "tid-1", "engram", {}, "reason1")

        assert fe.count_backlog() == 1
