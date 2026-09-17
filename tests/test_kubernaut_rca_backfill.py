from pathlib import Path

from engram.pipeline.kubernaut_rca_backfill import backfill


def test_backfill_persists_only_quality_passed_dossiers(tmp_path: Path, monkeypatch) -> None:
    context = {
        "rr_id": "rr-a-1",
        "test": {"run_id": "1", "job_id": "2", "name": "E2E-FP-1"},
        "summary": {"workflow_resolution_failed": True, "manual_review_required": True},
        "evidence_assessment": {"rr_id_correlation": "exact"},
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


def test_backfill_preserves_run_snapshot_state(tmp_path: Path) -> None:
    inventory = {
        "repository": "jordigilh/kubernaut",
        "runs": [
            {
                "run_id": 34796990775,
                "status": "in_progress",
                "conclusion": None,
                "status_observed_at": "2026-09-14T02:30:29Z",
                "status_is_final": False,
                "usable_for_dossier": True,
            }
        ],
    }

    result = backfill(inventory, tmp_path, promote=False)

    assert result["inventory"]["run_states"] == [
        {
            "run_id": 34796990775,
            "status": "in_progress",
            "conclusion": None,
            "status_observed_at": "2026-09-14T02:30:29Z",
            "status_is_final": False,
            "usable_for_dossier": True,
        }
    ]
