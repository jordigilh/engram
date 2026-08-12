#!/usr/bin/env python3
"""Hindsight learning script (hourly retain + nightly reflect).

Scans Cursor agent transcripts, detects corrections, and feeds
annotated sessions to Hindsight for pattern extraction and reflection.

Modes:
  --mode hourly   Retain only (watermark filter + hash dedup). Run by hourly launchd job.
  --mode nightly  Catch-all retain + reflect + probes + metrics. Run by 2am launchd job.
  --mode both     Run hourly then nightly (default, backward compat for manual runs).
"""

# Launchd jobs invoke this via /usr/bin/python3 (macOS system Python, 3.9.x),
# which predates PEP 604 (`X | Y` union syntax). Defer annotation evaluation
# so `list[str] | None`-style hints don't crash at import time on 3.9.
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Shared modules (correction_gate.py etc.) live in the src/engram/ package.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from engram import correction_gate  # noqa: E402
from engram import contradiction_resolution  # noqa: E402
from engram import fallback_extract  # noqa: E402
from engram import project_scope  # noqa: E402

HINDSIGHT_URL = "http://localhost:8888"
BANK_ID = "cursor-memory"
EPOCH_START_DATE = "2026-06-26"
BUCKET_TRIVIAL = 5000
BUCKET_SMALL = 15000
BUCKET_MEDIUM = 100000
TRANSCRIPTS_GLOB = os.path.expanduser(
    "~/.cursor/projects/*/agent-transcripts/**/*.jsonl"
)
LOG_DIR = Path.home() / ".hindsight" / "logs"
MAX_CONTENT_LEN = 12000  # max chars per retain item to control token usage

# Standing-cadence nudge for the human-review queue (lever #5 of the
# 2026-07-14 "reduce input tokens" review) -- see
# notify_pending_contradictions_backlog().
PENDING_CONTRADICTIONS_LOG = LOG_DIR / "contradictions-pending.jsonl"
CONTRADICTION_NOTIFY_THRESHOLD = int(
    os.environ.get("ENGRAM_CONTRADICTION_NOTIFY_THRESHOLD", "10")
)
CONTRADICTION_NOTIFY_STATE = LOG_DIR / "last-contradiction-notify.txt"

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
    # "you're not following AGENTS.md/the methodology/convention" — this
    # user's most common correction phrasing for process/methodology
    # violations (e.g. TDD phase confusion). Added 2026-07-08 after finding
    # 16 real corrections in 7 days, 100% missed by the patterns above.
    re.compile(r"\b(you'?re|you\s+are)\s+(still\s+)?not\s+(following|aligned)\b", re.I),
    re.compile(r"\bnot\s+following\s+(the\s+)?(project'?s?\s+)?(methodology|convention|AGENTS\.md|CLAUDE\.md)\b", re.I),
    re.compile(r"\byou\s+keep\s+making\s+the\s+same\s+mistake\b", re.I),
    # "mistake X for Y" / "mistaking X for Y" — the agent conflating two
    # distinct concepts (e.g. "mistake TDD refactoring for checkpoint").
    re.compile(r"\bmistak(?:e|ing)\b.{0,40}\bfor\b", re.I),
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
MODEL_REFRESH_STATE_PATH = STATE_DIR / "model-refresh-state.json"

# Lever #2 of the 2026-07-14 "reduce input tokens" review: refresh mental
# models on topic-shift (enough new material since the last refresh),
# not just on the nightly cycle. See maybe_refresh_mental_models_on_topic_shift().
TOPIC_SHIFT_REFRESH_THRESHOLD = int(
    os.environ.get("ENGRAM_TOPIC_SHIFT_REFRESH_THRESHOLD", "5")
)
TOPIC_SHIFT_REFRESH_MIN_INTERVAL_HOURS = float(
    os.environ.get("ENGRAM_TOPIC_SHIFT_REFRESH_MIN_INTERVAL_HOURS", "4")
)
# Bank -> mental model ids eligible for a topic-shift refresh. Only
# cursor-memory is listed: it's the only bank run_hourly() retains into
# directly (kubernaut-docs/issues and dcm-docs/issues are populated by the
# separate CocoIndex/ingest-issues pipelines and only refreshed nightly).
TOPIC_SHIFT_MODELS = {
    "cursor-memory": ("workflow-preferences", "architecture-decisions", "testing-methodology", "coding-conventions"),
}

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


