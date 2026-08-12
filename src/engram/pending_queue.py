"""contradictions-pending.jsonl: local queue of flagged contradictions that
were withheld from hindsight_retain() pending human confirmation.

Never auto-retained. review_contradictions.py is the only consumer that
removes entries (on approve/reject); report.py only reads the count.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

QUEUE_PATH = os.path.expanduser("~/.hindsight/logs/contradictions-pending.jsonl")


def append_pending(
    new_statement: str,
    conflicting_memory: str,
    conflicting_memory_index: int | None,
    explanation: str,
    memory_id: str | None = None,
    document_id: str | None = None,
    project: str | None = None,
) -> dict:
    """Append a flagged contradiction to the pending-review queue.

    Deduplicates on (new_statement, memory_id) before appending -- see
    docs/FINDINGS.md 2026-07-31. cocoindex-flows.py's live transcript
    watcher re-reads and re-scans the WHOLE transcript file from scratch on
    every file-change event (no watermark, unlike nightly-learn.py's
    hourly/nightly hash+position tracking), so a correction sitting anywhere
    in an actively-growing session gets re-extracted and re-checked against
    the same conflicting memory on every subsequent message written to that
    same session -- confirmed in production: one real contradiction was
    queued 104 times over 3 days from a single long-lived kubernaut session.
    Without this guard, every caller of contradiction_resolution.resolve()
    is exposed to the same risk, not just cocoindex-flows.py.
    """
    for existing in load_pending():
        if existing.get("new_statement") == new_statement and existing.get("memory_id") == memory_id:
            return existing

    entry = {
        "id": str(uuid.uuid4()),
        "new_statement": new_statement,
        "conflicting_memory": conflicting_memory,
        "conflicting_memory_index": conflicting_memory_index,
        "explanation": explanation,
        "memory_id": memory_id,
        "document_id": document_id,
        "project": project,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    with open(QUEUE_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def load_pending() -> list[dict]:
    if not os.path.exists(QUEUE_PATH):
        return []
    entries = []
    with open(QUEUE_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_pending(entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    with open(QUEUE_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def remove_pending(entry_id: str) -> bool:
    entries = load_pending()
    remaining = [e for e in entries if e.get("id") != entry_id]
    if len(remaining) == len(entries):
        return False
    save_pending(remaining)
    return True


def count_pending() -> int:
    return len(load_pending())
