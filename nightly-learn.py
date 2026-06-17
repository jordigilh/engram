#!/usr/bin/env python3
"""Hindsight learning script (hourly retain + nightly reflect).

Scans Cursor agent transcripts, detects corrections, and feeds
annotated sessions to Hindsight for pattern extraction and reflection.

Modes:
  --mode hourly   Retain only (watermark filter + hash dedup). Run by hourly launchd job.
  --mode nightly  Catch-all retain + reflect + probes + metrics. Run by 2am launchd job.
  --mode both     Run hourly then nightly (default, backward compat for manual runs).
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
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

# Patterns that indicate the user is establishing methodology, process, or requirements.
# These are instructional — not corrections, but equally valuable for memory.
INSTRUCTION_PATTERNS = [
    re.compile(r"\balways\s+(use|follow|run|start\s+with|ensure)", re.I),
    re.compile(r"\bnever\s+(skip|push|commit|deploy|use)", re.I),
    re.compile(r"\bmandatory\b", re.I),
    re.compile(r"\bour\s+(workflow|process|methodology|convention|standard)", re.I),
    re.compile(r"\bthe\s+rule\s+is\b", re.I),
    re.compile(r"\bfor\s+this\s+(project|repo|team)\s+we\b", re.I),
    re.compile(r"\bwe\s+(always|never|require|must)\b", re.I),
    re.compile(r"\bbefore\s+(implementing|proceeding|starting\s+any)", re.I),
]

STATE_DIR = Path.home() / ".hindsight"
WATERMARKS_PATH = STATE_DIR / "watermarks.json"
RETAINED_HASHES_PATH = STATE_DIR / "retained-hashes.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# --- State file helpers ---

def load_watermarks() -> dict:
    """Load transcript processing watermarks from disk."""
    if WATERMARKS_PATH.exists():
        try:
            return json.loads(WATERMARKS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt watermarks.json, starting fresh")
    return {}


def save_watermarks(watermarks: dict) -> None:
    """Atomically save watermarks to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WATERMARKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(watermarks, indent=2))
    tmp.rename(WATERMARKS_PATH)


def load_retained_hashes() -> set:
    """Load set of already-retained content hashes."""
    if RETAINED_HASHES_PATH.exists():
        try:
            data = json.loads(RETAINED_HASHES_PATH.read_text())
            return set(data.get("hashes", []))
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt retained-hashes.json, starting fresh")
    return set()


