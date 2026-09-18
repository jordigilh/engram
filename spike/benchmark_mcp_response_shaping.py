"""Benchmark the spike's lossless MCP response shaping.

This benchmark covers only the disabled spike implementation: lexical
JSON-whitespace compaction for structured docs/issues/rca-style payloads.
Code/search text and malformed or already-compact payloads are included to
prove their pass-through behavior.

Run from the repository root with::

    .venv/bin/python spike/benchmark_mcp_response_shaping.py

The output is suitable for copying into the companion benchmark document.
Fixtures are synthetic and contain no repository or user data.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engram.pipeline.engram_gateway import (  # noqa: E402
    _estimate_tokens,
)
from spike.response_shaping import _shape_tool_result  # noqa: E402


def _tool_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _recall_fixture() -> str:
    payload = {
        "results": [
            {
                "id": f"memory-{index}",
                "text": (
                    "The gateway keeps project routes isolated.  Preserve this "
                    "spacing inside the memory text."
                ),
                "fact_type": "world",
                "mentioned_at": "2026-09-18T10:00:00Z",
                "tags": ["engram", "gateway"],
                "scores": {"final": 0.91 - index / 100, "semantic": 0.88},
            }
            for index in range(1, 9)
        ],
        "trace": None,
        "entities": None,
        "chunks": None,
        "source_facts": None,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _rca_fixture() -> str:
    payload = {
        "run_id": "run-123",
        "project": "kubernaut",
        "branch": "main",
        "summary": "The test failed after the controller restart.",
        "evidence": [
            {
                "id": f"evidence-{index}",
                "type": "log",
                "timestamp": "2026-09-18T10:00:00Z",
                "source_file": "controller.log",
                "source_line": index * 10,
                "identifiers": {"component": "controller", "run": "run-123"},
                "content": "ERROR retry budget exhausted; preserving evidence text.",
            }
            for index in range(1, 7)
        ],
        "timeline": [
            {"evidence_id": f"evidence-{index}", "relation": "preceded"}
            for index in range(1, 7)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


FIXTURES = (
    ("docs", "Hindsight recall envelope", _recall_fixture()),
    ("rca", "RCA dossier envelope", _rca_fixture()),
    (
        "code",
        "CocoIndex/code text pass-through",
        "Code search [hybrid]: 2 results\n[1] src/app.py\nreturn value  # keep exact text\n",
    ),
    ("docs", "Malformed JSON pass-through", '{ "results": [ }\n'),
    ("docs", "Already compact JSON", '{"results":[{"id":"memory-1","text":"ok"}]}'),
)


def _semantic_preservation(original: str, shaped: str) -> str:
    try:
        return "yes" if json.loads(original) == json.loads(shaped) else "no"
    except json.JSONDecodeError:
        return "byte-identical" if original == shaped else "changed"


def _benchmark(backend: str, label: str, text: str, iterations: int = 1000) -> dict[str, object]:
    original_result = _tool_result(text)
    shaped_result, strategy = _shape_tool_result(backend, original_result)
    shaped = shaped_result["content"][0]["text"]

    samples_us: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        _shape_tool_result(backend, original_result)
        samples_us.append((time.perf_counter_ns() - started) / 1_000)
    samples_us.sort()

    return {
        "label": label,
        "strategy": strategy,
        "input_chars": len(text),
        "output_chars": len(shaped),
        "input_tokens": _estimate_tokens(text),
        "output_tokens": _estimate_tokens(shaped),
        "savings_tokens": max(0, _estimate_tokens(text) - _estimate_tokens(shaped)),
        "savings_percent": (
            100 * (_estimate_tokens(text) - _estimate_tokens(shaped)) / _estimate_tokens(text)
            if _estimate_tokens(text)
            else 0.0
        ),
        "p50_us": statistics.median(samples_us),
        "p95_us": samples_us[max(0, int(iterations * 0.95) - 1)],
        "preserved": _semantic_preservation(text, shaped),
    }


def main() -> None:
    print("| Fixture | Strategy | Input chars | Output chars | Input tokens | Output tokens | Saved | Savings | p50 us | p95 us | Preserved |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for backend, label, text in FIXTURES:
        result = _benchmark(backend, label, text)
        print(
            f"| {result['label']} | {result['strategy']} | {result['input_chars']} | "
            f"{result['output_chars']} | {result['input_tokens']} | {result['output_tokens']} | "
            f"{result['savings_tokens']} | "
            f"{result['savings_percent']:.1f}% | {result['p50_us']:.1f} | "
            f"{result['p95_us']:.1f} | {result['preserved']} |"
        )


if __name__ == "__main__":
    main()
