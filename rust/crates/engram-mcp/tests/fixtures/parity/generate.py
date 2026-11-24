#!/usr/bin/env python3
"""Regenerate the Python-reference parity fixtures for the Rust gateway.

Run from the repository root with the project installed, for example:

    PYTHONPATH=src python3 \
      rust/crates/engram-mcp/tests/fixtures/parity/generate.py

The Rust tests load these files with `include_str!`; they are the reference
behavior from `engram.pipeline.engram_gateway`. This script is not run in CI.
"""

from __future__ import annotations

import json
import pathlib
import sys

from engram.pipeline import engram_gateway as gateway

ROOT = pathlib.Path(__file__).resolve().parent


def write(name: str, value: object) -> None:
    (ROOT / name).write_text(json.dumps(value, indent=2) + "\n")


def recall(text_payload: object | None, *, structured: object | None = None, **extra: object) -> dict:
    result: dict[str, object] = {
        "content": [] if text_payload is None else [{"type": "text", "text": json.dumps(text_payload)}],
        "isError": False,
        **extra,
    }
    if structured is not None:
        result["structuredContent"] = structured
    return result


def main() -> None:
    bounded = {
        "results": [
            {
                "id": f"memory-{index}",
                "text": "fact " + ("x" * 1500),
                "fact_type": "world",
                "context": "project",
                "document_id": "engram-doc",
                "metadata": {
                    "repo": "engram",
                    "key_sentences": "y" * 1500,
                    "keywords": "gateway, recall",
                },
                "tags": ["engram"],
                "scores": {"final": 0.123456, "semantic": None},
            }
            for index in range(10)
        ],
        "source_facts_truncated": None,
    }
    write("recall_bounded_input.json", recall(bounded))
    write("recall_bounded_expected.json", gateway._normalize_recall_result(recall(bounded)))

    structured = {
        "content": [],
        "isError": False,
        "structuredContent": {"results": [{"id": "memory-1", "text": "kept", "tags": ["engram"]}]},
    }
    write("recall_structured_input.json", structured)
    write("recall_structured_expected.json", gateway._normalize_recall_result(structured))

    nulls = {
        "results": [
            {
                "id": "memory-null",
                "text": "present",
                "occurred_start": None,
                "occurred_end": None,
                "mentioned_at": None,
                "document_id": None,
                "tags": None,
                "fact_type": None,
            }
        ]
    }
    write("recall_nulls_input.json", recall(nulls))
    write("recall_nulls_expected.json", gateway._normalize_recall_result(recall(nulls)))

    for name, flag in (("true", True), ("one", 1)):
        payload = {
            "results": [{"id": "memory-1", "text": "one"}],
            "source_facts_truncated": flag,
        }
        write(f"recall_source_truncated_{name}_input.json", recall(payload))
        write(f"recall_source_truncated_{name}_expected.json", gateway._normalize_recall_result(recall(payload)))

    non_object = {"results": [42, {"id": "memory-1", "text": "kept"}]}
    write("recall_non_object_input.json", recall(non_object))
    write("recall_non_object_expected.json", gateway._normalize_recall_result(recall(non_object)))

    whitespace = {
        "results": [
            {
                "id": "memory-1",
                "summary": "   ",
                "text": "fallback text",
                "metadata": {"key_sentences": "key sentence"},
            }
        ]
    }
    write("recall_whitespace_summary_input.json", recall(whitespace))
    write("recall_whitespace_summary_expected.json", gateway._normalize_recall_result(recall(whitespace)))

    non_string_text = {"results": [{"id": "memory-1", "text": 42}]}
    write("recall_non_string_text_input.json", recall(non_string_text))
    write("recall_non_string_text_expected.json", gateway._normalize_recall_result(recall(non_string_text)))

    integer_scores = {
        "results": [{"id": "memory-1", "text": "scored", "scores": {"f": 1, "semantic": 0.123456}}]
    }
    write("recall_integer_scores_input.json", recall(integer_scores))
    write("recall_integer_scores_expected.json", gateway._normalize_recall_result(recall(integer_scores)))

    precedence = recall(
        {"results": [{"id": "from-text", "text": "text wins"}]},
        structured={"results": [{"id": "from-structured", "text": "structured wins"}]},
    )
    write("recall_precedence_input.json", precedence)
    write("recall_precedence_expected.json", gateway._normalize_recall_result(precedence))

    error = recall({"results": [{"id": "memory-1", "text": "error body"}]}, isError=True)
    write("recall_is_error_input.json", error)
    write("recall_is_error_expected.json", gateway._normalize_recall_result(error))


    falsy_labels = {
        "results": [
            {
                "id": "memory-1",
                "text": "fallback",
                "fact_type": "",
                "context": "",
                "document_id": "",
                "tags": "",
            }
        ]
    }
    write("recall_falsy_labels_input.json", recall(falsy_labels))
    write("recall_falsy_labels_expected.json", gateway._normalize_recall_result(recall(falsy_labels)))

    blank_text = {"results": [{"id": "memory-1", "summary": "  ", "text": "   "}]}
    write("recall_blank_text_input.json", recall(blank_text))
    write("recall_blank_text_expected.json", gateway._normalize_recall_result(recall(blank_text)))

    rounding_ties = {
        "results": [{"id": "memory-1", "text": "tie", "scores": {"a": 0.00035, "b": 0.00105}}]
    }
    write("recall_rounding_ties_input.json", recall(rounding_ties))
    write("recall_rounding_ties_expected.json", gateway._normalize_recall_result(recall(rounding_ties)))

    bool_scores = {
        "results": [{"id": "memory-1", "text": "bools", "scores": {"x": True, "y": False}}]
    }
    write("recall_bool_scores_input.json", recall(bool_scores))
    write("recall_bool_scores_expected.json", gateway._normalize_recall_result(recall(bool_scores)))

    empty_string_tags = {"results": [{"id": "memory-1", "text": "tags", "tags": [""]}]}
    write("recall_empty_string_tags_input.json", recall(empty_string_tags))
    write(
        "recall_empty_string_tags_expected.json",
        gateway._normalize_recall_result(recall(empty_string_tags)),
    )

    empty_text = {"results": [{"id": "memory-1", "text": ""}]}
    write("recall_empty_text_input.json", recall(empty_text))
    write("recall_empty_text_expected.json", gateway._normalize_recall_result(recall(empty_text)))

    separator_whitespace = {
        "results": [{"id": "memory-1", "summary": "\u001c", "text": "fallback"}]
    }
    write("recall_separator_whitespace_input.json", recall(separator_whitespace))
    write(
        "recall_separator_whitespace_expected.json",
        gateway._normalize_recall_result(recall(separator_whitespace)),
    )

    string_tags = {"results": [{"id": "memory-1", "text": "tags", "tags": "ab"}]}
    write("recall_string_tags_input.json", recall(string_tags))
    write("recall_string_tags_expected.json", gateway._normalize_recall_result(recall(string_tags)))

    pattern = {"content": [{"type": "text", "text": "0123456789" * 5}], "isError": False}
    write("pattern_truncate_input.json", pattern)
    write("pattern_truncate_expected.json", gateway._truncate_pattern_result(pattern, 20))

    exact = {"content": [{"type": "text", "text": "x" * 20}], "isError": False}
    write("pattern_exact_input.json", exact)
    write("pattern_exact_expected.json", gateway._truncate_pattern_result(exact, 20))

    multiple = {
        "content": [
            {"type": "text", "text": "a" * 30},
            {"type": "image", "text": "b" * 40},
            {"type": "text", "text": "short"},
        ],
        "isError": False,
    }
    write("pattern_multiple_input.json", multiple)
    write("pattern_multiple_expected.json", gateway._truncate_pattern_result(multiple, 20))

    non_list = {"content": "not-a-list", "isError": False}
    write("pattern_non_list_input.json", non_list)
    write("pattern_non_list_expected.json", gateway._truncate_pattern_result(non_list, 20))


    multibyte = {"content": [{"type": "text", "text": "é" * 30}], "isError": False}
    write("pattern_multibyte_input.json", multibyte)
    write("pattern_multibyte_expected.json", gateway._truncate_pattern_result(multibyte, 20))

    plus_one = {"content": [{"type": "text", "text": "x" * 21}], "isError": False}
    write("pattern_plus_one_input.json", plus_one)
    write("pattern_plus_one_expected.json", gateway._truncate_pattern_result(plus_one, 20))

    zero = {"content": [{"type": "text", "text": "x" * 30}], "isError": False}
    write("pattern_zero_input.json", zero)
    write("pattern_zero_expected.json", gateway._truncate_pattern_result(zero, 0))


if __name__ == "__main__":
    sys.exit(main())
