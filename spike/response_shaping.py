"""Experimental lossless JSON response shaping.

This module is intentionally spike-only. It is not imported by or invoked from
the production gateway. The implementation is retained for benchmarking a
future, more useful model-facing compaction strategy.
"""
from __future__ import annotations

import json


LOSSLESS_JSON_BACKENDS = frozenset({"docs", "issues", "rca"})


def _compact_json_whitespace(text: str) -> str:
    """Remove JSON whitespace without changing any JSON value or token.

    This is intentionally lexical rather than ``json.dumps(json.loads(...))``:
    it preserves key order, duplicate keys, numeric lexemes, and whitespace
    inside string values. Invalid or multi-value text passes through unchanged.
    """
    if not text or not text.strip():
        return text

    start = len(text) - len(text.lstrip())
    try:
        _, end = json.JSONDecoder().raw_decode(text, start)
    except (TypeError, ValueError):
        return text
    if text[end:].strip():
        return text

    compacted: list[str] = []
    in_string = False
    escaped = False
    for char in text[start:end]:
        if in_string:
            compacted.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            compacted.append(char)
        elif not char.isspace():
            compacted.append(char)

    result = "".join(compacted)
    return result if len(result) < len(text) else text


def _shape_tool_result(backend_key: str, result: dict) -> tuple[dict, str]:
    """Apply the experimental lossless shaping policy to one tool result."""
    if backend_key not in LOSSLESS_JSON_BACKENDS or result.get("isError"):
        return result, "none"

    content = result.get("content")
    if not isinstance(content, list):
        return result, "none"

    shaped_content: list[dict] = []
    changed = False
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
            shaped_content.append(item)
            continue
        original_text = item["text"]
        compacted_text = _compact_json_whitespace(original_text)
        if compacted_text == original_text:
            shaped_content.append(item)
            continue
        shaped_content.append({**item, "text": compacted_text})
        changed = True

    if not changed:
        return result, "none"
    return {**result, "content": shaped_content}, "json-whitespace"
