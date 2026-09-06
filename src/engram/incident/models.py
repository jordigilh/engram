from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class TestFailure:
    run_id: str
    job_id: str
    test_name: str
    failure_text: str
    rr_id: str | None = None
    failed_at: datetime | None = None


@dataclass(frozen=True)
class Evidence:
    id: str
    source_file: str
    evidence_type: str
    content: str
    timestamp: datetime | None = None
    rr_id: str | None = None
    namespace: str | None = None
    resource: str | None = None
    pod: str | None = None
    container: str | None = None
    service: str | None = None
    severity: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IncidentContext:
    rr_id: str
    evidence: tuple[Evidence, ...]
    timeline: tuple[Evidence, ...]
    clusters: tuple[dict[str, Any], ...]
    summary: dict[str, Any]
    confidence: float
