from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Evidence

RR_RE = re.compile(r"\brr-[a-z0-9]+(?:-[a-z0-9]+)+\b", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"(?P<ts>20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)")
NAMESPACE_RE = re.compile(r"\b(?:namespace|Namespace):\s*[\"']?([a-z0-9][a-z0-9.-]+)", re.IGNORECASE)
TARGET_NAMESPACE_RE = re.compile(
    r"targetResource.{0,500}?namespace[\"']?\s*[:=]\s*[\"']?([a-z0-9][a-z0-9.-]+)",
    re.IGNORECASE | re.DOTALL,
)
POD_RE = re.compile(r"\b(?:pod|Pod):\s*[\"']?([a-z0-9][a-z0-9.-]+)", re.IGNORECASE)
_TARGET_RESOURCE_PATTERNS = (
    re.compile(
        r"\b(?P<namespace>[a-z0-9][a-z0-9.-]+)/(?P<kind>[A-Z][A-Za-z0-9.-]*)/"
        r"(?P<name>[a-z0-9][a-z0-9.-]*)\b"
    ),
    re.compile(
        r"\b(?P<kind>[A-Z][A-Za-z0-9.-]*)/(?P<namespace>[a-z0-9][a-z0-9.-]+)/"
        r"(?P<name>[a-z0-9][a-z0-9.-]*)\b"
    ),
)
_WORKFLOW_EXECUTION_RE = re.compile(r"\bwe-[a-z0-9]+(?:-[a-z0-9]+)+\b", re.IGNORECASE)


def extract_rr_id(text: str) -> str | None:
    """Return the first stable remediation-request identifier in text."""
    match = RR_RE.search(text)
    return match.group(0) if match else None


def extract_rr_ids(text: str) -> tuple[str, ...]:
    """Return all distinct remediation-request identifiers in stable order."""
    return tuple(dict.fromkeys(match.group(0) for match in RR_RE.finditer(text)))


def extract_target_resource(text: str) -> dict[str, str] | None:
    """Extract a Kubernetes target from either namespace/kind/name ordering."""
    for pattern in _TARGET_RESOURCE_PATTERNS:
        match = pattern.search(text)
        if match:
            return {
                "kind": match.group("kind"),
                "namespace": match.group("namespace"),
                "name": match.group("name"),
            }
    return None


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _timestamp(value: Any, text: str) -> datetime | None:
    parsed = parse_timestamp(value)
    if parsed:
        return parsed
    match = TIMESTAMP_RE.search(text)
    return parse_timestamp(match.group("ts")) if match else None


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_flatten_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(_flatten_text(item) for item in value)
    return str(value) if value is not None else ""


def _json_log_payload(content: str) -> dict[str, Any] | None:
    match = re.match(r"^\S+\s+(\{.*\})$", content)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _identifiers(value: Any, content: str) -> dict[str, str]:
    payload = value if isinstance(value, dict) else _json_log_payload(content)
    if not isinstance(payload, dict):
        return {}

    found: dict[str, str] = {}
    key_names = {
        "agentsession": "agent_session_id",
        "agentsessionid": "agent_session_id",
        "a2ataskid": "a2a_task_id",
        "investigationsessionid": "investigation_session_id",
        "isname": "investigation_session_id",
        "kaCorrelationID".lower(): "ka_correlation_id",
        "kacorrelationid": "ka_correlation_id",
        "kasessionid": "ka_session_id",
        "mcpsessionid": "mcp_session_id",
        "reconcileid": "reconcile_id",
        "sessionid": "session_id",
    }

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            kind = item.get("kind")
            metadata = item.get("metadata")
            if kind == "AgentSession" and isinstance(metadata, dict) and metadata.get("name"):
                found.setdefault("agent_session_id", str(metadata["name"]))
            if kind == "InvestigationSession" and isinstance(metadata, dict) and metadata.get("name"):
                found.setdefault("investigation_session_id", str(metadata["name"]))
            for key, nested in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                identifier = key_names.get(normalized)
                if identifier and isinstance(nested, (str, int)):
                    found.setdefault(identifier, str(nested))
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(payload)
    return found


