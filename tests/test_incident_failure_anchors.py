from __future__ import annotations

import json
from pathlib import Path

from engram.incident.service import triage_test_failure


def test_failure_anchor_is_preserved_and_fixture_only_is_a_scope_gap(tmp_path: Path) -> None:
    (tmp_path / "must-gather").mkdir()
    (tmp_path / "must-gather" / "resources.yaml").write_text(
        """kind: RemediationRequest
metadata:
  name: rr-disc004-1789996569
  namespace: kubernaut-agent-e2e
"""
    )
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "job.log").write_text(
        "Created RR fixture: kubernaut-agent-e2e/rr-disc004-1789996569\n"
    )
    (tmp_path / "failure-anchors.jsonl").write_text(
        json.dumps(
            {
                "classification": "incident_with_rr",
                "rr_id": "rr-disc004-1789996569",
                "test_scenario": "E2E-KA-DISC-004",
                "failure_excerpt": (
                    "discovery must return a recommended workflow; "
                    "submit_result_no_workflow sentinel indicates SyncSignalFromRCA did not sync Kind\n"
                    "Expected\n<bool>: false\n to be true"
                ),
            }
        )
        + "\n"
    )
    (tmp_path / ".engram").mkdir()
    (tmp_path / ".engram" / "source-manifest.json").write_text(
        json.dumps(
            {
                "job_log": {"url": "https://github/job/1"},
                "artifacts": [{"name": "must-gather-logs-e2e", "role": "primary"}],
            }
        )
    )

    result = triage_test_failure(
        root=tmp_path,
        run_id="35601676999",
        job_id="106342482157",
        test_name="In [It] at interactive_discovery_e2e_test.go:316",
        failure_text='WorkflowExecution for "rr-disc004-1789996569" did not complete',
    )

    anchor = result["failure_anchor"]
    assert "SyncSignalFromRCA" in anchor["failure_excerpt"]
    assert anchor["scenario_id"] == "E2E-KA-DISC-004"
    assert anchor["expected"] == "true"
    assert anchor["actual"] == "false"
    assert anchor["job_url"] == "https://github/job/1"
    assert result["summary"]["status"] == "artifact_scope_gap"
    assert result["summary"]["manual_review_required"] is True
    assert result["summary"]["lifecycle_completeness"]["coverage"] == "artifact_scope_gap"


def test_sibling_terminal_failure_takes_precedence_over_absence(tmp_path: Path) -> None:
    (tmp_path / "siblings" / "integration-log-aianalysis").mkdir(parents=True)
    (tmp_path / "siblings" / "integration-log-aianalysis" / "integration-aianalysis.log").write_text(
        """2026-09-21T13:04:12Z AgentSession as-test-rr-prod-6f715e5e created
2026-09-21T13:04:12Z Workflow resolution failed: Analysis failed during investigation: workflow discovery did not return the workflow
2026-09-21T13:04:12Z Human review required: llm_parsing_error
"""
    )
    (tmp_path / ".engram").mkdir()
    (tmp_path / ".engram" / "source-manifest.json").write_text(
        json.dumps(
            {
                "job_log": {"url": "https://github/job/2"},
                "artifacts": [
                    {"name": "must-gather-logs-integration-aianalysis", "role": "primary"},
                    {"name": "integration-log-aianalysis", "role": "sibling"},
                ],
            }
        )
    )

    result = triage_test_failure(
        root=tmp_path,
        run_id="35601676999",
        job_id="106342482002",
        test_name="metrics approval",
        failure_text='WorkflowExecution for "rr-prod-6f715e5e" did not complete',
    )

    assert result["summary"]["status"] == "workflow_resolution_failed_manual_review_required"
    assert result["summary"]["lifecycle_completeness"]["coverage"] == "complete"
    assert "service_terminal_failure" in result["summary"]["lifecycle_completeness"]["observed_events"]
    assert result["summary"]["causal_boundary"]["classification"] == "observed_terminal_failure"
    assert result["summary"]["causal_boundary"]["source_files"] == [
        "siblings/integration-log-aianalysis/integration-aianalysis.log"
    ]
