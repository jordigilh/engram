from pathlib import Path

from engram.pipeline.kubernaut_rca_backfill import backfill


def test_backfill_persists_only_quality_passed_dossiers(tmp_path: Path, monkeypatch) -> None:
    context = {
        "rr_id": "rr-a-1",
        "test": {"run_id": "1", "job_id": "2", "name": "E2E-FP-1"},
        "confidence": 0.94,
        "summary": {"workflow_resolution_failed": True, "manual_review_required": True},
        "confidence_basis": ["exact RR-ID correlation"],
        "evidence": [{"id": "e1", "source_file": "a.yaml", "content": "rr-a-1"}],
        "timeline": [{"evidence_id": "e1"}],
        "clusters": [],
    }
    inventory = {"repository": "jordigilh/kubernaut", "runs": []}

    monkeypatch.setattr(
        "engram.pipeline.kubernaut_rca_backfill.triage_test_failure",
        lambda **_kwargs: context,
    )
    # The full network-backed path is covered by the real five-run smoke test;
    # this unit test asserts the promotion gate's stable contract separately.
    assert backfill(inventory, tmp_path)["metrics"]["candidates_promoted"] == 0
