"""Tests for Praxis-aware triage: document grouping, manual replacements, and
the non-destructive invalidate apply mode (no deletes, no LLM calls)."""
from __future__ import annotations

from datetime import datetime, timedelta

from engram.pipeline import triage_memories as triage


class TestPraxisDocumentGrouping:
    def test_cursor_memory_legacy_chunk_regex_preserved(self):
        memory = {"id": "m1", "chunk_id": "cursor-memory_abc-123_0"}
        assert triage.document_key(memory, "cursor-memory") == "abc-123"

    def test_praxis_groups_by_document_id(self):
        memory = {"id": "m2", "document_id": "ai-pr-1049-comment0"}
        assert triage.document_key(memory, "praxis-issues") == "ai-pr-1049-comment0"

    def test_praxis_orphan_observations_become_singleton_groups(self):
        memory = {"id": "m3", "fact_type": "observation", "document_id": None}
        assert triage.document_key(memory, "praxis-issues") == "orphan:m3"

    def test_family_key_strips_one_chunk_split(self):
        assert triage.family_key("ai-pr-1049-comment0-part1") == "ai-pr-1049"
        assert triage.family_key("ai-pr-1049-comment0") == "ai-pr-1049"
        assert triage.family_key("ai-pr-1049") == "ai-pr-1049"
        base = "praxis-manual-docs--ticket--73447a609f3b90ba"
        assert triage.family_key(f"{base}-part1") == base


class TestPraxisClassification:
    def test_format_noise_is_flagged_for_praxis_only(self):
        memory = {"text": "Use --comments\nfile:///var/folders/abc/praxis.html 30/30"}
        cutoff = datetime.now() - timedelta(days=14)
        assert "format-noise" in triage.classify_memory(memory, cutoff, "praxis-docs")
        assert "format-noise" not in triage.classify_memory(memory, cutoff, "cursor-memory")

    def test_valuable_pattern_still_wins(self):
        memory = {"text": "Architecture decision: file:///var/folders/abc/x is stored as a chunk."}
        cutoff = datetime.now() - timedelta(days=14)
        assert triage.classify_memory(memory, cutoff, "praxis-docs") == []


class TestManualReplacement:
    def test_replacement_preserves_text_and_adds_provenance(self):
        memory = {
            "id": "mem-1",
            "text": "StreamBuffer request bodies skip the ceiling.",
            "fact_type": "observation",
            "date": "2026-09-09T01:40:05+00:00",
            "tags": ["praxis", "open"],
        }
        item = triage.build_manual_replacement(
            memory, "praxis-issues", reviewer="manual-triage", reviewed_at="2026-09-11T12:30:00+00:00"
        )
        assert item["content"] == memory["text"]
        assert item["document_id"] == "manual-triage--praxis-issues--mem-1"
        assert item["timestamp"] == memory["date"]
        assert "strategy" not in item
        assert item["metadata"]["reviewed_by"] == "manual-triage"
        assert item["metadata"]["supersedes_memory_id"] == "mem-1"
        assert "manual-replacement" in item["tags"]

    def test_null_metadata_values_are_dropped_for_orphans(self):
        memory = {"id": "mem-2", "text": "Orphan observation text here.", "fact_type": "observation", "document_id": None}
        item = triage.build_manual_replacement(memory, "praxis-docs")
        assert all(v is not None for v in item["metadata"].values())
        assert "original_document_id" not in item["metadata"]


class TestInvalidateApply:
    def test_orphans_are_skipped_not_duplicated(self, monkeypatch):
        posted = []
        patched = []

        def fake_post(path, payload):
            posted.append((path, payload))
            return {"items_count": 1}

        def fake_patch(path, payload):
            patched.append((path, payload))
            return True

        monkeypatch.setattr(triage, "api_post", fake_post)
        monkeypatch.setattr(triage, "api_patch", fake_patch)

        orphan = {
            "id": "orphan-1",
            "text": "Legacy observation with enough words to avoid short classification.",
            "fact_type": "observation",
            "date": "2026-09-09T01:40:05+00:00",
            "document_id": None,
            "state": "valid",
        }
        result = triage._apply_invalidate_high_confidence(
            "praxis-issues", [orphan], {"orphan-1": orphan}, {}
        )
        # Server rejects curation of observations, so auto-apply must not
        # create duplicate replacements -- just count and report them.
        assert result["orphans_skipped_server_immortal"] == 1
        assert posted == []
        assert patched == []

    def test_exact_duplicates_keep_newest(self, monkeypatch):
        patched = []
        monkeypatch.setattr(triage, "api_post", lambda path, payload: {"items_count": 1})
        monkeypatch.setattr(
            triage, "api_patch", lambda path, payload: patched.append(path) or True
        )
        old = {"id": "old", "text": "Same words here.", "date": "2026-09-01", "state": "valid"}
        new = {"id": "new", "text": "Same words here.", "date": "2026-09-10", "state": "valid"}
        result = triage._apply_invalidate_high_confidence(
            "praxis-docs", [old, new], {"old": old, "new": new}, {"exact:x": ["old", "new"]}
        )
        assert result["invalidated_ids"] == ["old"]

    def test_praxis_dry_run_groups_orphans(self, monkeypatch):
        memories = [
            {"id": "o1", "text": "Legacy observation one with sufficient length for testing.", "fact_type": "observation", "date": "2026-09-09", "document_id": None},
            {"id": "w1", "text": "# Issue #1 (praxis): a healthy chunk with document linkage.", "fact_type": "world", "date": "2026-09-11", "document_id": "praxis-issue-1"},
        ]
        monkeypatch.setattr(triage, "fetch_all_memories", lambda bank_id: memories)
        summary = triage.triage(bank_id="praxis-issues", stale_days=3650, apply=False)
        assert summary["total_memories"] == 2
        assert summary["orphan_documents"] == 1
        assert summary["applied"] is False
