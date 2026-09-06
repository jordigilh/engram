from __future__ import annotations

from engram.pipeline.kubernaut_corpus_inventory import GitHubActionsClient


def test_inventory_filters_failed_jobs_and_relevant_artifacts() -> None:
    responses = {
        "actions/runs": {
            "workflow_runs": [
                {
                    "id": 123,
                    "run_number": 44,
                    "name": "CI Pipeline",
                    "conclusion": "failure",
                    "head_sha": "abc123",
                    "created_at": "2026-09-05T01:00:00Z",
                    "html_url": "https://github.com/jordigilh/kubernaut/actions/runs/123",
                }
            ]
        },
        "actions/runs/123/jobs": {
            "jobs": [
                {"id": 456, "name": "E2E", "conclusion": "failure"},
                {"id": 457, "name": "Lint", "conclusion": "success"},
            ]
        },
        "actions/runs/123/artifacts": {
            "artifacts": [
                {"id": 789, "name": "coverage-e2e-fullpipeline", "expired": False, "size_in_bytes": 10},
                {"id": 790, "name": "unrelated", "expired": False, "size_in_bytes": 10},
            ]
        },
    }

    def request(path, _query):
        return responses[path]

    result = GitHubActionsClient("jordigilh/kubernaut", "token", request=request).inventory(1)

    assert result["failed_runs_scanned"] == 1
    assert result["usable_runs"] == 1
    assert result["runs"][0]["failed_jobs"][0]["job_id"] == 456
    assert result["runs"][0]["primary_failed_jobs"][0]["job_id"] == 456
    assert [item["name"] for item in result["runs"][0]["relevant_artifacts"]] == ["coverage-e2e-fullpipeline"]


def test_inventory_marks_run_without_artifact_unusable() -> None:
    responses = {
        "actions/runs": {"workflow_runs": [{"id": 123, "conclusion": "failure"}]},
        "actions/runs/123/jobs": {"jobs": [{"id": 456, "conclusion": "failure"}]},
        "actions/runs/123/artifacts": {"artifacts": []},
    }

    result = GitHubActionsClient(
        "jordigilh/kubernaut", "token", request=lambda path, _query: responses[path]
    ).inventory(1)

    assert result["usable_runs"] == 0
    assert result["runs"][0]["usable_for_dossier"] is False


def test_inventory_can_scope_to_one_run() -> None:
    responses = {
        "actions/runs/123": {"id": 123, "conclusion": "failure"},
        "actions/runs/123/jobs": {"jobs": []},
        "actions/runs/123/artifacts": {"artifacts": []},
    }

    result = GitHubActionsClient(
        "jordigilh/kubernaut", "token", request=lambda path, _query: responses[path]
    ).inventory_run_id(123)

    assert result["runs"][0]["run_id"] == 123
    assert result["failed_runs_scanned"] == 1
