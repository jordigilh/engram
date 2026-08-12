#!/usr/bin/env python3
"""No-LLM heuristic extraction + local recovery buffer for retain resilience
(GitHub issue #5).

hindsight-api's structured-pattern extraction runs server-side (its own
internal Haiku call, configured by HINDSIGHT_API_RETAIN_LLM_MODEL) -- there
is no local Haiku call in nightly-learn.py's retain path to wrap. What IS
local and real: retain_windows()'s bare `except Exception` used to silently
drop a window whenever api_post() failed, with zero recovery. See
docs/FINDINGS.md 2026-07-29.

This module provides:
  - extract(text): a cheap, offline heuristic (capitalized-phrase/acronym
    entity detection, a topic keyword list tuned to this repo's own domain
    vocabulary, and a salience heuristic) -- mirrors tstockham96/engram's
    src/extract.ts in spirit, adapted to engineering corrections rather than
    general personal-assistant facts. Never calls an LLM, never raises.
  - record_fallback(...): appends a tagged entry to a local recovery buffer
    (~/.hindsight/logs/fallback-retained.jsonl) instead of losing the window
    outright, so it can be replayed once Vertex AI recovers (see
    nightly-learn.py's reprocess_fallback_backlog(), `--mode
    reprocess-fallback`).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_DIR = Path.home() / ".hindsight" / "logs"
FALLBACK_LOG_PATH = LOG_DIR / "fallback-retained.jsonl"

# Entity candidates: CamelCase/PascalCase words (e.g. "CocoIndex"),
# ALL_CAPS or SCREAMING_SNAKE_CASE acronyms/env-var names (e.g.
# "ENGRAM_CORRECTION_DETECTOR"), and hyphenated-or-plain multi-word Capitalized
# Phrases (e.g. "Hindsight API"). Deliberately simple regexes, not NLP --
# "works offline, costs nothing, handles most cases" per issue #5.
_CAMEL_CASE_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]*)+\b")
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_CAPITALIZED_PHRASE_RE = re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b")

# Topic keyword vocabulary pulled from docs/FINDINGS.md's own entry-title
# vocabulary (see issue #5) -- tuned to this repo's actual domains rather
# than generic personal-assistant topics.
TOPIC_KEYWORDS: dict[str, str] = {
    "contradiction resolution": "contradiction-resolution",
    "contradiction-resolution": "contradiction-resolution",
    "cocoindex": "cocoindex",
    "retain": "retain",
    "mental model": "mental-models",
    "mental models": "mental-models",
    "tagging": "tagging",
    "tag-scoped": "tagging",
    "launchd": "launchd",
    "correction gate": "correction-gate",
    "correction detection": "correction-gate",
    "watermark": "watermark",
    "hindsight": "hindsight",
    "vertex ai": "vertex-ai",
    "haiku": "haiku",
    "sonnet": "sonnet",
    "recall": "recall",
    "project scoping": "project-scoping",
}

# Reuses nightly-learn.py's own INSTRUCTION_PATTERNS-style vocabulary for
# "this sounds important" signal words.
HIGH_SALIENCE_WORDS = {
    "mandatory", "always", "never", "must", "critical", "required",
    "breaking", "regression", "outage", "production",
}
LOW_SALIENCE_WORDS = {"maybe", "perhaps", "might", "could", "someday", "eventually"}

_MIN_TEXT_LEN = 8


def extract(text: str) -> dict[str, Any]:
    """Cheap, offline entity/topic/salience extraction. Never raises."""
    if not text or len(text.strip()) < _MIN_TEXT_LEN:
        return {"entities": [], "topics": [], "salience": "low"}

    entities: list[str] = []
    for pattern in (_CAMEL_CASE_RE, _ACRONYM_RE, _CAPITALIZED_PHRASE_RE):
        for match in pattern.findall(text):
            if match not in entities:
                entities.append(match)

    lowered = text.lower()
    topics: list[str] = []
    for keyword, slug in TOPIC_KEYWORDS.items():
        if keyword in lowered and slug not in topics:
            topics.append(slug)

    words = set(re.findall(r"[a-z']+", lowered))
    if words & HIGH_SALIENCE_WORDS:
        salience = "high"
    elif words & LOW_SALIENCE_WORDS:
        salience = "low"
    else:
        salience = "medium" if (entities or topics) else "low"

    return {"entities": entities, "topics": topics, "salience": salience}


def record_fallback(
    window: str,
    transcript_id: str,
    project: str | None,
    extracted: dict[str, Any],
    reason: str,
) -> None:
    """Append a tagged fallback-extraction entry to the local recovery
    buffer, instead of losing the window when api_post() fails transiently.
    """
    FALLBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "transcript_id": transcript_id,
        "project": project,
        "window": window,
        "extracted": extracted,
        "reason": reason,
        "tags": ["fallback-extraction"],
    }
    with open(FALLBACK_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_backlog() -> list[dict[str, Any]]:
    """Return every buffered fallback entry, in the order they were written.
    Malformed lines are skipped, not fatal (matches report.py's
    count_pending_contradictions() convention)."""
    if not FALLBACK_LOG_PATH.exists():
        return []
    entries = []
    with open(FALLBACK_LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_backlog(entries: list[dict[str, Any]]) -> None:
    """Atomically overwrite the backlog with exactly the given entries --
    same tmp-then-rename pattern as nightly-learn.py's save_watermarks()."""
    FALLBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FALLBACK_LOG_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    os.replace(tmp, FALLBACK_LOG_PATH)


def count_backlog() -> int:
    return len(load_backlog())
