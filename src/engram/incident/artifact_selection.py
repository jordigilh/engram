from __future__ import annotations

import re
from typing import Any, Iterable


MUST_GATHER_NAME_RE = re.compile(r"(?:^|[-_. ])must[-_. ]?gather(?:$|[-_. ])", re.IGNORECASE)
COVERAGE_NAME_RE = re.compile(r"(?:^|[-_. ])coverage(?:$|[-_. ])", re.IGNORECASE)


def is_must_gather(name: str | None) -> bool:
    return bool(MUST_GATHER_NAME_RE.search(name or ""))


def _job_match_count(artifact_name: str, job_name: str | None) -> int:
    if not job_name:
        return 0
    artifact = artifact_name.lower()
    tokens = re.findall(r"[a-z0-9]+", job_name.lower())
    ignored = {"e2e", "tests", "test", "kind", "the", "with", "tls", "mode"}
    return sum(token not in ignored and token in artifact for token in tokens)


def _sort_key(artifact: dict[str, Any], job_name: str | None) -> tuple[int, int, str, str]:
    name = artifact.get("name") or ""
    if is_must_gather(name):
        kind_rank = 0
    elif COVERAGE_NAME_RE.search(name):
        kind_rank = 2
    else:
        kind_rank = 1
    return kind_rank, -_job_match_count(name, job_name), name.lower(), str(artifact.get("artifact_id", ""))


def select_artifact(
    artifacts: Iterable[dict[str, Any]],
    *,
    job_name: str | None = None,
    artifact_hint: str | None = None,
) -> dict[str, Any] | None:
    candidates = [artifact for artifact in artifacts if not artifact.get("expired")]
    if artifact_hint:
        normalized_hint = artifact_hint.casefold()
        hinted = [artifact for artifact in candidates if normalized_hint in (artifact.get("name") or "").casefold()]
        if not hinted:
            return None
        exact = [artifact for artifact in hinted if (artifact.get("name") or "").casefold() == normalized_hint]
        candidates = exact or hinted
    return min(candidates, key=lambda artifact: _sort_key(artifact, job_name), default=None)
