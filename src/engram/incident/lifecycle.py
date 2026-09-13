from __future__ import annotations

from typing import Any, Iterable

from .models import Evidence


def _text(evidence: Evidence) -> str:
    return evidence.content.lower()


def _has(evidence: Evidence, *terms: str) -> bool:
    text = _text(evidence)
    return all(term.lower() in text for term in terms)


def analyze_interactive_lifecycle(rr_id: str, evidence: Iterable[Evidence]) -> dict[str, Any]:
    """Analyze one RR's interactive investigation without temporal-only joins."""
    related = [item for item in evidence if item.rr_id == rr_id or rr_id in item.content]
    agent_sessions = [
        item
        for item in related
        if item.identifiers.get("agent_session_id")
        or '"kind": "agentsession"' in _text(item)
        or _has(item, "agentsession")
    ]
    pending_sessions = [
        item
        for item in agent_sessions
        if _has(item, "pending")
        and ("phase" in _text(item) or "interactive session created" in _text(item))
    ]
    investigation_sessions = [
        item for item in related if item.identifiers.get("investigation_session_id")
    ]
    af_start = [item for item in related if _has(item, "startinvestigation:", "calling mcp client")]
    mcp_established = [item for item in related if _has(item, "mcp session established")]
    ka_pending = [item for item in related if _has(item, "interactive session created with context", "pending")]
    requeues = [item for item in related if _has(item, "investigation still in progress", "requeuing")]
    timeout_extensions = [item for item in related if _has(item, "extending analyzing timeout")]
    possible_contributors = [
        "concurrent MCP 429/rate-limit activity"
        for item in related
        if "429" in _text(item) or "rate limit" in _text(item)
    ]

    expected_missing: list[str] = []
    if af_start and not mcp_established:
        expected_missing.append("MCP session established")
    if pending_sessions and not any(
        _has(item, "session handoff") or _has(item, "investigation completed") for item in related
    ):
        expected_missing.append("interactive investigation handoff completion")

    boundary = None
    if pending_sessions and af_start and not mcp_established:
        boundary = "AF-to-KA interactive session startup/handoff"

    investigation_session_ids = {
        item.identifiers["investigation_session_id"]
        for item in investigation_sessions
        if item.identifiers.get("investigation_session_id")
    }
    agent_session_ids = {
        item.identifiers["agent_session_id"]
        for item in agent_sessions
        if item.identifiers.get("agent_session_id")
    }
    return {
        "investigation_session_count": len(investigation_session_ids),
        "agent_session_count": len(agent_session_ids),
        "pending_agent_sessions": len(pending_sessions),
        "af_start_events": len(af_start),
        "mcp_established_events": len(mcp_established),
        "ka_pending_events": len(ka_pending),
        "requeue_events": len(requeues),
        "timeout_extension_events": len(timeout_extensions),
        "expected_missing_events": expected_missing,
        "earliest_causal_boundary": boundary,
        "possible_contributors": sorted(set(possible_contributors)),
        "evidence_ids": {
            "pending_sessions": [item.id for item in pending_sessions],
            "af_start": [item.id for item in af_start],
            "mcp_established": [item.id for item in mcp_established],
            "ka_pending": [item.id for item in ka_pending],
        },
    }