def load_model_refresh_state() -> dict:
    """Load per-bank topic-shift refresh counters from disk."""
    if MODEL_REFRESH_STATE_PATH.exists():
        try:
            return json.loads(MODEL_REFRESH_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt model-refresh-state.json, starting fresh")
    return {}


def save_model_refresh_state(state: dict) -> None:
    """Atomically save topic-shift refresh counters to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_REFRESH_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(MODEL_REFRESH_STATE_PATH)


def maybe_refresh_mental_models_on_topic_shift(bank_id: str, new_items_count: int) -> dict:
    """Trigger an out-of-cycle mental model refresh once enough new material
    has landed in a bank since its last refresh, instead of only ever
    refreshing on the nightly cycle (lever #2 of the 2026-07-14 "reduce
    input tokens" review).

    Mental models are Engram's only mechanism that survives a cold start or
    context-summarization event -- refreshing them only nightly means a
    same-day topic shift (e.g. a new architecture decision retained this
    afternoon) isn't reflected in recall until the next night at the
    earliest. Each refresh is a real Sonnet resynthesis call (~8-14KB of
    output per model, confirmed against the live cursor-memory bank during
    the 2026-07-14 spike), so this supplements rather than replaces the
    nightly unconditional refresh, and is gated by both a minimum new-item
    count (TOPIC_SHIFT_REFRESH_THRESHOLD) and a minimum time between forced
    refreshes (TOPIC_SHIFT_REFRESH_MIN_INTERVAL_HOURS) to bound cost.

    Returns a dict describing what happened (for logging/testing). Never
    raises -- a failed refresh trigger must not fail the hourly retain path.
    """
    result: dict[str, Any] = {
        "bank_id": bank_id, "triggered": False, "count_since_refresh": 0, "reason": None,
    }
    model_ids = TOPIC_SHIFT_MODELS.get(bank_id)
    if not model_ids or new_items_count <= 0:
        result["reason"] = "no_new_items_or_untracked_bank"
        return result

    try:
        state = load_model_refresh_state()
        bank_state = state.get(bank_id, {"count_since_refresh": 0, "last_triggered_at": None})
        bank_state["count_since_refresh"] += new_items_count
        result["count_since_refresh"] = bank_state["count_since_refresh"]

        if bank_state["count_since_refresh"] < TOPIC_SHIFT_REFRESH_THRESHOLD:
            result["reason"] = "below_threshold"
            state[bank_id] = bank_state
            save_model_refresh_state(state)
            return result

        last_triggered_at = bank_state.get("last_triggered_at")
        if last_triggered_at:
            elapsed_hours = (
                datetime.now() - datetime.fromisoformat(last_triggered_at)
            ).total_seconds() / 3600
            if elapsed_hours < TOPIC_SHIFT_REFRESH_MIN_INTERVAL_HOURS:
                result["reason"] = "debounced"
                state[bank_id] = bank_state
                save_model_refresh_state(state)
                return result

        for model_id in model_ids:
            resp = api_post(f"/v1/default/banks/{bank_id}/mental-models/{model_id}/refresh", {})
            if resp and "error" not in resp:
                log.info("  Topic-shift refresh: %s/%s", bank_id, model_id)
            else:
                log.warning("  Topic-shift refresh failed: %s/%s", bank_id, model_id)

        bank_state["count_since_refresh"] = 0
        bank_state["last_triggered_at"] = datetime.now().isoformat()
        state[bank_id] = bank_state
        save_model_refresh_state(state)
        result["triggered"] = True
    except Exception as e:
        result["reason"] = f"error: {e}"
    return result


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
    # TimeoutError/ConnectionError added 2026-08-10 -- a slow hindsight-api
    # under retain-consolidation load raises a raw socket TimeoutError/
    # ConnectionError (urllib only wraps connect-time failures as URLError; a
    # read-time stall or reset surfaces bare), which callers relying on
    # (HTTPError, URLError) alone don't catch. Log and re-raise like the
    # other branches so callers get a consistent, catchable exception type.
    except (TimeoutError, ConnectionError) as e:
        log.error("Connection error %s: %s", url, e)
        raise


PROJECTS_ROOT = Path(os.path.expanduser("~/.cursor/projects"))


def find_recent_transcripts(
    hours: int = 24,
    workspace_prefixes: list[str] | None = None,
    end_time: datetime | None = None,
) -> list[Path]:
    """Find transcript files modified in the last N hours.

    If workspace_prefixes is given, only transcripts whose Cursor project
    directory name (~/.cursor/projects/<name>/agent-transcripts/...) starts
    with one of the given prefixes are returned. Used to scope per-project
    analytics (e.g. effectiveness) to sessions that actually touched that
    project's repos, since transcripts otherwise span every workspace.

    end_time defaults to now (normal live usage). Pass an explicit historical
    timestamp to reconstruct the exact window a past nightly run would have
    seen — used for backfilling corrected metrics against still-on-disk
    transcripts without needing to "rewrite" anything, just recompute.
    """
    cutoff = (end_time or datetime.now()).timestamp() - (hours * 3600)
    results = []
    for path_str in glob(TRANSCRIPTS_GLOB, recursive=True):
        p = Path(path_str)
        if workspace_prefixes:
            try:
                project_dir_name = p.relative_to(PROJECTS_ROOT).parts[0]
            except ValueError:
                continue
            if not any(project_dir_name.startswith(pfx) for pfx in workspace_prefixes):
                continue
        if p.stat().st_mtime >= cutoff:
            results.append(p)
    return sorted(results, key=lambda p: p.stat().st_mtime)


def project_for_transcript_path(path: Path) -> str | None:
    """Resolve a transcript file's onboarded project label (kubernaut/dcm/
    engram), or None if it can't be resolved. Used to tag contradiction
    queue/log entries per-project -- see docs/FINDINGS.md 2026-07-19."""
    try:
        project_dir_name = path.relative_to(PROJECTS_ROOT).parts[0]
    except ValueError:
        return None
    return project_scope.resolve_project_label(project_dir_name)


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


def extract_mcp_tool_call(block: dict) -> tuple[str, str] | None:
    """Return (tool_name, server_or_namespace) if block is an MCP tool call.

    Cursor changed its MCP dispatch mechanism around 2026-07-23: the legacy
    "CallMcpTool" tool_use (input={"toolName": ..., "server": ...}) was
    replaced by a generic "CallDynamicTool" wrapper (input={"namespace": ...,
    "toolName": ...}) used for both MCP-backed tools *and* first-party Cursor
    tools (Task, WebSearch, TodoWrite, etc., under namespace="cursor"). Every
    transcript-parsing recall/bank-usage heuristic that only recognized
    "CallMcpTool" silently saw has_recall=False for every session recorded
    after the cutover -- recall adoption looked like it crashed to 0% when it
    hadn't. Handling both shapes here keeps that from recurring if Cursor
    changes the wrapper again. See docs/FINDINGS.md 2026-07-27.
    """
    name = block.get("name", "")
    inp = block.get("input", {})
    if name == "CallMcpTool":
        return inp.get("toolName", ""), inp.get("server", "")
    if name == "CallDynamicTool":
        return inp.get("toolName", ""), inp.get("namespace", "")
    return None


def effective_tool_name(block: dict) -> str:
    """Return the tool name to use for productive/exploration classification.

    First-party Cursor tools (Task, WebSearch, TodoWrite, ...) are dispatched
    through "CallDynamicTool" with namespace="cursor" -- unwrap those so
    PRODUCTIVE_TOOLS/EXPLORATION_TOOLS membership checks key off the real
    tool name (e.g. "Task") instead of the generic wrapper name. MCP-backed
    calls (recall, gopls, etc.) are handled separately by
    extract_mcp_tool_call() and are left as "CallDynamicTool" here since they
    aren't in either set.
    """
    name = block.get("name", "")
    if name == "CallDynamicTool" and block.get("input", {}).get("namespace") == "cursor":
        return block.get("input", {}).get("toolName", name)
    return name


def resolve_recalled_bank(server_or_namespace: str, recall_banks: set[str]) -> str | None:
    """Map a recall call's server/namespace to one of recall_banks, or None.

    Legacy "CallMcpTool" calls carried the raw bank name directly in
    "server" (e.g. "hindsight-docs"). The new "CallDynamicTool" wrapper
    carries a workspace-qualified namespace instead (e.g.
    "project-0-kubernaut-hindsight-docs" or "user-hindsight"). Match longest
    bank name first so e.g. "hindsight-docs" doesn't get shadowed by the
    shorter "hindsight".
    """
    for bank in sorted(recall_banks, key=len, reverse=True):
        if server_or_namespace == bank or server_or_namespace == f"user-{bank}":
            return bank
        if server_or_namespace.endswith(f"-{bank}"):
            return bank
    return None


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
    raw = "\n".join(texts).strip()
    if correction_gate.is_system_boilerplate(raw):
        return ""
    return raw


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
    """Detect if a user message looks like a correction.

    Delegates to correction_gate (Haiku-based, ~0.97 F1 vs. this file's own
    CORRECTION_PATTERNS/STRUCTURAL_CORRECTION_PATTERNS, ~24% recall -- see
    docs/FINDINGS.md 2026-07-08). Set ENGRAM_CORRECTION_DETECTOR=regex for an
    instant rollback to the patterns below, which are kept in place unused.
    """
    return correction_gate.is_correction(text, CORRECTION_PATTERNS + STRUCTURAL_CORRECTION_PATTERNS)


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


def retain_windows(windows: list[str], transcript_id: str, project: str | None = None) -> dict[str, Any]:
    """Send correction/instruction windows to Hindsight retain endpoint.

    Windows tagged [CORRECTION] (see extract_learning_windows/_build_windows)
    run the three-tier contradiction check (contradiction_resolution.resolve())
    first -- see docs/FINDINGS.md. Instruction windows retain unchanged:
    check_contradiction is only meaningful once a message is confirmed as a
    correction, not a forward-looking instruction.

    project (kubernaut/dcm/engram/None), if given, is forwarded to resolve()
    so any queued/auto-resolved contradiction is tagged with which onboarded
    project it came from (see docs/FINDINGS.md 2026-07-19). It is also written
    onto every retained item's own `tags` field -- see docs/FINDINGS.md
    2026-07-27 ("DCM recall polluted by kubernaut/FedRAMP content"): cursor-memory
    is a deliberately shared bank across kubernaut/dcm/engram for universal
    coding-hygiene lessons, but until this fix every retained fact was
    untagged, so project-specific content (e.g. kubernaut's FedRAMP/NIST-800-53
    control-mapping requirements) was indistinguishable from genuinely
    universal conventions and surfaced in every other project's recall/mental
    models. Tagging at write time is the only point in the pipeline where this
    is fixable -- Hindsight's memory-curation API (PATCH .../memories/{id}) has
    no tags field, so existing untagged facts can never be retagged
    retroactively; this only stops the pollution from growing going forward.

    On api_post() failure: hindsight-api's Haiku extraction runs server-side
    (see fallback_extract.py's module docstring), so a transient failure
    there surfaces here as an HTTPError/URLError/TimeoutError. Those are
    buffered locally via fallback_extract.record_fallback() instead of being
    silently dropped -- see docs/FINDINGS.md 2026-07-29 and GitHub issue #5.
    Any other exception is NOT treated as a transient/Vertex-AI-outage case
    (per the issue's "not all exceptions" acceptance criterion) -- it's
    logged and the window is skipped, same resilience as before this fix,
    just not misclassified or buffered.
    """
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    items_retained = 0
    contradictions_auto_resolved = 0
    contradictions_queued = 0
    fallback_used = 0

    for i, window in enumerate(windows):
        tags: list[str] = [project] if project else []
        if "[CORRECTION]" in window:
            resolution = contradiction_resolution.resolve(BANK_ID, window, project=project)
            if resolution.action == "auto_resolved":
                contradictions_auto_resolved += 1
                tags += ["CORRECTION", "supersedes-prior-memory"]
            elif resolution.action == "queued":
                contradictions_queued += 1
                # Withheld from retain pending human review (pending_queue.py's
                # contract: "never auto-retained"). review-contradictions.py
                # retains it itself on approve; reject then correctly means
                # nothing was ever written, matching its own "discard the new
                # statement" description. See docs/FINDINGS.md.
                continue

        item: dict[str, Any] = {
            "content": window,
            "metadata": {
                "source": "cursor-transcript",
                "transcript_id": transcript_id,
                "window": str(i),
            },
        }
        if tags:
            item["tags"] = tags
        payload = {"items": [item]}
        try:
            result = api_post(
                f"/v1/default/banks/{BANK_ID}/memories", payload
            )
            if result.get("success"):
                items_retained += result.get("items_count", 0)
                usage = result.get("usage", {})
                for k in total_usage:
                    total_usage[k] += usage.get(k, 0)
        except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
            extracted = fallback_extract.extract(window)
            fallback_extract.record_fallback(window, transcript_id, project, extracted, reason=str(e))
            fallback_used += 1
            log.warning(
                "Retain failed for window %d of %s (buffered locally, fallback-extracted): %s",
                i, transcript_id, e,
            )
        except Exception as e:
            log.error(
                "Unexpected error retaining window %d of %s (not a transient failure, not buffered): %s",
                i, transcript_id, e,
            )

    return {
        "items_retained": items_retained,
        "usage": total_usage,
        "contradictions_auto_resolved": contradictions_auto_resolved,
        "contradictions_queued": contradictions_queued,
        "fallback_used": fallback_used,
    }


def reprocess_fallback_backlog() -> dict[str, int]:
    """Retry every locally-buffered fallback-extraction entry (see
    fallback_extract.py) now that hindsight-api's Vertex AI dependency may
    have recovered. Entries that succeed are removed; entries that still
    fail remain buffered for the next --mode reprocess-fallback pass. See
    docs/FINDINGS.md 2026-07-29 and GitHub issue #5."""
    backlog = fallback_extract.load_backlog()
    if not backlog:
        return {"attempted": 0, "recovered": 0, "still_failing": 0}

    remaining = []
    recovered = 0
    for entry in backlog:
        window = entry.get("window", "")
        transcript_id = entry.get("transcript_id", "unknown")
        project = entry.get("project")

        item: dict[str, Any] = {
            "content": window,
            "metadata": {
                "source": "cursor-transcript",
                "transcript_id": transcript_id,
                "window": "reprocessed-from-fallback",
            },
        }
        if project:
            item["tags"] = [project]
        payload = {"items": [item]}

        recovered_this_entry = False
        try:
            result = api_post(f"/v1/default/banks/{BANK_ID}/memories", payload)
            if result.get("success"):
                recovered_this_entry = True
        except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
            log.warning("Reprocess-fallback still failing for %s: %s", transcript_id, e)

        if recovered_this_entry:
            recovered += 1
        else:
            remaining.append(entry)

    fallback_extract.save_backlog(remaining)
    return {"attempted": len(backlog), "recovered": recovered, "still_failing": len(remaining)}


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

        # Layer 2: has this file got any correction/instruction signal at all?
        # is_correction() is Haiku-backed via correction_gate (disk-cached),
        # not a cheap regex net -- the 2026-07-08 prefilter shadow trial found
        # no safe way to narrow Haiku's intake below "classify everything"
        # (best regex candidate: 24.4% recall vs. Haiku's own verdicts, see
        # docs/FINDINGS.md), so this scan intentionally classifies every new
        # message rather than pre-filtering. extract_learning_windows() below
        # re-checks the same messages to build windows; the cache makes that
        # second pass free.
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
    windows: list[str], transcript_id: str, seen_hashes: set, project: str | None = None
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
            "contradictions_auto_resolved": 0,
            "contradictions_queued": 0,
        }

    result = retain_windows(new_windows, transcript_id, project=project)
    result["skipped_duplicates"] = skipped
    return result


def notify_pending_contradictions_backlog(
    pending_log: Path = PENDING_CONTRADICTIONS_LOG,
) -> dict:
    """Standing-cadence nudge for the contradiction-review queue.

    Lever #5 of the 2026-07-14 "reduce input tokens" review: every day a
    queued contradiction sits unreviewed in
    ~/.hindsight/logs/contradictions-pending.jsonl is a day that fact isn't
    available to recall (contradiction_resolution.py's three-tier check
    withholds queued items from retain until review-contradictions.py
    resolves them -- see docs/PENDING_CONTRADICTIONS.md). The dashboard
    already surfaces the count passively; this fires an active macOS
    notification once per calendar day when the backlog is at or above
    CONTRADICTION_NOTIFY_THRESHOLD, so the review doesn't depend on someone
    remembering to check the dashboard.

    Idempotent across same-day runs (nightly-learn.py runs once per project
    via separate launchd plists, so this can be called more than once per
    day) via CONTRADICTION_NOTIFY_STATE. Never raises -- notification
    delivery is best-effort and must not fail the nightly pipeline.
    """
    result: dict[str, Any] = {"pending_count": 0, "notified": False, "skipped_reason": None}
    try:
        if not pending_log.exists():
            result["skipped_reason"] = "no_pending_log"
            return result

        count = sum(1 for line in pending_log.read_text().splitlines() if line.strip())
        result["pending_count"] = count

        if count < CONTRADICTION_NOTIFY_THRESHOLD:
            result["skipped_reason"] = "below_threshold"
            return result

        today = date.today().isoformat()
        if (
            CONTRADICTION_NOTIFY_STATE.exists()
            and CONTRADICTION_NOTIFY_STATE.read_text().strip() == today
        ):
            result["skipped_reason"] = "already_notified_today"
            return result

        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{count} contradictions awaiting review '
                f'(review-contradictions.py)" with title "Engram"',
            ],
            capture_output=True, timeout=10,
        )
        CONTRADICTION_NOTIFY_STATE.parent.mkdir(parents=True, exist_ok=True)
        CONTRADICTION_NOTIFY_STATE.write_text(today)
        result["notified"] = True
    except Exception as e:
        result["skipped_reason"] = f"error: {e}"
    return result


