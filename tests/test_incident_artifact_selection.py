from engram.incident.artifact_selection import is_must_gather, select_artifact
from engram.pipeline.kubernaut_corpus_inventory import GitHubActionsClient
from engram.pipeline.kubernaut_rca_backfill import _select_artifact


def test_artifact_selection_prefers_job_matching_must_gather() -> None:
    artifacts = [
        {"artifact_id": 1, "name": "must-gather-logs-e2e-fleet-1"},
        {"artifact_id": 2, "name": "must-gather-logs-helm-cert-manager-1"},
        {"artifact_id": 3, "name": "coverage-e2e-fullpipeline"},
        {"artifact_id": 4, "name": "expired-must-gather", "expired": True},
    ]

    assert _select_artifact({"relevant_artifacts": artifacts}, {"name": "E2E (fleet)"})["artifact_id"] == 1
    assert _select_artifact(
        {"relevant_artifacts": artifacts},
        {"name": "Helm Smoke Tests (Kind, tls=cert-manager)"},
    )["artifact_id"] == 2
    assert select_artifact(artifacts, artifact_hint="must-gather-logs-helm-cert-manager-1")["artifact_id"] == 2


def test_coverage_artifact_is_not_a_must_gather() -> None:
    assert is_must_gather("coverage-e2e-fullpipeline") is False
    assert is_must_gather("must-gather-logs-e2e-fleet-1") is True


def test_inventory_records_only_must_gather_artifacts() -> None:
    responses = {
        "actions/runs/123": {"id": 123, "conclusion": "failure"},
        "actions/runs/123/jobs": {"jobs": [{"id": 456, "name": "E2E (fleet)", "conclusion": "failure"}]},
        "actions/runs/123/artifacts": {
            "artifacts": [
                {"id": 1, "name": "coverage-e2e-fullpipeline", "expired": False},
                {"id": 2, "name": "must-gather-logs-e2e-fleet-1", "expired": False},
            ]
        },
    }

    result = GitHubActionsClient(
        "jordigilh/kubernaut", "token", request=lambda path, _query: responses[path]
    ).inventory_run_id(123)

    assert [item["name"] for item in result["runs"][0]["relevant_artifacts"]] == [
        "must-gather-logs-e2e-fleet-1"
    ]
