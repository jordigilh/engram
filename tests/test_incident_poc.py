from __future__ import annotations

import json
import io
import zipfile
from pathlib import Path

import pytest

from engram.incident.correlate import correlate
from engram.incident.models import TestFailure as FailureInput
from engram.incident.normalize import extract_rr_id, iter_evidence
from engram.incident.remote import github_api_url, ingest_urls
from engram.incident.retention import failure_history, family_signature, incident_timeline, promote_incident
from engram.incident.service import triage_test_failure


RR_ID = "rr-b82cf07ea239-9e8fb093"


FAILURE_TEXT = f"""[FAILED] AF A2A Phase-Transition Consent Gate
Turn 1 task: 01a06f58-6ec0-73c0-b347-b14f9082b0c2
WorkflowExecution for \"{RR_ID}\" did not complete
[FAILED] in [It] - af_helpers_test.go:624 @ 09/05/26 02:19:35.953
"""


def _write_fixture(root: Path) -> None:
    (root / "crds").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "crds" / "resources.yaml").write_text(
        f"""items:
- kind: RemediationRequest
  metadata:
    name: {RR_ID}
    namespace: kubernaut-system
    creationTimestamp: 2026-09-05T02:14:21Z
  spec:
    targetResource:
      name: memory-eater
      namespace: fp-cg3-cd0770c8
  status:
    overallPhase: Completed
    completionStatus:
      outcome: ManualReviewRequired
- kind: AIAnalysis
  metadata:
    name: ai-{RR_ID}
    creationTimestamp: 2026-09-05T02:14:23Z
  status:
    reason: WorkflowResolutionFailed
    review:
      humanReviewReason: operator_escalation
- kind: RemediationRequest
  metadata:
    name: rr-other
    namespace: kubernaut-system
    creationTimestamp: 2026-09-05T02:15:00Z
  spec:
    targetResource:
      name: memory-eater
      namespace: unrelated-namespace
"""
    )
    (root / "logs" / "controller.log").write_text(
        f"2026-09-05T02:14:25Z workflow resolution failed: {RR_ID}\n"
        "2026-09-05T02:14:25Z unrelated-namespace memory-eater completed\n"
    )


def test_extract_rr_id_from_failure_excerpt() -> None:
    assert extract_rr_id(FAILURE_TEXT) == RR_ID


