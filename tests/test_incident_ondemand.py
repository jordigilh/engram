from __future__ import annotations

from pathlib import Path

from engram.incident import ondemand


RR_A = "rr-b82cf07ea239-9e8fb093"
RR_B = "rr-aaaaaaaaaaaaaaaa-11111111"

FAILURE_A = f"""[FAILED] AF A2A Phase-Transition Consent Gate
Turn 1 task: 01a06f58-6ec0-73c0-b347-b14f9082b0c2
WorkflowExecution for "{RR_A}" did not complete
[FAILED] in [It] - af_helpers_test.go:624 @ 09/05/26 02:19:35.953
<< Timeline
"""

FAILURE_B = f"""[FAILED] second consent gate probe
WorkflowExecution for "{RR_B}" did not complete
[FAILED] in [It] - other_test.go:10 @ 09/05/26 02:20:00.000
<< Timeline
"""


def _write_must_gather(root: Path, rr_id: str = RR_A) -> None:
    (root / "must-gather").mkdir(parents=True, exist_ok=True)
    (root / "must-gather" / "resources.yaml").write_text(
        f"""items:
- kind: RemediationRequest
  metadata:
    name: {rr_id}
    namespace: kubernaut-system
    creationTimestamp: 2026-09-05T02:14:21Z
  spec:
    targetResource:
      name: memory-eater
      namespace: fp-cg3-cd0770c8
  status:
    overallPhase: Completed
"""
    )


def _discovery(job_id: str = "101", run_id: str = "100", artifact: dict | None = "default") -> dict:
    record = None
    if artifact == "default":
        record = {"artifact_id": 7, "name": "fullpipeline-e2e-diagnostics-100", "expired": False, "url": "https://example/artifact"}
    elif artifact:
        record = artifact
    return {
        "run": {"run_id": run_id, "workflow": "E2E", "conclusion": "failure", "head_sha": "abc", "head_branch": "main"},
        "job": {"job_id": job_id, "name": "e2e (fullpipeline)", "conclusion": "failure"},
        "log_url": f"https://github.com/jordigilh/kubernaut/actions/runs/{run_id}/job/{job_id}",
        "artifact": record,
        "artifact_url": record["url"] if record else None,
        "warnings": [],
    }


def test_ids_parsed_from_job_page_url_with_pr_query() -> None:
    run_id, job_id = ondemand._ids_from_job_url(
        "https://github.com/jordigilh/kubernaut/actions/runs/34236030006/job/102110076210?pr=2379"
    )
    assert (run_id, job_id) == ("34236030006", "102110076210")


def test_generate_rca_builds_dossier_from_discovered_urls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ondemand, "discover_ci_urls", lambda * _args, **_kwargs: _discovery())

    def fake_ingest(test_log_url: str, must_gather_url: str, destination: Path | None = None) -> dict:
        root = destination or tmp_path
        (root / "ci").mkdir(parents=True, exist_ok=True)
        (root / "ci" / "job.log").write_text(FAILURE_A)
        _write_must_gather(root)
        return {"root": str(root), "job_log": "ci/job.log", "artifact_files": 1,
                "sources": {"test_log_url": test_log_url, "must_gather_url": must_gather_url}}

    monkeypatch.setattr("engram.incident.remote.ingest_urls", fake_ingest)

    result = ondemand.generate_rca(run_id="100", repository="jordigilh/kubernaut", destination=tmp_path / "out")

    assert result["run_id"] == "100"
    assert result["metrics"]["dossiers_generated"] == 1
    assert result["dossiers"][0]["rr_id"] == RR_A
    assert result["degraded"] is None
    assert result["failure_manifest"][0]["rr_id"] == RR_A


def test_generate_rca_timeout_without_rr_returns_degraded(tmp_path: Path, monkeypatch) -> None:
    discovery = _discovery(artifact=None)
    discovery["warnings"] = ["run lists no artifacts; continuing with log-only RCA"]
    monkeypatch.setattr(ondemand, "discover_ci_urls", lambda *_args, **_kwargs: discovery)
    log_text = "2026-09-08T00:00:00Z starting e2e\nThe job has exceeded the maximum execution time of 25m0s\nThe operation was canceled.\n"
    monkeypatch.setattr("engram.incident.remote._download", lambda _url: log_text.encode())

    result = ondemand.generate_rca(run_id="34236030006", job_id="102110076210", destination=tmp_path / "out")

    assert result["dossiers"] == []
    assert result["degraded"] is not None
    assert result["degraded"]["classification"] == "timeout"
    assert result["degraded"]["timeout_detected"] is True
    assert result["degraded"]["cancelled_detected"] is True
    assert result["sources"]["log_only"] is True
    assert result["degraded"]["log_tail"]


def test_generate_rca_explicit_urls_skip_discovery(tmp_path: Path, monkeypatch) -> None:
    def no_discovery(**_kwargs):
        raise AssertionError("discovery must not run when both URLs are pinned")

    monkeypatch.setattr(ondemand, "discover_ci_urls", no_discovery)

    def fake_ingest(test_log_url: str, must_gather_url: str, destination: Path | None = None) -> dict:
        root = destination or tmp_path
        (root / "ci").mkdir(parents=True, exist_ok=True)
        (root / "ci" / "job.log").write_text(FAILURE_A)
        _write_must_gather(root)
        return {"root": str(root), "job_log": "ci/job.log", "artifact_files": 1,
                "sources": {"test_log_url": test_log_url, "must_gather_url": must_gather_url}}

    monkeypatch.setattr("engram.incident.remote.ingest_urls", fake_ingest)

    result = ondemand.generate_rca(
        run_id="100",
        job_id="101",
        test_log_url="https://github.com/jordigilh/kubernaut/actions/runs/100/job/101",
        must_gather_url="https://github.com/jordigilh/kubernaut/actions/runs/100/artifacts/7",
        destination=tmp_path / "out",
    )

    assert result["metrics"]["dossiers_generated"] == 1


def test_generate_rca_truncates_to_max_dossiers_with_warning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ondemand, "discover_ci_urls", lambda *_args, **_kwargs: _discovery())

    def fake_ingest(test_log_url: str, must_gather_url: str, destination: Path | None = None) -> dict:
        root = destination or tmp_path
        (root / "ci").mkdir(parents=True, exist_ok=True)
        (root / "ci" / "job.log").write_text(FAILURE_A + "\n" + FAILURE_B)
        _write_must_gather(root, RR_A)
        (root / "must-gather" / "more.yaml").write_text(f"kind: Event\nmessage: {RR_B}\n")
        return {"root": str(root), "job_log": "ci/job.log", "artifact_files": 2,
                "sources": {"test_log_url": test_log_url, "must_gather_url": must_gather_url}}

    monkeypatch.setattr("engram.incident.remote.ingest_urls", fake_ingest)

    result = ondemand.generate_rca(run_id="100", max_dossiers=1, destination=tmp_path / "out")

    assert len(result["dossiers"]) == 1
    assert any("max_dossiers=1" in warning for warning in result["warnings"])


def test_rca_gateway_allowlist_includes_generate_rca() -> None:
    from engram.pipeline import engram_gateway

    allowed = engram_gateway.RELEVANT_TOOLS_BY_BACKEND["rca"]
    assert "generate_rca" in allowed
    assert {"ingest_test_run", "triage_test_failure", "get_evidence"} <= allowed
