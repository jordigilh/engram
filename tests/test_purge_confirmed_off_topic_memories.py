"""Tests for purge-confirmed-off-topic-memories.py's pure logic:
plan_purge_candidates() (which documents are eligible for off-topic auditing)
and should_purge() (the high-confidence deletion gate). See docs/FINDINGS.md
2026-07-27.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "spike"))
import classify  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "purge_confirmed_off_topic_memories", REPO_ROOT / "purge-confirmed-off-topic-memories.py"
)
pcotm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pcotm)


class TestPlanPurgeCandidates:
    def test_untagged_document_is_a_candidate(self):
        docs = [{"id": "d1", "tags": []}]

        candidates = pcotm.plan_purge_candidates(docs)

        assert [d["id"] for d in candidates] == ["d1"]

    def test_document_missing_tags_key_entirely_is_a_candidate(self):
        docs = [{"id": "d1"}]

        candidates = pcotm.plan_purge_candidates(docs)

        assert [d["id"] for d in candidates] == ["d1"]

    def test_tagged_document_is_never_a_candidate(self):
        """Already resolved to an onboarded project -- in-scope by definition,
        must never be audited for deletion regardless of how it got tagged
        (transcript-path or content classification)."""
        docs = [{"id": "d1", "tags": ["kubernaut"]}]

        candidates = pcotm.plan_purge_candidates(docs)

        assert candidates == []

    def test_mixed_batch(self):
        docs = [
            {"id": "untagged", "tags": []},
            {"id": "kubernaut-tagged", "tags": ["kubernaut"]},
            {"id": "dcm-tagged", "tags": ["dcm"]},
        ]

        candidates = pcotm.plan_purge_candidates(docs)

        assert [d["id"] for d in candidates] == ["untagged"]


class TestShouldPurge:
    def test_high_confidence_off_topic_is_flagged(self):
        result = classify.OffTopicClassificationResult(
            off_topic=True, identified_project="koku", confidence=0.9,
            reasoning="", raw="", latency_s=0.1,
        )

        assert pcotm.should_purge(result, min_confidence=0.85) is True

    def test_low_confidence_off_topic_is_not_flagged(self):
        result = classify.OffTopicClassificationResult(
            off_topic=True, identified_project="koku", confidence=0.6,
            reasoning="", raw="", latency_s=0.1,
        )

        assert pcotm.should_purge(result, min_confidence=0.85) is False

    def test_not_off_topic_is_never_flagged_even_with_high_confidence(self):
        """Confidence here means 'confidence this fact is generic/universal',
        not confidence it's off-topic -- must never be misread as a delete signal."""
        result = classify.OffTopicClassificationResult(
            off_topic=False, identified_project=None, confidence=0.95,
            reasoning="", raw="", latency_s=0.1,
        )

        assert pcotm.should_purge(result, min_confidence=0.85) is False

    def test_error_result_is_never_flagged_regardless_of_fields(self):
        result = classify.OffTopicClassificationResult(
            off_topic=True, identified_project="koku", confidence=0.99,
            reasoning="", raw="", latency_s=0.1, error="API timeout",
        )

        assert pcotm.should_purge(result, min_confidence=0.85) is False

    def test_exact_threshold_boundary_is_flagged(self):
        result = classify.OffTopicClassificationResult(
            off_topic=True, identified_project="koku", confidence=0.85,
            reasoning="", raw="", latency_s=0.1,
        )

        assert pcotm.should_purge(result, min_confidence=0.85) is True

    def test_default_min_confidence_is_stricter_than_tagging_threshold(self):
        """Deletion is irreversible -- the bar must be higher than the 0.75
        used for applying a project tag in backfill-content-classified-tags.py."""
        assert pcotm.DEFAULT_MIN_CONFIDENCE > 0.75