def dedup_graph(bank_id: str) -> int:
    """Remove exact-duplicate documents from a bank by content_hash.

    Keeps the newest document for each hash. Returns count of deleted docs.
    """
    from collections import defaultdict

    all_docs = []
    offset = 0
    while True:
        resp = api_get(f"/v1/default/banks/{bank_id}/documents?limit=100&offset={offset}")
        if not resp:
            break
        docs = resp.get("documents", resp.get("items", []))
        if not docs:
            break
        all_docs.extend(docs)
        offset += len(docs)
        if len(docs) < 100:
            break

    by_hash = defaultdict(list)
    for doc in all_docs:
        by_hash[doc.get("content_hash", "")].append(doc)

    to_delete = []
    for docs in by_hash.values():
        if len(docs) <= 1:
            continue
        docs_sorted = sorted(docs, key=lambda d: d.get("created_at", ""), reverse=True)
        to_delete.extend(docs_sorted[1:])

    if not to_delete:
        return 0

    deleted = 0
    for doc in to_delete:
        try:
            url = f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/documents/{doc['id']}"
            req = Request(url, method="DELETE")
            urlopen(req, timeout=10)
            deleted += 1
        except Exception:
            pass

    if deleted:
        log.info("  Dedup: removed %d duplicate documents (%d memory units) from %s",
                 deleted, sum(d.get("memory_unit_count", 0) for d in to_delete), bank_id)
    return deleted


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


