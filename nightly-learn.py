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
import time
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


def is_instruction(text: str) -> bool:
    """Detect if a user message establishes methodology, process, or requirements."""
    if not text or len(text) < 20 or len(text) > 2000:
        return False
    for pat in INSTRUCTION_PATTERNS:
        if pat.search(text):
            return True
    return False


def extract_learning_windows(messages: list[dict], window: int = 2) -> tuple[list[str], list[str]]:
    """Extract messages around corrections AND instructions.

    Returns two lists:
      - correction_windows: context around user corrections
      - instruction_windows: context around methodology/process statements
    """
    parsed = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "user":
            text = extract_user_text(msg)
            if text:
                parsed.append({
                    "role": "user",
                    "text": text,
                    "is_correction": is_correction(text),
                    "is_instruction": is_instruction(text),
                })
        elif role == "assistant":
            text = extract_assistant_text(msg)
            if text:
                parsed.append({
                    "role": "assistant",
                    "text": text[:400],
                    "is_correction": False,
                    "is_instruction": False,
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

    correction_indices = [i for i, m in enumerate(parsed) if m["is_correction"]]
    instruction_indices = [
        i for i, m in enumerate(parsed)
        if m["is_instruction"] and not m["is_correction"]
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
    # Identify which sessions had MCP recall calls vs. which didn't
    sessions_with_recall = []
    sessions_without_recall = []

    for transcript_path in transcripts:
        messages = parse_transcript(transcript_path)
        if len(messages) < 4:
            continue

        # Check if this session had any MCP recall calls
        has_recall = False
        for msg in messages:
            content = msg.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "")
                        if name == "CallMcpTool":
                            inp = block.get("input", {})
                            tool = inp.get("toolName", "")
                            if "recall" in tool.lower():
                                has_recall = True
                                break
                if has_recall:
                    break

        corrections, _ = extract_learning_windows(messages)
        correction_count = len(corrections)

        session_info = {
            "transcript_id": transcript_path.stem,
            "corrections": correction_count,
            "messages": len(messages),
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


def main():
    log.info("=== Hindsight nightly learning started ===")

    # Collect bank stats for observability
    log.info("Collecting bank stats...")
    bank_stats = collect_bank_stats()
    for bank, s in bank_stats.items():
        log.info("  %s: %d nodes, %d docs", bank, s["total_nodes"], s["total_documents"])

    transcripts = find_recent_transcripts(hours=24)
    log.info("Found %d transcripts from last 24h", len(transcripts))

    if not transcripts:
        log.info("No transcripts to process. Running probes only.")
        run_observability_probes()
        log.info("=== Done (no transcripts) ===")
        sys.exit(0)

    results = {
        "date": date.today().isoformat(),
        "transcripts_found": len(transcripts),
        "transcripts_with_learnings": 0,
        "corrections_detected": 0,
        "instructions_detected": 0,
        "windows_retained": 0,
        "total_retain_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "reflect_result": None,
        "bank_stats": bank_stats,
        "observability_probes": [],
        "errors": [],
    }

    for transcript_path in transcripts:
        transcript_id = transcript_path.stem
        log.info("Processing: %s", transcript_id)

        messages = parse_transcript(transcript_path)
        if len(messages) < 4:
            log.info("  Skipping (too short: %d messages)", len(messages))
            continue

        corrections, instructions = extract_learning_windows(messages)
        all_windows = corrections + instructions

        if not all_windows:
            log.info("  No learnings found, skipping")
            continue

        results["corrections_detected"] += len(corrections)
        results["instructions_detected"] += len(instructions)
        results["transcripts_with_learnings"] += 1
        log.info(
            "  Found %d corrections, %d instructions",
            len(corrections), len(instructions),
        )

        try:
            retain_result = retain_windows(all_windows, transcript_id)
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

    # Phase: Refresh issues-bank mental models (nightly, after issue ingestion)
    log.info("Refreshing issues-bank mental models...")
    for model_id in ("active-priorities", "known-bugs"):
        resp = api_post(
            f"/v1/default/banks/kubernaut-issues/mental-models/{model_id}/refresh",
            {},
        )
        if resp and "error" not in resp:
            log.info("  %s: refresh triggered", model_id)
        else:
            log.warning("  %s: refresh failed", model_id)
    results["mental_model_refresh"] = "triggered"

    # Write daily log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{date.today().isoformat()}.json"
    with open(log_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info("Results saved to %s", log_path)

    log.info(
        "=== Done: %d transcripts, %d corrections, %d instructions, %d tokens ===",
        results["transcripts_with_learnings"],
        results["corrections_detected"],
        results["instructions_detected"],
        results["total_retain_usage"]["total_tokens"],
    )


if __name__ == "__main__":
    main()
