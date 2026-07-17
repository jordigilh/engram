#!/usr/bin/env python3
"""Shared correction-detection gate for production write paths
(nightly-learn.py, cocoindex-flows.py).

Replaces the regex-only CORRECTION_PATTERNS gate with spike/classify.py's
Haiku-based classify_correction(), which the 2026-07-08 prefilter shadow
trial measured at ~0.97 F1 against 630 Haiku-confirmed corrections across a
14-day/3,873-message backfill, vs. ~24.4% recall for the best regex/keyword
candidate tested (see docs/FINDINGS.md). ENGRAM_CORRECTION_DETECTOR=regex is
kept as a one-line rollback to the caller's own (unmodified, still-present)
regex pattern list.

Also ports the boilerplate-message filter found during that same shadow
trial: Cursor injects several role="user" templates (subagent-completion
prompts, <system_reminder>/<mcp_server_catalog>/... wrapper tags) that were
never typed by a human and that a semantic classifier seeing 100% of raw
traffic can misread as corrections. The old regex gate never shared
vocabulary with these templates so it was accidentally immune; adopting
semantic classification requires porting the filter too (see docs/FINDINGS.md
2026-07-08 "Found and Fixed a System-Boilerplate Contamination Bug").
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Pattern

sys.path.insert(0, str(Path(__file__).resolve().parent / "spike"))
from classify import classify_correction  # noqa: E402

CACHE_PATH = Path(os.path.expanduser("~/.hindsight/logs/correction-cache.json"))

# See prefilter-shadow-trial.py's identical filter -- kept in sync with it.
_BOILERPLATE_PREFIXES = (
    "Briefly inform the user about the task result and perform any follow-up actions",
    "The beginning of the above subagent result is already visible to the user.",
)
_BOILERPLATE_TAG_RE = re.compile(
    r"^\s*<(system_reminder|attached_files|system_notification|mcp_server_catalog|user_info)\b"
)


def is_system_boilerplate(text: str) -> bool:
    """True for Cursor-injected templates that appear with role="user" but were
    never typed by a human (subagent-completion prompts, system-reminder/tool-
    catalog wrapper tags).
    """
    stripped = text.strip()
    if any(stripped.startswith(p) for p in _BOILERPLATE_PREFIXES):
        return True
    return bool(_BOILERPLATE_TAG_RE.match(stripped))


_cache_lock = threading.Lock()
_cache: dict[str, dict] | None = None


def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Per-writer tmp filename (pid + thread id): a fixed ".tmp" name races
    # when two writers save concurrently -- either transcript-app threads in
    # this process, or a separate process like nightly-learn.py sharing the
    # same CACHE_PATH -- since threading.Lock() never spans processes and
    # can't prevent that. One writer's rename() would raise FileNotFoundError
    # after another already moved the shared tmp file away first. Observed in
    # production 2026-07-17 (see docs/FINDINGS.md). Path.replace() is used
    # (not rename()) so the final swap onto CACHE_PATH is still atomic even
    # if two writers finish at nearly the same time.
    tmp = CACHE_PATH.with_name(f"{CACHE_PATH.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(_cache))
    tmp.replace(CACHE_PATH)


def classify_cached(text: str) -> bool:
    """Haiku classify_correction(), memoized on disk by sha256(text).

    Necessary because cocoindex-flows.py's process_transcript reprocesses the
    entire transcript file's content on every change -- confirmed via
    CocoIndex's own File.__coco_memo_state__, which fingerprints the whole
    file and invalidates the memo (reruns the whole function) on any content
    change, not just the new tail. Without this cache, a long session would
    re-classify every earlier message with Haiku on every subsequent message
    (O(n^2) calls per session).
    """
    key = hashlib.sha256(text.encode()).hexdigest()
    with _cache_lock:
        cache = _load_cache()
        cached = cache.get(key)
        if cached is not None:
            return bool(cached["is_correction"])

    result = classify_correction(text)
    is_corr = bool(result.is_correction and not result.error)

    with _cache_lock:
        cache = _load_cache()
        cache[key] = {"is_correction": is_corr, "category": result.category}
        _save_cache()

    return is_corr


def detector_mode() -> str:
    """ENGRAM_CORRECTION_DETECTOR=haiku (default) | regex. One-line rollback
    switch: set to 'regex' to revert to the caller's own pattern list without
    a code change.
    """
    mode = os.environ.get("ENGRAM_CORRECTION_DETECTOR", "haiku").strip().lower()
    return mode if mode in ("haiku", "regex") else "haiku"


def is_correction(text: str, regex_patterns: list[Pattern[str]]) -> bool:
    """Correction-detection gate for production write paths.

    ENGRAM_CORRECTION_DETECTOR=haiku (default): boilerplate-filtered,
    disk-cached Haiku classification.
    ENGRAM_CORRECTION_DETECTOR=regex: instant rollback to regex_patterns
    (the caller's own, unmodified pattern list).
    """
    if not text or len(text) > 2000:
        return False
    if is_system_boilerplate(text):
        return False
    if detector_mode() == "regex":
        return any(pat.search(text) for pat in regex_patterns)
    return classify_cached(text)
