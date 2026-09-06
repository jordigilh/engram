from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .correlate import build_clusters, correlate, rank_evidence, resolve_rr_id
from .models import Evidence, TestFailure
from .normalize import extract_rr_id, iter_evidence


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


def _summary(rr_id: str, evidence: list[Evidence]) -> dict[str, Any]:
    text = "\n".join(item.content for item in evidence).lower()
    result = {
        "rr_id": rr_id,
        "evidence_count": len(evidence),
        "workflow_resolution_failed": "workflowresolutionfailed" in text or "workflow resolution failed" in text,
        "manual_review_required": "manualreviewrequired" in text or "manual review required" in text,
        "operator_escalation": "operator_escalation" in text,
        "workflow_execution_evidence": sum(
            '"kind": "WorkflowExecution"' in item.content
            or "kind: WorkflowExecution" in item.content
            for item in evidence
        ),
        "affected_namespaces": sorted({item.namespace for item in evidence if item.namespace}),
    }
    return result


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


def build_context(failure: TestFailure, evidence: Iterable[Evidence], max_tokens: int = 12000) -> dict[str, Any]:
    rr_id = resolve_rr_id(failure)
    related = correlate(failure, evidence)
    ranked = rank_evidence(failure, related)
    selected = _bounded(ranked, max_tokens * 4)
    summary = _summary(rr_id, related)
    timeline = [
        {
            "timestamp": item.timestamp.isoformat() if item.timestamp else None,
            "type": item.evidence_type,
            "source_file": item.source_file,
            "evidence_id": item.id,
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
        "confidence": 0.94 if summary["workflow_resolution_failed"] and summary["manual_review_required"] else 0.55,
        "confidence_basis": [
            "exact RR-ID correlation",
            "terminal AIAnalysis status",
            "RemediationRequest completion status",
            "absence of a correlated WorkflowExecution",
        ],
        "timeline": timeline,
        "clusters": build_clusters(related)[:20],
        "evidence": [
            {
                "id": item.id,
                "type": item.evidence_type,
                "timestamp": item.timestamp.isoformat() if item.timestamp else None,
                "source_file": item.source_file,
                "content": item.content[:2000],
            }
            for item in selected
        ],
    }
    output_budget = max_tokens * 4
    while len(json.dumps(result, default=str)) > output_budget and result["evidence"]:
        result["evidence"].pop()
        result["timeline"] = [
            item for item in result["timeline"] if item["evidence_id"] in {entry["id"] for entry in result["evidence"]}
        ]
    if len(json.dumps(result, default=str)) > output_budget:
        result["evidence"] = []
        result["timeline"] = []
        result["clusters"] = []
    return result


def triage_test_failure(
    *,
    root: Path,
    run_id: str,
    job_id: str,
    test_name: str,
    failure_text: str,
    rr_id: str | None = None,
    max_tokens: int = 12000,
) -> dict[str, Any]:
    """Build a bounded dossier from an extracted must-gather directory."""
    failure = _failure(run_id, job_id, test_name, failure_text, rr_id)
    return build_context(failure, iter_evidence(root), max_tokens=max_tokens)


def context_json(**kwargs: Any) -> str:
    return json.dumps(triage_test_failure(**kwargs), indent=2, default=str)
