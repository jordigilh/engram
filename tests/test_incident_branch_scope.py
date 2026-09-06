from engram.incident.branch_scope import normalize_branch
from engram.incident.retention import family_signature


def test_normalize_branch_accepts_main_and_release_lines() -> None:
    assert normalize_branch("main") == "main"
    assert normalize_branch("release/v1.5") == "v1.5"
    assert normalize_branch("v1.6") == "v1.6"


def test_normalize_branch_maps_feature_branch_to_main_target_line() -> None:
    assert normalize_branch("feature/triage") == "main"


def test_failure_families_are_branch_scoped() -> None:
    base = {
        "scope": {"project": "kubernaut", "branch": "main"},
        "test": {"name": "E2E-FP-1"},
        "summary": {"workflow_resolution_failed": True},
    }
    release = {**base, "scope": {"project": "kubernaut", "branch": "v1.5"}}

    assert family_signature(base) != family_signature(release)
