#!/usr/bin/env python3
"""Three-tier contradiction resolution shared by nightly-learn.py's
retain_windows() and cocoindex-flows.py's process_transcript() retain loop.

For each correction-tagged window, before retaining:
  1. recall() existing memories in the same bank (spike/hindsight_client.py).
  2. check_contradiction() against them (Sonnet, spike/classify.py).
  3. no contradiction -> caller retains the new statement as normal.
  4. contradicts, confidence >= ENGRAM_CONTRADICTION_AUTO_THRESHOLD ->
     auto-resolve: in ENGRAM_CONTRADICTION_AUTO_MODE=live, delete the old
     conflicting memory; in the default shadow mode, only log what would
     have been deleted. Either way, append an audit record to
     contradictions-auto-resolved.jsonl and the caller still retains the new
     statement (tagged supersedes-prior-memory).
  5. contradicts, confidence < threshold -> queue via spike/pending_queue.py
     for human review (review-contradictions.py); the caller withholds the
     new statement from retain entirely (action == "queued" means "do not
     retain yet"), matching pending_queue.py's own contract ("never
     auto-retained"). review-contradictions.py retains it on [a]pprove
     (tagged supersedes-prior-memory, old memory deleted) or leaves it
     un-retained forever on [r]eject.

ENGRAM_CONTRADICTION_CHECK=on|off (default on) disables the whole feature --
resolve() becomes a no-op that always returns action="retain".

See docs/FINDINGS.md for why auto-resolve ships shadow-first: a 4-sample
spike found confidence separated real cases (0.85 hard case vs. 0.95-0.99
clear cases) but n=4 isn't enough to trust live deletes yet.
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

sys.path.insert(0, str(Path(__file__).resolve().parent / "spike"))
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


def log_auto_resolved(
    bank_id: str,
    statement: str,
    superseded_document_id: str,
    superseded_text: str,
    confidence: float,
    explanation: str,
    mode: str,
    deleted: bool,
) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,  # "shadow" | "live"
        "deleted": deleted,  # always False in shadow mode
        "bank_id": bank_id,
        "statement": statement[:500],
        "superseded_document_id": superseded_document_id,
        "superseded_text": superseded_text[:500],
        "confidence": confidence,
        "explanation": explanation,
    }
    with open(AUTO_RESOLVED_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


@dataclass
class Resolution:
    action: str  # "retain" | "auto_resolved" | "queued"
    superseded_document_id: str | None = None
    confidence: float = 0.0
    explanation: str = ""


def resolve(bank_id: str, statement: str) -> Resolution:
    """Run the three-tier contradiction check for one correction-tagged window.

    action == "retain" or "auto_resolved": caller proceeds to retain the new
    statement as normal (the latter with a supersedes-prior-memory tag).
    action == "queued": caller must NOT retain the new statement -- it is
    withheld pending human review in review-contradictions.py, which retains
    it itself on approve. Skipping this would silently re-introduce the
    "reject discards nothing" bug found on 2026-07-12 (docs/FINDINGS.md).
    """
    if not check_enabled():
        return Resolution(action="retain")

    try:
        memory_pairs = recall(bank_id, statement, max_results=5)
    except Exception:
        return Resolution(action="retain")

    if not memory_pairs:
        return Resolution(action="retain")

    memories = [text for _, text in memory_pairs]
    result = check_contradiction(statement, memories)
    if result.error or not result.contradicts:
        return Resolution(action="retain")

    idx = result.conflicting_memory_index
    if idx is None or not isinstance(idx, int) or not (0 <= idx < len(memory_pairs)):
        return Resolution(action="retain")

    conflicting_id, conflicting_text = memory_pairs[idx]

    if result.confidence >= auto_threshold():
        mode = auto_mode()
        deleted = False
        if mode == "live":
            deleted = delete_document(bank_id, conflicting_id)
        log_auto_resolved(
            bank_id, statement, conflicting_id, conflicting_text,
            result.confidence, result.explanation, mode, deleted,
        )
        return Resolution(
            action="auto_resolved", superseded_document_id=conflicting_id,
            confidence=result.confidence, explanation=result.explanation,
        )

    pending_queue.append_pending(
        new_statement=statement,
        conflicting_memory=conflicting_text,
        conflicting_memory_index=idx,
        explanation=result.explanation,
        document_id=conflicting_id,
    )
    return Resolution(
        action="queued", superseded_document_id=conflicting_id,
        confidence=result.confidence, explanation=result.explanation,
    )
