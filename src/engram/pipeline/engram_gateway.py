#!/usr/bin/env python3
"""Generic MCP gateway with optional project-specific legacy configuration.

The config-driven runtime aggregates any configured HTTP or stdio MCP backends
behind one client-facing HTTP mount per project. The no-config native mode
retains the historical static registry for this installation's onboarded
projects; that registry is compatibility configuration, not gateway behavior.

The original use case aggregated hindsight-docs, hindsight-issues,
cocoindex-code, and serena behind ONE Cursor-facing MCP HTTP mount per repo,
instead of the 3-4 separate `.cursor/mcp.json` server entries every onboarded
repo had before this module existed.

Graduated from a single-repo (`praxis-grid`) spike on 2026-08-21 to cover
every onboarded repo across all families (kubernaut, koku, dcm, praxis,
rhdh-plugins, engram itself, and -- as a single cross-mounted recall-only
entry rather than one-mount-per-repo, see its own registry comment --
kuadrant) after the spike found no fundamental blocker --
see docs/findings/2026-08.md's 2026-08-21 entries for the spike results and
the full-rollout survey/decisions. `build_project_registry()` below is the
single source of truth for what each repo gets.

Background (see the "Engram unified MCP gateway spike" plan and
docs/findings/2026-08.md's many "MCP shows Disabled" entries): most of this
session's recurring MCP-flakiness incidents trace back to either Cursor's
client-side reconnect logic getting stuck on *one* of several live
connections, or a backend daemon restarting mid-session. Both get less
likely to matter the fewer independent connections Cursor has to keep
healthy per repo. `serena_multiplex.py` already proved the core pattern --
one stable Cursor-facing HTTP mount, thin relay to a flakier backend, retry
on backend hiccups -- for a *single* tool family. This module generalizes
that to aggregate **four** families behind one mount.

Two backend transports exist today:
  - hindsight-docs / hindsight-issues are already HTTP (hindsight-api on
    :8888) -- `HttpRelayAdapter` does one-shot POST-only MCP round-trips
    against them, the same "never open a GET/SSE listen stream" approach
    `serena_multiplex.py` uses (see that module's docstring for why: a
    persistent SSE client crashed the shared Serena daemon during the
    2026-08-13 spike -- known, still-open upstream `mcp`/fastmcp bugs).
  - cocoindex-code and serena are currently spawned fresh, per Cursor
    window, as **stdio** subprocesses. `StdioSubprocessAdapter` instead
    spawns each **once** and keeps it alive as this gateway's own
    long-lived child process, talking MCP-over-stdio to it via the `mcp`
    Python SDK's client session -- the same machinery Cursor itself would
    use, just hosted here instead of once per window.

Tool-name collisions: checked the real tool catalogs (see the plan) -- the
*only* collision is hindsight-docs vs hindsight-issues, since they're the
same backend type against different banks and expose identical tool names
(`recall`, `retain`, ...). cocoindex-code's and serena's tool names don't
collide with anything. So only the "docs"/"issues" backend keys get their
tool names prefixed (`docs_recall`, `issues_recall`); "code"/"serena" pass
through unprefixed.

Per-backend degradation is a first-class goal, not an afterthought: a
backend that fails to list its tools (dead subprocess, unreachable HTTP)
is dropped from the merged catalog and logged, but never takes down the
other three families or the gateway process itself. A backend that dies
*after* being listed (subprocess crash between tools/list and tools/call)
surfaces as a clean per-call JSON-RPC error instead of a hang or 500.

Runs as a single supervised launchd service
(`launchd/io.vectorize.engram-gateway-native.plist`) fronting every registry entry
at once -- see that plist's own header for the KeepAlive/restart rationale
(a bare, unsupervised `engram-gateway` process was found during the spike to
die silently without one, twice, in well under an hour).
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import math
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - engram-gateway - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("engram-gateway")

from engram import mcp_compat  # noqa: E402  (mcp 1.x/2.x Tool compat)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8896
FORWARD_TIMEOUT_S = 60.0
MAX_FORWARD_TIMEOUT_S = 600.0

# Hindsight recall returns both a text JSON envelope and an equivalent
# `structuredContent` object. The raw envelope is useful for debugging but is
# too large and opaque for a model-facing MCP result, especially when a broad
# query returns long document chunks. Keep recall bounded while preserving the
# fields needed to identify and verify each fact.
RECALL_SCHEMA_VERSION = "engram-recall.v1"
MAX_RECALL_RESULTS = 8
MAX_RECALL_SUMMARY_CHARS = 800
MAX_RECALL_METADATA_VALUE_CHARS = 240
SERENA_PATTERN_SCHEMA_VERSION = "engram-serena-pattern-search.v1"
MAX_SERENA_PATTERN_MATCHES = 100
MAX_SERENA_PATTERN_LINE_CHARS = 300
MAX_SERENA_PATTERN_RESULT_CHARS = 6000

# Gateway-owned call log, distinct from the Cursor-hook-authored
# ~/.engram/logs/mcp-calls.jsonl (cursor/hooks/log-mcp-calls.sh). That
# hook's `result_chars` is best-effort and frequently 0 -- its own comment
# notes Cursor's afterMCPExecution payload usually omits content text
# despite docs claiming a "full JSON result". `handle_tools_call` below is
# the one place that *does* see the real, un-redacted response for every
# backend (stdio and HTTP alike), so it's the reliable place to compute and
# log a real (well, tiktoken-estimated -- see _estimate_tokens) token count
# per call. 2026-08-30: added after the user asked whether MCP call token
# consumption could be calculated at all, and a cursor-guide subagent
# confirmed no Cursor hook or CLI surface carries real per-call token
# counts locally (Team/Enterprise usage APIs report at turn granularity,
# not per tool call, and require a paid plan).
GATEWAY_CALLS_LOG = pathlib.Path(os.path.expanduser("~/.engram/logs/gateway-calls.jsonl"))


@functools.lru_cache(maxsize=1)
def _tiktoken_encoding():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def _estimate_tokens(text: str) -> int:
    """Best-effort token estimate for `text` using tiktoken's cl100k_base
    BPE encoding. This is necessarily an approximation for Claude-backed
    Cursor sessions: Anthropic doesn't publish an open tokenizer, so
    OpenAI's encoding is the closest widely-available stand-in, not an
    exact match. Falls back to a chars/4 heuristic (a commonly-cited rule
    of thumb for English text) if tiktoken itself is unavailable/fails --
    e.g. a first-run encoding download with no network -- so a token-count
    side channel can never break the actual tool call it's observing."""
    if not text:
        return 0
    try:
        return len(_tiktoken_encoding().encode(text))
    except Exception:
        log.warning("tiktoken estimation failed, falling back to chars/4 heuristic", exc_info=True)
        return len(text) // 4


def _extract_result_text(result: dict) -> str:
    """Concatenate every text content item in an MCP tool result -- the
    same shape both HttpRelayAdapter and StdioSubprocessAdapter return
    (`{"content": [{"type": "text", "text": ...}, ...], "isError": ...}`).
    Non-text content items (images, etc.) contribute nothing; there's no
    well-defined token cost to estimate for those here."""
    content = result.get("content") or []
    return "".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")


def _bounded_text(value: object, limit: int) -> object:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}\n... [truncated; {len(value)} chars total]"


