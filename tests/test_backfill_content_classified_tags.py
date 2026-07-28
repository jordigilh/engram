"""Tests for backfill-content-classified-tags.py's pure logic:
plan_content_targets() (which documents need content-based classification)
and should_apply_tag() (the confidence-threshold gate). See docs/FINDINGS.md
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
    "backfill_content_classified_tags", REPO_ROOT / "backfill-content-classified-tags.py"
)
bcct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bcct)


class TestPlanContentTargets:
    def test_document_with_no_transcript_id_is_a_target(self):
        docs = [{"id": "d1", "tags": [], "document_metadata": {"source": "triage-rearrange"}}]

        targets = bcct.plan_content_targets(docs)

        assert [d["id"] for d in targets] == ["d1"]

    def test_document_with_transcript_id_is_not_a_target(self):
        """Those belong to backfill-memory-tags.py's transcript-path resolution instead."""
        docs = [{"id": "d1", "tags": [], "document_metadata": {"transcript_id": "t1"}}]

        targets = bcct.plan_content_targets(docs)

        assert targets == []

    def test_already_tagged_document_is_not_a_target(self):
        docs = [{"id": "d1", "tags": ["kubernaut"], "document_metadata": {}}]

        targets = bcct.plan_content_targets(docs)

        assert targets == []

    def test_mixed_batch(self):
        docs = [
            {"id": "needs-classification", "tags": [], "document_metadata": {"source": "triage-rearrange"}},
            {"id": "has-lineage", "tags": [], "document_metadata": {"transcript_id": "t1"}},
            {"id": "already-tagged", "tags": ["dcm"], "document_metadata": {}},
        ]

        targets = bcct.plan_content_targets(docs)

        assert [d["id"] for d in targets] == ["needs-classification"]


class TestShouldApplyTag:
    def test_high_confidence_project_is_applied(self):
        result = classify.ProjectClassificationResult(
            project="kubernaut", confidence=0.9, reasoning="", raw="", latency_s=0.1,
        )

        assert bcct.should_apply_tag(result, min_confidence=0.75) is True

    def test_low_confidence_project_is_not_applied(self):
        result = classify.ProjectClassificationResult(
            project="kubernaut", confidence=0.5, reasoning="", raw="", latency_s=0.1,
        )

        assert bcct.should_apply_tag(result, min_confidence=0.75) is False

    def test_generic_result_is_not_applied_even_with_high_confidence(self):
        # classify_project_from_content() maps "generic" -> project=None before
        # returning, but guard the None case explicitly here too.
        result = classify.ProjectClassificationResult(
            project=None, confidence=0.95, reasoning="", raw="", latency_s=0.1,
        )

        assert bcct.should_apply_tag(result, min_confidence=0.75) is False

    def test_error_result_is_never_applied_regardless_of_fields(self):
        result = classify.ProjectClassificationResult(
            project="kubernaut", confidence=0.99, reasoning="", raw="", latency_s=0.1,
            error="API timeout",
        )

        assert bcct.should_apply_tag(result, min_confidence=0.75) is False

    def test_exact_threshold_boundary_is_applied(self):
        result = classify.ProjectClassificationResult(
            project="dcm", confidence=0.75, reasoning="", raw="", latency_s=0.1,
        )

        assert bcct.should_apply_tag(result, min_confidence=0.75) is True
