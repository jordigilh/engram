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


def extract_rr_id(text: str) -> str | None:
    """Return the first stable remediation-request identifier in text."""
    match = RR_RE.search(text)
    return match.group(0) if match else None


def extract_rr_ids(text: str) -> tuple[str, ...]:
    """Return all distinct remediation-request identifiers in stable order."""
    return tuple(dict.fromkeys(match.group(0) for match in RR_RE.finditer(text)))


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
    """Parse supported must-gather YAML, JSON, and log files deterministically."""
    ordinal = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(".pre-sanitize"):
            continue
        relative = str(path.relative_to(root))
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
            elif path.name.endswith(".log") or path.name == "logs.txt":
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
                ):
                    if line.strip():
                        yield _from_value(line, relative, ordinal, "log_event", source_line=line_number)
                        ordinal += 1
        except (OSError, UnicodeError, ValueError):
            continue
