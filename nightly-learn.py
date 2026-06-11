#!/usr/bin/env python3
"""Hindsight nightly learning script.

Scans today's Cursor agent transcripts, detects corrections, and feeds
annotated sessions to Hindsight for pattern extraction and reflection.
"""

import json
import logging
import os
import re
import sys
from datetime import date, datetime
from glob import glob
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HINDSIGHT_URL = "http://localhost:8888"
BANK_ID = "cursor-memory"
TRANSCRIPTS_GLOB = os.path.expanduser(
    "~/.cursor/projects/*/agent-transcripts/**/*.jsonl"
)
LOG_DIR = Path.home() / ".hindsight" / "logs"
MAX_CONTENT_LEN = 12000  # max chars per retain item to control token usage

CORRECTION_PATTERNS = [
    re.compile(r"\bno[,.]?\s+that'?s\s+(not|wrong|incorrect)", re.I),
    re.compile(r"\bdon'?t\s+do\s+that", re.I),
    re.compile(r"\bI\s+(said|meant)\s+", re.I),
    re.compile(r"\bwrong\s+(file|path|dir|approach|method|function|model|endpoint)", re.I),
    re.compile(r"\bthat\s+broke", re.I),
    re.compile(r"\bundo\s+(that|this|it)", re.I),
    re.compile(r"\bthat'?s\s+not\s+what\s+I", re.I),
    re.compile(r"\byou\s+(shouldn'?t|should\s+not)\s+have", re.I),
    re.compile(r"\bdo\s+not\s+use\b", re.I),
    re.compile(r"\bwe\s+don'?t\s+use\b", re.I),
]