def test_correlation_excludes_same_named_workload_in_other_namespace(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    evidence = list(iter_evidence(tmp_path))
    failure = FailureInput("run", "job", "consent gate", FAILURE_TEXT)

    related = correlate(failure, evidence)

    assert related
    assert all(RR_ID in item.content for item in related)
    assert not any("unrelated-namespace" in item.content for item in related)


def test_context_returns_bounded_structured_dossier(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    result = triage_test_failure(
        root=tmp_path,
        run_id="33937171903",
        job_id="101229480072",
        test_name="E2E-FP-1899-002",
        failure_text=FAILURE_TEXT,
        max_tokens=1000,
    )

    assert result["rr_id"] == RR_ID
    assert result["summary"]["workflow_resolution_failed"] is True
    assert result["summary"]["manual_review_required"] is True
    assert result["summary"]["operator_escalation"] is True
    assert result["confidence"] == 0.94
    assert result["evidence"]
    assert json.dumps(result, default=str).count("unrelated-namespace") == 0
    assert len(json.dumps(result, default=str)) <= 4000


def _write_pending_interactive_fixture(root: Path) -> None:
    (root / "crds").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    target = "rr-target-1"
    neighbor_a = "rr-neighbor-a-1"
    neighbor_b = "rr-neighbor-b-1"
    (root / "crds" / "sessions.yaml").write_text(
        f"""items:
- kind: InvestigationSession
  metadata:
    name: is-{target}
    uid: is-uid-target
    labels:
      kubernaut.ai/rr-name: {target}
  spec:
    a2aTaskID: a2a-{target}
    remediationRequestRef:
      name: {target}
- kind: AgentSession
  metadata:
    name: as-{target}
  status:
    phase: Pending
    sessionID: session-target
- kind: AgentSession
  metadata:
    name: as-{neighbor_a}
  status:
    phase: Running
    sessionID: session-neighbor-a
"""
    )
    (root / "logs" / "concurrent.log").write_text(
        "\n".join(
            [
                f'2026-09-07T22:42:54Z {{"msg":"StartInvestigation: calling MCP client","rr_id":"{target}","ka_session_id":"session-target"}}',
                f'2026-09-07T22:42:54Z {{"msg":"StartInvestigation: MCP session established","rr_id":"{neighbor_a}","session_id":"session-neighbor-a"}}',
                f'2026-09-07T22:42:54Z {{"msg":"interactive session created with context (pending)","remediation_id":"{target}","session_id":"session-target"}}',
                f'2026-09-07T22:42:55Z {{"msg":"Investigation still in progress, requeuing","agentSession":"as-{target}","rr_id":"{target}"}}',
                f'2026-09-07T22:42:56Z {{"msg":"Extending Analyzing timeout for active interactive session","rr_id":"{target}","sessionID":"session-target"}}',
                f'2026-09-07T22:42:57Z {{"msg":"StartInvestigation: MCP session established","rr_id":"{neighbor_b}","session_id":"session-neighbor-b"}}',
                f'2026-09-07T22:42:58Z {{"msg":"MCP request returned 429","rr_id":"{target}"}}',
            ]
        )
        + "\n"
    )


def test_pending_interactive_lifecycle_isolated_from_concurrent_remediations(tmp_path: Path) -> None:
    _write_pending_interactive_fixture(tmp_path)

    result = triage_test_failure(
        root=tmp_path,
        run_id="run-pending",
        job_id="job-pending",
        test_name="E2E-FP-1899-002",
        failure_text='WorkflowExecution for "rr-target-1" did not complete',
        rr_id="rr-target-1",
    )

    lifecycle = result["summary"]["interactive_lifecycle"]
    assert lifecycle["investigation_session_count"] == 1
    assert lifecycle["agent_session_count"] == 1
    assert lifecycle["pending_agent_sessions"] == 1
    assert lifecycle["af_start_events"] == 1
    assert lifecycle["mcp_established_events"] == 0
    assert lifecycle["ka_pending_events"] == 1
    assert lifecycle["requeue_events"] == 1
    assert lifecycle["timeout_extension_events"] == 1
    assert lifecycle["earliest_causal_boundary"] == "AF-to-KA interactive session startup/handoff"
    assert lifecycle["expected_missing_events"] == [
        "MCP session established",
        "interactive investigation handoff completion",
    ]
    assert lifecycle["possible_contributors"] == ["concurrent MCP 429/rate-limit activity"]
    assert all("neighbor" not in json.dumps(item) for item in result["evidence"])


def test_missing_rr_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RR ID"):
        triage_test_failure(
            root=tmp_path,
            run_id="run",
            job_id="job",
            test_name="test",
            failure_text="WorkflowExecution did not complete",
        )


def test_github_urls_resolve_to_authenticated_api_downloads() -> None:
    job_url = "https://github.com/jordigilh/kubernaut/actions/runs/33937171903/job/101229480072"
    artifact_url = "https://github.com/jordigilh/kubernaut/actions/runs/33937171903/artifacts/9961133141"

    assert github_api_url(job_url) == (
        "https://api.github.com/repos/jordigilh/kubernaut/actions/jobs/101229480072/logs",
        "job_logs",
    )
    assert github_api_url(artifact_url) == (
        "https://api.github.com/repos/jordigilh/kubernaut/actions/artifacts/9961133141/zip",
        "artifact",
    )


def test_ingest_urls_writes_log_and_extracts_archive(tmp_path: Path, monkeypatch) -> None:
    artifact = io.BytesIO()
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("must-gather/events.yaml", "kind: Event\nmessage: rr-b82cf07ea239-9e8fb093\n")
    downloads = {
        "https://api.github.com/repos/jordigilh/kubernaut/actions/jobs/101229480072/logs": b"rr-b82cf07ea239-9e8fb093 failed\n",
        "https://api.github.com/repos/jordigilh/kubernaut/actions/artifacts/9961133141/zip": artifact.getvalue(),
    }
    monkeypatch.setattr("engram.incident.remote._download", downloads.__getitem__)

    manifest = ingest_urls(
        "https://github.com/jordigilh/kubernaut/actions/runs/33937171903/job/101229480072",
        "https://github.com/jordigilh/kubernaut/actions/runs/33937171903/artifacts/9961133141",
        destination=tmp_path,
    )

    assert manifest["artifact_files"] == 1
    assert manifest["indexed_evidence"] == 2
    assert manifest["index_file"] == ".engram/evidence-index.jsonl"
    assert (tmp_path / "ci" / "job.log").exists()
    assert (tmp_path / "must-gather" / "must-gather" / "events.yaml").exists()


def test_promote_incident_aggregates_repeated_failure_and_links_changes(tmp_path: Path) -> None:
    _write_fixture(tmp_path / "evidence")
    context = triage_test_failure(
        root=tmp_path / "evidence",
        run_id="run-1",
        job_id="job-1",
        test_name="E2E-FP-1899-002",
        failure_text=FAILURE_TEXT,
    )
    db_path = tmp_path / "retention.sqlite3"
    change = {"commit_sha": "abc123", "change_type": "source_commit", "component": "aianalysis"}

    first = promote_incident(context, db_path, validated_by="test-owner", changes=[change])
    second = promote_incident(context, db_path, changes=[change])

    assert first["new_incident"] is True
    assert second["new_incident"] is False
    history = failure_history(db_path, family_signature(context))
    assert len(history) == 1
    assert incident_timeline(db_path, first["incident_id"])["changes"][0]["commit_sha"] == "abc123"
