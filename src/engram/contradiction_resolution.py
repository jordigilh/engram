#!/usr/bin/env python3
"""Three-tier contradiction resolution shared by nightly-learn.py's
retain_windows() and cocoindex-flows.py's process_transcript() retain loop.

For each correction-tagged window, before retaining:
  1. recall() existing memories in the same bank (hindsight_client.py).
  2. check_contradiction() against them (Sonnet, classify.py).
  3. no contradiction -> caller retains the new statement as normal.
  4. contradicts, confidence >= ENGRAM_CONTRADICTION_AUTO_THRESHOLD ->
     auto-resolve: in ENGRAM_CONTRADICTION_AUTO_MODE=live, invalidate the old
     conflicting memory (non-destructive, reversible -- see invalidate_memory());
     in the default shadow mode, only log what would have been invalidated.
     Either way, append an audit record to contradictions-auto-resolved.jsonl
     and the caller still retains the new statement (tagged
     supersedes-prior-memory).
  5. contradicts, confidence < threshold -> queue via pending_queue.py
     for human review (review-contradictions.py); the caller withholds the
     new statement from retain entirely (action == "queued" means "do not
     retain yet"), matching pending_queue.py's own contract ("never
     auto-retained"). review-contradictions.py retains it on [a]pprove
     (tagged supersedes-prior-memory, old memory invalidated) or leaves it
     un-retained forever on [r]eject.

ENGRAM_CONTRADICTION_CHECK=on|off (default on) disables the whole feature --
resolve() becomes a no-op that always returns action="retain".

See docs/FINDINGS.md for why auto-resolve ships shadow-first: a 4-sample
spike found confidence separated real cases (0.85 hard case vs. 0.95-0.99
clear cases) but n=4 isn't enough to trust live changes yet.

2026-07-29 (GitHub issue #1): auto-resolve and the review-contradictions.py
approve path previously hard-deleted the conflicting memory's entire source
document via delete_document() -- destroying every fact in that document
(not just the conflicting one) with no way to recover from a false positive.
Switched both paths to invalidate_memory(), which uses Hindsight's native
PATCH .../memories/{memory_id} {"state": "invalidated"} curation endpoint:
non-destructive (excluded from recall/consolidation but kept for audit),
reversible ({"state": "valid"}), and scoped to the single conflicting fact
rather than its whole source document. delete_document() itself is
unchanged and still the correct tool for purge-*-memories.py's genuinely
destructive off-topic/out-of-scope cleanup -- that's not a supersession
operation and should stay a hard delete.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# classify.py/hindsight_client.py/pending_queue.py are siblings in this same
# src/engram/ directory (promoted out of spike/ during the package
# restructure -- they're load-bearing, not throwaway spikes). Inserting our
# own resolved directory onto sys.path lets these keep resolving as plain
# top-level modules regardless of how this file itself was reached: as
# `engram.contradiction_resolution` (src/ on sys.path) or via the
# `~/.hindsight/contradiction_resolution.py` flat symlink used by
# hooks/_hindsight_check_worker.py (no `engram` package context at all there)
# -- Path(__file__).resolve() always lands on the real src/engram/ directory
# either way.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify import check_contradiction  # noqa: E402
from hindsight_client import recall  # noqa: E402
import pending_queue  # noqa: E402

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")
LOG_DIR = Path.home() / ".hindsight" / "logs"
AUTO_RESOLVED_LOG_PATH = LOG_DIR / "contradictions-auto-resolved.jsonl"

DEFAULT_AUTO_THRESHOLD = 0.9


def check_enabled() -> bool:
    """ENGRAM_CONTRADICTION_CHECK=on (default) | off."""
    return os.environ.get("ENGRAM_CONTRADICTION_CHECK", "on").strip().lower() != "off"


def auto_threshold() -> float:
    """ENGRAM_CONTRADICTION_AUTO_THRESHOLD (default 0.9). Conservative start --
    no production data yet to calibrate against (see docs/FINDINGS.md)."""
    try:
        return float(os.environ.get("ENGRAM_CONTRADICTION_AUTO_THRESHOLD", str(DEFAULT_AUTO_THRESHOLD)))
    except ValueError:
        return DEFAULT_AUTO_THRESHOLD


def auto_mode() -> str:
    """ENGRAM_CONTRADICTION_AUTO_MODE=shadow (default) | live."""
    mode = os.environ.get("ENGRAM_CONTRADICTION_AUTO_MODE", "shadow").strip().lower()
    return mode if mode in ("shadow", "live") else "shadow"


def delete_document(bank_id: str, document_id: str, retries: int = 2) -> bool:
    """DELETE a document by id. Mirrors nightly-learn.py's dedup_graph()
    delete pattern. Returns True only if the document was actually deleted
    (404 -- already gone -- returns False, not an error)."""
    url = f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/documents/{document_id}"
    for attempt in range(retries + 1):
        try:
            req = Request(url, method="DELETE")
            urlopen(req, timeout=10)
            return True
        except HTTPError as e:
            if e.code == 404:
                return False
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False
        except (URLError, TimeoutError):
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False
    return False


def invalidate_memory(bank_id: str, memory_id: str, reason: str, retries: int = 2) -> bool:
    """PATCH a memory unit to curation state 'invalidated'. Non-destructive
    and reversible (state: 'valid' reverts it) -- excluded from recall,
    consolidation, and graph maintenance but kept for audit. Only
    world/experience facts can be curated; recall()'s default types
    (world + experience, per RecallRequest) already exclude observations,
    so callers here never hit that restriction. Mirrors delete_document()'s
    retry/error-handling shape for consistency. Returns True only on a
    successful PATCH (404 -- already gone -- returns False, not an error)."""
    url = f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/memories/{memory_id}"
    body = json.dumps({"state": "invalidated", "reason": reason}).encode()
    for attempt in range(retries + 1):
        try:
            req = Request(url, data=body, method="PATCH", headers={"Content-Type": "application/json"})
            urlopen(req, timeout=10)
            return True
        except HTTPError as e:
            if e.code == 404:
                return False
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False
        except (URLError, TimeoutError):
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False
    return False


def log_auto_resolved(
    bank_id: str,
    statement: str,
    superseded_memory_id: str,
    superseded_document_id: str | None,
    superseded_text: str,
    confidence: float,
    explanation: str,
    mode: str,
    invalidated: bool,
    project: str | None = None,
) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,  # "shadow" | "live"
        "invalidated": invalidated,  # always False in shadow mode
        "bank_id": bank_id,
        "statement": statement[:500],
        "superseded_memory_id": superseded_memory_id,
        "superseded_document_id": superseded_document_id,
        "superseded_text": superseded_text[:500],
        "confidence": confidence,
        "explanation": explanation,
        "project": project,
    }
    with open(AUTO_RESOLVED_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


@dataclass
class Resolution:
    action: str  # "retain" | "auto_resolved" | "queued"
    superseded_memory_id: str | None = None
    superseded_document_id: str | None = None
    confidence: float = 0.0
    explanation: str = ""


def resolve(bank_id: str, statement: str, project: str | None = None) -> Resolution:
    """Run the three-tier contradiction check for one correction-tagged window.

    action == "retain" or "auto_resolved": caller proceeds to retain the new
    statement as normal (the latter with a supersedes-prior-memory tag).
    action == "queued": caller must NOT retain the new statement -- it is
    withheld pending human review in review-contradictions.py, which retains
    it itself on approve. Skipping this would silently re-introduce the
    "reject discards nothing" bug found on 2026-07-12 (docs/FINDINGS.md).

    project (kubernaut/dcm/engram/None), if given, is stamped onto both the
    auto-resolved log and the pending-review queue entry so downstream
    reporting can actually filter by project -- see docs/FINDINGS.md
    2026-07-19 ("196 pending contradictions, all project=null").
    """
    if not check_enabled():
        return Resolution(action="retain")

    try:
        memory_triples = recall(bank_id, statement, max_results=5)
    except Exception:
        return Resolution(action="retain")

    if not memory_triples:
        return Resolution(action="retain")

    memories = [text for _, _, text in memory_triples]
    result = check_contradiction(statement, memories)
    if result.error or not result.contradicts:
        return Resolution(action="retain")

    idx = result.conflicting_memory_index
    if idx is None or not isinstance(idx, int) or not (0 <= idx < len(memory_triples)):
        return Resolution(action="retain")

    conflicting_memory_id, conflicting_document_id, conflicting_text = memory_triples[idx]

    if result.confidence >= auto_threshold():
        mode = auto_mode()
        invalidated = False
        if mode == "live":
            reason = f"superseded: {statement[:200]}"
            invalidated = invalidate_memory(bank_id, conflicting_memory_id, reason=reason)
        log_auto_resolved(
            bank_id, statement, conflicting_memory_id, conflicting_document_id, conflicting_text,
            result.confidence, result.explanation, mode, invalidated,
            project=project,
        )
        return Resolution(
            action="auto_resolved",
            superseded_memory_id=conflicting_memory_id,
            superseded_document_id=conflicting_document_id,
            confidence=result.confidence, explanation=result.explanation,
        )

    pending_queue.append_pending(
        new_statement=statement,
        conflicting_memory=conflicting_text,
        conflicting_memory_index=idx,
        explanation=result.explanation,
        memory_id=conflicting_memory_id,
        document_id=conflicting_document_id,
        project=project,
    )
    return Resolution(
        action="queued",
        superseded_memory_id=conflicting_memory_id,
        superseded_document_id=conflicting_document_id,
        confidence=result.confidence, explanation=result.explanation,
    )