def _parse_fact_timestamp(raw: str | None) -> datetime | None:
    """Parse a recall result's mentioned_at/occurred_start into a tz-aware UTC
    datetime, tolerating both the naive-looking timestamps this REST endpoint
    has been observed to return and offset-suffixed ones (seen from the MCP
    recall tool against the same data) -- see reflect_windowed()."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def reflect_windowed(days: int = 7) -> dict[str, Any]:
    """Time-windowed variant of reflect().

    reflect()'s /reflect endpoint has no date-range parameter at all (confirmed
    by inspecting its schema), and /recall's query_timestamp only anchors
    relative-date parsing and recency *scoring* -- it does not filter out
    older memories (confirmed empirically, see docs/findings/2026-08.md). That
    combination means a plain reflect() call reasons over the bank's entire
    all-time correction history every single night, so the "top 3 recurring
    patterns" answer barely changes night to night even after a pattern has
    genuinely stopped recurring -- there is no way to tell "still happening"
    from "happened once, months ago" from the output alone.

    This works around the missing server-side filter by doing the filtering
    client-side: recall() correction-tagged facts (tag "CORRECTION", applied
    at retain time -- see retain_windows_deduped()), keep only those whose
    mentioned_at/occurred_start falls within the last `days` days, and then
    hand that already-correct subset to reflect() as explicit `context` with
    a query that tells it to reason ONLY over the enumerated facts. This
    still relies on reflect() actually honoring "only use what's in context"
    rather than pulling in its own unscoped recall -- not independently
    verified beyond the spot-check in docs/findings/2026-08.md -- so treat
    the output as a best-effort recency signal, not a guarantee.

    Returns {"window_days", "corrections_in_window", "result"}; `result` is
    None (not an empty reflect() call) when zero corrections fall in the
    window -- itself a meaningful, distinct signal from reflect()'s output.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        recall_result = api_post(
            f"/v1/default/banks/{BANK_ID}/memories/recall",
            {
                "query": "corrections and mistakes made by the assistant",
                "tags": ["CORRECTION"],
                "tags_match": "any",
                "max_tokens": 4096,
                "budget": "high",
            },
        )
    except Exception as e:
        log.warning("Windowed reflect: recall failed: %s", e)
        return {"window_days": days, "corrections_in_window": 0, "result": None, "error": str(e)}

    recent_facts = []
    for fact in recall_result.get("results", []):
        ts = _parse_fact_timestamp(fact.get("mentioned_at") or fact.get("occurred_start"))
        if ts is not None and ts >= cutoff:
            text = fact.get("text", "")
            if text:
                recent_facts.append(text)

    if not recent_facts:
        log.info("Windowed reflect: no corrections in last %d days", days)
        return {"window_days": days, "corrections_in_window": 0, "result": None}

    # Capped rather than exhaustive -- this is a context block for one LLM
    # call, not a full export; 40 facts is generous headroom over what a
    # healthy `days`-sized window should realistically contain.
    context_block = "\n".join(f"- {t}" for t in recent_facts[:40])
    payload = {
        "query": (
            f"Based ONLY on the {len(recent_facts)} correction(s) listed in the context below "
            f"(all from the last {days} days -- do not use any other memories you may know "
            "about), what are the top 3 recurring patterns where the assistant made errors? "
            "For each, state what went wrong and what should be done instead. If fewer than "
            "3 distinct patterns are present, report only as many as genuinely exist."
        ),
        "context": context_block,
        "budget": "low",
        "max_tokens": 1024,
    }
    try:
        result = api_post(f"/v1/default/banks/{BANK_ID}/reflect", payload)
    except Exception as e:
        log.warning("Windowed reflect failed: %s", e)
        return {
            "window_days": days,
            "corrections_in_window": len(recent_facts),
            "result": None,
            "error": str(e),
        }

    return {
        "window_days": days,
        "corrections_in_window": len(recent_facts),
        "result": result,
    }


RECALL_SIGNALS_PATH = LOG_DIR / "recall-signals.jsonl"

