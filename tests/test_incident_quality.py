from engram.incident.quality import validate_dossier


def test_high_confidence_terminal_dossier_passes() -> None:
    dossier = {
        "rr_id": "rr-a-1",
        "test": {"name": "E2E-FP-1"},
        "confidence": 0.94,
        "summary": {"workflow_resolution_failed": True, "manual_review_required": True},
        "evidence": [{"id": "e1"}],
        "timeline": [{"evidence_id": "e1"}],
    }

    assert validate_dossier(dossier) == []


def test_low_confidence_or_weak_attribution_needs_review() -> None:
    dossier = {
        "rr_id": "rr-a-1",
        "test": {"name": "In [It] at test.go:1"},
        "confidence": 0.55,
        "summary": {},
        "evidence": [{"id": "e1"}],
        "timeline": [{"evidence_id": "e1"}],
    }

    assert set(validate_dossier(dossier)) == {"weak_test_attribution", "low_confidence"}
