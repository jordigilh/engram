from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .correlate import build_clusters, correlate, rank_evidence, resolve_rr_id
from .branch_scope import normalize_branch
from .causal import analyze_resource_busy
from .models import Evidence, TestFailure
from .lifecycle import analyze_interactive_lifecycle
from .normalize import extract_rr_id, iter_evidence

MAX_FAILURE_ANCHOR_CHARS = 20_000

def _failure(run_id: str, job_id: str, test_name: str, failure_text: str, rr_id: str | None) -> TestFailure:
    return TestFailure(
        run_id=run_id,
        job_id=job_id,
        test_name=test_name,
        failure_text=failure_text,
        rr_id=rr_id or extract_rr_id(failure_text),
        failed_at=_failure_timestamp(failure_text),
    )


def _failure_timestamp(text: str) -> datetime | None:
    import re

    match = re.search(r"@\s*(\d\d/\d\d/\d\d\s+\d\d:\d\d:\d\d\.\d+)", text)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%m/%d/%y %H:%M:%S.%f").astimezone()


def _anchor_payload(item: Evidence) -> dict[str, Any] | None:
    if item.evidence_type != "failure_anchor" and not item.metadata.get("failure_anchor"):
        return None
    try:
        value = json.loads(item.content)
    except json.JSONDecodeError:
        return {"failure_excerpt": item.content}
    return value if isinstance(value, dict) else {"failure_excerpt": item.content}


