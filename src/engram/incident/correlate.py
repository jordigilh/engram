from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from .models import Evidence, TestFailure
from .normalize import extract_rr_id
from .testlog import infer_rr_ids


def resolve_rr_id(failure: TestFailure, evidence: Iterable[Evidence] | None = None) -> str:
    rr_id = failure.rr_id or extract_rr_id(failure.failure_text)
    if not rr_id and evidence is not None:
        inferred = infer_rr_ids({"failure_text": failure.failure_text}, list(evidence))
        if len(inferred) == 1:
            rr_id = inferred[0]
    if not rr_id:
        raise ValueError("failure text does not contain an RR ID")
    return rr_id


def correlate(failure: TestFailure, evidence: Iterable[Evidence]) -> list[Evidence]:
    evidence = list(evidence)
    rr_id = resolve_rr_id(failure, evidence)
    related = [
        item
        for item in evidence
        if item.rr_id == rr_id
        or rr_id.casefold() in item.content.casefold()
        or rr_id in item.metadata.get("rr_ids", ())
    ]
    blocking_workflows = {
        item.metadata.get("structured", {}).get("blocking_workflow_execution")
        for item in related
        if item.metadata.get("structured", {}).get("kind", "").lower() == "remediationrequest"
        and item.metadata.get("structured", {}).get("name") == rr_id
    }
    blocking_workflows.discard(None)
    for item in evidence:
        structured = item.metadata.get("structured", {})
        if (
            structured.get("name") in blocking_workflows
            or structured.get("remediation_request_ref") == rr_id
            or any(workflow.casefold() in item.content.casefold() for workflow in blocking_workflows)
        ):
            related.append(item)
    # Service-specific sibling logs often identify the RR only on the session
    # creation line, then emit terminal events without repeating that ID.
    related_times = {
        item.timestamp
        for item in related
        if item.timestamp and any(marker in item.source_file.lower() for marker in ("integration", "service", "aianalysis"))
    }
    if related_times:
        for item in evidence:
            if not item.timestamp or not any(
                marker in item.source_file.lower() for marker in ("integration", "service", "aianalysis")
            ):
                continue
            if any(abs((item.timestamp - timestamp).total_seconds()) <= 10 for timestamp in related_times):
                related.append(item)
    # A resource name alone is not a safe join: the fixture contains many
    # independent memory-eater incidents in different namespaces.
    unique = {item.id: item for item in related}
    return sorted(
        unique.values(),
        key=lambda item: item.timestamp or datetime.max.replace(tzinfo=timezone.utc),
    )


def build_clusters(evidence: Iterable[Evidence]) -> list[dict]:
    groups: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        normalized = re.sub(r"[0-9a-f]{8,}", "<id>", item.content.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) > 240:
            normalized = normalized[:240]
        groups[normalized].append(item)
    return [
        {
            "cluster_id": f"cluster-{index}",
            "signature": signature,
            "count": len(items),
            "first_seen": min((item.timestamp for item in items if item.timestamp), default=None),
            "last_seen": max((item.timestamp for item in items if item.timestamp), default=None),
            "representative_evidence_id": items[0].id,
        }
        for index, (signature, items) in enumerate(sorted(groups.items(), key=lambda entry: (-len(entry[1]), entry[0])))
    ]


def rank_evidence(failure: TestFailure, evidence: Iterable[Evidence]) -> list[Evidence]:
    evidence = list(evidence)
    rr_id = resolve_rr_id(failure, evidence)
    target_time = failure.failed_at
    scored: list[tuple[float, Evidence]] = []
    for item in evidence:
        score = 0.0
        if item.rr_id == rr_id or rr_id in item.content:
            score += 0.45
        if item.timestamp and target_time:
            distance = abs((item.timestamp - target_time).total_seconds())
            score += 0.25 * max(0.0, 1.0 - min(distance / 900.0, 1.0))
        if item.severity in {"error", "warning"}:
            score += 0.15
        if any(term in item.content.lower() for term in ("workflow", "manualreview", "operator_escalation", "failed")):
            score += 0.15
        structured = item.metadata.get("structured", {})
        if structured.get("kind", "").lower() in {"remediationrequest", "workflowexecution"}:
            score += 0.25
        if structured.get("block_reason", "").replace("_", "").replace("-", "").lower() == "resourcebusy":
            score += 0.4
        if structured.get("failure_reason", "").replace("_", "").replace("-", "").lower() == "unsupportedengine":
            score += 0.2
        scored.append((score, item))
    return [
        item
        for _score, item in sorted(
            scored,
            key=lambda pair: (
                -pair[0],
                pair[1].timestamp or datetime.max.replace(tzinfo=timezone.utc),
            ),
        )
    ]