PROJECT_CONFIGS = {
    "kubernaut": {
        "banks": ["cursor-memory", "kubernaut-docs", "kubernaut-issues"],
        "mental_models": {
            "kubernaut-issues": ("active-priorities", "known-bugs"),
            "cursor-memory": ("workflow-preferences", "architecture-decisions", "testing-methodology", "coding-conventions"),
            # operator-architecture/console-architecture are tag-scoped views
            # (tags=["kubernaut-operator"]/["kubernaut-console"]) on this same
            # shared bank, not a physical per-repo split -- see docs/FINDINGS.md.
            "kubernaut-docs": ("af-pipeline", "platform-topology", "ka-architecture", "operator-architecture", "console-architecture"),
        },
        "probes": [
            ("cursor-memory", "Go testing conventions and patterns"),
            ("kubernaut-docs", "signal processing architecture and data flow"),
            ("kubernaut-docs", "remediation orchestrator CRD spec"),
            ("kubernaut-issues", "rate limiter design decisions and requirements"),
            ("kubernaut-issues", "A2A streaming event structure"),
            ("kubernaut-docs", "kubernaut-operator reconciliation loop and CRD controllers"),
            ("kubernaut-docs", "kubernaut-console UI components and platform API usage"),
        ],
        "recall_banks": {"hindsight", "hindsight-docs", "hindsight-issues", "cocoindex-code"},
        "code_bank": "cocoindex-code",
        "log_suffix": "",
        "workspace_prefixes": ["Users-jgil-go-src-github-com-jordigilh-kubernaut"],
        "issues_repos": [
            "jordigilh/kubernaut",
            "jordigilh/kubernaut-operator",
            "jordigilh/kubernaut-console",
            "jordigilh/kubernaut-demo-scenarios",
        ],
    },
    "dcm": {
        "banks": ["cursor-memory", "dcm-docs", "dcm-issues"],
        "mental_models": {
            "dcm-docs": ("dcm-architecture", "dcm-enhancements", "dcm-api-contracts"),
            "dcm-issues": ("active-priorities", "known-bugs"),
            # Tag-isolated (tags=["dcm"], strict match) sibling of kubernaut's
            # cursor-memory models below -- created 2026-07-27 after the
            # existing 4 shared models were found to be ~100% kubernaut/
            # FedRAMP-specific despite having no tags at all, polluting DCM's
            # recall. See docs/FINDINGS.md.
            "cursor-memory": ("dcm-workflow-preferences", "dcm-architecture-decisions", "dcm-testing-methodology", "dcm-coding-conventions"),
        },
        "probes": [
            ("dcm-docs", "DCM architecture and service provider model"),
            ("dcm-docs", "placement policy and catalog item lifecycle"),
            ("dcm-issues", "open issues and active priorities"),
        ],
        "recall_banks": {"hindsight", "dcm-docs", "dcm-issues", "dcm-code"},
        "code_bank": "dcm-code",
        "log_suffix": "-dcm",
        "workspace_prefixes": ["Users-jgil-go-src-github-com-dcm-project-"],
        "issues_repos": [
            "dcm-project/dcm",
            "dcm-project/control-plane",
            "dcm-project/cli",
            "dcm-project/kubevirt-service-provider",
            "dcm-project/k8s-container-service-provider",
            "dcm-project/acm-cluster-service-provider",
            "dcm-project/three-tier-app-demo-service-provider",
            "dcm-project/utilities",
            "dcm-project/dcm-project.github.io",
            "dcm-project/enhancements",
            "dcm-project/shared-workflows",
            "dcm-project/quadlet-deploy",
        ],
    },
    "engram": {
        # No issues bank: this repo has zero GitHub issues (decisions and bugs
        # are tracked in docs/FINDINGS.md instead), so no "issues_repos" key.
        "banks": ["cursor-memory", "engram-docs"],
        "mental_models": {
            "engram-docs": ("engram-architecture", "engram-operations"),
            # Tag-isolated (tags=["engram"], strict match) sibling of
            # kubernaut's/dcm's cursor-memory models -- see docs/FINDINGS.md
            # 2026-07-27.
            "cursor-memory": ("engram-workflow-preferences", "engram-architecture-decisions", "engram-testing-methodology", "engram-coding-conventions"),
        },
        "probes": [
            ("engram-docs", "Haiku correction gate and contradiction resolution design"),
            ("engram-docs", "project scoping allowlist for transcript ingestion"),
        ],
        "recall_banks": {"hindsight", "hindsight-docs", "cocoindex-code"},
        "code_bank": "cocoindex-code",
        "log_suffix": "-engram",
        "workspace_prefixes": ["Users-jgil-go-src-github-com-jordigilh-engram"],
    },
    "koku": {
        "banks": ["cursor-memory", "koku-docs", "koku-issues"],
        "mental_models": {
            "koku-docs": ("koku-architecture", "koku-operations"),
            "koku-issues": ("active-priorities", "known-bugs"),
            # Tag-isolated (tags=["koku"], strict match) sibling of
            # kubernaut's/dcm's/engram's cursor-memory models -- applied from
            # day one per the 2026-07-27 pollution fix, see docs/FINDINGS.md.
            "cursor-memory": ("koku-workflow-preferences", "koku-architecture-decisions", "koku-testing-methodology", "koku-coding-conventions"),
        },
        "probes": [
            ("koku-docs", "Koku cost-model and report-processing architecture"),
            ("koku-docs", "masu Celery task orchestration"),
            ("koku-issues", "open issues and active priorities"),
        ],
        "recall_banks": {"hindsight", "koku-docs", "koku-issues", "koku-code"},
        "code_bank": "koku-code",
        "log_suffix": "-koku",
        # Two prefixes onboarded: the current checkout path and older
        # "insights-onprem-koku"/"-pr5933" sessions that predate this
        # allowlist -- see project_scope.py.
        "workspace_prefixes": [
            "Users-jgil-go-src-github-com-project-koku",
            "Users-jgil-go-src-github-com-insights-onprem-koku",
        ],
        # koku-service-operator (2026-08-10) is folded into this same project
        # scope rather than getting its own PROJECT_CONFIGS entry -- see
        # koku-cocoindex-flows.py's module docstring. "workspace_prefixes"
        # above already covers it via the shared "...project-koku" prefix
        # (startswith match), no separate entry needed there.
        "issues_repos": ["project-koku/koku", "project-koku/koku-service-operator"],
    },
    "praxis": {
        "banks": ["cursor-memory", "praxis-docs", "praxis-issues"],
        "mental_models": {
            "praxis-docs": ("praxis-architecture", "praxis-enhancements", "praxis-api-contracts"),
            # praxis-roadmap-priorities synthesizes org Project (v2) board
            # Status + issue milestones -- the actual prioritization signal
            # for this org, which plain issue labels/state don't carry. See
            # the 2026-08-10 "roadmap-signal scope" entry in the onboarding
            # plan / docs/findings/2026-08.md.
            "praxis-issues": ("active-priorities", "known-bugs", "praxis-roadmap-priorities"),
            # Tag-isolated (tags=["praxis"], strict match) sibling of
            # kubernaut's/dcm's/engram's/koku's cursor-memory models -- same
            # 2026-07-27 pollution fix applied from day one, see docs/FINDINGS.md.
            "cursor-memory": ("praxis-workflow-preferences", "praxis-architecture-decisions", "praxis-testing-methodology", "praxis-coding-conventions"),
        },
        "probes": [
            ("praxis-docs", "Praxis Grid routing overlay rendering and candidate scoring"),
            ("praxis-docs", "filter chain model and CRDT/SWIM state propagation"),
            ("praxis-issues", "open issues and active priorities"),
            ("praxis-issues", "AI Grid project board status and mixture-of-models routing epic"),
        ],
        "recall_banks": {"hindsight", "praxis-docs", "praxis-issues", "praxis-code"},
        "code_bank": "praxis-code",
        "log_suffix": "-praxis",
        "workspace_prefixes": ["Users-jgil-go-src-github-com-praxis-proxy"],
        # pingora deliberately excluded (vendored cloudflare/pingora fork,
        # not team-authored architecture) -- see the onboarding plan's
        # "Confirmed scope" section.
        "issues_repos": [
            "praxis-proxy/praxis",
            "praxis-proxy/conventions",
            "praxis-proxy/praxis-proxy.github.io",
            "praxis-proxy/ai",
            "praxis-proxy/forge",
            "praxis-proxy/policy",
            "praxis-proxy/operator",
            "praxis-proxy/experiments",
            "praxis-proxy/grid",
            "praxis-proxy/demos",
        ],
    },
}

BANKS = ["cursor-memory", "kubernaut-docs", "kubernaut-issues"]


def api_get(path: str) -> dict:
    """GET from Hindsight API."""
    url = f"{HINDSIGHT_URL}{path}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
        log.warning("GET %s failed: %s", url, e)
        return {}