def save_retained_hashes(hashes: set) -> None:
    """Atomically save retained hashes to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RETAINED_HASHES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"hashes": sorted(hashes)}))
    tmp.rename(RETAINED_HASHES_PATH)


def prune_watermarks(watermarks: dict, max_age_days: int = 7) -> dict:
    """Remove watermark entries not seen in max_age_days."""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    return {
        tid: wm for tid, wm in watermarks.items()
        if wm.get("last_processed", "") >= cutoff
    }


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


def is_instruction(text: str) -> bool:
    """Detect if a user message establishes methodology, process, or requirements."""
    if not text or len(text) < 20 or len(text) > 2000:
        return False
    for pat in INSTRUCTION_PATTERNS:
        if pat.search(text):
            return True
    return False


def extract_learning_windows(
    messages: list[dict], window: int = 2, start_index: int = 0
) -> tuple[list[str], list[str]]:
    """Extract messages around corrections AND instructions.

    Args:
        messages: Full message list (used for context windows).
        window: Number of surrounding messages to include.
        start_index: Only generate windows for corrections/instructions at raw
                     message indices >= start_index. Context still drawn from
                     the full list.

    Returns two lists:
      - correction_windows: context around user corrections
      - instruction_windows: context around methodology/process statements
    """
    parsed = []
    raw_to_parsed = {}  # maps raw message index -> parsed index
    for raw_idx, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "user":
            text = extract_user_text(msg)
            if text:
                raw_to_parsed[raw_idx] = len(parsed)
                parsed.append({
                    "role": "user",
                    "text": text,
                    "is_correction": is_correction(text),
                    "is_instruction": is_instruction(text),
                    "raw_idx": raw_idx,
                })
        elif role == "assistant":
            text = extract_assistant_text(msg)
            if text:
                raw_to_parsed[raw_idx] = len(parsed)
                parsed.append({
                    "role": "assistant",
                    "text": text[:400],
                    "is_correction": False,
                    "is_instruction": False,
                    "raw_idx": raw_idx,
                })

    def _build_windows(indices: list[int], tag: str) -> list[str]:
        windows = []
        used = set()
        for idx in indices:
            if idx in used:
                continue
            used.add(idx)
            start = max(0, idx - window)
            end = min(len(parsed), idx + window + 1)
            lines = []
            for i in range(start, end):
                m = parsed[i]
                prefix = f"[{tag}] " if i == idx else ""
                lines.append(f"{prefix}{m['role'].title()}: {m['text'][:300]}")
            windows.append("\n\n".join(lines))
        return windows

    correction_indices = [
        i for i, m in enumerate(parsed)
        if m["is_correction"] and m["raw_idx"] >= start_index
    ]
    instruction_indices = [
        i for i, m in enumerate(parsed)
        if m["is_instruction"] and not m["is_correction"] and m["raw_idx"] >= start_index
    ]

    return (
        _build_windows(correction_indices, "CORRECTION"),
        _build_windows(instruction_indices, "INSTRUCTION"),
    )


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


def filter_and_scan(
    transcripts: list[Path], watermarks: dict
) -> list[tuple[Path, list[dict], int]]:
    """Return transcripts with new learning signals past their watermark.

    Returns list of (path, full_messages, start_index) tuples.
    Updates watermarks dict in place for all scanned transcripts.
    """
    candidates = []
    for path in transcripts:
        stat = path.stat()
        tid = path.stem
        wm = watermarks.get(tid, {})

        # Layer 1: size gate — skip unchanged files
        if stat.st_size <= wm.get("size", 0):
            continue

        messages = parse_transcript(path)
        prev_count = wm.get("message_count", 0)
        new_messages = messages[prev_count:]

        if not new_messages:
            # File grew (maybe trailing newline) but no new parseable messages
            watermarks[tid] = {
                "size": stat.st_size,
                "message_count": len(messages),
                "last_processed": datetime.now().isoformat(),
            }
            continue

        # Layer 2: regex pre-filter on new messages only
        has_signal = False
        for m in new_messages:
            if m.get("role") != "user":
                continue
            text = extract_user_text(m)
            if is_correction(text) or is_instruction(text):
                has_signal = True
                break

        # Update watermark regardless (we've scanned these messages)
        watermarks[tid] = {
            "size": stat.st_size,
            "message_count": len(messages),
            "last_processed": datetime.now().isoformat(),
        }

        if has_signal:
            candidates.append((path, messages, prev_count))

    return candidates


def retain_windows_deduped(
    windows: list[str], transcript_id: str, seen_hashes: set
) -> dict[str, Any]:
    """Retain only windows whose content hasn't been retained before."""
    new_windows = []
    for w in windows:
        h = hashlib.sha256(w.encode()).hexdigest()
        if h not in seen_hashes:
            new_windows.append(w)
            seen_hashes.add(h)

    skipped = len(windows) - len(new_windows)
    if not new_windows:
        return {
            "items_retained": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "skipped_duplicates": skipped,
        }

    result = retain_windows(new_windows, transcript_id)
    result["skipped_duplicates"] = skipped
    return result


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


RECALL_SIGNALS_PATH = LOG_DIR / "recall-signals.jsonl"
BANKS = ["cursor-memory", "kubernaut-docs", "kubernaut-issues"]


def api_get(path: str) -> dict:
    """GET from Hindsight API."""
    url = f"{HINDSIGHT_URL}{path}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except (HTTPError, URLError) as e:
        log.warning("GET %s failed: %s", url, e)
        return {}