def _expected_actual(text: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"Expected\s*\n\s*(?:<[^>]+>:\s*)?(?P<actual>.+?)\s+to be\s+(?P<expected>[^\r\n]+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None, None
    return match.group("expected").strip(), match.group("actual").strip()


def _scenario_ids(text: str) -> list[str]:
    return sorted(set(re.findall(r"\bE2E-[A-Z0-9]+(?:-[A-Z0-9]+)+\b", text, re.IGNORECASE)), key=str.lower)


def _failure_anchor(
    failure: TestFailure,
    evidence: list[Evidence],
    source_scope: dict[str, Any] | None,
) -> dict[str, Any]:
    anchor_item = next((item for item in evidence if _anchor_payload(item)), None)
    anchor = dict(_anchor_payload(anchor_item) or {}) if anchor_item else {}
    excerpt = str(anchor.get("failure_excerpt") or anchor.get("failure_text") or failure.failure_text)
    if len(excerpt) > MAX_FAILURE_ANCHOR_CHARS:
        excerpt = excerpt[:MAX_FAILURE_ANCHOR_CHARS] + "\n[...failure anchor truncated]"
    scenario_ids = anchor.get("scenario_ids") or _scenario_ids(excerpt)
    if isinstance(scenario_ids, str):
        scenario_ids = [scenario_ids]
    expected, actual = _expected_actual(excerpt)
    anchor.setdefault("failure_excerpt", excerpt)
    anchor.setdefault("test_name", failure.test_name)
    anchor.setdefault("scenario_ids", scenario_ids)
    anchor.setdefault(
        "scenario_id",
        anchor.get("test_scenario") or (scenario_ids[0] if scenario_ids else None),
    )
    anchor.setdefault("expected", expected)
    anchor.setdefault("actual", actual)
    anchor.setdefault("run_id", failure.run_id)
    anchor.setdefault("job_id", failure.job_id)
    if anchor_item:
        anchor.setdefault("source_file", anchor_item.source_file)
        anchor.setdefault("source_line", anchor_item.source_line)
    else:
        anchor.setdefault("source_file", "ci/job.log")
        anchor.setdefault("source_line", None)
    if source_scope:
        anchor.setdefault("job_url", source_scope.get("job_log", {}).get("url"))
        artifacts = source_scope.get("artifacts", [])
        anchor.setdefault("source_artifacts", artifacts)
    if not anchor.get("source_artifact"):
        artifact = next(
            (
                candidate
                for candidate in anchor.get("source_artifacts", [])
                if candidate.get("root") and anchor["source_file"].startswith(f"{candidate['root']}/")
            ),
            None,
        )
        anchor["source_artifact"] = {
            "source_file": anchor["source_file"],
            "artifact": artifact,
            "job_url": anchor.get("job_url"),
        }
    return anchor


def _service_evidence(item: Evidence) -> bool:
    source = item.source_file.lower()
    return any(marker in source for marker in ("integration", "service", "aianalysis"))


def _terminal_failure_ids(evidence: list[Evidence]) -> list[str]:
    markers = (
        "analysis failed",
        "failed during investigation",
        "human review required",
        "humanreviewrequired",
        "workflow resolution failed",
        "phase transition: investigating → failed",
        '"phase": "failed"',
    )
    return [
        item.id
        for item in evidence
        if not _anchor_payload(item) and any(marker in item.content.lower() for marker in markers)
    ]


def _lifecycle_matrix(
    failure: TestFailure,
    evidence: list[Evidence],
    anchor: dict[str, Any],
    source_scope: dict[str, Any] | None,
) -> tuple[dict[str, Any], str, bool, list[str]]:
    terminal_ids = _terminal_failure_ids(evidence)
    service_ids = [item.id for item in evidence if _service_evidence(item)]
    fixture_ids = [
        item.id
        for item in evidence
        if "fixture" in item.content.lower()
        or item.metadata.get("structured", {}).get("kind", "").lower() == "remediationrequest"
    ]
    service_artifact_collected = bool(
        service_ids
        or any(
            any(marker in str(artifact.get("name", "")).lower() for marker in ("integration", "service", "aianalysis"))
            for artifact in (source_scope or {}).get("artifacts", [])
        )
    )
    service_lifecycle_observed = bool(service_ids or terminal_ids)
    matrix = [
        {
            "event": "ci_failure_assertion",
            "expected": True,
            "observed": bool(anchor.get("failure_excerpt")),
            "evidence_ids": [item.id for item in evidence if _anchor_payload(item)],
            "missing_interpretation": None,
        },
        {
            "event": "rr_fixture_or_setup",
            "expected": True,
            "observed": bool(fixture_ids),
            "evidence_ids": fixture_ids[:20],
            "missing_interpretation": "not_emitted" if not fixture_ids else None,
        },
        {
            "event": "service_lifecycle",
            "expected": True,
            "observed": service_lifecycle_observed,
            "evidence_ids": service_ids[:20],
            "missing_interpretation": None if service_lifecycle_observed else (
                "not_emitted" if service_artifact_collected else "not_collected"
            ),
        },
        {
            "event": "service_terminal_failure",
            "expected": True,
            "observed": bool(terminal_ids),
            "evidence_ids": terminal_ids[:20],
            "missing_interpretation": None if terminal_ids else (
                "not_emitted" if service_artifact_collected else "not_collected"
            ),
        },
    ]
    fixture_only = bool(fixture_ids) and not service_lifecycle_observed and not terminal_ids
    if not evidence:
        coverage = "not_observed"
    elif fixture_only and not service_artifact_collected:
        coverage = "artifact_scope_gap"
    elif fixture_only:
        coverage = "insufficient_evidence"
    elif not service_lifecycle_observed and not service_artifact_collected:
        coverage = "insufficient_evidence"
    else:
        coverage = "complete"
    return {
        "expected_events": [item["event"] for item in matrix if item["expected"]],
        "observed_events": [item["event"] for item in matrix if item["observed"]],
        "missing_events": [item["event"] for item in matrix if not item["observed"]],
        "matrix": matrix,
        "service_artifact_collected": service_artifact_collected,
        "service_lifecycle_observed": service_lifecycle_observed,
        "coverage": coverage,
    }, coverage, fixture_only, terminal_ids


def _summary(
    failure: TestFailure,
    rr_id: str,
    evidence: list[Evidence],
    anchor: dict[str, Any],
    source_scope: dict[str, Any] | None,
) -> dict[str, Any]:
    text = "\n".join(item.content for item in evidence).lower()
    lifecycle = analyze_interactive_lifecycle(rr_id, evidence)
    causal_finding = analyze_resource_busy(rr_id, evidence)
    workflow_resolution_failed = "workflowresolutionfailed" in text or "workflow resolution failed" in text
    manual_review_required = any(
        marker in text for marker in ("manualreviewrequired", "manual review required", "human review required")
    )
    lifecycle_matrix, coverage, fixture_only, terminal_ids = _lifecycle_matrix(
        failure, evidence, anchor, source_scope
    )
    manual_review_required = manual_review_required or fixture_only
    if coverage == "artifact_scope_gap":
        status = "artifact_scope_gap"
    elif coverage == "insufficient_evidence":
        status = "insufficient_evidence"
    elif workflow_resolution_failed and manual_review_required:
        status = "workflow_resolution_failed_manual_review_required"
    elif workflow_resolution_failed:
        status = "workflow_resolution_failed"
    elif manual_review_required:
        status = "manual_review_required"
    elif terminal_ids:
        status = "terminal_failure_observed"
    else:
        status = "not_observed"
    source_files = sorted({item.source_file for item in evidence if item.id in terminal_ids})
    source_artifacts = (source_scope or {}).get("artifacts", [])
    if terminal_ids:
        causal_boundary = {
            "classification": "observed_terminal_failure",
            "statement": (
                f"CI failure for {rr_id} reached a confirmed service terminal failure; "
                "the terminal signal takes precedence over downstream absence."
            ),
            "evidence_ids": terminal_ids[:20],
            "source_files": source_files,
            "source_artifacts": source_artifacts,
            "coverage": coverage,
        }
    elif coverage in {"artifact_scope_gap", "insufficient_evidence"}:
        causal_boundary = {
            "classification": coverage,
            "statement": "The correlated RR has no sufficient service lifecycle evidence for a causal conclusion.",
            "evidence_ids": [item.id for item in evidence],
            "source_files": sorted({item.source_file for item in evidence}),
            "source_artifacts": source_artifacts,
            "coverage": coverage,
        }
    else:
        causal_boundary = {
            "classification": "not_observed",
            "statement": "No matching terminal service failure was observed in the collected evidence.",
            "evidence_ids": [item.id for item in evidence],
            "source_files": sorted({item.source_file for item in evidence}),
            "source_artifacts": source_artifacts,
            "coverage": coverage,
        }
    result = {
        "rr_id": rr_id,
        "evidence_count": len(evidence),
        "status": status,
        "workflow_resolution_failed": workflow_resolution_failed,
        "manual_review_required": manual_review_required,
        "review_required_reason": "fixture_only_without_service_lifecycle" if fixture_only else None,
        "operator_escalation": "operator_escalation" in text,
        "workflow_execution_evidence": sum(
            '"kind": "WorkflowExecution"' in item.content
            or "kind: WorkflowExecution" in item.content
            for item in evidence
        ),
        "affected_namespaces": sorted({item.namespace for item in evidence if item.namespace}),
        "interactive_lifecycle": lifecycle,
        "causal_finding": causal_finding,
        "lifecycle_completeness": lifecycle_matrix,
        "causal_boundary": causal_boundary,
    }
    return result


def _evidence_assessment(rr_id: str, evidence: list[Evidence], summary: dict[str, Any]) -> dict[str, Any]:
    if any(item.rr_id == rr_id for item in evidence):
        rr_id_correlation = "exact"
    elif any(rr_id.casefold() in item.content.casefold() for item in evidence):
        rr_id_correlation = "content_match"
    else:
        rr_id_correlation = "missing"
    return {
        "rr_id_correlation": rr_id_correlation,
        "evidence_count": len(evidence),
        "workflow_resolution_failed": summary["workflow_resolution_failed"],
        "manual_review_required": summary["manual_review_required"],
        "workflow_execution_count": summary["workflow_execution_evidence"],
        "causal_classification": summary["causal_finding"]["classification"],
        "coverage": summary["lifecycle_completeness"]["coverage"],
        "service_artifact_collected": summary["lifecycle_completeness"]["service_artifact_collected"],
        "review_required": summary["manual_review_required"],
        "causal_boundary": summary["causal_boundary"],
    }


def _bounded(items: Iterable[Evidence], max_chars: int) -> list[Evidence]:
    result: list[Evidence] = []
    used = 0
    per_item_limit = max(100, min(8000, max_chars // 4))
    for item in items:
        if len(item.content) > per_item_limit:
            item = replace(item, content=item.content[:per_item_limit] + "\n[...evidence truncated]")
        cost = len(item.content) + len(item.source_file) + 180
        if result and used + cost > max_chars:
            break
        result.append(item)
        used += cost
    return result


def build_context(
    failure: TestFailure,
    evidence: Iterable[Evidence],
    max_tokens: int = 12000,
    source_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = list(evidence)
    rr_id = resolve_rr_id(failure, evidence)
    related = correlate(failure, evidence)
    ranked = rank_evidence(failure, related)
    selected = _bounded(ranked, max_tokens * 4)
    anchor = _failure_anchor(failure, related, source_scope)
    summary = _summary(failure, rr_id, related, anchor, source_scope)
    evidence_assessment = _evidence_assessment(rr_id, related, summary)
    timeline = [
        {
            "timestamp": item.timestamp.isoformat() if item.timestamp else None,
            "type": item.evidence_type,
            "source_file": item.source_file,
            "source_line": item.source_line,
            "evidence_id": item.id,
            "identifiers": item.identifiers,
            "metadata": item.metadata,
            "content": item.content[:1000],
        }
        for item in sorted(
            selected,
            key=lambda item: item.timestamp or datetime.max.replace(tzinfo=timezone.utc),
        )
    ]
    result = {
        "rr_id": rr_id,
        "test": {"run_id": failure.run_id, "job_id": failure.job_id, "name": failure.test_name},
        "summary": summary,
        "evidence_assessment": evidence_assessment,
        "failure_anchor": anchor,
        "sources": source_scope or {},
        "timeline": timeline,
        "clusters": build_clusters(related)[:20],
        "evidence": [
            {
                "id": item.id,
                "type": item.evidence_type,
                "timestamp": item.timestamp.isoformat() if item.timestamp else None,
                "source_file": item.source_file,
                "source_line": item.source_line,
                "identifiers": item.identifiers,
                "metadata": item.metadata,
                "content": item.content[:2000],
            }
            for item in selected
        ],
    }
    output_budget = max_tokens * 4
    # Preserve representative evidence by discarding lower-value cluster summaries first.
    while len(json.dumps(result, default=str)) > output_budget and result["clusters"]:
        result["clusters"].pop()
    while len(json.dumps(result, default=str)) > output_budget and len(result["evidence"]) > 1:
        result["evidence"].pop()
        result["timeline"] = [
            item for item in result["timeline"] if item["evidence_id"] in {entry["id"] for entry in result["evidence"]}
        ]
    if len(json.dumps(result, default=str)) > output_budget:
        result["clusters"] = []
        result["evidence"] = result["evidence"][:1]
        result["timeline"] = result["timeline"][:1]
        for item in result["evidence"]:
            item["content"] = item["content"][:500]
        for item in result["timeline"]:
            item["content"] = item["content"][:500]
        result["failure_anchor"]["failure_excerpt"] = result["failure_anchor"]["failure_excerpt"][:1200]
        lifecycle = result["summary"]["lifecycle_completeness"]
        lifecycle["matrix"] = [
            {
                "event": item["event"],
                "observed": item["observed"],
                "evidence_ids": item["evidence_ids"][:3],
                "missing_interpretation": item["missing_interpretation"],
            }
            for item in lifecycle["matrix"]
        ]
    if len(json.dumps(result, default=str)) > output_budget:
        result["evidence"] = result["evidence"][:1]
        result["timeline"] = result["timeline"][:1]
        result["summary"]["causal_finding"]["evidence_ids"] = {
            key: (value[:3] if isinstance(value, list) else value)
            for key, value in result["summary"]["causal_finding"]["evidence_ids"].items()
        }
        result["summary"]["causal_boundary"]["evidence_ids"] = result["summary"]["causal_boundary"]["evidence_ids"][:3]
        lifecycle = result["summary"]["lifecycle_completeness"]
        lifecycle.pop("expected_events", None)
        lifecycle.pop("observed_events", None)
        lifecycle.pop("missing_events", None)
        result["evidence_assessment"].pop("causal_boundary", None)
        result["summary"]["interactive_lifecycle"] = {
            key: value
            for key, value in result["summary"]["interactive_lifecycle"].items()
            if key.endswith("_count") or key.endswith("_events") or key in {"earliest_causal_boundary"}
        }
        result["evidence"][0] = {
            key: result["evidence"][0][key]
            for key in ("id", "type", "source_file", "content")
        }
        result["evidence"][0]["content"] = result["evidence"][0]["content"][:160]
        result["timeline"][0] = {
            key: result["timeline"][0][key]
            for key in ("timestamp", "type", "source_file", "evidence_id", "content")
        }
        result["timeline"][0]["content"] = result["timeline"][0]["content"][:160]
    return result


def triage_test_failure(
    *,
    root: Path,
    run_id: str,
    job_id: str,
    test_name: str,
    failure_text: str,
    rr_id: str | None = None,
    branch: str = "main",
    project: str = "kubernaut",
    max_tokens: int = 12000,
) -> dict[str, Any]:
    """Build a bounded dossier from an extracted must-gather directory."""
    failure = _failure(run_id, job_id, test_name, failure_text, rr_id)
    source_scope = None
    source_manifest = root / ".engram" / "source-manifest.json"
    if source_manifest.exists():
        try:
            source_scope = json.loads(source_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            source_scope = None
    context = build_context(failure, iter_evidence(root), max_tokens=max_tokens, source_scope=source_scope)
    context["scope"] = {"project": project, "branch": normalize_branch(branch)}
    context["test"]["project"] = project
    context["test"]["branch"] = normalize_branch(branch)
    return context


def context_json(**kwargs: Any) -> str:
    return json.dumps(triage_test_failure(**kwargs), indent=2, default=str)
