"""One-shot on-demand RCA from a CI run when the CI dossier job did not produce one.

The stateless CI dossier phase (``kubernaut_rca_backfill --dossiers-only``) only
runs when the E2E matrix result is exactly ``failure``. A timed-out or
cancelled job -- e.g. a 25m execution cap with "operation was canceled" and no
``[FAILED]`` block -- skips that phase, so no ``rca-dossiers-<run_id>``
artifact exists for upstream to consume. This module gives the interactive
MCP path the same deterministic pipeline the batch job uses, in a single
call:

1. resolve the job log + must-gather artifact URLs (explicit URLs win;
   otherwise discover them from ``run_id``/``job_id`` via the GitHub API),
2. download and index them transiently (must-gather optional -- a missing or
   expired artifact degrades to a log-only RCA instead of an error),
3. extract failures from the job log (Ginkgo blocks, deduplicated, with
   RR-ID inference from must-gather evidence),
4. build bounded dossiers for the RR-linked failures via
   :func:`engram.incident.service.triage_test_failure`,
5. fall back to a degraded timeout/log analysis when no RR-linked failure
   exists, so a timeout/cancelled run still returns something actionable.

Artifact/job selection regexes intentionally mirror
``engram.pipeline.kubernaut_corpus_inventory`` (must-gather name match,
downstream ``summary``/``merge-gate``/``report`` exclusion). Keep them in
sync by hand if either side changes.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .branch_scope import normalize_branch
from .normalize import iter_evidence
from .testlog import classify_failure, deduplicate_failures, extract_test_failures, infer_rr_ids

MUST_GATHER_NAME_RE = re.compile(r"must.?gather|fullpipeline|coverage-e2e-fullpipeline", re.IGNORECASE)
DOWNSTREAM_JOB_RE = re.compile(r"summary|merge.?gate|report", re.IGNORECASE)

_TIMEOUT_MARKERS = (
    "exceeded the maximum execution time",
    "exceeded maximum execution time",
    "the operation was canceled",
    "operation was canceled",
    "canceling the workflow",
    "timed out",
    "context deadline exceeded",
    "timed out waiting",
    "no output received",
    "lost communication with the server",
)
_CANCELLED_MARKERS = (
    "the operation was canceled",
    "operation was canceled",
    "workflow execution was canceled",
    "canceling the workflow",
    "cancelled",
)

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_RUNNER_PREFIX_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s+")
_JOB_URL_RE = re.compile(r"^/([^/]+)/([^/]+)/actions/runs/(\d+)(?:/job/(\d+))?")
_REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

LOG_TAIL_LINES = 80
LOG_TAIL_MAX_CHARS_PER_LINE = 500


def _github_token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _api_get(repository: str, path: str, token: str, **query: int) -> dict[str, Any]:
    params = urllib.parse.urlencode({key: str(value) for key, value in query.items()})
    url = f"https://api.github.com/repos/{repository}/{path}"
    if params:
        url += f"?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "engram-kubernaut-rca-ondemand",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _validate_repository(repository: str) -> str:
    if not _REPO_RE.match(repository or ""):
        raise ValueError(f"repository must look like 'owner/repo', got {repository!r}")
    return repository


def _ids_from_job_url(url: str) -> tuple[str | None, str | None]:
    """Extract (run_id, job_id) from a GitHub Actions run/job page URL.

    Tolerates query strings such as ``?pr=2379`` on the linked run page.
    Returns (None, None) when the URL is not a run/job page.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:  # malformed URL input degrades to no IDs
        return None, None
    if parsed.netloc != "github.com":
        return None, None
    match = _JOB_URL_RE.match(parsed.path)
    if not match:
        return None, None
    if "/job/" in parsed.path:
        return match.group(3), match.group(4)
    return match.group(3), None


