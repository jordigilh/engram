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
    document_id: str | None = None,
    project: str | None = None,
) -> dict:
    entry = {
        "id": str(uuid.uuid4()),
        "new_statement": new_statement,
        "conflicting_memory": conflicting_memory,
        "conflicting_memory_index": conflicting_memory_index,
        "explanation": explanation,
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