STRUCTURAL_CORRECTION_PATTERNS = [
    re.compile(r"^(no|nope|wrong|incorrect)\s*[.!]?\s*$", re.I | re.M),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def api_post(path: str, payload: dict) -> dict:
    """POST JSON to Hindsight API."""
    url = f"{HINDSIGHT_URL}{path}"
    data = json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log.error("API error %s %s: %s", e.code, url, body[:500])
        raise
    except URLError as e:
        log.error("Connection error %s: %s", url, e.reason)
        raise


def find_recent_transcripts(hours: int = 24) -> list[Path]:
    """Find transcript files modified in the last N hours."""
    cutoff = datetime.now().timestamp() - (hours * 3600)
    results = []
    for path_str in glob(TRANSCRIPTS_GLOB, recursive=True):
        p = Path(path_str)
        if p.stat().st_mtime >= cutoff:
            results.append(p)
    return sorted(results, key=lambda p: p.stat().st_mtime)


def parse_transcript(path: Path) -> list[dict]:
    """Parse a JSONL transcript into a list of messages."""
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                messages.append(obj)
            except json.JSONDecodeError:
                continue
    return messages


def extract_user_text(msg: dict) -> str:
    """Extract plain text from a user message."""
    content = msg.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            # Strip system/external_links XML wrappers, keep user_query
            match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
            if match:
                texts.append(match.group(1))
            elif not text.startswith("<external_links>"):
                texts.append(text)
    return "\n".join(texts).strip()


def extract_assistant_text(msg: dict) -> str:
    """Extract text content from assistant message (skip tool_use blocks)."""
    content = msg.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "\n".join(texts).strip()


def is_correction(text: str) -> bool:
    """Detect if a user message looks like a correction."""
    if not text or len(text) > 2000:
        return False
    for pat in CORRECTION_PATTERNS:
        if pat.search(text):
            return True
    for pat in STRUCTURAL_CORRECTION_PATTERNS:
        if pat.search(text):
            return True
    return False


def extract_correction_windows(messages: list[dict], window: int = 2) -> list[str]:
    """Extract only the messages around corrections (not full transcript).

    Returns a list of correction "windows" - each containing context
    before the correction, the correction itself, and what follows.
    This dramatically reduces token usage vs sending full transcripts.
    """
    parsed = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "user":
            text = extract_user_text(msg)
            if text:
                parsed.append({"role": "user", "text": text, "is_correction": is_correction(text)})
        elif role == "assistant":
            text = extract_assistant_text(msg)
            if text:
                parsed.append({"role": "assistant", "text": text[:400], "is_correction": False})

    correction_indices = [i for i, m in enumerate(parsed) if m["is_correction"]]
    if not correction_indices:
        return []

    windows = []
    used = set()
    for idx in correction_indices:
        start = max(0, idx - window)
        end = min(len(parsed), idx + window + 1)
        if idx in used:
            continue
        used.add(idx)

        lines = []
        for i in range(start, end):
            m = parsed[i]
            prefix = "[CORRECTION] " if m["is_correction"] else ""
            lines.append(f"{prefix}{m['role'].title()}: {m['text'][:300]}")
        windows.append("\n\n".join(lines))

    return windows


def retain_windows(windows: list[str], transcript_id: str) -> dict[str, Any]:
    """Send correction windows to Hindsight retain endpoint."""
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    items_retained = 0

    for i, window in enumerate(windows):
        payload = {
            "items": [
                {
                    "content": window,
                    "metadata": {
                        "source": "cursor-transcript",
                        "transcript_id": transcript_id,
                        "window": str(i),
                    },
                }
            ]
        }
        try:
            result = api_post(
                f"/v1/default/banks/{BANK_ID}/memories", payload
            )
            if result.get("success"):
                items_retained += result.get("items_count", 0)
                usage = result.get("usage", {})
                for k in total_usage:
                    total_usage[k] += usage.get(k, 0)
        except Exception as e:
            log.warning("Retain failed for window %d of %s: %s", i, transcript_id, e)

    return {"items_retained": items_retained, "usage": total_usage}


def reflect() -> dict[str, Any]:
    """Use reflect to evaluate accumulated correction patterns."""
    payload = {
        "query": (
            "Based on the corrections and mistakes you've seen, what are the top 3 "
            "recurring patterns where the assistant made errors? For each, state what "
            "went wrong and what should be done instead."
        ),
        "budget": "low",
        "max_tokens": 1024,
    }
    try:
        result = api_post(f"/v1/default/banks/{BANK_ID}/reflect", payload)
        return result
    except Exception as e:
        log.warning("Reflect failed: %s", e)
        return {"error": str(e)}


def main():
    log.info("=== Hindsight nightly learning started ===")

    transcripts = find_recent_transcripts(hours=24)
    log.info("Found %d transcripts from last 24h", len(transcripts))

    if not transcripts:
        log.info("No transcripts to process. Exiting.")
        sys.exit(0)

    results = {
        "date": date.today().isoformat(),
        "transcripts_found": len(transcripts),
        "transcripts_with_corrections": 0,
        "corrections_detected": 0,
        "windows_retained": 0,
        "total_retain_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "reflect_result": None,
        "errors": [],
    }

    for transcript_path in transcripts:
        transcript_id = transcript_path.stem
        log.info("Processing: %s", transcript_id)

        messages = parse_transcript(transcript_path)
        if len(messages) < 4:
            log.info("  Skipping (too short: %d messages)", len(messages))
            continue

        windows = extract_correction_windows(messages)
        if not windows:
            log.info("  No corrections found, skipping")
            continue

        results["corrections_detected"] += len(windows)
        results["transcripts_with_corrections"] += 1
        log.info("  Found %d correction windows", len(windows))

        try:
            retain_result = retain_windows(windows, transcript_id)
            results["windows_retained"] += retain_result["items_retained"]
            for k in results["total_retain_usage"]:
                results["total_retain_usage"][k] += retain_result["usage"].get(k, 0)
            log.info(
                "  Retained: %d items, %d tokens",
                retain_result["items_retained"],
                retain_result["usage"]["total_tokens"],
            )
        except Exception as e:
            results["errors"].append({"transcript": transcript_id, "error": str(e)})
            log.error("  Failed: %s", e)

    # Phase: Reflect on accumulated patterns
    if results["windows_retained"] > 0:
        log.info("Running reflect...")
        results["reflect_result"] = reflect()

    # Write daily log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{date.today().isoformat()}.json"
    with open(log_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info("Results saved to %s", log_path)

    log.info(
        "=== Done: %d transcripts w/corrections, %d windows, %d tokens ===",
        results["transcripts_with_corrections"],
        results["corrections_detected"],
        results["total_retain_usage"]["total_tokens"],
    )


if __name__ == "__main__":
    main()