def discover_ci_urls(
    repository: str,
    run_id: str | int,
    job_id: str | int | None = None,
    artifact_hint: str | None = None,
) -> dict[str, Any]:
    """Resolve job-log and must-gather artifact URLs for one CI run via the API.

    Returns ``{"job": ..., "log_url": ..., "artifact": ...|None,
    "artifact_url": ...|None, "run": ..., "warnings": [...]}``. A missing
    must-gather artifact is a warning, not an error -- the caller degrades
    to a log-only RCA.
    """
    repository = _validate_repository(repository)
    token = _github_token()
    if not token:
        raise ValueError("GH_TOKEN or GITHUB_TOKEN is required for run discovery")
    run_id = str(run_id)
    warnings: list[str] = []

    run = _api_get(repository, f"actions/runs/{run_id}", token)
    jobs = _api_get(repository, f"actions/runs/{run_id}/jobs", token, per_page=100).get("jobs", [])
    artifacts = _api_get(repository, f"actions/runs/{run_id}/artifacts", token, per_page=100).get("artifacts", [])

    job: dict[str, Any] | None = None
    if job_id is not None:
        wanted = str(job_id)
        job = next((item for item in jobs if str(item.get("id")) == wanted), None)
        if job is None:
            raise ValueError(f"job {wanted} not found in run {run_id} ({len(jobs)} jobs listed)")
    else:
        failed = [item for item in jobs if item.get("conclusion") in {"failure", "cancelled", "timed_out"}]
        primary = [item for item in failed if not DOWNSTREAM_JOB_RE.search(item.get("name") or "")]
        pool = primary or failed or jobs
        if not pool:
            raise ValueError(f"run {run_id} lists no jobs")
        job = pool[0]
        if len(pool) > 1:
            warnings.append(f"{len(pool)} candidate jobs; using {job.get('name')} ({job.get('id')})")

    log_url = f"https://github.com/{repository}/actions/runs/{run_id}/job/{job['id']}"

    artifact_records = [
        {
            "artifact_id": item["id"],
            "name": item.get("name"),
            "expired": item.get("expired", False),
            "size_in_bytes": item.get("size_in_bytes", 0),
            "created_at": item.get("created_at"),
            "url": f"https://github.com/{repository}/actions/runs/{run_id}/artifacts/{item['id']}",
        }
        for item in artifacts
    ]
    candidates = [item for item in artifact_records if not item.get("expired")]
    if artifact_hint:
        hinted = [item for item in candidates if artifact_hint.lower() in (item.get("name") or "").lower()]
        if hinted:
            candidates = hinted
        else:
            warnings.append(f"artifact_hint {artifact_hint!r} matched nothing; falling back to must-gather match")
    must_gather = [item for item in candidates if MUST_GATHER_NAME_RE.search(item.get("name") or "")]
    artifact = (must_gather or candidates or [None])[0]
    if must_gather:
        pass  # exact must-gather match: no warning needed
    elif candidates:
        warnings.append(f"no must-gather-named artifact; using {artifact['name']!r} instead")
    elif artifact_records:
        warnings.append("all artifacts are expired; continuing with log-only RCA")
        artifact = None
    else:
        warnings.append("run lists no artifacts; continuing with log-only RCA")
        artifact = None

    return {
        "run": {
            "run_id": run_id,
            "workflow": run.get("name"),
            "conclusion": run.get("conclusion"),
            "head_sha": run.get("head_sha"),
            "head_branch": run.get("head_branch"),
        },
        "job": {
            "job_id": str(job["id"]),
            "name": job.get("name"),
            "conclusion": job.get("conclusion"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
        },
        "log_url": log_url,
        "artifact": artifact,
        "artifact_url": artifact["url"] if artifact else None,
        "warnings": warnings,
    }


def _clean_log_line(line: str) -> str:
    return _RUNNER_PREFIX_RE.sub("", _ANSI_RE.sub("", line)).replace("\r", "").rstrip()


def _log_tail(log_text: str, limit: int = LOG_TAIL_LINES) -> list[str]:
    lines = [_clean_log_line(line) for line in log_text.splitlines()]
    lines = [line for line in lines if line.strip()]
    tail = lines[-limit:]
    return [line[:LOG_TAIL_MAX_CHARS_PER_LINE] for line in tail]


def _detect_timeout_cancelled(log_text: str) -> tuple[bool, bool]:
    lowered = log_text.lower()
    timeout = any(marker in lowered for marker in _TIMEOUT_MARKERS)
    cancelled = any(marker in lowered for marker in _CANCELLED_MARKERS)
    return timeout, cancelled


def _evidence_summary(evidence: list[Any]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    namespaces: dict[str, int] = {}
    for item in evidence:
        by_type[item.evidence_type] = by_type.get(item.evidence_type, 0) + 1
        if item.severity:
            by_severity[item.severity] = by_severity.get(item.severity, 0) + 1
        if item.namespace:
            namespaces[item.namespace] = namespaces.get(item.namespace, 0) + 1
    timestamps = [item.timestamp for item in evidence if item.timestamp]
    top_namespaces = sorted(namespaces.items(), key=lambda entry: (-entry[1], entry[0]))[:10]
    return {
        "indexed_evidence": len(evidence),
        "by_type": by_type,
        "by_severity": by_severity,
        "top_namespaces": [{"namespace": name, "count": count} for name, count in top_namespaces],
        "earliest": min(timestamps).isoformat() if timestamps else None,
        "latest": max(timestamps).isoformat() if timestamps else None,
    }


def _failure_manifest_entry(
    run_id: str, job_id: str, failure: dict[str, Any], log_excerpt_limit: int = 2000
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "job_id": job_id,
        "test_name": failure["test_name"],
        "rr_id": failure["rr_id"],
        "rr_ids": failure["rr_ids"],
        "failure_timestamp": failure["failure_timestamp"],
        "task_ids": failure["task_ids"],
        "issue_refs": failure["issue_refs"],
        "resources": failure["resources"],
        "namespaces": failure["namespaces"],
        "failure_excerpt": failure["failure_text"][:log_excerpt_limit],
        "fallback_anchor_candidate": bool(
            not failure["rr_id"]
            and (failure["resources"] or any(ns.lower().startswith("fp-") for ns in failure["namespaces"]))
        ),
        "classification": classify_failure(failure),
    }


def generate_rca(
    *,
    run_id: str | int | None = None,
    job_id: str | int | None = None,
    repository: str = "jordigilh/kubernaut",
    branch: str = "main",
    project: str = "kubernaut",
    test_log_url: str | None = None,
    must_gather_url: str | None = None,
    artifact_hint: str | None = None,
    commit_sha: str | None = None,
    workflow: str | None = None,
    max_tokens: int = 12000,
    max_dossiers: int = 3,
    destination: Path | None = None,
) -> dict[str, Any]:
    """Download one CI run and build bounded RCA dossiers in a single call.

    Either pass explicit ``test_log_url``/``must_gather_url`` (the
    ``ingest_test_run`` URL shapes) or a ``run_id`` with an optional
    ``job_id`` for API discovery. ``run_id``/``job_id`` may also be parsed
    out of ``test_log_url`` when it is a run/job page URL carrying extra
    query parameters (e.g. ``?pr=2379``).
    """
    from .remote import ingest_urls
    from .service import triage_test_failure

    repository = _validate_repository(repository)
    scope_branch = normalize_branch(branch)
    warnings: list[str] = []

    if run_id is None and test_log_url:
        parsed_run, parsed_job = _ids_from_job_url(test_log_url)
        run_id = run_id or parsed_run
        job_id = job_id or parsed_job
    if test_log_url is None and run_id is None:
        raise ValueError("provide run_id (optionally job_id) or test_log_url")

    discovery: dict[str, Any] | None = None
    if test_log_url is None or (must_gather_url is None and run_id is not None):
        # Discovery fills in whichever side the caller did not pin explicitly.
        discovery = discover_ci_urls(repository, str(run_id), job_id, artifact_hint)
        warnings.extend(discovery["warnings"])
        run_id = str(run_id)
        job_id = str(discovery["job"]["job_id"])
        test_log_url = test_log_url or discovery["log_url"]
        if must_gather_url is None:
            must_gather_url = discovery["artifact_url"]
            if must_gather_url is None:
                warnings.append("no usable must-gather artifact; continuing with log-only RCA")
        commit_sha = commit_sha or discovery["run"].get("head_sha")
        workflow = workflow or discovery["run"].get("workflow")
    else:
        if run_id is None or job_id is None:
            raise ValueError("when passing explicit URLs without discovery, provide run_id and job_id")
        run_id, job_id = str(run_id), str(job_id)

    assert test_log_url is not None
    root = destination or Path(tempfile.mkdtemp(prefix="engram-kubernaut-rca-ondemand-"))
    root.mkdir(parents=True, exist_ok=True)

    log_only = not must_gather_url
    if log_only:
        from .remote import github_api_url

        api_url, kind = github_api_url(test_log_url)
        if kind != "job_logs":
            raise ValueError("test_log_url must be a GitHub Actions job URL")
        from .remote import _download as download_raw

        log_data = download_raw(api_url)
        log_path = root / "ci" / "job.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(log_data)
        manifest = {
            "root": str(root),
            "job_log": str(log_path.relative_to(root)),
            "artifact_files": 0,
            "indexed_evidence": 0,
            "sources": {"test_log_url": test_log_url, "must_gather_url": None},
        }
    else:
        manifest = ingest_urls(test_log_url, must_gather_url, destination=root)

    log_text = (root / manifest["job_log"]).read_text(encoding="utf-8", errors="replace")
    evidence = list(iter_evidence(root))
    manifest["indexed_evidence"] = len(evidence)

    failures = extract_test_failures(log_text)
    unique_failures = deduplicate_failures(failures)
    failure_manifest = [_failure_manifest_entry(str(run_id), str(job_id), failure) for failure in unique_failures]

    # RR-ID inference against must-gather evidence, same as the batch backfill.
    resolved = 0
    for failure, entry in zip(unique_failures, failure_manifest):
        if not failure["rr_id"]:
            inferred = infer_rr_ids(failure, evidence)
            if len(inferred) == 1:
                failure["rr_id"] = inferred[0]
                failure["rr_ids"] = inferred
                entry["rr_id"] = inferred[0]
                entry["rr_ids"] = inferred
                entry["classification"] = classify_failure(failure)
                resolved += 1

    dossiers: list[dict[str, Any]] = []
    skipped_without_rr = 0
    for failure in unique_failures:
        if not failure["rr_id"]:
            skipped_without_rr += 1
            continue
        if len(dossiers) >= max(1, max_dossiers):
            break
        dossiers.append(
            triage_test_failure(
                root=root,
                run_id=str(run_id),
                job_id=str(job_id),
                test_name=failure["test_name"],
                failure_text=failure["failure_text"],
                rr_id=failure["rr_id"],
                branch=scope_branch,
                project=project,
                max_tokens=min(max_tokens, 20000),
            )
        )
    if len([failure for failure in unique_failures if failure["rr_id"]]) > len(dossiers):
        warnings.append(f"dossier list truncated to max_dossiers={max_dossiers}")

    degraded: dict[str, Any] | None = None
    if not dossiers:
        timeout, cancelled = _detect_timeout_cancelled(log_text)
        if skipped_without_rr:
            warnings.append(f"{skipped_without_rr} failure block(s) without an RR ID were skipped")
        if timeout:
            classification = "timeout"
        elif cancelled:
            classification = "cancelled"
        elif failure_manifest:
            classification = classify_failure(
                {"rr_id": None, "resources": [], "namespaces": [], "failure_text": log_text[-4000:]}
            )
            classification = failure_manifest[0]["classification"]
        else:
            classification = "unresolved_failure"
        degraded = {
            "degraded": True,
            "classification": classification,
            "timeout_detected": timeout,
            "cancelled_detected": cancelled,
            "test": {"run_id": str(run_id), "job_id": str(job_id), "project": project, "branch": scope_branch},
            "job": discovery["job"] if discovery else {"job_id": str(job_id)},
            "log_tail": _log_tail(log_text),
            "evidence_summary": _evidence_summary(evidence),
            "failure_manifest": failure_manifest,
            "suggested_next_steps": [
                "Inspect log_tail for the last failing step and timeout/cancel markers.",
                "Check evidence_summary top_namespaces for fp-* namespaces left behind by the run.",
                "Re-run with must-gather artifact when available for RR-scoped evidence correlation.",
            ],
        }

    return {
        "repository": repository,
        "run_id": str(run_id),
        "job_id": str(job_id),
        "branch": scope_branch,
        "project": project,
        "commit_sha": commit_sha,
        "workflow": workflow,
        "sources": {
            "test_log_url": test_log_url,
            "must_gather_url": must_gather_url,
            "log_only": log_only,
            "artifact_name": (discovery["artifact"] or {}).get("name") if discovery else None,
        },
        "metrics": {
            "failures_extracted": len(failures),
            "unique_failures": len(unique_failures),
            "fallback_rr_resolved": resolved,
            "failures_without_rr": sum(1 for entry in failure_manifest if not entry["rr_id"]),
            "dossiers_generated": len(dossiers),
            "indexed_evidence": len(evidence),
            "artifact_files": manifest.get("artifact_files", 0),
        },
        "failure_manifest": failure_manifest,
        "dossiers": dossiers,
        "degraded": degraded,
        "warnings": warnings,
        "root": str(root),
    }
