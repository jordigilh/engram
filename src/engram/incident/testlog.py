"""Extract stable failure anchors from Ginkgo/Gomega CI output."""
from __future__ import annotations

import re
import hashlib
from typing import Any

from .normalize import RR_RE, extract_rr_id

_FAILURE_START = re.compile(r"^\s*\[FAILED\]\s+(?!in \[It\]|Timed out)")
_TIMESTAMP = re.compile(r"@\s*(\d\d/\d\d/\d\d\s+\d\d:\d\d:\d\d\.\d+)")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_RUNNER_PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s+")
_TASK_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_ISSUE_REF = re.compile(r"\b(?:E2E-FP-\d+-\d+|FP-[A-Z0-9-]+|issue-\d+)\b", re.IGNORECASE)
_RESOURCE = re.compile(r"\b(?:Deployment|StatefulSet|DaemonSet|Pod|Service)/[a-z0-9][a-z0-9.-]+\b", re.IGNORECASE)
_NAMESPACE = re.compile(r"\b(?:namespace|ns)[=:/ ]+([a-z0-9][a-z0-9.-]+)\b", re.IGNORECASE)
_NAMESPACE_CANDIDATE = re.compile(r"\bfp-(?!\d)[a-z0-9][a-z0-9.-]+\b", re.IGNORECASE)


def extract_test_failures(text: str) -> list[dict[str, Any]]:
    """Return one bounded failure block per top-level Ginkgo failure."""
    blocks: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = _RUNNER_PREFIX.sub("", _ANSI.sub("", raw_line)).replace("\r", "")
        line = re.sub(r"^\s*[•*-]\s+", "", line)
        if _FAILURE_START.match(line):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
            continue
        if current:
            current.append(line)
            if "<< Timeline" in line:
                blocks.append("\n".join(current).strip())
                current = []
    if current:
        blocks.append("\n".join(current).strip())

    failures = []
    for block in blocks:
        ids = sorted(set(RR_RE.findall(block)))
        timestamp = _TIMESTAMP.search(block)
        lines = block.splitlines()
        first_line = next(
            (line for line in lines if "[It]" in line and not line.lstrip().startswith("[FAILED]")),
            lines[0] if lines else "",
        )
        failures.append({
            "test_name": first_line.removeprefix("[FAILED] ").strip(),
            "failure_text": block,
            "rr_ids": ids,
            "rr_id": extract_rr_id(block),
            "failure_timestamp": timestamp.group(1) if timestamp else None,
            "task_ids": sorted(set(_TASK_ID.findall(block)), key=str.lower),
            "issue_refs": sorted(set(_ISSUE_REF.findall(block)), key=str.lower),
            "resources": sorted(set(_RESOURCE.findall(block)), key=str.lower),
            "namespaces": sorted(
                {
                    candidate
                    for candidate in _NAMESPACE.findall(block) + _NAMESPACE_CANDIDATE.findall(block)
                    if candidate.lower() not in {"exists", "the", "this"}
                },
                key=str.lower,
            ),
        })
    return failures


def infer_rr_ids(failure: dict, evidence: list[Any]) -> list[str]:
    """Infer an RR only from an exact namespace/resource evidence intersection."""
    namespaces = [namespace for namespace in failure.get("namespaces", []) if namespace.lower().startswith("fp-")]
    resources = failure.get("resources", [])
    if not namespaces or not resources:
        return []
    rr_ids: set[str] = set()
    for item in evidence:
        content = item.content
        if not any(namespace in content for namespace in namespaces):
            continue
        if not any(
            resource in content
            or all(part in content for part in resource.split("/"))
            for resource in resources
        ):
            continue
        rr_id = extract_rr_id(content)
        if rr_id:
            rr_ids.add(rr_id)
    return sorted(rr_ids)


def classify_failure(failure: dict) -> str:
    if failure.get("rr_id"):
        return "incident_with_rr"
    if failure.get("resources") or any(namespace.lower().startswith("fp-") for namespace in failure.get("namespaces", [])):
        return "incident_anchor_candidate"
    text = failure.get("failure_text", "").lower()
    if any(marker in text for marker in ("synchronizedbeforesuite", "image loads failed", "k8sclient is nil", "setup failure")):
        return "environment_setup_failure"
    return "unresolved_failure"


def deduplicate_failures(failures: list[dict]) -> list[dict]:
    """Collapse repeated output for one logical test/setup failure."""
    unique: dict[str, dict] = {}
    for failure in failures:
        text = failure.get("failure_text", "").lower()
        if "synchronizedbeforesuite" in text:
            key = "setup:" + failure.get("test_name", "synchronizedbeforesuite").lower()
        else:
            key = failure.get("rr_id") or failure.get("test_name", "")
        digest = hashlib.sha1(key.encode()).hexdigest()
        unique.setdefault(digest, failure)
    return list(unique.values())