def collect_bank_stats(project: str = "kubernaut") -> dict:
    """Collect stats from project banks and write to recall-signals.jsonl."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stats = {}
    for bank in PROJECT_CONFIGS[project]["banks"]:
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
        avg_staleness_hours = None
        now = datetime.now()
        staleness_values = []
        for r in results_list:
            mentioned_at = r.get("mentioned_at")
            if mentioned_at:
                try:
                    ts = datetime.fromisoformat(mentioned_at)
                    staleness_h = (now - ts).total_seconds() / 3600
                    if staleness_h >= 0:
                        staleness_values.append(staleness_h)
                except (ValueError, TypeError):
                    pass
        if staleness_values:
            avg_staleness_hours = round(
                sum(staleness_values) / len(staleness_values), 2
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
            "avg_staleness_hours": avg_staleness_hours,
        }
        with open(RECALL_SIGNALS_PATH, "a") as f:
            f.write(json.dumps(signal) + "\n")
        return signal
    except Exception as e:
        log.warning("Recall probe failed for %s: %s", bank, e)
        return {"error": str(e)}


def run_observability_probes(project: str = "kubernaut"):
    """Run a set of recall probes to measure system health and quality."""
    probes = PROJECT_CONFIGS[project]["probes"]
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


def analyze_mcp_effectiveness(
    transcripts: list[Path],
    hours: int = 24,
    workspace_prefixes: list[str] | None = None,
    end_time: datetime | None = None,
    report_date: date | None = None,
    project: str = "kubernaut",
) -> dict:
    """Analyze MCP usage from hook logs and correlate with correction rates.

    Reads mcp-calls.jsonl (written by afterMCPExecution hook) and correlates
    with per-session correction counts to measure effectiveness.

    If workspace_prefixes is given, only counts hook-log entries whose
    project_dir (Cursor project directory name, e.g. from transcript_path)
    starts with one of the given prefixes. Entries logged before the hook
    started recording project_dir (empty string) are excluded from scoped
    views since their project cannot be determined.

    end_time/report_date default to now/today (normal live usage). Pass
    explicit historical values to backfill a past night's report using the
    exact window it would have seen, against raw data (transcripts,
    mcp-calls.jsonl) that is still on disk — see backfill-effectiveness.py.
    """
    cutoff = (end_time or datetime.now()).timestamp() - (hours * 3600)

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
                    if workspace_prefixes:
                        project_dir = entry.get("project_dir", "")
                        if not any(project_dir.startswith(pfx) for pfx in workspace_prefixes):
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
    # Derived per-project rather than hardcoded to kubernaut's server names --
    # DCM (and now engram) use different MCP server names for their docs/
    # issues/code banks (e.g. "dcm-docs" not "hindsight-docs"), so a fixed
    # kubernaut-shaped set silently zeroed out banks_recalled for every other
    # project's sessions. See docs/FINDINGS.md.
    pconfig_for_banks = PROJECT_CONFIGS.get(project, PROJECT_CONFIGS["kubernaut"])
    RECALL_BANKS = pconfig_for_banks["recall_banks"]
    CODE_BANK = pconfig_for_banks["code_bank"]

    sessions_with_recall = []
    sessions_without_recall = []

    proactive_recall_sessions = 0
    total_agent_turns = 0
    agent_turns_with_recall = 0
    subagent_sessions_excluded = 0

    for transcript_path in transcripts:
        # Subagent transcripts (agent-transcripts/<id>/subagents/<id>.jsonl) are
        # excluded from recall-adoption scoring. Most subagents (e.g. Task-tool
        # `explore`, or any launched with readonly=true) run with no MCP access
        # at all, so they are *structurally* unable to call recall regardless of
        # the alwaysApply rule. Counting them as "sessions without recall"
        # conflates "the rule didn't fire" with "the tool wasn't even available",
        # which previously dragged the blended recall_adoption_pct down to ~40%
        # while genuine top-level conversations were actually recalling ~82% of
        # the time. See docs/FINDINGS.md for the investigation.
        if "/subagents/" in str(transcript_path):
            subagent_sessions_excluded += 1
            continue
        messages = parse_transcript(transcript_path)
        if len(messages) < 6:
            continue

        has_recall = False
        first_recall_turn = None
        user_requested_recall = False
        turn_idx = 0
        banks_recalled = set()
        exploration_before_productive = 0
        seen_productive_action = False

        # K-curve tracking
        first_productive_turn = None
        preamble_chars = 0
        total_session_chars = 0
        productive_action_count = 0
        correction_char_positions = []

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
                if is_correction(text):
                    correction_char_positions.append(total_session_chars)

            if role == "assistant":
                turn_idx += 1
                total_agent_turns += 1
                turn_has_recall = False
                turn_has_productive = False

                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = effective_tool_name(block)
                            mcp_call = extract_mcp_tool_call(block)
                            if mcp_call:
                                tool, server = mcp_call
                                if "recall" in tool.lower():
                                    has_recall = True
                                    turn_has_recall = True
                                    bank = resolve_recalled_bank(server, RECALL_BANKS)
                                    if bank:
                                        banks_recalled.add(bank)
                                    if first_recall_turn is None:
                                        first_recall_turn = turn_idx
                            if name in PRODUCTIVE_TOOLS:
                                productive_action_count += 1
                                turn_has_productive = True
                                if not seen_productive_action:
                                    seen_productive_action = True
                            if name in EXPLORATION_TOOLS and not seen_productive_action:
                                exploration_before_productive += 1

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

        # Rework tokens: for each correction, the segment between it and the next
        # correction (or session end) is partially rework. We estimate half of each
        # post-correction segment as rework tokens.
        rework_chars = 0
        for i, pos in enumerate(correction_char_positions):
            next_boundary = (
                correction_char_positions[i + 1]
                if i + 1 < len(correction_char_positions)
                else total_session_chars
            )
            segment = next_boundary - pos
            rework_chars += segment // 2
        rework_tokens = round(rework_chars / 4)

        bucket = (
            "trivial" if total_session_tokens < BUCKET_TRIVIAL
            else "small" if total_session_tokens < BUCKET_SMALL
            else "medium" if total_session_tokens < BUCKET_MEDIUM
            else "large"
        )
        productivity_density = round(
            productive_action_count / (total_session_tokens / 1000), 4
        ) if total_session_tokens > 0 else 0.0
        rework_ratio = round(
            rework_tokens / total_session_tokens, 4
        ) if total_session_tokens > 0 else 0.0
        session_info = {
            "transcript_id": transcript_path.stem,
            "corrections": correction_count,
            "messages": len(messages),
            "first_productive_turn": first_productive_turn or turn_idx,
            "context_loading_tokens": context_loading_tokens,
            "total_session_tokens": total_session_tokens,
            "productive_actions": productive_action_count,
            "effectiveness_ratio": effectiveness_ratio,
            "rework_tokens": rework_tokens,
            "productivity_density": productivity_density,
            "rework_ratio": rework_ratio,
            "size_bucket": bucket,
            "banks_recalled": sorted(banks_recalled),
            "exploration_actions_before_productive": exploration_before_productive,
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

    # Session distribution by bucket (diagnostic)
    all_sessions = sessions_with_recall + sessions_without_recall
    session_distribution = {}
    for bucket in ("trivial", "small", "medium", "large"):
        session_distribution[bucket] = {
            "with_recall": len([s for s in sessions_with_recall if s["size_bucket"] == bucket]),
            "without_recall": len([s for s in sessions_without_recall if s["size_bucket"] == bucket]),
        }

    # Exploration efficiency
    cocoindex_sessions = [s for s in sessions_with_recall if CODE_BANK in s["banks_recalled"]]
    non_cocoindex_sessions = [s for s in all_sessions if CODE_BANK not in s.get("banks_recalled", [])]
    exploration_efficiency = {
        "with_recall": {
            "avg_exploration_before_productive": _avg(sessions_with_recall, "exploration_actions_before_productive"),
            "sessions": len(sessions_with_recall),
        },
        "without_recall": {
            "avg_exploration_before_productive": _avg(sessions_without_recall, "exploration_actions_before_productive"),
            "sessions": len(sessions_without_recall),
        },
        "with_cocoindex": {
            "avg_exploration_before_productive": _avg(cocoindex_sessions, "exploration_actions_before_productive"),
            "sessions": len(cocoindex_sessions),
        },
        "without_cocoindex": {
            "avg_exploration_before_productive": _avg(non_cocoindex_sessions, "exploration_actions_before_productive"),
            "sessions": len(non_cocoindex_sessions),
        },
    }

    # Weekly trend: this run only has visibility into its own 24h window, so
    # it can only ever honestly report *this run's* contribution to *one*
    # ISO week — not a real week-over-week series. (A previous version of
    # this code looped over every week since epoch and stamped the same
    # non_trivial_recall list on all of them, which fabricated identical
    # "trend" data for weeks that had nothing to do with this run.)
    # report.py aggregates multiple days' entries (keyed by "date") into the
    # actual multi-week trend; this field is that aggregation's raw input.
    non_trivial_recall = [
        s for s in sessions_with_recall
        if s["size_bucket"] != "trivial"
    ]
    weekly_trend = []
    if non_trivial_recall:
        this_run_date = report_date or date.today()
        iso_year, iso_week, _ = this_run_date.isocalendar()
        weekly_trend.append({
            "week": f"{iso_year}-W{iso_week:02d}",
            "sessions": len(non_trivial_recall),
            "corrections_per_session": _avg(non_trivial_recall, "corrections"),
            "rework_pct": round(_avg(non_trivial_recall, "rework_ratio") * 100, 2),
            "productivity_density": _avg(non_trivial_recall, "productivity_density"),
            "first_productive_turn": _avg(non_trivial_recall, "first_productive_turn"),
        })

    report = {
        "date": (report_date or date.today()).isoformat(),
        # Written by run_nightly per project; report.py's multi-day
        # aggregation groups effectiveness-report.jsonl entries by this
        # field. Entries written before this field existed have no
        # "project" key and are treated as "kubernaut" (the only project
        # that existed at the time) by report.py for backward compatibility.
        "project": project,
        "epoch_start_date": EPOCH_START_DATE,
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
            # Subagent transcripts excluded from the counts above — see comment
            # at subagent_sessions_excluded in the Phase 2 loop for rationale.
            "subagent_sessions_excluded": subagent_sessions_excluded,
        },
        "session_distribution": session_distribution,
        "recall_session_stats": {
            "sessions": len(non_trivial_recall),
            "avg_corrections": _avg(non_trivial_recall, "corrections"),
            "avg_rework_pct": round(_avg(non_trivial_recall, "rework_ratio") * 100, 2),
            "avg_productivity_density": _avg(non_trivial_recall, "productivity_density"),
            "avg_first_productive_turn": _avg(non_trivial_recall, "first_productive_turn"),
            "avg_total_tokens": _avg(non_trivial_recall, "total_session_tokens"),
            "avg_context_loading_tokens": _avg(non_trivial_recall, "context_loading_tokens"),
        },
        "weekly_trend": weekly_trend,
        "exploration_efficiency": exploration_efficiency,
        "token_signals": {
            "avg_session_messages_with_recall": _avg(sessions_with_recall, "messages"),
            "avg_session_messages_without_recall": _avg(sessions_without_recall, "messages"),
        },
    }

    # Write effectiveness report
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(EFFECTIVENESS_LOG, "a") as f:
        f.write(json.dumps(report) + "\n")

    return report


def run_hourly(watermarks: dict, seen_hashes: set) -> dict:
    """Hourly mode: retain new corrections/instructions with watermark + hash dedup."""
    log.info("=== Hourly retain started ===")

    # Scoped to onboarded projects only (project_scope.py) -- see docs/FINDINGS.md
    # 2026-07-13. Before this, every one of ~270 Cursor workspaces on this
    # machine fed the shared cursor-memory bank, not just kubernaut/dcm/engram.
    transcripts = find_recent_transcripts(
        hours=2, workspace_prefixes=project_scope.ALLOWED_WORKSPACE_PREFIXES
    )
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
        "contradictions_auto_resolved": 0,
        "contradictions_queued": 0,
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
        project = project_for_transcript_path(path)
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
                all_windows, transcript_id, seen_hashes, project=project
            )
            results["windows_retained"] += retain_result["items_retained"]
            results["skipped_duplicates"] += retain_result.get("skipped_duplicates", 0)
            results["contradictions_auto_resolved"] += retain_result.get("contradictions_auto_resolved", 0)
            results["contradictions_queued"] += retain_result.get("contradictions_queued", 0)
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

    # Topic-shift mental model refresh (lever #2, 2026-07-14 review) --
    # cursor-memory is the only bank this loop retains into.
    refresh_result = maybe_refresh_mental_models_on_topic_shift(
        "cursor-memory", results["windows_retained"]
    )
    results["topic_shift_refresh"] = refresh_result
    if refresh_result["triggered"]:
        log.info("Topic-shift refresh triggered for cursor-memory")
    elif refresh_result["count_since_refresh"] > 0:
        log.info(
            "Topic-shift refresh: %d/%d new items since last refresh (%s)",
            refresh_result["count_since_refresh"], TOPIC_SHIFT_REFRESH_THRESHOLD,
            refresh_result["reason"],
        )

    log.info(
        "=== Hourly done: %d retained, %d duplicates skipped, %d tokens ===",
        results["windows_retained"],
        results["skipped_duplicates"],
        results["total_retain_usage"]["total_tokens"],
    )
    return results


def run_memory_triage(bank_id: str) -> dict:
    """Dynamically load triage-memories.py (kept as a standalone, separately
    runnable script rather than an importable package) and apply its triage
    pass against `bank_id`. Extracted to a module-level function -- rather
    than inlined in run_nightly() -- specifically so tests can monkeypatch
    `nightly_learn.run_memory_triage` instead of exercising the real,
    mutating (apply=True) triage against the live hindsight-api."""
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "triage_memories",
        Path(__file__).resolve().parent / "triage-memories.py",
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod.triage(bank_id=bank_id, stale_days=14, apply=True)


def run_nightly(watermarks: dict, seen_hashes: set, project: str = "kubernaut") -> dict:
    """Nightly mode: catch-all retain + reflect + probes + metrics + mental models."""
    pconfig = PROJECT_CONFIGS[project]
    log_suffix = pconfig["log_suffix"]
    log.info("=== Nightly processing started (project=%s) ===", project)

    # Collect bank stats for observability
    log.info("Collecting bank stats...")
    bank_stats = collect_bank_stats(project)
    for bank, s in bank_stats.items():
        log.info("  %s: %d nodes, %d docs", bank, s["total_nodes"], s["total_documents"])

    # Scoped to onboarded projects only -- see run_hourly()'s comment above and
    # docs/FINDINGS.md 2026-07-13.
    transcripts = find_recent_transcripts(
        hours=24, workspace_prefixes=project_scope.ALLOWED_WORKSPACE_PREFIXES
    )
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
        "contradictions_auto_resolved": 0,
        "contradictions_queued": 0,
        "total_retain_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "reflect_result": None,
        "reflect_windowed_7d": None,
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
            # NOTE: intentionally a distinct name from the outer `project`
            # function parameter -- this is the *transcript's own* resolved
            # project (for correctly tagging its retained corrections), which
            # is not necessarily the same project this run_nightly() call was
            # invoked for (e.g. a transcript from another onboarded project's
            # workspace can appear here since find_recent_transcripts() above
            # scans across all onboarded projects, not just this one). Reusing
            # the outer `project` name here previously clobbered it for the
            # rest of the function, corrupting the "project" field written to
            # effectiveness-report.jsonl below with whichever transcript was
            # processed last -- see docs/FINDINGS.md 2026-07-30.
            transcript_project = project_for_transcript_path(path)
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
                    all_windows, transcript_id, seen_hashes, project=transcript_project
                )
                results["windows_retained"] += retain_result["items_retained"]
                results["skipped_duplicates"] += retain_result.get("skipped_duplicates", 0)
                results["contradictions_auto_resolved"] += retain_result.get("contradictions_auto_resolved", 0)
                results["contradictions_queued"] += retain_result.get("contradictions_queued", 0)
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
    #
    # Only the windowed variant is called here. The unscoped, all-time
    # reflect() (still defined above for any ad-hoc/manual use) reasons over
    # the bank's entire correction history with no way to bound it, and as
    # of 2026-08-10 that made it deterministically fail with HTTP 500
    # ("LiteLLM response was truncated due to token limit") once
    # cursor-memory grew large enough -- confirmed not to be a transient
    # outage by manually re-running it against a healthy hindsight-api and
    # reproducing the identical error. It would only keep failing as the
    # bank keeps growing, and reflect_result was never read downstream
    # anyway (generate-dashboard.py only renders reflect_windowed_7d), so it
    # was retired rather than fixed in place. results["reflect_result"]
    # stays None (its initialized value above) for schema compatibility with
    # historical JSON logs. See docs/FINDINGS.md.
    if results["windows_retained"] > 0 or any(
        watermarks.get(t.stem, {}).get("message_count", 0) > 0 for t in transcripts
    ):
        log.info("Running reflect...")
        results["reflect_windowed_7d"] = reflect_windowed(days=7)

    # Phase: Observability probes
    log.info("Running recall observability probes...")
    results["observability_probes"] = run_observability_probes(project)

    # Phase: MCP effectiveness analysis
    # Scoped to this project's own workspaces so session/token/recall stats
    # AND raw MCP call counts (mcp_usage) reflect this project's work, not
    # every Cursor workspace on the machine. mcp_usage scoping relies on the
    # project_dir field the afterMCPExecution hook started stamping on each
    # log line; older log lines predating that change have no project_dir
    # and are excluded from scoped views.
    log.info("Analyzing MCP effectiveness...")
    workspace_prefixes = pconfig.get("workspace_prefixes")
    project_transcripts = find_recent_transcripts(
        hours=24, workspace_prefixes=workspace_prefixes
    )
    log.info(
        "  Scoped to %d/%d transcripts for project=%s",
        len(project_transcripts), len(transcripts), project,
    )
    effectiveness = analyze_mcp_effectiveness(
        project_transcripts, workspace_prefixes=workspace_prefixes, project=project
    )
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
    dist = effectiveness.get("session_distribution", {})
    if dist:
        log.info(
            "  Session distribution: trivial=%d/%d small=%d/%d medium=%d/%d large=%d/%d (recall/no-recall)",
            dist.get("trivial", {}).get("with_recall", 0), dist.get("trivial", {}).get("without_recall", 0),
            dist.get("small", {}).get("with_recall", 0), dist.get("small", {}).get("without_recall", 0),
            dist.get("medium", {}).get("with_recall", 0), dist.get("medium", {}).get("without_recall", 0),
            dist.get("large", {}).get("with_recall", 0), dist.get("large", {}).get("without_recall", 0),
        )
    recall_stats = effectiveness.get("recall_session_stats", {})
    if recall_stats and recall_stats.get("sessions", 0) > 0:
        log.info(
            "  Recall sessions (non-trivial): %d sessions, %.2f corrections/session, "
            "%.1f%% rework, %.4f productivity density",
            recall_stats["sessions"], recall_stats["avg_corrections"],
            recall_stats["avg_rework_pct"], recall_stats["avg_productivity_density"],
        )
    weekly = effectiveness.get("weekly_trend", [])
    if weekly:
        for wk in weekly:
            log.info(
                "  Weekly [%s]: %d sessions, %.2f corr/sess, %.1f%% rework, "
                "%.4f prod density, %.1f first-productive-turn",
                wk["week"], wk["sessions"], wk["corrections_per_session"],
                wk["rework_pct"], wk["productivity_density"],
                wk["first_productive_turn"],
            )
    explore_eff = effectiveness.get("exploration_efficiency", {})
    if explore_eff:
        wr = explore_eff.get("with_recall", {})
        wor = explore_eff.get("without_recall", {})
        log.info(
            "  Exploration efficiency: %.1f avg actions before productive (recall, %d sessions) "
            "vs %.1f (no recall, %d sessions)",
            wr.get("avg_exploration_before_productive", 0), wr.get("sessions", 0),
            wor.get("avg_exploration_before_productive", 0), wor.get("sessions", 0),
        )
        wc = explore_eff.get("with_cocoindex", {})
        woc = explore_eff.get("without_cocoindex", {})
        if wc.get("sessions", 0) > 0:
            log.info(
                "  Exploration efficiency [cocoindex]: %.1f avg actions (%d sessions) "
                "vs %.1f without (%d sessions)",
                wc.get("avg_exploration_before_productive", 0), wc.get("sessions", 0),
                woc.get("avg_exploration_before_productive", 0), woc.get("sessions", 0),
            )

    # Phase: Refresh all mental models
    log.info("Refreshing mental models...")
    models_to_refresh = pconfig["mental_models"]
    for bank, model_ids in models_to_refresh.items():
        for model_id in model_ids:
            try:
                resp = api_post(
                    f"/v1/default/banks/{bank}/mental-models/{model_id}/refresh",
                    {},
                )
            except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
                log.warning("  %s/%s: refresh failed: %s", bank, model_id, e)
                continue
            if resp and "error" not in resp:
                log.info("  %s/%s: refresh triggered", bank, model_id)
            else:
                log.warning("  %s/%s: refresh failed", bank, model_id)
    results["mental_model_refresh"] = "triggered"

    # This unconditional refresh just covered every bank it manages,
    # including cursor-memory (shared across projects) -- reset the
    # topic-shift counters so run_hourly() doesn't force a redundant
    # refresh later today for material this nightly pass already covered.
    refresh_state = load_model_refresh_state()
    for bank in models_to_refresh:
        if bank in TOPIC_SHIFT_MODELS:
            refresh_state[bank] = {
                "count_since_refresh": 0, "last_triggered_at": datetime.now().isoformat(),
            }
    save_model_refresh_state(refresh_state)

    # Phase: Deduplicate graph (remove exact content-hash duplicates)
    dedup_graph(BANK_ID)

    # Phase: Triage memories (remove ephemeral, stale, duplicate content)
    log.info("Running memory triage...")
    try:
        triage_result = run_memory_triage(BANK_ID)
        results["triage"] = triage_result
        log.info(
            "  Triage: %d/%d flagged (%.1f%%), %d documents deleted",
            triage_result.get("total_flagged", 0),
            triage_result.get("total_memories", 0),
            triage_result.get("flagged_pct", 0),
            triage_result.get("docs_deleted", 0),
        )
    except Exception as e:
        log.warning("Memory triage failed: %s", e)
        results["triage"] = {"error": str(e)}

    # Write daily log (after all phases so triage results are included)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    results["project"] = project
    log_path = LOG_DIR / f"{date.today().isoformat()}{log_suffix}.json"
    with open(log_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info("Results saved to %s", log_path)

    # Regenerate dashboard from all daily reports
    try:
        import importlib.util
        _dash_spec = importlib.util.spec_from_file_location(
            "generate_dashboard",
            Path(__file__).resolve().parent / "generate-dashboard.py",
        )
        _dash_mod = importlib.util.module_from_spec(_dash_spec)
        _dash_spec.loader.exec_module(_dash_mod)
        _dash_mod.main()
        log.info("Dashboard updated")
    except Exception as e:
        log.warning("Dashboard generation failed: %s", e)

    # Phase: Standing-cadence nudge for the pending-contradictions backlog
    notify_result = notify_pending_contradictions_backlog()
    results["pending_contradictions_notify"] = notify_result
    if notify_result["notified"]:
        log.info(
            "Notified: %d pending contradictions >= threshold",
            notify_result["pending_count"],
        )
    elif notify_result["pending_count"] > 0:
        log.info(
            "Pending contradictions: %d (%s)",
            notify_result["pending_count"], notify_result["skipped_reason"],
        )

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
        choices=["hourly", "nightly", "both", "reprocess-fallback"],
        default="both",
        help="Run mode: hourly (retain only), nightly (reflect+probes), both (default), "
             "reprocess-fallback (retry fallback-retained.jsonl entries buffered when "
             "hindsight-api's retain call failed transiently -- see GitHub issue #5)",
    )
    parser.add_argument(
        "--project",
        choices=list(PROJECT_CONFIGS.keys()),
        default="kubernaut",
        help="Project scope for nightly phases (default: kubernaut)",
    )
    args = parser.parse_args()

    watermarks = load_watermarks()
    seen_hashes = load_retained_hashes()

    try:
        if args.mode in ("hourly", "both"):
            run_hourly(watermarks, seen_hashes)

        if args.mode in ("nightly", "both"):
            run_nightly(watermarks, seen_hashes, project=args.project)

        if args.mode == "reprocess-fallback":
            result = reprocess_fallback_backlog()
            log.info(
                "Reprocess-fallback: %d attempted, %d recovered, %d still failing",
                result["attempted"], result["recovered"], result["still_failing"],
            )
    finally:
        save_watermarks(watermarks)
        save_retained_hashes(seen_hashes)


if __name__ == "__main__":
    main()