def _recall_payload(result: dict) -> dict | None:
    """Find the Hindsight recall envelope in either MCP result representation."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("results"), list):
        return structured

    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            return payload
    return None


def _normalize_recall_record(record: object) -> dict | None:
    if not isinstance(record, dict):
        return None

    normalized: dict[str, object] = {}
    for key in (
        "id",
        "fact_type",
        "context",
        "occurred_start",
        "occurred_end",
        "mentioned_at",
        "document_id",
        "tags",
    ):
        value = record.get(key)
        if value is not None:
            normalized[key] = value

    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        summary = metadata.get("key_sentences")
        if not isinstance(summary, str) or not summary.strip():
            summary = record.get("text")
        if isinstance(summary, str):
            normalized["summary"] = _bounded_text(summary, MAX_RECALL_SUMMARY_CHARS)

        keywords = metadata.get("keywords")
        if isinstance(keywords, str):
            compact_keywords = [keyword.strip() for keyword in keywords.split(",") if keyword.strip()]
            if compact_keywords:
                normalized["keywords"] = compact_keywords

        compact_metadata = {
            str(key): _bounded_text(value, MAX_RECALL_METADATA_VALUE_CHARS)
            for key, value in metadata.items()
            if key not in {"key_sentences", "keywords"}
            and isinstance(key, str)
            and isinstance(value, (str, int, float, bool))
        }
        if compact_metadata:
            normalized["metadata"] = compact_metadata
    elif isinstance(record.get("text"), str):
        normalized["summary"] = _bounded_text(record["text"], MAX_RECALL_SUMMARY_CHARS)

    scores = record.get("scores")
    if isinstance(scores, dict):
        compact_scores = {
            key: round(value, 4) if isinstance(value, (int, float)) else value
            for key, value in scores.items()
            if value is not None
        }
        if compact_scores:
            normalized["scores"] = compact_scores

    return normalized


def _format_recall_text(records: list[dict], total: int) -> str:
    lines = [
        f"Engram recall ({len(records)} of {total} results; schema {RECALL_SCHEMA_VERSION})",
    ]
    for index, record in enumerate(records, 1):
        labels = [str(record[key]) for key in ("fact_type", "context") if record.get(key)]
        document_id = record.get("document_id")
        if document_id:
            labels.append(f"document={document_id}")
        tags = record.get("tags")
        if tags:
            labels.append("tags=" + ",".join(str(tag) for tag in tags))
        lines.append(f"\n[{index}] " + (" | ".join(labels) or "memory"))
        if record.get("summary"):
            lines.append(str(record["summary"]))
    if total > len(records):
        lines.append(f"\n... {total - len(records)} lower-ranked results omitted; narrow the query for more detail.")
    return "\n".join(lines)


def _normalize_recall_result(result: dict) -> dict:
    """Return a bounded, readable and structured Hindsight recall result.

    This is deliberately deterministic and fail-open. It only changes a
    result when it contains Hindsight's normal `results` envelope; malformed
    or unfamiliar backend output remains untouched for diagnosis.
    """
    if result.get("isError"):
        return result

    payload = _recall_payload(result)
    if payload is None:
        return result

    raw_records = payload["results"]
    records = [
        normalized
        for raw in raw_records[:MAX_RECALL_RESULTS]
        if (normalized := _normalize_recall_record(raw)) is not None
    ]
    normalized_payload = {
        "schema_version": RECALL_SCHEMA_VERSION,
        "results": records,
        "total_results": len(raw_records),
        "returned_results": len(records),
        "truncated": len(raw_records) > len(records) or bool(payload.get("source_facts_truncated")),
    }
    if payload.get("source_facts_truncated"):
        normalized_payload["source_facts_truncated"] = True

    return {
        **result,
        "content": [{"type": "text", "text": _format_recall_text(records, len(raw_records))}],
        "structuredContent": normalized_payload,
    }


def _serena_overflow_matches(text: str) -> dict | None:
    """Extract Serena's per-file match list from its max-answer diagnostic."""
    marker = "Matched lines per file; use read_file with the line numbers for surrounding context:"
    marker_index = text.find(marker)
    if marker_index < 0 or not re.search(r"answer is too long", text, re.IGNORECASE):
        return None

    serialized_matches = text[marker_index + len(marker):].lstrip()
    try:
        payload, _end = json.JSONDecoder().raw_decode(serialized_matches)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _serena_diagnostic_field(text: str, label: str) -> str | None:
    match = re.search(rf"^{re.escape(label)}\s*(.*?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _serena_pattern_result(result: dict) -> dict:
    """Turn Serena's oversized-pattern diagnostic into bounded partial results.

    Serena includes useful matched file/line data even when surrounding context
    exceeds ``max_answer_chars``. Extract that list and return it grouped by
    file, rather than passing through an unusable error containing a JSON blob.
    Unknown or malformed responses remain untouched for diagnosis.
    """
    original_text = _extract_result_text(result)
    matches_by_path = _serena_overflow_matches(original_text)
    if matches_by_path is None:
        return result

    collected: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, int, str]] = set()
    matched_paths: set[str] = set()
    total_matches = 0
    for path, raw_matches in matches_by_path.items():
        if not isinstance(path, str) or not isinstance(raw_matches, list):
            continue
        for raw_match in raw_matches:
            if not isinstance(raw_match, dict) or not isinstance(raw_match.get("text"), str):
                continue
            try:
                line_number = int(raw_match.get("line"))
            except (TypeError, ValueError):
                continue
            match_text = raw_match["text"].replace("\r", "").replace("\n", "\\n").strip()
            key = (path, line_number, match_text)
            if key in seen:
                continue
            seen.add(key)
            matched_paths.add(path)
            total_matches += 1
            if total_matches > MAX_SERENA_PATTERN_MATCHES:
                continue
            collected.setdefault(path, []).append({
                "line": line_number,
                "text": _bounded_text(match_text, MAX_SERENA_PATTERN_LINE_CHARS),
            })

    if not collected:
        return result

    limit_match = re.search(r"^Max answer chars:\s*(\d+)\s*$", original_text, re.MULTILINE)
    answer_match = re.search(r"answer is too long\s*\(([\d,]+) characters\)", original_text, re.IGNORECASE)
    answer_chars = int(answer_match.group(1).replace(",", "")) if answer_match else None
    max_answer_chars = int(limit_match.group(1)) if limit_match else None
    pattern = _serena_diagnostic_field(original_text, "Substring pattern:")
    path_glob = _serena_diagnostic_field(original_text, "Paths include glob:")
    truncated = total_matches > MAX_SERENA_PATTERN_MATCHES
    structured = {
        "schema_version": SERENA_PATTERN_SCHEMA_VERSION,
        "status": "partial",
        "reason": "upstream_answer_too_long",
        "pattern": pattern,
        "path_glob": path_glob,
        "answer_chars": answer_chars,
        "max_answer_chars": max_answer_chars,
        "matched_file_count": len(matched_paths),
        "matched_line_count": total_matches,
        "returned_match_count": sum(len(matches) for matches in collected.values()),
        "truncated": truncated,
        "matches_by_file": [
            {"path": path, "matches": matches}
            for path, matches in collected.items()
        ],
    }

    lines = [
        f"Serena pattern search: {structured['matched_line_count']} matches in "
        f"{structured['matched_file_count']} files (partial results).",
    ]
    if answer_chars is not None and max_answer_chars is not None:
        lines.append(
            f"The full answer ({answer_chars:,} chars) exceeded the {max_answer_chars:,}-character limit; "
            "surrounding context was omitted."
        )
    if pattern:
        lines.append(f"Pattern: {pattern}")
    if path_glob:
        lines.append(f"Path scope: {path_glob}")
    lines.append("Matches are grouped by file; use read_file at a listed line for surrounding context.")

    display_truncated = False
    displayed_match_count = 0
    for path, matches in collected.items():
        group_lines = [f"\n- {path}"]
        group_lines.extend(f"  - L{match['line']}: {match['text']}" for match in matches)
        candidate = "\n".join(lines + group_lines)
        if len(candidate) > MAX_SERENA_PATTERN_RESULT_CHARS:
            display_truncated = True
            break
        lines.extend(group_lines)
        displayed_match_count += len(matches)
    omitted_matches = max(0, total_matches - displayed_match_count)
    if omitted_matches:
        suffix = f"\n... {omitted_matches} additional matches omitted from the compact display."
        while lines and len("\n".join(lines)) + len(suffix) > MAX_SERENA_PATTERN_RESULT_CHARS:
            lines.pop()
        lines.append(suffix.lstrip("\n"))
    structured["truncated"] = truncated or display_truncated

    normalized = {
        **result,
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": structured,
        "isError": False,
    }
    if "is_error" in result:
        normalized["is_error"] = False
    return normalized


def _log_gateway_call(
    *,
    project: str | None,
    backend: str,
    tool: str,
    is_error: bool,
    result_chars: int,
    est_tokens: int,
) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "project": project,
        "backend": backend,
        "tool": tool,
        "is_error": is_error,
        "result_chars": result_chars,
        "est_tokens": est_tokens,
    }
    try:
        GATEWAY_CALLS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with GATEWAY_CALLS_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        log.warning("failed to write gateway call metrics for %r/%r", backend, tool, exc_info=True)

# Backend keys whose tool names collide across families (both are the same
# hindsight-api backend type, just pointed at a different bank) and
# therefore need disambiguating. Anything not in this set (code, serena)
# passes through unprefixed -- verified empirically that their tool names
# don't collide with each other or with docs/issues (see plan).
#
# kuadrant_docs/kuadrant_issues added 2026-08-27: the "kuadrant" project
# entry is cross-mounted as a *second* MCP server into every praxis-*
# repo's .cursor/mcp.json (see build_project_registry()'s "kuadrant" entry
# and RELEVANT_TOOLS_BY_BACKEND below) alongside that repo's own "engram"
# mount -- two independent MCP servers both offering a bare "recall" tool
# would collide client-side, so these get project-qualified names
# (kuadrant_docs_recall, kuadrant_issues_recall) instead of the usual
# docs_recall/issues_recall.
PREFIXED_BACKENDS = frozenset({
    "docs",
    "issues",
    "docs_manual",
    "issues_manual",
    "kuadrant_docs",
    "kuadrant_issues",
})


class BackendAdapter(Protocol):
    """What `aggregate_tools_list`/`handle_tools_call` need from a backend,
    real (HttpRelayAdapter, StdioSubprocessAdapter) or fake (tests)."""

    async def list_tools(self) -> list[dict]: ...

    async def call_tool(self, name: str, arguments: dict) -> dict: ...


def prefixed_tool_name(backend_key: str, raw_name: str) -> str:
    """Aggregated-catalog name for a tool `raw_name` from `backend_key`."""
    if backend_key in {"docs_manual", "issues_manual"} and raw_name == "manual_mental_model":
        return f"{backend_key}_mental_model"
    if backend_key in PREFIXED_BACKENDS:
        return f"{backend_key}_{raw_name}"
    return raw_name


def build_catalog(per_backend_tools: dict[str, list[dict]]) -> tuple[dict[str, tuple[str, str]], list[dict]]:
    """Merge each backend's raw tool list into one aggregated catalog.

    Returns (catalog, tool_defs):
      - catalog maps the aggregated (possibly-prefixed) tool name to
        (backend_key, raw_tool_name), used by `route_call` to dispatch
        `tools/call`.
      - tool_defs is the list of tool definitions to hand back verbatim in
        a `tools/list` response, with just the `name` field rewritten to
        the aggregated name.

    Fails safe on an unexpected unprefixed-name collision across backends
    (shouldn't happen today -- verified empirically, see module docstring
    -- but a future backend addition could introduce one): the colliding
    tool is qualified with its backend key instead of being silently dropped.
    """
    catalog: dict[str, tuple[str, str]] = {}
    tool_defs: list[dict] = []
    for backend_key, tools in per_backend_tools.items():
        for tool in tools:
            raw_name = tool["name"]
            final_name = prefixed_tool_name(backend_key, raw_name)
            if final_name in catalog:
                base_name = f"{backend_key}_{raw_name}"
                final_name = base_name
                duplicate = 2
                while final_name in catalog:
                    final_name = f"{base_name}_{duplicate}"
                    duplicate += 1
                log.warning(
                    "tool name collision: %r from backend %r qualified as %r; already owned by backend %r",
                    raw_name,
                    backend_key,
                    final_name,
                    catalog[prefixed_tool_name(backend_key, raw_name)][0]
                    if prefixed_tool_name(backend_key, raw_name) in catalog
                    else "another backend",
                )
            catalog[final_name] = (backend_key, raw_name)
            tool_defs.append({**tool, "name": final_name})
    return catalog, tool_defs


