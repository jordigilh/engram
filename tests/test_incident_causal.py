from engram.incident.models import Evidence, TestFailure as FailureInput
from engram.incident.normalize import extract_target_resource
from engram.incident.service import build_context
from engram.incident.testlog import infer_rr_ids


RR_ID = "rr-a9e386cdec65-adaee9ca"
BLOCKER_ID = "we-rr-f6f8af3eb958-835c6954"


def _evidence(evidence_id: str, content: str, structured: dict) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_file="crds/resources.yaml",
        evidence_type="kubernetes_resource",
        content=content,
        metadata={"structured": structured},
    )


def _resource_busy_evidence() -> list[Evidence]:
    target = {"kind": "Deployment", "namespace": "kubernaut-system", "name": "memory-eater"}
    return [
        _evidence(
            "rr-evidence",
            RR_ID,
            {
                "kind": "RemediationRequest",
                "name": RR_ID,
                "cluster_id": "remote-cluster",
                "target_resource": target,
                "block_reason": "ResourceBusy",
                "blocking_workflow_execution": BLOCKER_ID,
            },
        ),
        _evidence(
            "wfe-evidence",
            f"{BLOCKER_ID} unsupported execution engine",
            {
                "kind": "WorkflowExecution",
                "name": BLOCKER_ID,
                "cluster_id": "prod-west",
                "target_resource": target,
                "workflow_engine": "tekton",
                "failure_reason": "UnsupportedEngine",
            },
        ),
    ]


def test_namespace_kind_name_target_is_parsed_without_swapping_fields() -> None:
    assert extract_target_resource("kubernaut-system/Deployment/memory-eater") == {
        "kind": "Deployment",
        "namespace": "kubernaut-system",
        "name": "memory-eater",
    }
    assert extract_target_resource("/home/runner/work/kubernaut/kubernaut/test.go") is None


def test_unique_structured_resource_busy_blocker_infers_rr() -> None:
    evidence = _resource_busy_evidence()

    assert infer_rr_ids(
        {"failure_text": "Timed out waiting for the remediation", "resources": [], "namespaces": []},
        evidence,
    ) == [RR_ID]


def test_context_reports_cross_cluster_lock_collision() -> None:
    result = build_context(
        FailureInput(
            run_id="run-fleet",
            job_id="job-fleet",
            test_name="E2E-FLEET-004",
            failure_text="Timed out waiting for the remediation",
        ),
        _resource_busy_evidence(),
    )

    finding = result["summary"]["causal_finding"]
    assert result["rr_id"] == RR_ID
    assert finding["classification"] == "cross_cluster_target_lock_collision"
    assert finding["blocking_workflow_execution"] == BLOCKER_ID
    assert finding["request_cluster_id"] == "remote-cluster"
    assert finding["blocking_cluster_id"] == "prod-west"
    assert finding["same_target"] is True
    assert finding["different_clusters"] is True
