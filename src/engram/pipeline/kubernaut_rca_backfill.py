"""Generate deterministic RCA dossiers from a corpus inventory manifest."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from engram.incident.remote import ingest_urls
from engram.incident.normalize import iter_evidence
from engram.incident.postgres_retention import promote_incident_pg
from engram.incident.quality import validate_dossier
from engram.incident.retention import promote_incident
from engram.incident.service import triage_test_failure
from engram.incident.testlog import classify_failure, deduplicate_failures, extract_test_failures, infer_rr_ids


def _select_artifact(run: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = [item for item in run.get("relevant_artifacts", []) if not item.get("expired")]
    artifacts.sort(key=lambda item: (0 if re.search(r"must.?gather", item.get("name", ""), re.I) else 1, item.get("name", "")))
    return artifacts[0] if artifacts else None


def backfill(
    inventory: dict[str, Any],
    output: Path,
    limit: int | None = None,
    db_path: Path | None = None,
    pg_url: str | None = None,
    promote: bool = True,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    runs = inventory.get("runs", [])[:limit] if limit else inventory.get("runs", [])
    metrics = {
        "runs_scanned": 0,
        "jobs_scanned": 0,
        "jobs_downloaded": 0,
        "failures_extracted": 0,
        "unique_failures": 0,
        "duplicate_failures_collapsed": 0,
        "rr_linked_failures": 0,
        "dossiers_generated": 0,
        "failures_without_rr": 0,
        "fallback_anchor_candidates": 0,
        "fallback_rr_resolved": 0,
        "environment_setup_failures": 0,
        "unresolved_failures": 0,
        "errors": [],
        "candidates_promoted": 0,
        "promotion_skipped_quality": 0,
        "promotion_errors": 0,
    }
    failure_manifest: list[dict[str, Any]] = []
    for run in runs:
        metrics["runs_scanned"] += 1
        artifact = _select_artifact(run)
        if not artifact:
            metrics["errors"].append({"run_id": run.get("run_id"), "error": "no usable must-gather artifact"})
            continue
        run_output = output / str(run["run_id"])
        for job in run.get("primary_failed_jobs", run.get("failed_jobs", [])):
            metrics["jobs_scanned"] += 1
            with tempfile.TemporaryDirectory(prefix=f"engram-rca-{run['run_id']}-") as temp_dir:
                try:
                    ingested = ingest_urls(job["log_url"], artifact["url"], destination=Path(temp_dir))
                    metrics["jobs_downloaded"] += 1
                    log_text = (Path(temp_dir) / ingested["job_log"]).read_text(encoding="utf-8", errors="replace")
                    failures = extract_test_failures(log_text)
                    indexed_evidence = list(iter_evidence(Path(temp_dir)))
                    metrics["failures_extracted"] += len(failures)
                    unique_failures = deduplicate_failures(failures)
                    metrics["unique_failures"] += len(unique_failures)
                    metrics["duplicate_failures_collapsed"] += len(failures) - len(unique_failures)
                    for failure in unique_failures:
                        failure_manifest.append({
                            "run_id": run["run_id"],
                            "job_id": job["job_id"],
                            "test_name": failure["test_name"],
                            "rr_id": failure["rr_id"],
                            "rr_ids": failure["rr_ids"],
                            "failure_timestamp": failure["failure_timestamp"],
                            "task_ids": failure["task_ids"],
                            "issue_refs": failure["issue_refs"],
                            "resources": failure["resources"],
                            "namespaces": failure["namespaces"],
                            "failure_excerpt": failure["failure_text"][:2000],
                            "fallback_anchor_candidate": bool(
                                not failure["rr_id"]
                                and (
                                    failure["resources"]
                                    or any(namespace.lower().startswith("fp-") for namespace in failure["namespaces"])
                                )
                            ),
                            "classification": classify_failure(failure),
                        })
                        if not failure["rr_id"]:
                            metrics["failures_without_rr"] += 1
                            inferred = infer_rr_ids(failure, indexed_evidence)
                            if len(inferred) == 1:
                                failure["rr_id"] = inferred[0]
                                failure_manifest[-1]["rr_id"] = inferred[0]
                                failure_manifest[-1]["rr_ids"] = inferred
                                metrics["fallback_rr_resolved"] += 1
                            if failure["resources"] or any(
                                namespace.lower().startswith("fp-") for namespace in failure["namespaces"]
                            ):
                                metrics["fallback_anchor_candidates"] += 1
                            if not failure["rr_id"]:
                                classification = classify_failure(failure)
                                if classification == "environment_setup_failure":
                                    metrics["environment_setup_failures"] += 1
                                else:
                                    metrics["unresolved_failures"] += 1
                                continue
                        metrics["rr_linked_failures"] += 1
                        context = triage_test_failure(
                            root=Path(temp_dir),
                            run_id=str(run["run_id"]),
                            job_id=str(job["job_id"]),
                            test_name=failure["test_name"],
                            failure_text=failure["failure_text"],
                            rr_id=failure["rr_id"],
                            branch=run.get("target_branch", "main"),
                            project="kubernaut",
                        )
                        destination_path = run_output / f"{failure['rr_id']}.json"
                        destination_path.parent.mkdir(parents=True, exist_ok=True)
                        destination_path.write_text(json.dumps(context, indent=2, default=str) + "\n")
                        metrics["dossiers_generated"] += 1
                        if not promote:
                            continue
                        quality_problems = validate_dossier(context)
                        if quality_problems:
                            metrics["promotion_skipped_quality"] += 1
                            continue
                        change = {
                            "commit_sha": run.get("commit_sha"),
                            "change_type": "ci_run_commit",
                            "component": run.get("repository"),
                            "relation": "incident_commit",
                            "confidence": 0.5,
                        }
                        try:
                            if pg_url:
                                promote_incident_pg(
                                    context,
                                    pg_url,
                                    validated_by="ci-candidate",
                                    changes=[change],
                                )
                            else:
                                promote_incident(
                                    context,
                                    db_path or output / "retention.sqlite3",
                                    validated_by="ci-candidate",
                                    changes=[change],
                                )
                            metrics["candidates_promoted"] += 1
                        except Exception as error:  # noqa: BLE001 - retain dossier even if store is unavailable
                            metrics["promotion_errors"] += 1
                            metrics["errors"].append({"run_id": run.get("run_id"), "rr_id": context.get("rr_id"), "error": str(error)})
                except Exception as error:  # noqa: BLE001 - corpus backfill continues per job
                    metrics["errors"].append({"run_id": run.get("run_id"), "job_id": job.get("job_id"), "error": str(error)})
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
    result = {"inventory": {"repository": inventory.get("repository"), "runs": len(runs)}, "metrics": metrics}
    (output / "baseline-metrics.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "failure-anchors.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in failure_manifest)
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--pg-url", default=None)
    parser.add_argument(
        "--dossiers-only",
        action="store_true",
        help="Generate and uploadable dossiers only; do not create or promote retention records",
    )
    args = parser.parse_args()
    result = backfill(
        json.loads(args.inventory.read_text()),
        args.output,
        args.limit,
        args.db,
        args.pg_url,
        promote=not args.dossiers_only,
    )
    print(json.dumps(result["metrics"]))


if __name__ == "__main__":
    main()
