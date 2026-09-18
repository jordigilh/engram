"""Measure the disabled lossless shaping spike against live Hindsight responses.

This is a read-only benchmark. It uses the same ``HttpRelayAdapter`` as the
gateway, calls only Hindsight's ``recall`` tool, applies the disabled spike
locally, and prints aggregate metadata without printing or persisting project
content.

Run from the repository root::

    .venv/bin/python spike/benchmark_live_hindsight_response_shaping.py

The bank contents are live and can change. The fixed query matrix and UTC
timestamp in the output make each run auditable; the companion report should
record the resulting aggregate rather than treating it as a permanent corpus
snapshot.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engram.pipeline.engram_gateway import (
    HttpRelayAdapter,
    _estimate_tokens,
    _extract_result_text,
)
from spike.response_shaping import _shape_tool_result


logging.disable(logging.INFO)


QUERY_MATRIX = (
    ("architecture", "architecture and components"),
    ("installation", "installation and setup"),
    ("gateway", "MCP gateway and routing"),
    ("branching", "branch selection and release routing"),
    ("testing", "testing and CI"),
    ("recovery", "error handling and recovery"),
    ("release", "release readiness and blockers"),
    ("performance", "performance and latency"),
)

BANKS = (
    ("kubernaut-docs", "docs"),
    ("kubernaut-issues", "issues"),
    ("praxis-docs", "docs"),
    ("praxis-issues", "issues"),
)


@dataclass
class Sample:
    bank: str
    category: str
    raw_chars: int
    shaped_chars: int
    raw_tokens: int
    shaped_tokens: int
    result_count: int | None
    remote_ms: float
    shaping_ms: float
    strategy: str
    response_form: str

    @property
    def savings_tokens(self) -> int:
        return max(0, self.raw_tokens - self.shaped_tokens)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _percentile_triplet(values: list[float]) -> str:
    return "/".join(f"{_percentile(values, fraction):.0f}" for fraction in (0.5, 0.9, 0.95))


def _result_count(text: str) -> int | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
    return len(results) if isinstance(results, list) else None


def _response_form(text: str, strategy: str) -> str:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return "non-JSON"
    return "compact JSON" if strategy == "none" else "JSON with whitespace"


async def _measure_bank(bank: str, backend: str, base_url: str, max_tokens: int) -> tuple[list[Sample], int]:
    adapter = HttpRelayAdapter(f"{base_url.rstrip('/')}/mcp/{bank}/")
    samples: list[Sample] = []
    errors = 0
    for category, query in QUERY_MATRIX:
        started = time.perf_counter_ns()
        try:
            result = await adapter.call_tool("recall", {"query": query, "max_tokens": max_tokens})
        except Exception:
            errors += 1
            continue
        remote_ms = (time.perf_counter_ns() - started) / 1_000_000
        if result.get("isError"):
            errors += 1
            continue

        raw_text = _extract_result_text(result)
        shape_started = time.perf_counter_ns()
        shaped_result, strategy = _shape_tool_result(backend, result)
        shaping_ms = (time.perf_counter_ns() - shape_started) / 1_000_000
        shaped_text = _extract_result_text(shaped_result)
        samples.append(
            Sample(
                bank=bank,
                category=category,
                raw_chars=len(raw_text),
                shaped_chars=len(shaped_text),
                raw_tokens=_estimate_tokens(raw_text),
                shaped_tokens=_estimate_tokens(shaped_text),
                result_count=_result_count(raw_text),
                remote_ms=remote_ms,
                shaping_ms=shaping_ms,
                strategy=strategy,
                response_form=_response_form(raw_text, strategy),
            )
        )
    return samples, errors


def _format_bank_summary(bank: str, samples: list[Sample], errors: int) -> str:
    raw_tokens = [sample.raw_tokens for sample in samples]
    shaped_tokens = [sample.shaped_tokens for sample in samples]
    savings = [sample.savings_tokens for sample in samples]
    savings_percent = [
        100 * sample.savings_tokens / sample.raw_tokens
        for sample in samples
        if sample.raw_tokens
    ]
    remote_ms = [sample.remote_ms for sample in samples]
    shaping_ms = [sample.shaping_ms for sample in samples]
    raw_chars = [sample.raw_chars for sample in samples]
    shaped_chars = [sample.shaped_chars for sample in samples]
    result_counts = [sample.result_count for sample in samples if sample.result_count is not None]
    response_forms = ", ".join(
        f"{form} ({count})" for form, count in sorted(Counter(sample.response_form for sample in samples).items())
    )
    return (
        f"| {bank} | {len(samples)} | {errors} | {response_forms} | "
        f"{_median(result_counts):.0f} | {_median(raw_chars):.0f} | "
        f"{_median(shaped_chars):.0f} | {_percentile_triplet(raw_tokens)} | "
        f"{_percentile_triplet(shaped_tokens)} | {_percentile_triplet(savings)} | "
        f"{_median(savings_percent):.1f}% | {_percentile(remote_ms, 0.5):.0f} | "
        f"{_percentile(remote_ms, 0.95):.0f} | {_percentile(shaping_ms, 0.95):.3f} |"
    )


async def _run(args: argparse.Namespace) -> None:
    print(f"Measured at: {datetime.now(timezone.utc).isoformat()}")
    print(f"Base URL: {args.base_url}")
    print(f"Query matrix: {len(QUERY_MATRIX)} fixed queries per bank")
    print(f"Recall max_tokens: {args.max_tokens}")
    print()
    print("| Bank | Samples | Errors | Response form | Median results | Median input chars | Median output chars | Input tokens p50/p90/p95 | Output tokens p50/p90/p95 | Saved tokens p50/p90/p95 | Median savings | Remote p50 ms | Remote p95 ms | Shaping p95 ms |")
    print("|---|---:|---:|---|---:|---:|---:|---|---|---|---:|---:|---:|---:|")

    all_samples: list[Sample] = []
    total_errors = 0
    for bank, backend in BANKS:
        samples, errors = await _measure_bank(bank, backend, args.base_url, args.max_tokens)
        all_samples.extend(samples)
        total_errors += errors
        print(_format_bank_summary(bank, samples, errors))

    if all_samples:
        raw_tokens = [sample.raw_tokens for sample in all_samples]
        savings = [sample.savings_tokens for sample in all_samples]
        percentages = [100 * sample.savings_tokens / sample.raw_tokens for sample in all_samples if sample.raw_tokens]
        print(_format_bank_summary("ALL BANKS", all_samples, total_errors))
        print()
        print(f"Successful samples: {len(all_samples)}")
        print(f"Errors: {total_errors}")
        print(f"Total estimated input tokens: {sum(raw_tokens)}")
        print(f"Total estimated tokens saved: {sum(savings)}")
        print(f"Aggregate savings: {100 * sum(savings) / sum(raw_tokens):.1f}%")
        print(f"Median per-sample savings: {_median(percentages):.1f}%")
    else:
        print("No successful samples; no savings estimate was produced.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8888")
    parser.add_argument("--max-tokens", type=int, default=2048)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
