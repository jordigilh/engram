from __future__ import annotations

from typing import Any, Iterable

from .models import Evidence


def _structured(item: Evidence) -> dict[str, Any]:
    value = item.metadata.get("structured", {})
    return value if isinstance(value, dict) else {}


def _normalized(value: Any) -> str:
    return str(value or "").replace("_", "").replace("-", "").lower()


def _target_key(target: Any) -> tuple[str, str, str] | None:
    if not isinstance(target, dict):
        return None
    values = tuple(str(target.get(key) or "") for key in ("kind", "namespace", "name"))
    return values if all(values) else None


def _best_structured(items: list[Evidence]) -> Evidence | None:
    if not items:
        return None
    return max(
        items,
        key=lambda item: (
            bool(_structured(item).get("cluster_id")),
            bool(_structured(item).get("target_resource")),
            bool(_structured(item).get("blocking_workflow_execution")),
            len(_structured(item)),
        ),
    )


def analyze_resource_busy(rr_id: str, evidence: Iterable[Evidence]) -> dict[str, Any]:
    """Identify a cluster-scoping lock collision from linked CRD evidence."""
    evidence = list(evidence)
    rr_items = [
        item
        for item in evidence
        if _structured(item).get("kind", "").lower() == "remediationrequest"
        and _structured(item).get("name") == rr_id
    ]
    rr_item = _best_structured(rr_items)
    rr_data = _structured(rr_item) if rr_item else {}
    blocker_id = rr_data.get("blocking_workflow_execution")
    wfe_items = [
        item
        for item in evidence
        if _structured(item).get("kind", "").lower() == "workflowexecution"
        and _structured(item).get("name") == blocker_id
    ]
    wfe_item = _best_structured(wfe_items)
    wfe_data = _structured(wfe_item) if wfe_item else {}
    block_reason = _normalized(rr_data.get("block_reason"))
    request_target = _target_key(rr_data.get("target_resource"))
    blocking_target = _target_key(wfe_data.get("target_resource"))
    same_target = request_target is not None and request_target == blocking_target
    request_cluster = rr_data.get("cluster_id")
    blocking_cluster = wfe_data.get("cluster_id")
    different_clusters = bool(request_cluster and blocking_cluster and request_cluster != blocking_cluster)

    resource_busy_ids = [
        item.id
        for item in evidence
        if _normalized(_structured(item).get("block_reason")) == "resourcebusy"
        or (rr_id.casefold() in item.content.casefold() and "resourcebusy" in item.content.casefold())
    ]
    secondary_failures: list[dict[str, Any]] = []
    if blocker_id:
        unsupported_ids = [
            item.id
            for item in evidence
            if blocker_id.casefold() in item.content.casefold()
            and (
                _normalized(_structured(item).get("failure_reason")) == "unsupportedengine"
                or "unsupported execution engine" in item.content.casefold()
            )
        ]
        if _normalized(wfe_data.get("workflow_engine")) == "tekton" and unsupported_ids:
            secondary_failures.append(
                {
                    "classification": "unsupported_execution_engine",
                    "workflow_execution": blocker_id,
                    "engine": wfe_data.get("workflow_engine"),
                    "evidence_ids": sorted(set(unsupported_ids))[:20],
                }
            )

    if block_reason == "resourcebusy" and blocker_id and same_target and different_clusters:
        classification = "cross_cluster_target_lock_collision"
        primary_cause = (
            "ResourceBusy lock comparison treated the same namespace/kind/name target as occupied "
            "across different cluster IDs"
        )
        conclusion = (
            f"{rr_id} was blocked by {blocker_id}: both target {request_target[1]}/{request_target[0]}/{request_target[2]} "
            f"matched, but the request cluster was {request_cluster} and the blocking workflow cluster was {blocking_cluster}."
        )
    elif block_reason == "resourcebusy":
        classification = "resource_busy"
        primary_cause = "RemediationRequest routing was blocked by an active workflow on the target resource"
        conclusion = rr_data.get("block_message") or "RemediationRequest was blocked with ResourceBusy"
    else:
        classification = "not_observed"
        primary_cause = None
        conclusion = None

    return {
        "classification": classification,
        "primary_cause": primary_cause,
        "conclusion": conclusion,
        "target_resource": {
            "kind": request_target[0],
            "namespace": request_target[1],
            "name": request_target[2],
        }
        if request_target
        else None,
        "request_cluster_id": request_cluster,
        "blocking_workflow_execution": blocker_id,
        "blocking_cluster_id": blocking_cluster,
        "same_target": same_target,
        "different_clusters": different_clusters,
        "evidence_ids": {
            "remediation_request": rr_item.id if rr_item else None,
            "blocking_workflow_execution": wfe_item.id if wfe_item else None,
            "resource_busy": sorted(set(resource_busy_ids))[:20],
        },
        "secondary_failures": secondary_failures,
    }
