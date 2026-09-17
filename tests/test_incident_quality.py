from engram.incident.quality import validate_dossier


def test_complete_terminal_dossier_passes() -> None:
    dossier = {
        "rr_id": "rr-a-1",
        "test": {"name": "E2E-FP-1"},
        "summary": {"workflow_resolution_failed": True, "manual_review_required": True},
        "evidence": [{"id": "e1"}],
        "timeline": [{"evidence_id": "e1"}],
    }

    assert validate_dossier(dossier) == []


def test_incomplete_terminal_or_weak_attribution_needs_review() -> None:
    dossier = {
        "rr_id": "rr-a-1",
        "test": {"name": "In [It] at test.go:1"},
        "summary": {},
        "evidence": [{"id": "e1"}],
        "timeline": [{"evidence_id": "e1"}],
    }

    assert set(validate_dossier(dossier)) == {"weak_test_attribution", "incomplete_terminal_state"}