def collect_bank_stats() -> dict:
    """Collect stats from all configured banks and write to recall-signals.jsonl."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stats = {}
    for bank in BANKS:
        bank_stats = api_get(f"/v1/default/banks/{bank}/stats")
        if bank_stats:
            stats[bank] = {
                "total_nodes": bank_stats.get("total_nodes", 0),
                "total_documents": bank_stats.get("total_documents", 0),
                "total_links": bank_stats.get("total_links", 0),
                "pending_operations": bank_stats.get("pending_operations", 0),
                "pending_consolidation": bank_stats.get("pending_consolidation", 0),
            }
    signal = {
        "ts": datetime.now().isoformat(),
        "type": "bank_stats",
        "banks": stats,
    }
    with open(RECALL_SIGNALS_PATH, "a") as f:
        f.write(json.dumps(signal) + "\n")
    return stats


def measure_recall_quality(bank: str, query: str) -> dict:
    """Run a recall and measure latency/results for observability."""
    payload = {"query": query, "max_tokens": 2048, "include": {"chunks": {}}}
    start = time.time()
    try:
        result = api_post(f"/v1/default/banks/{bank}/memories/recall", payload)
        latency_ms = int((time.time() - start) * 1000)
        results_list = result.get("results", [])
        chunks = result.get("chunks", {})
        num_chunks = len(chunks) if isinstance(chunks, dict) else 0
        approx_tokens = sum(
            len(c.get("text", "")) // 4
            for c in (chunks.values() if isinstance(chunks, dict) else [])
            if isinstance(c, dict)
        )
        signal = {
            "ts": datetime.now().isoformat(),
            "type": "recall_probe",
            "query": query,
            "bank": bank,
            "latency_ms": latency_ms,
            "results": len(results_list),
            "chunks": num_chunks,
            "approx_tokens": approx_tokens,
        }
        with open(RECALL_SIGNALS_PATH, "a") as f:
            f.write(json.dumps(signal) + "\n")
        return signal
    except Exception as e:
        log.warning("Recall probe failed for %s: %s", bank, e)
        return {"error": str(e)}


def run_observability_probes():
    """Run a set of recall probes to measure system health and quality."""
    probes = [
        ("cursor-memory", "Go testing conventions and patterns"),
        ("kubernaut-docs", "signal processing architecture and data flow"),
        ("kubernaut-docs", "remediation orchestrator CRD spec"),
        ("kubernaut-issues", "rate limiter design decisions and requirements"),
        ("kubernaut-issues", "A2A streaming event structure"),
    ]
    results = []
    for bank, query in probes:
        r = measure_recall_quality(bank, query)
        results.append(r)
        if "error" not in r:
            log.info(
                "  Probe [%s] %s: %dms, %d results, %d chunks, ~%d tokens",
                bank, query[:40], r["latency_ms"], r["results"],
                r.get("chunks", 0), r.get("approx_tokens", 0),
            )
    return results


MCP_CALLS_LOG = LOG_DIR / "mcp-calls.jsonl"
EFFECTIVENESS_LOG = LOG_DIR / "effectiveness-report.jsonl"


def analyze_mcp_effectiveness(transcripts: list[Path], hours: int = 24) -> dict:
    """Analyze MCP usage from hook logs and correlate with correction rates.

    Reads mcp-calls.jsonl (written by afterMCPExecution hook) and correlates
    with per-session correction counts to measure effectiveness.
    """
    cutoff = datetime.now().timestamp() - (hours * 3600)

    # Phase 1: Aggregate MCP call stats from hook log
    mcp_usage: dict[str, dict] = {}
    if MCP_CALLS_LOG.exists():
        with open(MCP_CALLS_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("ts", "")
                    try:
                        entry_ts = datetime.fromisoformat(ts_str).timestamp()
                    except ValueError:
                        continue
                    if entry_ts < cutoff:
                        continue
                    server = entry.get("server", "unknown")
                    if server not in mcp_usage:
                        mcp_usage[server] = {"calls": 0, "hits": 0, "misses": 0}
                    mcp_usage[server]["calls"] += 1
                    if entry.get("hit"):
                        mcp_usage[server]["hits"] += 1
                    else:
                        mcp_usage[server]["misses"] += 1
                except json.JSONDecodeError:
                    continue

    for server, stats in mcp_usage.items():
        total = stats["calls"]
        stats["hit_rate"] = round(stats["hits"] / total, 3) if total > 0 else 0.0

    # Phase 2: Per-session analysis from transcripts
    # Tracks: recall usage, proactive recall, context loading cost, K-curve effectiveness
    PRODUCTIVE_TOOLS = {"Shell", "Write", "StrReplace", "EditNotebook", "Delete"}
    EXPLORATION_TOOLS = {"Read", "Grep", "Glob", "SemanticSearch", "Task", "WebSearch"}

    sessions_with_recall = []
    sessions_without_recall = []

    proactive_recall_sessions = 0
    total_agent_turns = 0
    agent_turns_with_recall = 0

    for transcript_path in transcripts:
        messages = parse_transcript(transcript_path)
        if len(messages) < 6:
            continue

        has_recall = False
        first_recall_turn = None
        user_requested_recall = False
        turn_idx = 0

        # K-curve tracking
        first_productive_turn = None
        preamble_chars = 0
        total_session_chars = 0
        productive_action_count = 0

        for msg in messages:
            role = msg.get("role") or msg.get("message", {}).get("role", "")
            content = msg.get("message", {}).get("content", [])

            # Estimate message size in characters
            msg_chars = 0
            if isinstance(content, str):
                msg_chars = len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            msg_chars += len(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            msg_chars += len(json.dumps(block.get("input", {})))
            total_session_chars += msg_chars

            if role == "user":
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                else:
                    text = ""
                if re.search(r"\b(recall|memory|hindsight|remember)\b", text, re.IGNORECASE):
                    user_requested_recall = True
                if first_productive_turn is None:
                    preamble_chars += msg_chars

            if role == "assistant":
                turn_idx += 1
                total_agent_turns += 1
                turn_has_recall = False
                turn_has_productive = False

                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "")
                            if name == "CallMcpTool":
                                inp = block.get("input", {})
                                tool = inp.get("toolName", "")
                                if "recall" in tool.lower():
                                    has_recall = True
                                    turn_has_recall = True
                                    if first_recall_turn is None:
                                        first_recall_turn = turn_idx
                            if name in PRODUCTIVE_TOOLS:
                                productive_action_count += 1
                                turn_has_productive = True

                if turn_has_recall:
                    agent_turns_with_recall += 1
                if turn_has_productive and first_productive_turn is None:
                    first_productive_turn = turn_idx
                if first_productive_turn is None:
                    preamble_chars += msg_chars

        # Proactive = recall happened before user mentioned memory-related keywords
        if has_recall and not user_requested_recall:
            proactive_recall_sessions += 1
        elif has_recall and first_recall_turn == 1:
            proactive_recall_sessions += 1

        corrections, _ = extract_learning_windows(messages)
        correction_count = len(corrections)

        # Token estimates (chars / 4)
        context_loading_tokens = round(preamble_chars / 4)
        total_session_tokens = round(total_session_chars / 4)
        effectiveness_ratio = round(
            productive_action_count / (total_session_tokens / 1000), 3
        ) if total_session_tokens > 0 else 0.0

        bucket = (
            "small" if total_session_tokens < 50000
            else "medium" if total_session_tokens < 500000
            else "large"
        )
        session_info = {
            "transcript_id": transcript_path.stem,
            "corrections": correction_count,
            "messages": len(messages),
            "first_productive_turn": first_productive_turn or turn_idx,
            "context_loading_tokens": context_loading_tokens,
            "total_session_tokens": total_session_tokens,
            "productive_actions": productive_action_count,
            "effectiveness_ratio": effectiveness_ratio,
            "size_bucket": bucket,
        }

        if has_recall:
            sessions_with_recall.append(session_info)
        else:
            sessions_without_recall.append(session_info)

    # Phase 3: Compute effectiveness metrics
    def _avg(items, key):
        if not items:
            return 0.0
        return round(sum(s[key] for s in items) / len(items), 2)

    total_sessions = len(sessions_with_recall) + len(sessions_without_recall)
    recall_adoption_pct = round(
        len(sessions_with_recall) / total_sessions * 100, 1
    ) if total_sessions > 0 else 0.0
    proactive_pct = round(
        proactive_recall_sessions / total_sessions * 100, 1
    ) if total_sessions > 0 else 0.0
    turn_recall_pct = round(
        agent_turns_with_recall / total_agent_turns * 100, 1
    ) if total_agent_turns > 0 else 0.0

    # K-curve computation
    eff_ratio_with = _avg(sessions_with_recall, "effectiveness_ratio")
    eff_ratio_without = _avg(sessions_without_recall, "effectiveness_ratio")
    k_score = round(eff_ratio_with / eff_ratio_without, 2) if eff_ratio_without > 0 else None

    ctx_load_with = _avg(sessions_with_recall, "context_loading_tokens")
    ctx_load_without = _avg(sessions_without_recall, "context_loading_tokens")
    ctx_reduction_pct = round(
        (1 - ctx_load_with / ctx_load_without) * 100, 1
    ) if ctx_load_without > 0 else None

    token_eff_gain_pct = round(
        (eff_ratio_with / eff_ratio_without - 1) * 100, 1
    ) if eff_ratio_without > 0 else None

    # Per-bucket K-scores for normalized comparison
    k_curve_by_bucket = {}
    weighted_scores = []
    for bucket in ("small", "medium", "large"):
        with_b = [s for s in sessions_with_recall if s["size_bucket"] == bucket]
        without_b = [s for s in sessions_without_recall if s["size_bucket"] == bucket]
        if with_b and without_b:
            eff_w = _avg(with_b, "effectiveness_ratio")
            eff_wo = _avg(without_b, "effectiveness_ratio")
            bucket_k = round(eff_w / eff_wo, 2) if eff_wo > 0 else None
            k_curve_by_bucket[bucket] = {
                "with_recall": len(with_b),
                "without_recall": len(without_b),
                "k_score": bucket_k,
            }
            if bucket_k is not None:
                weighted_scores.append((bucket_k, len(with_b) + len(without_b)))

    k_score_normalized = None
    if weighted_scores:
        total_weight = sum(w for _, w in weighted_scores)
        k_score_normalized = round(
            sum(score * w for score, w in weighted_scores) / total_weight, 2
        )

    report = {
        "date": date.today().isoformat(),
        "period_hours": hours,
        "mcp_usage": mcp_usage,
        "effectiveness": {
            "sessions_with_recall": len(sessions_with_recall),
            "sessions_without_recall": len(sessions_without_recall),
            "corrections_per_session_with_recall": _avg(sessions_with_recall, "corrections"),
            "corrections_per_session_without_recall": _avg(sessions_without_recall, "corrections"),
        },
        "proactive_recall": {
            "total_sessions": total_sessions,
            "sessions_with_any_recall": len(sessions_with_recall),
            "sessions_with_proactive_recall": proactive_recall_sessions,
            "recall_adoption_pct": recall_adoption_pct,
            "proactive_pct": proactive_pct,
            "total_agent_turns": total_agent_turns,
            "agent_turns_with_recall": agent_turns_with_recall,
            "turn_recall_pct": turn_recall_pct,
        },
        "k_curve": {
            "with_recall": {
                "sessions": len(sessions_with_recall),
                "avg_context_loading_tokens": ctx_load_with,
                "avg_productive_actions": _avg(sessions_with_recall, "productive_actions"),
                "avg_corrections": _avg(sessions_with_recall, "corrections"),
                "avg_total_tokens": _avg(sessions_with_recall, "total_session_tokens"),
                "effectiveness_ratio": eff_ratio_with,
            },
            "without_recall": {
                "sessions": len(sessions_without_recall),
                "avg_context_loading_tokens": ctx_load_without,
                "avg_productive_actions": _avg(sessions_without_recall, "productive_actions"),
                "avg_corrections": _avg(sessions_without_recall, "corrections"),
                "avg_total_tokens": _avg(sessions_without_recall, "total_session_tokens"),
                "effectiveness_ratio": eff_ratio_without,
            },
            "k_score": k_score,
            "k_score_normalized": k_score_normalized,
            "by_bucket": k_curve_by_bucket,
            "context_loading_reduction_pct": ctx_reduction_pct,
            "token_efficiency_gain_pct": token_eff_gain_pct,
        },
        "token_signals": {
            "avg_session_messages_with_recall": _avg(sessions_with_recall, "messages"),
            "avg_session_messages_without_recall": _avg(sessions_without_recall, "messages"),
        },
    }

    # Compute estimated reduction percentage
    with_rate = report["effectiveness"]["corrections_per_session_with_recall"]
    without_rate = report["effectiveness"]["corrections_per_session_without_recall"]
    if without_rate > 0:
        reduction = round((1 - with_rate / without_rate) * 100, 1)
        report["effectiveness"]["estimated_reduction_pct"] = reduction
    else:
        report["effectiveness"]["estimated_reduction_pct"] = None

    # Write effectiveness report
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(EFFECTIVENESS_LOG, "a") as f:
        f.write(json.dumps(report) + "\n")

    return report


def run_hourly(watermarks: dict, seen_hashes: set) -> dict:
    """Hourly mode: retain new corrections/instructions with watermark + hash dedup."""
    log.info("=== Hourly retain started ===")

    transcripts = find_recent_transcripts(hours=2)
    log.info("Found %d transcripts from last 2h", len(transcripts))

    results = {
        "mode": "hourly",
        "date": date.today().isoformat(),
        "transcripts_scanned": len(transcripts),
        "transcripts_with_learnings": 0,
        "corrections_detected": 0,
        "instructions_detected": 0,
        "windows_retained": 0,
        "skipped_duplicates": 0,
        "total_retain_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "errors": [],
    }

    if not transcripts:
        log.info("No recent transcripts. Done.")
        return results

    candidates = filter_and_scan(transcripts, watermarks)
    log.info("After filter: %d candidates with learning signals", len(candidates))

    for path, messages, start_index in candidates:
        transcript_id = path.stem
        log.info("Processing: %s (from message %d)", transcript_id, start_index)

        corrections, instructions = extract_learning_windows(
            messages, start_index=start_index
        )
        all_windows = corrections + instructions

        if not all_windows:
            log.info("  No extractable windows, skipping")
            continue

        results["corrections_detected"] += len(corrections)
        results["instructions_detected"] += len(instructions)
        results["transcripts_with_learnings"] += 1
        log.info(
            "  Found %d corrections, %d instructions",
            len(corrections), len(instructions),
        )

        try:
            retain_result = retain_windows_deduped(
                all_windows, transcript_id, seen_hashes
            )
            results["windows_retained"] += retain_result["items_retained"]
            results["skipped_duplicates"] += retain_result.get("skipped_duplicates", 0)
            for k in results["total_retain_usage"]:
                results["total_retain_usage"][k] += retain_result["usage"].get(k, 0)
            log.info(
                "  Retained: %d items (%d duplicates skipped), %d tokens",
                retain_result["items_retained"],
                retain_result.get("skipped_duplicates", 0),
                retain_result["usage"].get("total_tokens", 0),
            )
        except Exception as e:
            results["errors"].append({"transcript": transcript_id, "error": str(e)})
            log.error("  Failed: %s", e)

    log.info(
        "=== Hourly done: %d retained, %d duplicates skipped, %d tokens ===",
        results["windows_retained"],
        results["skipped_duplicates"],
        results["total_retain_usage"]["total_tokens"],
    )
    return results


def run_nightly(watermarks: dict, seen_hashes: set) -> dict:
    """Nightly mode: catch-all retain + reflect + probes + metrics + mental models."""
    log.info("=== Nightly processing started ===")

    # Collect bank stats for observability
    log.info("Collecting bank stats...")
    bank_stats = collect_bank_stats()
    for bank, s in bank_stats.items():
        log.info("  %s: %d nodes, %d docs", bank, s["total_nodes"], s["total_documents"])

    transcripts = find_recent_transcripts(hours=24)
    log.info("Found %d transcripts from last 24h", len(transcripts))

    results = {
        "mode": "nightly",
        "date": date.today().isoformat(),
        "transcripts_found": len(transcripts),
        "transcripts_with_learnings": 0,
        "corrections_detected": 0,
        "instructions_detected": 0,
        "windows_retained": 0,
        "skipped_duplicates": 0,
        "total_retain_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "reflect_result": None,
        "bank_stats": bank_stats,
        "observability_probes": [],
        "errors": [],
    }

    if not transcripts:
        log.info("No transcripts to process. Running probes only.")
        run_observability_probes()
        return results

    # Catch-all: process transcripts not yet handled by hourly runs
    missed = []
    for path in transcripts:
        tid = path.stem
        wm = watermarks.get(tid, {})
        stat = path.stat()
        if stat.st_size > wm.get("size", 0):
            missed.append(path)

    if missed:
        log.info("Catch-all: %d transcripts have new content since last hourly", len(missed))
        candidates = filter_and_scan(missed, watermarks)
        for path, messages, start_index in candidates:
            transcript_id = path.stem
            log.info("Processing: %s (from message %d)", transcript_id, start_index)

            corrections, instructions = extract_learning_windows(
                messages, start_index=start_index
            )
            all_windows = corrections + instructions

            if not all_windows:
                continue

            results["corrections_detected"] += len(corrections)
            results["instructions_detected"] += len(instructions)
            results["transcripts_with_learnings"] += 1
            log.info(
                "  Found %d corrections, %d instructions",
                len(corrections), len(instructions),
            )

            try:
                retain_result = retain_windows_deduped(
                    all_windows, transcript_id, seen_hashes
                )
                results["windows_retained"] += retain_result["items_retained"]
                results["skipped_duplicates"] += retain_result.get("skipped_duplicates", 0)
                for k in results["total_retain_usage"]:
                    results["total_retain_usage"][k] += retain_result["usage"].get(k, 0)
                log.info(
                    "  Retained: %d items (%d skipped), %d tokens",
                    retain_result["items_retained"],
                    retain_result.get("skipped_duplicates", 0),
                    retain_result["usage"].get("total_tokens", 0),
                )
            except Exception as e:
                results["errors"].append({"transcript": transcript_id, "error": str(e)})
                log.error("  Failed: %s", e)
    else:
        log.info("All transcripts already processed by hourly runs")

    # Phase: Reflect on accumulated patterns
    if results["windows_retained"] > 0 or any(
        watermarks.get(t.stem, {}).get("message_count", 0) > 0 for t in transcripts
    ):
        log.info("Running reflect...")
        results["reflect_result"] = reflect()

    # Phase: Observability probes
    log.info("Running recall observability probes...")
    results["observability_probes"] = run_observability_probes()

    # Phase: MCP effectiveness analysis
    log.info("Analyzing MCP effectiveness...")
    effectiveness = analyze_mcp_effectiveness(transcripts)
    results["effectiveness"] = effectiveness
    if effectiveness["mcp_usage"]:
        for server, stats in effectiveness["mcp_usage"].items():
            log.info(
                "  %s: %d calls, %d hits, %d misses (%.0f%% hit rate)",
                server, stats["calls"], stats["hits"], stats["misses"],
                stats["hit_rate"] * 100,
            )
    eff = effectiveness["effectiveness"]
    log.info(
        "  Sessions with recall: %d (avg %.1f corrections), without: %d (avg %.1f corrections)",
        eff["sessions_with_recall"],
        eff["corrections_per_session_with_recall"],
        eff["sessions_without_recall"],
        eff["corrections_per_session_without_recall"],
    )
    if eff.get("estimated_reduction_pct") is not None:
        log.info("  Estimated correction reduction: %.1f%%", eff["estimated_reduction_pct"])
    proactive = effectiveness.get("proactive_recall", {})
    if proactive:
        log.info(
            "  Proactive recall: %d/%d sessions (%.1f%%), %d/%d agent turns (%.1f%%)",
            proactive["sessions_with_proactive_recall"],
            proactive["total_sessions"],
            proactive["proactive_pct"],
            proactive["agent_turns_with_recall"],
            proactive["total_agent_turns"],
            proactive["turn_recall_pct"],
        )
    k_curve = effectiveness.get("k_curve", {})
    if k_curve and k_curve.get("k_score") is not None:
        log.info(
            "  K-curve: score=%.2fx | context loading: %d tok (recall) vs %d tok (no recall) [-%s%%]",
            k_curve["k_score"],
            k_curve["with_recall"]["avg_context_loading_tokens"],
            k_curve["without_recall"]["avg_context_loading_tokens"],
            k_curve.get("context_loading_reduction_pct", "?"),
        )
        log.info(
            "  K-curve: effectiveness ratio: %.3f (recall) vs %.3f (no recall) [+%s%%]",
            k_curve["with_recall"]["effectiveness_ratio"],
            k_curve["without_recall"]["effectiveness_ratio"],
            k_curve.get("token_efficiency_gain_pct", "?"),
        )
        by_bucket = k_curve.get("by_bucket", {})
        for bucket, bk in by_bucket.items():
            if bk.get("k_score") is not None:
                log.info(
                    "  K-curve [%s]: %d vs %d sessions, score=%.2fx",
                    bucket, bk["with_recall"], bk["without_recall"], bk["k_score"],
                )
        if k_curve.get("k_score_normalized") is not None:
            log.info("  K-curve normalized: %.2fx", k_curve["k_score_normalized"])

    # Phase: Refresh all mental models
    log.info("Refreshing mental models...")
    models_to_refresh = {
        "kubernaut-issues": ("active-priorities", "known-bugs"),
        "cursor-memory": ("workflow-preferences", "architecture-decisions", "testing-methodology", "coding-conventions"),
        "kubernaut-docs": ("af-pipeline", "platform-topology", "ka-architecture"),
    }
    for bank, model_ids in models_to_refresh.items():
        for model_id in model_ids:
            resp = api_post(
                f"/v1/default/banks/{bank}/mental-models/{model_id}/refresh",
                {},
            )
            if resp and "error" not in resp:
                log.info("  %s/%s: refresh triggered", bank, model_id)
            else:
                log.warning("  %s/%s: refresh failed", bank, model_id)
    results["mental_model_refresh"] = "triggered"

    # Write daily log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{date.today().isoformat()}.json"
    with open(log_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info("Results saved to %s", log_path)

    # Clear retained hashes (start fresh for tomorrow), prune old watermarks
    seen_hashes.clear()
    pruned = prune_watermarks(watermarks)
    watermarks.clear()
    watermarks.update(pruned)

    log.info(
        "=== Nightly done: %d transcripts, %d corrections, %d instructions, %d tokens ===",
        results["transcripts_with_learnings"],
        results["corrections_detected"],
        results["instructions_detected"],
        results["total_retain_usage"]["total_tokens"],
    )
    return results


def main():
    parser = argparse.ArgumentParser(description="Hindsight learning pipeline")
    parser.add_argument(
        "--mode",
        choices=["hourly", "nightly", "both"],
        default="both",
        help="Run mode: hourly (retain only), nightly (reflect+probes), both (default)",
    )
    args = parser.parse_args()

    watermarks = load_watermarks()
    seen_hashes = load_retained_hashes()

    try:
        if args.mode in ("hourly", "both"):
            run_hourly(watermarks, seen_hashes)

        if args.mode in ("nightly", "both"):
            run_nightly(watermarks, seen_hashes)
    finally:
        save_watermarks(watermarks)
        save_retained_hashes(seen_hashes)


if __name__ == "__main__":
    main()
