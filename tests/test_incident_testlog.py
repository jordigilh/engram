from engram.incident.models import Evidence
from engram.incident.testlog import classify_failure, deduplicate_failures, extract_test_failures, infer_rr_ids


def test_extracts_failure_block_and_rr_id() -> None:
    text = """[FAILED] [315.487 seconds] AF A2A consent gate [E2E-FP-1899-002]
  Timeline >>
  Turn 1 task: 01a06f58-6ec0-73c0-b347-b14f9082b0c2
  WorkflowExecution for \"rr-b82cf07ea239-9e8fb093\" did not complete
  [FAILED] in [It] @ 09/05/26 02:19:35.953
  << Timeline
"""

    failures = extract_test_failures(text)

    assert len(failures) == 1
    assert failures[0]["rr_id"] == "rr-b82cf07ea239-9e8fb093"
    assert failures[0]["failure_timestamp"] == "09/05/26 02:19:35.953"
    assert "01a06f58-6ec0-73c0-b347-b14f9082b0c2" in failures[0]["task_ids"]
    assert "E2E-FP-1899-002" in failures[0]["issue_refs"]
    assert failures[0]["namespaces"] == []


def test_extracts_multiple_failures_without_merging_blocks() -> None:
    text = """[FAILED] first test [FP-1]
  [FAILED] in [It]
  << Timeline
[FAILED] second test [FP-2]
  [FAILED] in [It]
  << Timeline
"""

    failures = extract_test_failures(text)

    assert [failure["test_name"] for failure in failures] == ["first test [FP-1]", "second test [FP-2]"]


def test_infer_rr_requires_one_namespace_resource_match() -> None:
    failure = {"namespaces": ["fp-test-123"], "resources": ["Deployment/memory-eater"]}
    evidence = [
        Evidence("1", "crd.yaml", "resource", 'namespace: fp-test-123\nkind: Deployment\nname: memory-eater\nrr-abc-123', namespace="fp-test-123"),
        Evidence("2", "other.yaml", "resource", 'namespace: fp-other\nkind: Deployment\nname: memory-eater\nrr-def-456', namespace="fp-other"),
    ]

    assert infer_rr_ids(failure, evidence) == ["rr-abc-123"]


def test_classifies_environment_failures_without_forcing_incident_identity() -> None:
    assert classify_failure({"failure_text": "image loads failed: preload failed"}) == "environment_setup_failure"
    assert classify_failure({"failure_text": "test failed", "resources": [], "namespaces": []}) == "unresolved_failure"


def test_deduplicates_repeated_synchronized_setup_failures() -> None:
    failures = [
        {"test_name": "SynchronizedBeforeSuite failed", "failure_text": "SynchronizedBeforeSuite failed on process #1"},
        {"test_name": "SynchronizedBeforeSuite failed", "failure_text": "SynchronizedBeforeSuite failed on process #2"},
        {"test_name": "different", "failure_text": "image loads failed"},
    ]

    assert len(deduplicate_failures(failures)) == 2