def _metadata(value: Any, text: str) -> dict[str, Any]:
    serialized = _flatten_text(value) if not isinstance(value, str) else value
    namespace = None
    pod = None
    if isinstance(value, dict):
        metadata = value.get("metadata") or {}
        namespace = metadata.get("namespace") or value.get("namespace")
        pod = metadata.get("name") if value.get("kind") == "Pod" else value.get("pod")
    namespace = (
        TARGET_NAMESPACE_RE.search(serialized)
        or namespace
        or NAMESPACE_RE.search(serialized)
        or NAMESPACE_RE.search(text)
    )
    pod = pod or (POD_RE.search(serialized) or POD_RE.search(text))
    return {
        "namespace": namespace.group(1) if hasattr(namespace, "group") else namespace,
        "pod": pod.group(1) if hasattr(pod, "group") else pod,
    }


def _first_value(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _target_resource(value: Any, content: str, spec: dict[str, Any]) -> dict[str, str] | None:
    target = spec.get("targetResource")
    if target is None and isinstance(value, dict):
        target = value.get("targetResource")
    if target is None:
        parameters = spec.get("parameters")
        if isinstance(parameters, dict):
            target = {
                "kind": parameters.get("TARGET_RESOURCE_KIND"),
                "name": parameters.get("TARGET_RESOURCE_NAME"),
                "namespace": parameters.get("TARGET_RESOURCE_NAMESPACE"),
            }
    if isinstance(target, dict):
        kind = target.get("kind")
        name = target.get("name")
        namespace = target.get("namespace")
        if kind and name and namespace:
            return {"kind": str(kind), "namespace": str(namespace), "name": str(name)}
    if isinstance(target, str):
        parsed = extract_target_resource(target)
        if parsed:
            return parsed
    return extract_target_resource(content)


def _structured_metadata(value: Any, content: str) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else _json_log_payload(content)
    if not isinstance(payload, dict):
        return {}
    spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    resource_kind = str(_first_value(payload.get("kind"), payload.get("controllerKind")) or "")
    resource = payload.get(resource_kind) if resource_kind and isinstance(payload.get(resource_kind), dict) else {}
    resource_metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    object_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    object_metadata = object_metadata or resource_metadata
    if not resource_kind and resource:
        resource_kind = str(_first_value(resource.get("kind"), payload.get("kind")) or "")

    target = _target_resource(payload, content, spec)
    routing = status.get("routingStatus") if isinstance(status.get("routingStatus"), dict) else {}
    reference = spec.get("remediationRequestRef")
    if not isinstance(reference, dict):
        reference = payload.get("remediationRequestRef") if isinstance(payload.get("remediationRequestRef"), dict) else {}
    block_reason = _first_value(
        routing.get("blockReason"),
        status.get("blockReason"),
        payload.get("blockReason"),
        payload.get("reason"),
    )
    blocking_workflow = _first_value(
        routing.get("blockingWorkflowExecution"),
        payload.get("blockingWorkflowExecution"),
        payload.get("workflowExecution"),
    )
    if not blocking_workflow and block_reason and "resourcebusy" in str(block_reason).replace("_", "").replace("-", "").lower():
        workflow_match = _WORKFLOW_EXECUTION_RE.search(content)
        blocking_workflow = workflow_match.group(0) if workflow_match else None
    workflow_ref = spec.get("workflowRef") if isinstance(spec.get("workflowRef"), dict) else {}
    cluster_id = _first_value(
        spec.get("clusterID"),
        payload.get("clusterID"),
        resource.get("clusterID"),
        target.get("clusterID") if target else None,
    )
    structured = {
        "kind": resource_kind or None,
        "name": _first_value(object_metadata.get("name"), payload.get("name"), resource.get("name")),
        "namespace": _first_value(object_metadata.get("namespace"), payload.get("namespace"), resource.get("namespace")),
        "cluster_id": str(cluster_id) if cluster_id is not None else None,
        "target_resource": target,
        "phase": _first_value(status.get("overallPhase"), status.get("phase"), payload.get("phase"), payload.get("wePhase")),
        "block_reason": str(block_reason) if block_reason is not None else None,
        "block_message": _first_value(routing.get("blockMessage"), payload.get("blockMessage")),
        "blocking_workflow_execution": str(blocking_workflow) if blocking_workflow is not None else None,
        "remediation_request_ref": _first_value(reference.get("name"), payload.get("remediationRequest")),
        "workflow_engine": _first_value(workflow_ref.get("executionEngine"), payload.get("engine")),
        "failure_reason": _first_value(
            status.get("failureReason"),
            status.get("reason"),
            payload.get("failureReason"),
            payload.get("reason"),
        ),
    }
    return {key: item for key, item in structured.items() if item not in (None, "", {})}


def _evidence_id(source_file: str, content: str, ordinal: int) -> str:
    digest = hashlib.sha1(f"{source_file}\0{ordinal}\0{content}".encode()).hexdigest()[:16]
    return f"evidence-{digest}"


def _from_value(
    value: Any,
    source_file: str,
    ordinal: int,
    evidence_type: str,
    source_line: int | None = None,
) -> Evidence:
    content = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    metadata = _metadata(value, content)
    rr_ids = extract_rr_ids(content)
    rr_id = rr_ids[0] if rr_ids else None
    metadata["rr_ids"] = rr_ids
    if evidence_type == "failure_anchor":
        metadata["failure_anchor"] = True
    structured = _structured_metadata(value, content)
    if structured:
        metadata["structured"] = structured
        metadata["namespace"] = metadata.get("namespace") or structured.get("namespace")
    timestamp = None
    if isinstance(value, dict):
        for key in ("timestamp", "creationTimestamp", "createdAt", "firingTime", "firstTimestamp", "lastTimestamp"):
            timestamp = _timestamp(value.get(key), content)
            if timestamp:
                break
    timestamp = timestamp or _timestamp(None, content)
    lower = content.lower()
    severity = "error" if any(word in lower for word in ("failed", "failure", "error", "timeout")) else None
    if any(word in lower for word in ("warning", "backoff", "oomkilled")):
        severity = "warning"
    return Evidence(
        id=_evidence_id(source_file, content, ordinal),
        source_file=source_file,
        evidence_type=evidence_type,
        content=content,
        timestamp=timestamp,
        rr_id=rr_id,
        namespace=metadata["namespace"],
        pod=metadata["pod"],
        severity=severity,
        source_line=source_line,
        identifiers=_identifiers(value, content),
        metadata=metadata,
    )


def _load_yaml_documents(path: Path) -> Iterator[Any]:
    import yaml

    with path.open(encoding="utf-8", errors="replace") as handle:
        for document in yaml.safe_load_all(handle):
            if isinstance(document, dict) and isinstance(document.get("items"), list):
                yield from document["items"]
            else:
                yield document


def iter_evidence(root: Path) -> Iterator[Evidence]:
    """Parse supported must-gather YAML, JSON, failure-anchor, and log files."""
    ordinal = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(".pre-sanitize"):
            continue
        relative = str(path.relative_to(root))
        if relative == ".engram" or relative.startswith(".engram/"):
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in {".yaml", ".yml"}:
                import yaml

                try:
                    values = list(_load_yaml_documents(path))
                except yaml.YAMLError:
                    # Must-gathers may contain kubectl-rendered YAML with
                    # unquoted scalars such as `@timestamp`. Preserve the
                    # file as searchable raw evidence instead of dropping it.
                    values = [path.read_text(encoding="utf-8", errors="replace")]
                for value in values:
                    if value is None:
                        continue
                    yield _from_value(value, relative, ordinal, "kubernetes_resource")
                    ordinal += 1
            elif suffix == ".json":
                value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                values = value.get("items", []) if isinstance(value, dict) and isinstance(value.get("items"), list) else [value]
                for item in values:
                    yield _from_value(item, relative, ordinal, "json_record")
                    ordinal += 1
            elif path.name == "failure-anchors.jsonl":
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
                ):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        value = line
                    yield _from_value(value, relative, ordinal, "failure_anchor", source_line=line_number)
                    ordinal += 1
            elif path.name.endswith(".log") or path.name == "logs.txt":
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
                ):
                    if line.strip():
                        yield _from_value(line, relative, ordinal, "log_event", source_line=line_number)
                        ordinal += 1
        except (OSError, UnicodeError, ValueError):
            continue