def route_call(tool_name: str, catalog: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """Which backend owns `tool_name`, and what its raw (un-prefixed) name
    is there -- or None if unknown (never existed, or its backend was
    dropped from the catalog by `aggregate_tools_list` for being down)."""
    return catalog.get(tool_name)


# Tools rarely exercised by the documented recall/retain/reflect/mental-model
# workflow (hindsight-memory.mdc) or by Serena's symbol/search/edit tools
# (local-mcp-tool-preference.mdc) get dropped from the aggregated catalog
# entirely, not just hidden client-side -- every extra tool definition costs
# context-window budget and counts against Cursor's own active-tool ceiling.
# A single hindsight-api backend's raw catalog is 29 tools (mostly bank/
# document/operation/directive administration never used day-to-day); a repo
# wired to BOTH hindsight-docs and hindsight-issues plus serena's 27 tools
# (kubernaut-family) hit 87 total -- more than double Cursor's ~40-tool
# ceiling, and got stuck permanently `Disabled` no matter how many times it
# was re-enabled (see docs/findings/2026-08.md, 2026-08-22 "MCP shows
# Disabled, root cause: tool-count ceiling" entry). Trimming every hindsight-
# shaped and serena backend to its actually-used subset keeps every onboarded
# repo well under budget without losing any tool actually exercised in
# practice; the dropped administrative tools remain reachable via a direct
# curl against hindsight-api / the serena daemon if ever genuinely needed.
#
# `create_mental_model` added back 2026-08-26: until this repo's own
# `engram.maintenance.create_mental_models` admin script was the only way to
# mint a new mental model, project teams (first raised by praxis, wanting a
# "processes and policies" model none of the 3 pre-seeded praxis-docs models
# covered) had no self-serve path at all. Re-adding it to every onboarded
# project's docs/issues surface (not just praxis -- same gap applies
# everywhere) costs 2 tools per project (docs + issues, where both are
# wired); kubernaut-family, the highest today at 33, has ample headroom
# under the ~40 ceiling.
RELEVANT_HINDSIGHT_TOOLS = frozenset(
    {
        "retain",
        "sync_retain",
        "recall",
        "reflect",
        "list_mental_models",
        "get_mental_model",
        "create_mental_model",
        "refresh_mental_model",
    }
)

# Recall-only variant for cross-project reference mounts (2026-08-27,
# Kuadrant onboarding): "kuadrant" is prior-art reference material ingested
# for recall from *other* projects' workspaces, not something anyone
# retains into, reflects on, or manages mental models for from a praxis
# window -- that admin/curation work happens wherever the ingestion itself
# runs. Cross-mounting the full RELEVANT_HINDSIGHT_TOOLS set (8 tools x 2
# backends = 16) into every praxis-* repo on top of that repo's own ~33-35
# tools would reliably blow past Cursor's ~40-tool ceiling (see
# RELEVANT_HINDSIGHT_TOOLS's own comment above); trimming to just `recall`
# keeps the added footprint to 1 tool per backend.
RECALL_ONLY_HINDSIGHT_TOOLS = frozenset({"recall"})

# Same rationale as RECALL_ONLY_HINDSIGHT_TOOLS, for kuadrant's code-search
# backend: only the semantic/BM25 search tool is exposed cross-project,
# not pattern-search or the call-graph tools (impact-analysis tooling for
# a codebase nobody here refactors) -- see engram.search.kuadrant's module
# docstring.
RECALL_ONLY_CODE_TOOLS = frozenset({"kuadrant_code_search"})

MANUAL_MENTAL_MODEL_TOOLS = frozenset({"manual_mental_model"})

RELEVANT_SERENA_TOOLS = frozenset(
    {
        "replace_content",
        "replace_in_files",
        "replace_symbol_body",
        "insert_after_symbol",
        "insert_before_symbol",
        "search_for_pattern",
        "get_symbols_overview",
        "find_symbol",
        "find_referencing_symbols",
        "find_implementations",
        "find_declaration",
        "get_diagnostics_for_file",
        "rename_symbol",
        "safe_delete_symbol",
        "activate_project",
        "list_queryable_projects",
        "query_project",
    }
)

SERENA_PATTERN_SEARCH_GUIDANCE = (
    "Prefer `find_symbol` for symbol definitions and `find_referencing_symbols` for callers/references. "
    "Use this tool for literal or regex text searches. `substring_pattern` is a regex; with multiline matching, "
    "`.` can cross lines and `|` needs grouping. Prefer anchored patterns and narrow path/context filters; "
    "split broad alternatives into focused searches and reduce scope or context before raising `max_answer_chars`. "
    "If the answer limit is exceeded, Engram returns the available matches grouped by file and line."
)

# Backends with no entry here (code/cocoindex, and any future family) pass
# through unfiltered -- their catalogs are already small (2 tools today).
RELEVANT_TOOLS_BY_BACKEND: dict[str, frozenset[str]] = {
    "docs": RELEVANT_HINDSIGHT_TOOLS,
    "issues": RELEVANT_HINDSIGHT_TOOLS,
    "serena": RELEVANT_SERENA_TOOLS,
    "docs_manual": MANUAL_MENTAL_MODEL_TOOLS,
    "issues_manual": MANUAL_MENTAL_MODEL_TOOLS,
    "kuadrant_docs": RECALL_ONLY_HINDSIGHT_TOOLS,
    "kuadrant_issues": RECALL_ONLY_HINDSIGHT_TOOLS,
    "kuadrant_code": RECALL_ONLY_CODE_TOOLS,
    "rca": frozenset({
        "ingest_test_run",
        "triage_test_failure",
        "generate_rca",
        "get_evidence",
        "get_related_events",
        "promote_incident",
        "get_failure_history",
        "get_incident_timeline",
    }),
}


def filter_relevant_tools(backend_key: str, tools: list[dict]) -> list[dict]:
    """Drop rarely-used administrative tools for backends known to expose an
    oversized catalog. Add usage guidance to selected tools where the
    upstream description needs project-specific routing advice. See
    `RELEVANT_TOOLS_BY_BACKEND`'s module-level comment for why this exists."""
    allowed = RELEVANT_TOOLS_BY_BACKEND.get(backend_key)
    if allowed is None:
        return tools

    filtered = []
    for tool in tools:
        if tool["name"] not in allowed:
            continue
        if backend_key == "serena" and tool["name"] == "search_for_pattern":
            description = (tool.get("description") or "").rstrip()
            tool = {
                **tool,
                "description": f"{description}\n\n{SERENA_PATTERN_SEARCH_GUIDANCE}".strip(),
            }
        filtered.append(tool)
    return filtered


async def _fetch_backend_tools(backend_key: str, adapter: BackendAdapter) -> tuple[str, list[dict] | Exception]:
    try:
        tools = await adapter.list_tools()
        return backend_key, tools
    except Exception as exc:  # noqa: BLE001 - a dead backend must not raise into the caller
        return backend_key, exc


async def aggregate_tools_list(
    backends: dict[str, BackendAdapter],
) -> tuple[list[dict], dict[str, tuple[str, str]], dict[str, str]]:
    """Query every backend's `list_tools()` concurrently, merge the
    survivors into one catalog, and report which backends (if any) failed
    -- without ever raising, so one dead backend can't break `tools/list`
    for the other three."""
    results = await asyncio.gather(*(_fetch_backend_tools(key, adapter) for key, adapter in backends.items()))

    per_backend_tools: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for backend_key, outcome in results:
        if isinstance(outcome, Exception):
            errors[backend_key] = str(outcome)
            log.warning("backend %r failed to list tools: %s", backend_key, outcome)
            continue
        per_backend_tools[backend_key] = filter_relevant_tools(backend_key, outcome)

    catalog, tool_defs = build_catalog(per_backend_tools)
    return tool_defs, catalog, errors


def _jsonrpc_error_result(message_id: Any, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def _jsonrpc_success_result(message_id: Any, backend_result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": backend_result}


async def handle_tools_call(
    message: dict,
    catalog: dict[str, tuple[str, str]],
    backends: dict[str, BackendAdapter],
    project: str | None = None,
) -> dict | None:
    """Route a `tools/call` JSON-RPC message to whichever backend owns the
    (aggregated) tool name, returning a well-formed JSON-RPC response.
    Returns None for any other method, so the caller knows to forward it
    generically instead (e.g. `initialize`, `notifications/initialized`).

    Never raises: an unknown tool name and a backend that throws mid-call
    both come back as a normal (isError=True) tool result instead of an
    exception, so a single dead/unknown-tool call can't take down the
    connection the way an unhandled exception in the ASGI endpoint would.

    `project` is optional and only used for the call-metrics log line (see
    _log_gateway_call) -- existing callers/tests that predate that feature
    don't need to pass it.
    """
    if message.get("method") != "tools/call":
        return None

    message_id = message.get("id")
    params = message.get("params") or {}
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}

    routed = route_call(tool_name, catalog)
    if routed is None:
        return _jsonrpc_error_result(
            message_id,
            f"Unknown tool: {tool_name!r} (never existed, or its backend is currently down)",
        )

    backend_key, raw_name = routed
    adapter = backends.get(backend_key)
    if adapter is None:
        return _jsonrpc_error_result(message_id, f"Backend {backend_key!r} for tool {tool_name!r} is not wired up")

    try:
        result = await adapter.call_tool(raw_name, arguments)
    except Exception as exc:  # noqa: BLE001 - degrade to a clean tool error, don't crash the gateway
        log.warning("backend %r failed on tools/call(%r): %s", backend_key, raw_name, exc)
        _log_gateway_call(
            project=project, backend=backend_key, tool=tool_name,
            is_error=True, result_chars=0, est_tokens=0,
        )
        return _jsonrpc_error_result(message_id, f"backend {backend_key!r} failed: {exc}")

    # The legacy single-endpoint runtime registry forwards already-prefixed
    # names (for example, ``docs_recall``) through a host gateway. Newer
    # per-backend registries strip that prefix before reaching this point.
    if raw_name == "recall" or raw_name.endswith("_recall"):
        result = _normalize_recall_result(result)
    elif backend_key == "serena" and raw_name == "search_for_pattern":
        result = _serena_pattern_result(result)
    result_text = _extract_result_text(result)
    _log_gateway_call(
        project=project, backend=backend_key, tool=tool_name,
        is_error=bool(result.get("isError")),
        result_chars=len(result_text),
        est_tokens=_estimate_tokens(result_text),
    )
    return _jsonrpc_success_result(message_id, result)


# ---------------------------------------------------------------------------
# Real backend adapters (not exercised by the unit tests above -- those use
# FakeAdapter; these are validated live during the plan's gateway-verify /
# gateway-degradation-test steps).
# ---------------------------------------------------------------------------


def _parse_sse_json(body: bytes) -> dict:
    """Same SSE-or-plain-JSON parsing as serena_multiplex.py -- duplicated
    rather than imported so this spike module has no dependency on that
    one; worth de-duplicating into a shared helper if this graduates past
    spike status."""
    text = body.decode("utf-8", errors="replace").strip()
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload:
                return json.loads(payload)
    return json.loads(text)


class HttpRelayAdapter:
    """Backend adapter for an already-HTTP MCP server (hindsight-docs,
    hindsight-issues). Each call is a fresh one-shot MCP session
    (initialize -> notifications/initialized -> the real call -> delete),
    matching serena_multiplex.py's "POST-only, never GET/SSE" design to
    avoid the upstream mcp/fastmcp SSE-reconnect bug documented there."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = FORWARD_TIMEOUT_S,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout_seconds = timeout_seconds

    async def _roundtrip(self, method: str, params: dict | None = None) -> dict:
        import httpx

        # Some local MCP adapters route by the HTTP Host header and reject the
        # container bridge hostname. Keep this configurable in the runtime
        # image instead of embedding host-specific behavior in the adapter.
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            init_resp = await client.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "engram-gateway", "version": "0.0.1"},
                    },
                },
                headers=headers,
            )
            init_resp.raise_for_status()
            session_id = init_resp.headers.get("mcp-session-id")
            session_headers = {**headers, "mcp-session-id": session_id} if session_id else headers

            await client.post(
                self.url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=session_headers,
            )

            call_resp = await client.post(
                self.url,
                json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}},
                headers=session_headers,
            )
            call_resp.raise_for_status()
            result = _parse_sse_json(call_resp.content)

            if session_id:
                await client.delete(self.url, headers=session_headers)

        if "error" in result:
            raise RuntimeError(f"{method} failed: {result['error']}")
        return result.get("result", {})

    async def list_tools(self) -> list[dict]:
        result = await self._roundtrip("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._roundtrip("tools/call", {"name": name, "arguments": arguments})


ZVEC_SHADOWED_TOOLS = {
    "zvec_grep_search": "cocoindex_search",
    "zvec_grep_callgraph_blast_radius": "cocoindex_call_graph_blast_radius",
    "zvec_grep_callgraph_shortest_path": "cocoindex_call_graph_shortest_path",
    "zvec_grep_callgraph_cluster": "cocoindex_call_graph_get_cluster",
}
MAX_PENDING_ZVEC_SHADOWS = 8


def _kubernaut_shadow_source_roots() -> dict[str, pathlib.Path]:
    home = pathlib.Path.home()
    configured = {
        "kubernaut": os.environ.get("ENGRAM_CODE_DIR", str(home / "go/src/github.com/jordigilh/kubernaut")),
        "kubernaut-operator": os.environ.get(
            "ENGRAM_OPERATOR_DIR", str(home / "go/src/github.com/jordigilh/kubernaut-operator")
        ),
        "kubernaut-console": os.environ.get(
            "ENGRAM_CONSOLE_DIR", str(home / "go/src/github.com/jordigilh/kubernaut-console")
        ),
        "kubernaut-demo-scenarios": os.environ.get(
            "ENGRAM_SCENARIOS_DIR", str(home / "go/src/github.com/jordigilh/kubernaut-demo-scenarios")
        ),
    }
    return {repo: pathlib.Path(root).expanduser().resolve() for repo, root in configured.items()}


def _git_workspace_metadata(root: pathlib.Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {"workspace": str(root), "branch": None, "commit": None, "dirty": None}
    for key, command in (
        ("branch", ["git", "-C", str(root), "branch", "--show-current"]),
        ("commit", ["git", "-C", str(root), "rev-parse", "HEAD"]),
    ):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            metadata[key] = result.stdout.strip() or None
    try:
        tracked_changes = []
        for command in (
            ["git", "-C", str(root), "diff", "--quiet"],
            ["git", "-C", str(root), "diff", "--cached", "--quiet"],
        ):
            tracked_changes.append(
                subprocess.run(command, capture_output=True, text=True, check=False, timeout=5)
            )
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "--directory"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if all(result.returncode in {0, 1} for result in tracked_changes) and untracked.returncode == 0:
            metadata["dirty"] = any(result.returncode == 1 for result in tracked_changes) or bool(
                untracked.stdout.strip()
            )
    except (OSError, subprocess.SubprocessError):
        pass
    return metadata


def _cocoindex_branch(repo: str, root: pathlib.Path, branch: str | None) -> str:
    release = re.search(r"-v(\d+\.\d+)$", root.name)
    if release:
        return f"v{release.group(1)}"
    if branch and (match := re.match(r"^release/v(\d+\.\d+)$", branch)):
        return f"v{match.group(1)}"
    return "main"


def _zvec_shadow_arguments(
    tool: str,
    arguments: dict,
    *,
    repo: str,
    root: pathlib.Path,
    branch: str | None,
) -> tuple[str, dict] | None:
    shadow_tool = ZVEC_SHADOWED_TOOLS[tool]
    common = {"repo": repo, "branch": _cocoindex_branch(repo, root, branch)}
    if tool == "zvec_grep_search":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            for key in ("queries", "fts", "vector"):
                value = arguments.get(key)
                values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
                query = " ".join(item.strip() for item in values if isinstance(item, str) and item.strip())
                if query:
                    break
        if not isinstance(query, str) or not query.strip():
            return None
        limit = arguments.get("limit", 10)
        if isinstance(limit, bool) or not isinstance(limit, int):
            limit = 10
        return shadow_tool, {"query": query, "limit": min(max(limit, 1), 20), **common}

    if tool == "zvec_grep_callgraph_blast_radius":
        payload = {key: arguments[key] for key in ("function", "depth") if key in arguments}
    elif tool == "zvec_grep_callgraph_shortest_path":
        payload = {key: arguments[key] for key in ("source", "target") if key in arguments}
    else:
        payload = {"function": arguments.get("function")}
    return shadow_tool, {**payload, **common}


def _append_shadow_jsonl(path: pathlib.Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, default=str, separators=(",", ":")) + "\n")


def _tool_result_text(result: dict) -> str:
    return "\n".join(
        block.get("text", "")
        for block in result.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _normalize_comparison_path(path: str, repo: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    for prefix in (f"{repo}/", f"{repo}@release-"):
        if path.startswith(prefix):
            if prefix.endswith("release-"):
                _release, separator, relative = path.partition("/")
                return relative if separator else path
            return path[len(prefix):]
    return path


def _ranked_search_paths(result: dict, *, engine: str, repo: str) -> list[dict[str, Any]]:
    structured = result.get("structuredContent", result.get("structured_content"))
    if engine == "zvec-grep" and isinstance(structured, dict):
        items = structured.get("items")
        if isinstance(items, list):
            paths: list[dict[str, Any]] = []
            seen: set[str] = set()
            for position, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                raw_path = next(
                    (item[key] for key in ("path", "relative_path", "relativePath", "filepath")
                     if isinstance(item.get(key), str)),
                    None,
                )
                if raw_path is None:
                    continue
                path = _normalize_comparison_path(raw_path, repo)
                if path not in seen:
                    seen.add(path)
                    paths.append({"rank": item.get("rank", position), "path": path})
            if paths:
                return paths

    text = _tool_result_text(result)
    paths = []
    seen = set()
    if engine == "zvec-grep":
        pattern = re.compile(r"^#(?P<rank>\d+)(?:\s+\[[^\]]+\])?\s+matchedBy=[^\s]+\s+(?P<location>.+?):\d+(?:-\d+)?$")
        for line in text.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            path = _normalize_comparison_path(match.group("location"), repo)
            if path not in seen:
                seen.add(path)
                paths.append({"rank": int(match.group("rank")), "path": path})
    else:
        pattern = re.compile(r"^\[(?P<rank>\d+)\]\s+(?P<path>.+?)\s+\(score:")
        for line in text.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            path = _normalize_comparison_path(match.group("path"), repo)
            if path not in seen:
                seen.add(path)
                paths.append({"rank": int(match.group("rank")), "path": path})
    return paths


def _graph_callers(result: dict, *, engine: str, repo: str) -> list[list[str]]:
    if engine == "zvec-grep":
        structured = result.get("structuredContent", result.get("structured_content"))
        if isinstance(structured, dict) and isinstance(structured.get("callers_by_depth"), list):
            return [
                [_normalize_comparison_path(path, repo) for path in level if isinstance(path, str)]
                for level in structured["callers_by_depth"]
            ]
    text = _tool_result_text(result)
    depth_callers: dict[int, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*depth\s+(\d+):\s*(.*)$", line)
        if match:
            depth = int(match.group(1))
            callers = [value.strip() for value in match.group(2).split(",") if value.strip()]
            depth_callers[depth] = [_normalize_comparison_path(value, repo) for value in callers]
    if not depth_callers:
        return []
    return [depth_callers[key] for key in range(1, max(depth_callers) + 1)]


def _graph_path(result: dict, *, engine: str, repo: str) -> list[str] | None:
    if engine == "zvec-grep":
        structured = result.get("structuredContent", result.get("structured_content"))
        if isinstance(structured, dict):
            path = structured.get("path")
            if path is None:
                return None
            if isinstance(path, list):
                return [_normalize_comparison_path(item, repo) for item in path if isinstance(item, str)]
    text = _tool_result_text(result)
    if "No call path found" in text:
        return None
    for line in text.splitlines():
        value = line.strip()
        if line.startswith("  ") and " -> " in value:
            return [_normalize_comparison_path(item, repo) for item in value.split(" -> ")]
    return None


def _graph_cluster_members(result: dict, *, engine: str, repo: str) -> list[str]:
    if engine == "zvec-grep":
        structured = result.get("structuredContent", result.get("structured_content"))
        if isinstance(structured, dict) and isinstance(structured.get("members"), list):
            return [
                _normalize_comparison_path(member, repo)
                for member in structured["members"]
                if isinstance(member, str)
            ]
    members: list[str] = []
    for line in _tool_result_text(result).splitlines():
        if line.startswith("  ") and line.strip():
            members.append(_normalize_comparison_path(line.strip(), repo))
    return members


def _graph_resolution_counts(result: dict, *, engine: str) -> dict[str, int] | None:
    if engine == "zvec-grep":
        structured = result.get("structuredContent", result.get("structured_content"))
        if isinstance(structured, dict):
            values = ("unresolved_calls", "total_calls", "ambiguous_calls")
            if all(isinstance(structured.get(key), int) for key in values):
                return {key: structured[key] for key in values}
    match = re.search(
        r"(?P<unresolved>[\d,]+)/(?P<total>[\d,]+) calls.*?and "
        r"(?P<ambiguous>[\d,]+) matched 2\+ candidates",
        _tool_result_text(result),
        re.DOTALL,
    )
    if not match:
        return None
    return {
        "unresolved_calls": int(match.group("unresolved").replace(",", "")),
        "total_calls": int(match.group("total").replace(",", "")),
        "ambiguous_calls": int(match.group("ambiguous").replace(",", "")),
    }


def _compare_shadow_responses(tool: str, primary: dict, shadow: dict, repo: str) -> dict[str, Any]:
    comparison: dict[str, Any] = {
        "reference_engine": "cocoindex",
        "source_of_truth": "cocoindex",
        "candidate_engine": "zvec-grep",
        "evaluation_note": "CocoIndex is the comparison reference; rankings are not adjudicated relevance labels.",
    }
    if tool == "zvec_grep_search":
        reference = _ranked_search_paths(shadow, engine="cocoindex", repo=repo)
        candidate = _ranked_search_paths(primary, engine="zvec-grep", repo=repo)
        freshness = {}
        for line in _tool_result_text(primary).splitlines():
            key, separator, value = line.partition(":")
            if separator and key in {"freshness", "results", "background_refresh"}:
                freshness[key] = value.strip()
        reference_ranks = {item["path"]: item["rank"] for item in reference}
        candidate_ranks = {item["path"]: item["rank"] for item in candidate}
        comparison.update({
            "comparison_type": "unique_file_rank_overlap",
            "zvec_provenance": freshness,
            "cocoindex_ranked_files": reference,
            "zvec_ranked_files": candidate,
            "cocoindex_only_top_k": [item["path"] for item in reference if item["path"] not in candidate_ranks],
            "zvec_only_top_k": [item["path"] for item in candidate if item["path"] not in reference_ranks],
            "shared_file_rank_deltas": [
                {
                    "path": path,
                    "cocoindex_rank": reference_ranks[path],
                    "zvec_rank": candidate_ranks[path],
                    "zvec_minus_cocoindex": candidate_ranks[path] - reference_ranks[path],
                }
                for path in sorted(
                    reference_ranks.keys() & candidate_ranks.keys(),
                    key=lambda value: reference_ranks[value],
                )
            ],
        })
        comparison["file_order_matches_reference"] = [item["path"] for item in reference] == [
            item["path"] for item in candidate
        ]
    elif tool == "zvec_grep_callgraph_blast_radius":
        reference = _graph_callers(shadow, engine="cocoindex", repo=repo)
        candidate = _graph_callers(primary, engine="zvec-grep", repo=repo)
        comparison.update({
            "comparison_type": "callers_by_depth",
            "cocoindex_callers_by_depth": reference,
            "zvec_callers_by_depth": candidate,
            "cocoindex_only_by_depth": [
                [name for name in reference[level] if level >= len(candidate) or name not in candidate[level]]
                for level in range(len(reference))
            ],
            "zvec_only_by_depth": [
                [name for name in candidate[level] if level >= len(reference) or name not in reference[level]]
                for level in range(len(candidate))
            ],
        })
        comparison["callers_match_reference"] = reference == candidate
        reference_counts = _graph_resolution_counts(shadow, engine="cocoindex")
        candidate_counts = _graph_resolution_counts(primary, engine="zvec-grep")
        if reference_counts is not None:
            comparison["cocoindex_resolution_counts"] = reference_counts
        if candidate_counts is not None:
            comparison["zvec_resolution_counts"] = candidate_counts
        if reference_counts is not None and candidate_counts is not None:
            comparison["zvec_minus_cocoindex_resolution_counts"] = {
                key: candidate_counts[key] - reference_counts[key]
                for key in reference_counts
            }
    elif tool == "zvec_grep_callgraph_shortest_path":
        reference = _graph_path(shadow, engine="cocoindex", repo=repo)
        candidate = _graph_path(primary, engine="zvec-grep", repo=repo)
        comparison.update({
            "comparison_type": "shortest_call_path",
            "cocoindex_path": reference,
            "zvec_path": candidate,
            "path_matches_reference": reference == candidate,
        })
    elif tool == "zvec_grep_callgraph_cluster":
        reference = _graph_cluster_members(shadow, engine="cocoindex", repo=repo)
        candidate = _graph_cluster_members(primary, engine="zvec-grep", repo=repo)
        reference_set = set(reference)
        candidate_set = set(candidate)
        comparison.update({
            "comparison_type": "callgraph_cluster_members",
            "cocoindex_members": reference,
            "zvec_members": candidate,
            "cocoindex_only_members": sorted(reference_set - candidate_set),
            "zvec_only_members": sorted(candidate_set - reference_set),
            "members_match_reference": reference_set == candidate_set,
        })
    else:
        comparison["comparison_type"] = "raw_result_only"
        comparison["status"] = "no_ranked-comparison-parser"
    return comparison


class ZvecShadowRelayAdapter:
    """Forward zvec-grep tools unchanged and compare selected calls in the background.

    The primary backend owns the client-visible tool catalog and response. CocoIndex
    is only called for matching, indexed live roots; failures are recorded and never
    alter the zvec-grep result.
    """

    def __init__(
        self,
        primary: BackendAdapter,
        shadow: BackendAdapter,
        *,
        log_path: pathlib.Path,
        shadow_timeout_seconds: float,
        source_roots: dict[str, pathlib.Path] | None = None,
    ) -> None:
        self.primary = primary
        self.shadow = shadow
        self.log_path = log_path.expanduser()
        self.shadow_timeout_seconds = shadow_timeout_seconds
        self.source_roots = source_roots or _kubernaut_shadow_source_roots()
        self._shadow_tasks: set[asyncio.Task[None]] = set()
        self._shadow_semaphore = asyncio.Semaphore(1)
        self._log_lock = asyncio.Lock()

    async def list_tools(self) -> list[dict]:
        return await self.primary.list_tools()

    async def call_tool(self, name: str, arguments: dict) -> dict:
        started = time.perf_counter()
        primary = await self.primary.call_tool(name, arguments)
        primary_elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if (
            name in ZVEC_SHADOWED_TOOLS
            and not primary.get("isError", primary.get("is_error", False))
            and len(self._shadow_tasks) < MAX_PENDING_ZVEC_SHADOWS
        ):
            task = asyncio.create_task(
                self._run_shadow(name, dict(arguments), primary, primary_elapsed_ms)
            )
            self._shadow_tasks.add(task)
            task.add_done_callback(self._shadow_tasks.discard)
        elif name in ZVEC_SHADOWED_TOOLS and len(self._shadow_tasks) >= MAX_PENDING_ZVEC_SHADOWS:
            log.warning("dropping CocoIndex shadow for %s: %d comparisons already pending", name, len(self._shadow_tasks))
        return primary

    async def _run_shadow(
        self,
        name: str,
        arguments: dict,
        primary: dict,
        primary_elapsed_ms: float,
    ) -> None:
        started = time.perf_counter()
        raw_root = arguments.get("root")
        root: pathlib.Path | None = None
        if isinstance(raw_root, str) and raw_root.strip():
            try:
                root = pathlib.Path(raw_root).expanduser().resolve(strict=True)
            except OSError:
                root = None
        metadata = await asyncio.to_thread(_git_workspace_metadata, root) if root else {
            "workspace": raw_root, "branch": None, "commit": None, "dirty": None,
        }
        entry: dict[str, Any] = {
            "schema_version": "zvec-cocoindex-shadow.v1",
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": name,
            "arguments": arguments,
            "primary": {
                "engine": "zvec-grep",
                "elapsed_ms": primary_elapsed_ms,
                "response": primary,
            },
            "shadow": {"engine": "cocoindex"},
            "comparison_reference": "cocoindex",
            **metadata,
        }
        indexed_repo = next(
            (repo for repo, indexed_root in self.source_roots.items() if root == indexed_root),
            None,
        )
        if root is None:
            entry["shadow"].update({"skipped": "missing_or_unavailable_absolute_root"})
        elif indexed_repo is None:
            entry["shadow"].update({"skipped": "root_is_not_a_configured_cocoindex_source"})
        elif name.startswith("zvec_grep_callgraph_") and indexed_repo not in {
            "kubernaut", "kubernaut-operator"
        }:
            entry["shadow"].update({"skipped": "cocoindex_callgraph_only_covers_go_core_and_operator"})
        else:
            call = _zvec_shadow_arguments(
                name, arguments, repo=indexed_repo, root=root, branch=metadata.get("branch")
            )
            if call is None:
                entry["shadow"].update({"skipped": "no_text_query_to_compare"})
            else:
                shadow_tool, shadow_arguments = call
                entry["shadow"].update({"tool": shadow_tool, "arguments": shadow_arguments})
                try:
                    async with self._shadow_semaphore:
                        result = await asyncio.wait_for(
                            self.shadow.call_tool(shadow_tool, shadow_arguments),
                            timeout=self.shadow_timeout_seconds,
                        )
                    entry["shadow"].update({
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                        "response": result,
                        "is_error": bool(result.get("isError", result.get("is_error", False))),
                    })
                    if entry["shadow"]["is_error"]:
                        entry["comparison"] = {
                            "reference_engine": "cocoindex",
                            "status": "reference_tool_error",
                        }
                    else:
                        entry["comparison"] = _compare_shadow_responses(name, primary, result, indexed_repo)
                except Exception as exc:  # noqa: BLE001 - shadow errors must not affect primary
                    entry["shadow"].update({
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    entry["comparison"] = {
                        "reference_engine": "cocoindex",
                        "status": "reference_call_failed",
                    }
                    log.warning("CocoIndex shadow failed for %s: %s", name, exc)

        if "comparison" not in entry:
            entry["comparison"] = {
                "reference_engine": "cocoindex",
                "status": "skipped",
                "reason": entry["shadow"].get("skipped", "not_compared"),
            }

        try:
            async with self._log_lock:
                await asyncio.to_thread(_append_shadow_jsonl, self.log_path, entry)
        except Exception:  # noqa: BLE001 - logging must not affect primary
            log.warning("failed to write zvec/CocoIndex shadow log %s", self.log_path, exc_info=True)


MANUAL_MENTAL_MODEL_TOOL = {
    "name": "manual_mental_model",
    "description": (
        "Store caller-generated Markdown as a canonical replacement memory for a mental model. "
        "This does not refresh or overwrite Hindsight's pinned mental-model record; it replaces "
        "the previous manual document for the same model_id and avoids the LLM-backed refresh path."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["model_id", "content"],
        "properties": {
            "model_id": {
                "type": "string",
                "minLength": 1,
                "description": "Stable lowercase mental-model ID, for example 'engram-architecture'.",
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "Complete manually generated Markdown content.",
            },
            "name": {
                "type": "string",
                "description": "Optional human-readable mental-model name kept as provenance metadata.",
            },
            "source_query": {
                "type": "string",
                "description": "Optional query or scope used to manually produce the content.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional visibility tags for the canonical memory.",
            },
            "metadata": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Optional additional string metadata.",
            },
        },
    },
}


def _manual_tool_error(message: str) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": message})}],
        "isError": True,
    }


def _valid_manual_model_id(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.lower():
        return False
    parts = value.split("-")
    return all(part.isalnum() for part in parts)


class ManualMentalModelAdapter:
    """Expose manual mental-model replacement on top of Hindsight retain.

    Hindsight's pinned mental-model content is only writable through its
    refresh pipeline. This adapter deliberately stores the caller's finished
    Markdown as a stable document instead, using the existing retain tool and
    replacing the prior manual document for the same model ID.
    """

    def __init__(self, hindsight_adapter: BackendAdapter) -> None:
        self.hindsight_adapter = hindsight_adapter

    async def list_tools(self) -> list[dict]:
        return [MANUAL_MENTAL_MODEL_TOOL]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if name != "manual_mental_model":
            return _manual_tool_error(f"Unknown tool: {name!r}")

        model_id = arguments.get("model_id")
        content = arguments.get("content")
        if not _valid_manual_model_id(model_id):
            return _manual_tool_error("model_id must be lowercase alphanumeric words separated by single hyphens")
        if not isinstance(content, str) or not content.strip():
            return _manual_tool_error("content must be a non-empty Markdown string")

        tags = arguments.get("tags")
        if tags is not None and (not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)):
            return _manual_tool_error("tags must be an array of strings")

        metadata = arguments.get("metadata")
        if metadata is not None and (
            not isinstance(metadata, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items())
        ):
            return _manual_tool_error("metadata must be an object of string values")

        name = arguments.get("name")
        source_query = arguments.get("source_query")
        if name is not None and not isinstance(name, str):
            return _manual_tool_error("name must be a string")
        if source_query is not None and not isinstance(source_query, str):
            return _manual_tool_error("source_query must be a string")

        manual_metadata = dict(metadata or {})
        manual_metadata.update({
            "source": "manual_mental_model",
            "mental_model_id": model_id,
        })
        if name:
            manual_metadata["name"] = name
        if source_query:
            manual_metadata["source_query"] = source_query

        retain_arguments = {
            "content": content,
            "context": "mental-models",
            "document_id": f"manual-mental-model-{model_id}",
            "metadata": manual_metadata,
            "update_mode": "replace",
        }
        if tags is not None:
            retain_arguments["tags"] = tags
        return await self.hindsight_adapter.call_tool("retain", retain_arguments)


class StdioSubprocessAdapter:
    """Backend adapter that spawns a stdio MCP server (cocoindex-code,
    serena) ONCE and keeps it alive as this gateway's own long-lived child
    process, instead of Cursor spawning a fresh one per window. Calls are
    serialized behind one lock per adapter (same single-writer precedent as
    serena_multiplex.py's ActiveProjectTracker) since a stdio MCP session is
    not safe for concurrent use."""

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
        self.command = command
        self.args = args
        self.env = env
        self._lock = asyncio.Lock()
        self._session = None
        self._exit_stack = None

    async def _ensure_started(self) -> None:
        if self._session is not None:
            return

        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._exit_stack = AsyncExitStack()
        params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session

    async def _restart(self) -> None:
        """Drop the dead session/exit-stack and spawn a fresh one.

        Deliberately does NOT call `self._exit_stack.aclose()` here: each
        Starlette/uvicorn request is its own asyncio task, so the exit stack
        (built by whichever earlier request's task first called
        `_ensure_started`) has anyio cancel scopes bound to *that* task --
        closing it from a *different* task (this one, reached because the
        subprocess died and a later request's call_tool is retrying) raises
        "Attempted to exit cancel scope in a different task than it was
        entered in" (verified live during the spike's degradation test).
        Since we only ever reach this path because the underlying process is
        already dead/broken, there is nothing left to cleanly close anyway
        -- just abandon the old stack and let a fresh `_ensure_started()`
        build a new one from *this* task, which is then self-consistent for
        whichever task closes it next."""
        self._session = None
        self._exit_stack = None
        await self._ensure_started()

    async def list_tools(self) -> list[dict]:
        async with self._lock:
            await self._ensure_started()
            try:
                result = await self._session.list_tools()
            except Exception:
                # Same one-retry-only self-heal as call_tool() (see
                # _restart()'s docstring). Without this, a subprocess that
                # dies *after* a previously-successful start leaves
                # `self._session` set to a dead object forever --
                # `_ensure_started()` short-circuits on a non-None session,
                # so every future list_tools() call hits the same broken
                # pipe and fails identically, with no self-heal (see
                # docs/findings/2026-08.md, 2026-08-27 entry: dcm's
                # osac-service-provider Serena process died hours into a
                # session and silently dropped all 14 serena tools from
                # every subsequent tools/list, indefinitely, until this fix).
                await self._restart()
                result = await self._session.list_tools()
            return [
                # Tool input schema attribute renamed across mcp SDK
                # versions (inputSchema 1.x <-> input_schema 2.x); read it
                # version-tolerantly (see engram.mcp_compat) instead of
                # pinning one spelling that breaks on every mcp bump --
                # see the 2026-08-27 incident this comment originally
                # described, repeated in reverse by the 2026-09-09
                # mcp<2.0 pin.
                {"name": t.name, "description": t.description or "",
                 "inputSchema": mcp_compat.tool_input_schema(t)}
                for t in result.tools
            ]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        async with self._lock:
            await self._ensure_started()
            try:
                result = await self._session.call_tool(name, arguments)
            except Exception:
                # The subprocess may have died between calls -- restart once
                # and retry, same one-retry-only philosophy as
                # serena_multiplex._forward_scoped.
                await self._restart()
                result = await self._session.call_tool(name, arguments)
            return {
                # exclude_none=True matters: mcp SDK content models (TextContent
                # etc.) have optional fields like `annotations`/`meta` that
                # default to None, and a bare model_dump() serializes those as
                # explicit `null` rather than omitting the key. Several MCP
                # clients' schemas require `annotations` to be either a real
                # object or absent -- `null` fails validation and the client
                # discards the whole result before the caller ever sees the
                # data, surfacing as a generic "backend is currently down"
                # (see docs/findings/2026-08.md, 2026-08-25 entry).
                "content": [c.model_dump(exclude_none=True) if hasattr(c, "model_dump") else c for c in result.content],
                # mcp==2.0.0 renamed CallToolResult.isError -> is_error (same
                # 2026-08-22 dependabot bump that broke input_schema and
                # FastMCP -- see docs/findings/2026-08.md's 2026-08-27 entry).
                # Live venv pins mcp<2.0 so only isError exists; read it via
                # mcp_compat like inputSchema above (code backend was failing
                # every tools/call with "no attribute 'is_error'").
                "isError": mcp_compat.call_tool_is_error(result),
            }


_prewarm_tasks: set[asyncio.Task] = set()


async def _prewarm_stdio_backends(projects: dict[str, dict[str, BackendAdapter]]) -> None:
    """Fire off a background `list_tools()` for every stdio backend (Serena,
    cocoindex-code) right at gateway startup, without blocking the gateway
    from accepting other traffic while these are still warming up.

    Why this exists: most MCP clients (Cursor included) call `tools/list`
    exactly once per session, right after `initialize`, and cache the
    result for the life of that connection -- there's no server-initiated
    `notifications/tools/list_changed` push from this gateway to make a
    client re-fetch later. So if that one `tools/list` call happens to race
    a cold LSP/language-server startup (which can take several seconds) and
    gets cancelled or returns a reduced tool set, the client is stuck with
    that degraded snapshot until its *next* fresh reconnect -- no amount of
    "disable/enable" or "restart Cursor" helps if the reconnect's own
    `tools/list` call lands in the same race window again. Pre-warming here
    means that by the time any real client connects (almost always more
    than a few seconds after this process starts), the subprocess is
    already up and `list_tools()` returns immediately. See
    docs/findings/2026-08.md, 2026-08-25 entry (`praxis-grid` stuck on a
    16-tool snapshot from exactly this race, right after a gateway restart)."""

    async def _warm_one(label: str, adapter: BackendAdapter) -> None:
        try:
            await adapter.list_tools()
            log.info("pre-warmed backend %r", label)
        except Exception:
            log.warning(
                "pre-warm failed for backend %r -- will retry lazily on first real request", label, exc_info=True
            )

    for project, backends in projects.items():
        for key, adapter in backends.items():
            if isinstance(adapter, StdioSubprocessAdapter):
                task = asyncio.create_task(_warm_one(f"{project}/{key}", adapter))
                _prewarm_tasks.add(task)
                task.add_done_callback(_prewarm_tasks.discard)


async def _prewarm_project_catalogs(projects: dict[str, dict[str, BackendAdapter]], state: dict[str, dict]) -> None:
    """Eagerly run `aggregate_tools_list()` for every project at gateway
    startup and populate `state[project]["catalog"]` -- without this,
    `state[...]["catalog"]` starts as `{}` and stays empty for a given
    project until *some* client happens to send that project's `tools/list`
    in this process's lifetime (see `_make_endpoint`'s `tools/list` branch,
    the only other place that assigns it).

    2026-08-27 incident: kubernaut's client reported `initialize` succeeding
    ("namespace ready") but every `tools/call` failing with "Unknown tool
    ... backend is currently down" -- `route_call()` returns None on an
    empty catalog, which produces exactly that message (see its call site
    in `handle_tools_call`). Root cause: the gateway process had been
    restarted (to pick up same-day fixes) while kubernaut's Cursor session
    was already connected; per `_prewarm_stdio_backends`'s docstring,
    clients cache `tools/list` per-session and don't re-fetch on their own,
    so their session kept calling tools by name against the new process's
    still-empty `state["kubernaut"]["catalog"]`. `_prewarm_stdio_backends`
    above only warms each stdio subprocess's *connection* so its first real
    `list_tools()` is fast -- it never actually calls `aggregate_tools_list()`
    or touches `state`, so it didn't close this gap. This does, for every
    project (stdio- and HTTP-backed alike), independently of whether any
    client ever reconnects -- a client whose session predates the restart
    still needs to reconnect for its *own* cached tool names to refresh,
    but every *other* client (or that same one after reconnecting) now
    finds a warm, populated catalog immediately instead of racing the first
    real `tools/list`.

    Awaits all projects concurrently via `asyncio.gather` rather than firing
    independent per-project tasks like `_prewarm_stdio_backends` does above:
    the non-blocking-at-startup property is already provided by the caller
    wrapping this whole coroutine in its own `asyncio.create_task` (see
    `_lifespan`), so there's no need to duplicate that here -- and a plain
    `await` (vs. bare `create_task` calls with no corresponding await) is
    what actually guarantees this work runs to completion rather than
    racing `asyncio.run()`'s own shutdown/cancellation in a bare script or
    test."""

    async def _warm_one_catalog(project: str, backends: dict[str, BackendAdapter]) -> None:
        try:
            _tool_defs, catalog, errors = await aggregate_tools_list(backends)
            state[project]["catalog"] = catalog
            if errors:
                log.warning("project %r: backends unavailable during catalog pre-warm: %s", project, errors)
            log.info("pre-warmed catalog for project %r (%d tools)", project, len(catalog))
        except Exception:
            log.warning(
                "catalog pre-warm failed for project %r -- will populate lazily on first real tools/list",
                project,
                exc_info=True,
            )

    await asyncio.gather(*(_warm_one_catalog(project, backends) for project, backends in projects.items()))


def build_app(projects: dict[str, dict[str, BackendAdapter]]):
    """Build the Starlette app: one route per project, each aggregating
    that project's own set of backend adapters. `projects` maps project
    name -> {backend_key: BackendAdapter}."""
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    # Cache of (catalog, backends) per project. Populated two ways: eagerly
    # at startup by _prewarm_project_catalogs() (see its docstring for why
    # that's necessary, not just nice-to-have), and refreshed on every real
    # tools/list a client sends -- backends that flap back up are picked up
    # on the next list without a restart either way.
    state: dict[str, dict] = {name: {"catalog": {}, "backends": backends} for name, backends in projects.items()}

    @asynccontextmanager
    async def _lifespan(_app):
        # Scheduled, not awaited-to-completion: returns almost immediately
        # so the gateway can start accepting requests for other projects
        # while these stdio backends warm up in the background.
        asyncio.create_task(_prewarm_stdio_backends(projects))
        # Separate from the above: populates state[project]["catalog"] for
        # every project so tools/call works immediately after a restart,
        # even for a client whose session predates it and never re-sends
        # tools/list on its own (see _prewarm_project_catalogs's docstring).
        asyncio.create_task(_prewarm_project_catalogs(projects, state))
        yield

    def _make_endpoint(project: str):
        async def endpoint(request: Request) -> Response:
            if request.method == "GET":
                return Response(status_code=405)

            body = await request.body()
            try:
                message = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return Response(status_code=400)

            backends = state[project]["backends"]
            method = message.get("method")

            if method == "tools/list":
                tool_defs, catalog, errors = await aggregate_tools_list(backends)
                state[project]["catalog"] = catalog
                if errors:
                    log.warning("project %r: backends unavailable this round: %s", project, errors)
                result = {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": tool_defs}}
                return Response(
                    content=f"event: message\ndata: {json.dumps(result)}\n\n",
                    media_type="text/event-stream",
                )

            if method == "tools/call":
                response = await handle_tools_call(message, state[project]["catalog"], backends, project=project)
                return Response(
                    content=f"event: message\ndata: {json.dumps(response)}\n\n",
                    media_type="text/event-stream",
                )

            if message.get("id") is not None:
                # initialize and any other request we don't special-case:
                # acknowledge minimally rather than hang the caller.
                result = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "engram-gateway", "version": "0.0.1"},
                    },
                }
                return Response(
                    content=f"event: message\ndata: {json.dumps(result)}\n\n",
                    media_type="text/event-stream",
                )

            # A notification (no id, e.g. notifications/initialized) -- MCP
            # requires no response body.
            return Response(status_code=202)

        return endpoint

    routes = [Route(f"/mcp/{project}", _make_endpoint(project), methods=["GET", "POST", "DELETE"]) for project in projects]
    return Starlette(routes=routes, lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Legacy native registry (2026-08-21): every onboarded repo, config only (no
# I/O, no adapter instantiation -- see `build_backend_adapters` for that).
# The portable runtime path uses `load_instance_registry()` instead and does
# not depend on this installation-specific project list.
# Deliberately preserves each repo's *existing* backend set exactly rather
# than normalizing towards a uniform 4-backend shape: several repos are
# missing one or more backends today (kubernaut-console has no serena,
# kubernaut-docs has no cocoindex-code, engram itself has neither issues nor
# serena) and this registry must keep reflecting that, not silently add
# capabilities a repo never had. See docs/findings/2026-08.md's 2026-08-21
# rollout entry for the full survey this was built from.
#
# Explicitly excluded (see that entry for why): the two kubernaut-fix-1995-*
# scratch worktrees (serena still points directly at the raw upstream daemon
# on :8892 rather than through the :8893 multiplex, a stale/pre-multiplex
# config on what look like abandoned one-off branch-fix clones -- not guessed
# at here).
# ---------------------------------------------------------------------------

_HINDSIGHT_BASE = "http://localhost:8888"
_PG_URL = "postgresql://hindsight:hindsight@localhost:5432/hindsight"

DCM_REPOS = [
    "udlm",
    "k8s-container-service-provider",
    "three-tier-app-demo-service-provider",
    "quadlet-deploy",
    "acm-cluster-service-provider",
    "shared-workflows",
    "kubevirt-service-provider",
    "cli",
    "utilities",
    "osac-service-provider",
    "enhancements",
    "dcm-project.github.io",
]
PRAXIS_REPOS_WITH_SERENA = [
    "praxis",
    "praxis-ai",
    "praxis-benchmarks",
    "praxis-demos",
    "praxis-experiments",
    "praxis-forge",
    "praxis-grid",
    "praxis-operator",
    "praxis-policy",
]
PRAXIS_REPOS_WITHOUT_SERENA = ["praxis-conventions", "praxis-enhancements", "praxis-proxy.github.io"]


def _hindsight(bank: str) -> dict:
    return {"kind": "http", "url": f"{_HINDSIGHT_BASE}/mcp/{bank}/"}


def _http(
    url: str,
    headers: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    spec = {"kind": "http", "url": url}
    if headers:
        spec["headers"] = headers
    if timeout_seconds is not None:
        spec["timeout_seconds"] = timeout_seconds
    return spec


def _shadow_http(
    primary_url: str,
    shadow_url: str,
    *,
    log_path: str,
    timeout_seconds: float = 300.0,
    shadow_timeout_seconds: float = 180.0,
    shared_key: str | None = None,
) -> dict:
    spec = {
        "kind": "shadow_http",
        "url": primary_url,
        "shadow_url": shadow_url,
        "timeout_seconds": timeout_seconds,
        "shadow_timeout_seconds": shadow_timeout_seconds,
        "shadow_log": log_path,
    }
    if shared_key is not None:
        spec["shared_key"] = shared_key
    return spec


def _stdio(command: str, args: list[str] | None = None, env: dict | None = None, shared_key: str | None = None) -> dict:
    spec = {"kind": "stdio", "command": command, "args": args or [], "env": env}
    if shared_key is not None:
        spec["shared_key"] = shared_key
    return spec


def _serena_stdio(home: str, workspace: str) -> dict:
    return _stdio(
        f"{home}/.local/bin/uvx",
        [
            "--from",
            "git+https://github.com/oraios/serena@1bbe53546124c00e4238972f1599a91fbf8c6539",
            "serena",
            "start-mcp-server",
            "--project",
            workspace,
            "--context",
            "ide",
            "--add-mode",
            "no-memories",
            "--open-web-dashboard",
            "false",
        ],
    )


def build_project_registry(home: str) -> dict[str, dict[str, dict]]:
    """project_name -> {backend_key: backend_spec}, pure config derived from
    the 2026-08-21 survey of every onboarded repo's actual .cursor/mcp.json.
    `home` is injected (rather than read from `os.path.expanduser` here) so
    this stays a pure, easily-testable function."""
    venv_bin = f"{home}/.engram/venv/bin"
    registry: dict[str, dict[str, dict]] = {}

    # Kubernaut's cold Go call-graph build takes around 70s on the live corpus;
    # keep it within the single request's forwarding budget instead of
    # timing out just before the fingerprinted cache is populated.
    kubernaut_http_code = _http("http://127.0.0.1:8891/mcp", timeout_seconds=180)
    kubernaut_zvec_code = _shadow_http(
        os.environ.get("ZVEC_GREP_MCP_URL", "http://127.0.0.1:7999/mcp"),
        os.environ.get("COCOINDEX_MCP_URL", "http://127.0.0.1:8891/mcp"),
        log_path=f"{home}/.engram/logs/zvec-cocoindex-shadow.jsonl",
        shared_key="kubernaut-zvec-shadow",
    )
    kubernaut_rca = _http("http://127.0.0.1:8897/mcp")

    def kubernaut_serena(project: str) -> dict:
        return _http(f"http://127.0.0.1:8893/mcp/{project}")

    # Kubernaut's live-worktree code route is zvec-grep primary. CocoIndex runs
    # only as an asynchronous shadow comparator; the other family workspaces
    # continue using the shared CocoIndex route until separately migrated.
    registry["kubernaut"] = {
        "docs": _hindsight("kubernaut-docs"),
        "issues": _hindsight("kubernaut-issues"),
        "code": kubernaut_zvec_code,
        "rca": kubernaut_rca,
        "serena": kubernaut_serena("kubernaut"),
    }
    for name in ("kubernaut-operator", "kubernaut-v1.5"):
        registry[name] = {
            "docs": _hindsight("kubernaut-docs"),
            "issues": _hindsight("kubernaut-issues"),
            "code": kubernaut_http_code,
            "rca": kubernaut_rca,
            "serena": kubernaut_serena(name),
        }
    registry["kubernaut-demo-scenarios"] = {
        "docs": _hindsight("kubernaut-docs"),
        "issues": _hindsight("kubernaut-issues"),
        "code": kubernaut_http_code,
        "serena": kubernaut_serena("kubernaut-demo-scenarios"),
    }
    registry["kubernaut-console"] = {
        "docs": _hindsight("kubernaut-docs"),
        "issues": _hindsight("kubernaut-issues"),
        # Shared with kubernaut/kubernaut-operator, NOT a standalone stdio
        # process (fixed 2026-08-25): this used to spawn
        # `~/.engram/cocoindex-search.py`, a flat symlink the 2026-08-12
        # src/engram/ package restructuring had already deleted 9 days
        # before this registry entry was even authored, so it was dead on
        # arrival -- kubernaut-console's `code` tools silently dropped from
        # its aggregated catalog every time (see engram_gateway.py's
        # per-backend degradation). Even had that path still existed, it
        # was a bare single-repo invocation (no --repo scoping args), so it
        # could only ever have searched kubernaut-console's own code, never
        # kubernaut/kubernaut-operator upstream. `kubernaut_http_code`
        # (engram-search-kubernaut / src/engram/search/kubernaut.py) already
        # indexes all three repos into one cocoindex.code_embeddings table
        # and defaults `cocoindex_search`/`cocoindex_pattern_search` to
        # whole-platform results -- wiring kubernaut-console to it too is
        # what actually restores operator + kubernaut-upstream code search.
        "code": kubernaut_http_code,
        # Added 2026-08-25: kubernaut-console was the only kubernaut-family
        # repo with no serena entry at all (see this function's module-level
        # survey comment), despite `KUBERNAUT_FAMILY_PROJECTS` in
        # serena_multiplex.py already listing "kubernaut-console" as a
        # registered project on the shared daemon. Wiring it up like every
        # other family member gives it both symbol-level tools scoped to its
        # own repo AND read-only cross-repo lookups into kubernaut/
        # kubernaut-operator via query_project/list_queryable_projects
        # (project-agnostic tools, forwarded untouched -- see
        # serena_multiplex.py's PROJECT_AGNOSTIC_TOOLS), with no extra
        # registry work needed for the "all go repos" half of the ask.
        "serena": kubernaut_serena("kubernaut-console"),
    }
    registry["kubernaut-docs"] = {
        "docs": _hindsight("kubernaut-docs"),
        "issues": _hindsight("kubernaut-issues"),
        "serena": kubernaut_serena("kubernaut-docs"),
    }

    koku_code_stdio = _stdio(f"{venv_bin}/engram-search-koku", env={"COCOINDEX_PG_URL": _PG_URL}, shared_key="koku-code")
    for name in ("koku", "koku-service-operator"):
        registry[name] = {
            "docs": _hindsight("koku-docs"),
            "issues": _hindsight("koku-issues"),
            "code": koku_code_stdio,
            "serena": _http(f"http://127.0.0.1:8895/mcp/{name}"),
        }
    registry["koku-insights-onprem"] = {
        "docs": _hindsight("koku-docs"),
        "issues": _hindsight("koku-issues"),
        "code": koku_code_stdio,
        "serena": _serena_stdio(home, f"{home}/go/src/github.com/insights-onprem/koku"),
    }

    dcm_code_stdio = _stdio(
        f"{venv_bin}/engram-search-dcm", env={"COCOINDEX_PG_URL": _PG_URL, "HF_HUB_OFFLINE": "1"}, shared_key="dcm-code"
    )
    # The architecture and control-plane checkouts are the two DCM review
    # workspaces whose old MCP links predate the unified gateway. Keep their
    # exact directory names as routes so the plugin can derive them without a
    # special alias; the remaining DCM routes retain their established
    # dcm-<repo> names.
    for repo in (*DCM_REPOS, "dcm", "control-plane"):
        route = repo if repo in {"dcm", "control-plane"} else f"dcm-{repo}"
        registry[route] = {
            "docs": _hindsight("dcm-docs"),
            "issues": _hindsight("dcm-issues"),
            "code": dcm_code_stdio,
            "serena": _serena_stdio(home, f"{home}/go/src/github.com/dcm-project/{repo}"),
        }

    praxis_code_stdio = _stdio(f"{venv_bin}/engram-search-praxis", env={"COCOINDEX_PG_URL": _PG_URL}, shared_key="praxis-code")
    for repo in PRAXIS_REPOS_WITH_SERENA:
        registry[repo] = {
            "docs": _hindsight("praxis-docs"),
            "issues": _hindsight("praxis-issues"),
            "code": praxis_code_stdio,
            "serena": _serena_stdio(home, f"{home}/go/src/github.com/praxis-proxy/{repo}"),
        }
    for repo in PRAXIS_REPOS_WITHOUT_SERENA:
        registry[repo.replace(".", "-")] = {
            "docs": _hindsight("praxis-docs"),
            "issues": _hindsight("praxis-issues"),
            "code": praxis_code_stdio,
        }

    # rhdh-plugins: DISABLED -- no longer contributing to this project
    # (2026-09-09). Registry entry removed so the gateway no longer spawns
    # engram-search-rhdh-plugins / serena subprocesses for it, and the
    # launchd job io.vectorize.cocoindex.rhdh-plugins has been booted out
    # with its installed plist removed. Source modules
    # (flows/search rhdh_plugins) and launchd/io.vectorize.cocoindex.rhdh-plugins.plist
    # remain in the repo for reference only.

    registry["engram"] = {
        "docs": _hindsight("engram-docs"),
        "code": _stdio(f"{venv_bin}/engram-search-engram", env={"COCOINDEX_PG_URL": _PG_URL}),
    }

    # kuadrant: ingestion-only prior-art reference for praxis-proxy (2026-08-27
    # onboarding, extended to 9 repos 2026-08-28 with mcp-gateway) -- no repo
    # is ever opened as its own Cursor workspace (hence no serena, and this
    # is the only registry entry not named after a local checkout dir), so
    # this single entry aggregates all 9 repos' docs/issues/code and is
    # meant to be cross-mounted as a *second* MCP server into
    # every praxis-* repo's .cursor/mcp.json alongside its own "engram" entry.
    # Uses "kuadrant_docs"/"kuadrant_issues"/"kuadrant_code" backend keys
    # (not the usual "docs"/"issues"/"code") so RELEVANT_TOOLS_BY_BACKEND can
    # apply the narrower recall-only/search-only filter without affecting any
    # other project's own docs/issues/code backends.
    registry["kuadrant"] = {
        "kuadrant_docs": _hindsight("kuadrant-docs"),
        "kuadrant_issues": _hindsight("kuadrant-issues"),
        "kuadrant_code": _stdio(f"{venv_bin}/engram-search-kuadrant", env={"COCOINDEX_PG_URL": _PG_URL}),
    }

    return registry


def _parse_runtime_backend(instance: str, backend: str, settings: Any) -> dict:
    """Validate and normalize one backend from the container registry."""
    if not isinstance(settings, dict):
        raise ValueError(f"Backend {instance!r}/{backend!r} must be a TOML table")

    kind = settings.get("kind")
    if kind not in {"http", "shadow_http", "stdio"}:
        raise ValueError(f"Backend {instance!r}/{backend!r} kind must be 'http', 'shadow_http', or 'stdio'")

    if kind in {"http", "shadow_http"}:
        endpoint = settings.get("url")
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError(f"Backend {instance!r}/{backend!r} requires a non-empty url")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Backend {instance!r}/{backend!r} url must be an HTTP(S) URL: {endpoint!r}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"Backend {instance!r}/{backend!r} url must not contain credentials")

        headers = settings.get("headers")
        if headers is not None:
            if not isinstance(headers, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in headers.items()
            ):
                raise ValueError(f"Backend {instance!r}/{backend!r} headers must be a string-to-string table")

        timeout_seconds = settings.get("timeout_seconds")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= MAX_FORWARD_TIMEOUT_S
        ):
            raise ValueError(
                f"Backend {instance!r}/{backend!r} timeout_seconds must be between 1 and "
                f"{MAX_FORWARD_TIMEOUT_S:g}"
            )

        spec = {"kind": "http", "url": endpoint}
        if headers:
            spec["headers"] = dict(headers)
        if timeout_seconds is not None:
            spec["timeout_seconds"] = float(timeout_seconds)
        if kind == "shadow_http":
            shadow_endpoint = settings.get("shadow_url")
            if not isinstance(shadow_endpoint, str) or not shadow_endpoint:
                raise ValueError(f"Backend {instance!r}/{backend!r} requires a non-empty shadow_url")
            shadow_parsed = urlparse(shadow_endpoint)
            if shadow_parsed.scheme not in {"http", "https"} or not shadow_parsed.netloc:
                raise ValueError(
                    f"Backend {instance!r}/{backend!r} shadow_url must be an HTTP(S) URL: {shadow_endpoint!r}"
                )
            if shadow_parsed.username is not None or shadow_parsed.password is not None:
                raise ValueError(f"Backend {instance!r}/{backend!r} shadow_url must not contain credentials")
            shadow_timeout = settings.get("shadow_timeout_seconds", 60.0)
            if (
                isinstance(shadow_timeout, bool)
                or not isinstance(shadow_timeout, (int, float))
                or not math.isfinite(shadow_timeout)
                or not 1 <= shadow_timeout <= MAX_FORWARD_TIMEOUT_S
            ):
                raise ValueError(
                    f"Backend {instance!r}/{backend!r} shadow_timeout_seconds must be between 1 and "
                    f"{MAX_FORWARD_TIMEOUT_S:g}"
                )
            shadow_log = settings.get("shadow_log")
            if not isinstance(shadow_log, str) or not shadow_log.strip():
                raise ValueError(f"Backend {instance!r}/{backend!r} requires a non-empty shadow_log")
            shared_key = settings.get("shared_key")
            if shared_key is not None and (not isinstance(shared_key, str) or not shared_key):
                raise ValueError(f"Backend {instance!r}/{backend!r} shared_key must be a non-empty string")
            spec.update({
                "kind": "shadow_http",
                "shadow_url": shadow_endpoint,
                "shadow_timeout_seconds": float(shadow_timeout),
                "shadow_log": shadow_log,
            })
            if shared_key is not None:
                spec["shared_key"] = shared_key
        return spec

    command = settings.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError(f"Backend {instance!r}/{backend!r} requires a non-empty command")

    args = settings.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError(f"Backend {instance!r}/{backend!r} args must be a string array")

    env = settings.get("env")
    if env is not None:
        if not isinstance(env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in env.items()
        ):
            raise ValueError(f"Backend {instance!r}/{backend!r} env must be a string-to-string table")

    shared_key = settings.get("shared_key")
    if shared_key is not None and (not isinstance(shared_key, str) or not shared_key):
        raise ValueError(f"Backend {instance!r}/{backend!r} shared_key must be a non-empty string")

    spec = {"kind": "stdio", "command": command, "args": list(args), "env": dict(env) if env else None}
    if shared_key is not None:
        spec["shared_key"] = shared_key
    return spec


def load_instance_registry(path: str | pathlib.Path) -> dict[str, dict[str, dict]]:
    """Load the container runtime registry from a TOML file.

    The original ``endpoint`` form remains valid as a compatibility shim for
    a host adapter. New configs use ``backends`` to aggregate multiple HTTP or
    stdio MCP servers directly inside this gateway process.

    Example::

        [instances.kubernaut.backends.docs]
        kind = "http"
        url = "http://host.containers.internal:8888/mcp/kubernaut-docs/"

        [instances.kubernaut.backends.serena]
        kind = "http"
        url = "http://host.containers.internal:8893/mcp/kubernaut"
    """
    import tomllib

    config_path = pathlib.Path(path)
    try:
        with config_path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except OSError as exc:
        raise ValueError(f"Cannot read instance config {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid instance config {config_path}: {exc}") from exc

    instances = document.get("instances")
    if not isinstance(instances, dict) or not instances:
        raise ValueError("Instance config must contain a non-empty [instances] table")

    registry: dict[str, dict[str, dict]] = {}
    for name, settings in instances.items():
        if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
            raise ValueError(f"Invalid instance route: {name!r}")
        if not isinstance(settings, dict):
            raise ValueError(f"Instance {name!r} must be a TOML table")

        endpoint = settings.get("endpoint")
        backend_settings = settings.get("backends")
        if endpoint is not None and backend_settings is not None:
            raise ValueError(f"Instance {name!r} cannot define both endpoint and backends")

        if endpoint is not None:
            host_settings = {"kind": "http", "url": endpoint}
            if "timeout_seconds" in settings:
                host_settings["timeout_seconds"] = settings["timeout_seconds"]
            registry[name] = {"host": _parse_runtime_backend(name, "host", host_settings)}
            continue

        if not isinstance(backend_settings, dict) or not backend_settings:
            raise ValueError(f"Instance {name!r} requires a non-empty backends table")

        registry[name] = {}
        for backend, backend_config in backend_settings.items():
            if not isinstance(backend, str) or not backend or "/" in backend:
                raise ValueError(f"Invalid backend key for instance {name!r}: {backend!r}")
            registry[name][backend] = _parse_runtime_backend(name, backend, backend_config)

    return registry


def _bank_name_from_spec(spec: dict, suffix: str) -> str:
    """Extract a Hindsight bank name from one of the registry's HTTP specs."""
    url = spec.get("url", "")
    marker = "/mcp/"
    if marker not in url:
        raise ValueError(f"Hindsight backend URL has no MCP bank path: {url!r}")
    bank = url.split(marker, 1)[1].rstrip("/")
    if not bank.endswith(suffix):
        raise ValueError(f"Expected Hindsight bank suffix {suffix!r}, got {bank!r}")
    return bank


def build_gateway_identity_registry(
    registry: dict[str, dict[str, dict]],
) -> dict[str, dict[str, str | None]]:
    """Build the legacy Hindsight project/family identity view.

    The gateway route is the project key. Backend specs remain authoritative for
    actual routing, while this derived view validates the historical native
    registry's Hindsight bank relationship. Config-driven runtime registries
    are intentionally not required to use Hindsight or these bank suffixes.
    """
    identities: dict[str, dict[str, str | None]] = {}
    for project, backends in registry.items():
        if not project or "/" in project:
            raise ValueError(f"Invalid gateway project route: {project!r}")
        docs_key = "docs" if "docs" in backends else "kuadrant_docs"
        docs_spec = backends.get(docs_key)
        if not docs_spec or docs_spec.get("kind") != "http":
            raise ValueError(f"Gateway project {project!r} has no HTTP docs backend")

        docs_bank = _bank_name_from_spec(docs_spec, "-docs")
        issues_key = "issues" if "issues" in backends else "kuadrant_issues"
        issues_spec = backends.get(issues_key)
        issues_bank = (
            _bank_name_from_spec(issues_spec, "-issues")
            if issues_spec
            else None
        )
        identities[project] = {
            "project": project,
            "family": docs_bank.removesuffix("-docs"),
            "docs_bank": docs_bank,
            "issues_bank": issues_bank,
        }
    return identities


def validate_gateway_identity_registry(
    registry: dict[str, dict[str, dict]],
) -> None:
    """Fail startup early when a gateway route has an invalid identity binding."""
    identities = build_gateway_identity_registry(registry)
    if set(identities) != set(registry):
        raise ValueError("Gateway identity registry does not cover every project route")


def build_backend_adapters(registry: dict[str, dict[str, dict]]) -> dict[str, dict[str, BackendAdapter]]:
    """Instantiate real adapters from registry specs (still no I/O -- both
    adapter types connect lazily on first use). Stdio specs carrying the
    same `shared_key` (the family-wide cocoindex-code search binaries,
    which are stateless w.r.t. which repo mount is asking) collapse to one
    shared `StdioSubprocessAdapter` instance instead of one per project --
    unlike serena, which is bound to one repo's filesystem via `--project`
    and can never be shared."""
    shared_stdio_cache: dict[str, StdioSubprocessAdapter] = {}
    shared_shadow_cache: dict[str, ZvecShadowRelayAdapter] = {}
    adapters: dict[str, dict[str, BackendAdapter]] = {}

    for project, specs in registry.items():
        adapters[project] = {}
        for backend_key, spec in specs.items():
            if spec["kind"] == "shadow_http":
                shared_key = spec.get("shared_key")
                if shared_key is not None and shared_key in shared_shadow_cache:
                    adapters[project][backend_key] = shared_shadow_cache[shared_key]
                    continue
                primary = HttpRelayAdapter(
                    spec["url"], spec.get("headers"), spec.get("timeout_seconds", FORWARD_TIMEOUT_S)
                )
                shadow = HttpRelayAdapter(
                    spec["shadow_url"], timeout_seconds=spec["shadow_timeout_seconds"]
                )
                adapter = ZvecShadowRelayAdapter(
                    primary,
                    shadow,
                    log_path=pathlib.Path(spec["shadow_log"]),
                    shadow_timeout_seconds=spec["shadow_timeout_seconds"],
                )
                if shared_key is not None:
                    shared_shadow_cache[shared_key] = adapter
                adapters[project][backend_key] = adapter
                continue
            if spec["kind"] == "http":
                adapters[project][backend_key] = HttpRelayAdapter(
                    spec["url"], spec.get("headers"), spec.get("timeout_seconds", FORWARD_TIMEOUT_S)
                )
                continue

            shared_key = spec.get("shared_key")
            if shared_key is not None:
                if shared_key not in shared_stdio_cache:
                    shared_stdio_cache[shared_key] = StdioSubprocessAdapter(spec["command"], spec["args"], spec["env"])
                adapters[project][backend_key] = shared_stdio_cache[shared_key]
            else:
                adapters[project][backend_key] = StdioSubprocessAdapter(spec["command"], spec["args"], spec["env"])

        # Hindsight does not expose a content-write tool for pinned mental
        # models. Add a gateway-owned replacement tool beside each writable
        # docs/issues bank without changing the underlying adapter type.
        for bank_key in ("docs", "issues"):
            hindsight_adapter = adapters[project].get(bank_key)
            if hindsight_adapter is not None:
                adapters[project][f"{bank_key}_manual"] = ManualMentalModelAdapter(hindsight_adapter)

    return adapters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        help="TOML instance config for container mode; without it, use the legacy static registry",
    )
    args = parser.parse_args()

    if args.config is None:
        home = os.path.expanduser("~")
        registry = build_project_registry(home)
        validate_gateway_identity_registry(registry)
    else:
        registry = load_instance_registry(args.config)
    projects = build_backend_adapters(registry)
    log.info("starting on %s:%d, %d projects: %s", args.host, args.port, len(projects), sorted(projects))
    app = build_app(projects)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
