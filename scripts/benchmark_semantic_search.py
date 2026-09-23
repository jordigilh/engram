#!/usr/bin/env python3
"""Compare CocoIndex and Codanna semantic retrieval on one worktree.

This benchmark intentionally scores retrieval separately from graph accuracy.
CocoIndex returns code chunks; Codanna returns symbols and optional context.
The output records both ranking quality and the response shape/token proxy so a
semantic-search replacement is not selected on rank alone.

Example:
    python scripts/benchmark_semantic_search.py \
        --codanna /Users/jgil/.cargo/bin/codanna \
        --config /path/to/kubernaut/.codanna/settings.toml \
        --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    query: str
    targets: tuple[str, ...]


CASES = (
    BenchmarkCase(
        "membership-before-selection",
        "where is workflow discovery membership validated before workflow selection",
        ("SetDiscoveredWorkflowState", "IsAllowed", "authorizeSelectionDriver"),
    ),
    BenchmarkCase(
        "self-correction-catalog-validation",
        "how does investigator self-correction validate catalog workflows",
        ("selfCorrectWorkflowSelection", "workflowSelectionRetryOrHumanReview", "enrichFromCatalog"),
    ),
    BenchmarkCase(
        "catalog-filtering",
        "how does workflow catalog filter workflows by action type and signal context",
        ("ListWorkflowsByActionType", "filtersFromSignal", "ListActions"),
    ),
    BenchmarkCase(
        "signal-filters-forwarding",
        "where are signal context filters forwarded to list_workflows",
        ("filtersFromSignal", "ListWorkflowsByActionType", "WorkflowDiscoveryFilters"),
    ),
    BenchmarkCase(
        "parameter-schema-validation",
        "how does remediation workflow parameter schema validation fail closed",
        ("ValidateWorkflowParameters", "SetWorkflowMeta", "IsAllowed"),
    ),
    BenchmarkCase(
        "exact-discovery-state-symbol",
        "SetDiscoveredWorkflowState",
        ("SetDiscoveredWorkflowState",),
    ),
    BenchmarkCase(
        "list-action-types",
        "how does workflow discovery list available action types",
        ("ListActions", "listActionsFromCache"),
    ),
    BenchmarkCase(
        "catalog-pagination",
        "how does pagination move through workflow catalog results",
        ("ListWorkflowsByActionType", "ListActions"),
    ),
    BenchmarkCase(
        "catalog-not-ready",
        "what happens when the workflow catalog is not ready",
        ("ErrCatalogNotReady", "LazyCatalog", "workflowCatalogFetcher"),
    ),
    BenchmarkCase(
        "catalog-enrichment",
        "how are selected workflows enriched with catalog metadata",
        ("enrichFromCatalog", "buildWorkflowMeta"),
    ),
    BenchmarkCase(
        "selection-parse-retry",
        "how does investigator handle parse failures during workflow selection",
        ("workflowSelectionRetryOrHumanReview", "attemptWorkflowSubmitRetry"),
    ),
    BenchmarkCase(
        "discovery-entry-conversion",
        "how are workflow discovery entries converted for the LLM",
        ("convertWorkflowsToDiscoveryEntries", "ListWorkflows"),
    ),
    BenchmarkCase(
        "parameter-validation",
        "where does workflow parameter validation reject invalid fields",
        ("ValidateWorkflowParameters", "Validator"),
    ),
    BenchmarkCase(
        "catalog-fetch-per-request",
        "where is the workflow catalog fetched per request",
        ("workflowCatalogFetcher", "CatalogFetcher"),
    ),
    BenchmarkCase(
        "validation-human-review",
        "how is human review required when workflow validation cannot complete",
        ("handleHumanReviewRequired", "workflowSelectionRetryOrHumanReview"),
    ),
    BenchmarkCase(
        "signal-action-context",
        "where does workflow action type context come from the signal",
        ("filtersFromSignal", "signalFromContext"),
    ),
)


def _approx_tokens(value: Any) -> int:
    """Use a stable character proxy; exact tokenizer parity is not required."""
    return max(1, len(json.dumps(value, ensure_ascii=True, separators=(",", ":"))) // 4)


def _target_rank(text: str, targets: tuple[str, ...]) -> int | None:
    for rank, target in enumerate(targets, 1):
        if any(target in str(item) for item in (text,)):
            return rank
    return None


def _rank_results(results: list[Any], render: Callable[[Any], str], targets: tuple[str, ...]) -> int | None:
    for rank, result in enumerate(results, 1):
        if any(target in render(result) for target in targets):
            return rank
    return None


def _cocoindex_runner(repo: str | None, branch: str | None):
    from engram.search import kubernaut

    def run(case: BenchmarkCase, limit: int) -> dict[str, Any]:
        started = time.perf_counter()
        results = kubernaut.search_code(case.query, limit=limit, repo=repo, branch=branch)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        rendered = [
            {
                "filepath": result.get("filepath"),
                "chunk_index": result.get("chunk_index"),
                "score": result.get("score"),
                "code": result.get("code", ""),
            }
            for result in results
        ]
        rank = _rank_results(rendered, lambda item: json.dumps(item), case.targets)
        return {
            "engine": "cocoindex",
            "case": case.name,
            "query": case.query,
            "target_rank": rank,
            "top1_hit": rank == 1,
            "result_count": len(rendered),
            "response_tokens_proxy": _approx_tokens(rendered),
            "result_fields": sorted({key for item in rendered for key in item}),
            "elapsed_ms": elapsed_ms,
            "results": rendered,
        }

    return run


def _codanna_runner(binary: str, config: str, tool: str):
    def run(case: BenchmarkCase, limit: int) -> dict[str, Any]:
        command = [
            binary,
            "mcp",
            tool,
            f"query:{case.query}",
            f"limit:{limit}",
            "--config",
            config,
            "--json",
        ]
        started = time.perf_counter()
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Codanna failed")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Codanna returned non-JSON output: {completed.stdout[:500]!r}") from exc
        results = payload.get("data", [])
        rank = _rank_results(results, lambda item: json.dumps(item), case.targets)
        return {
            "engine": "codanna",
            "case": case.name,
            "query": case.query,
            "target_rank": rank,
            "top1_hit": rank == 1,
            "result_count": len(results),
            "response_tokens_proxy": _approx_tokens(payload),
            "result_fields": sorted({key for item in results if isinstance(item, dict) for key in item}),
            "elapsed_ms": elapsed_ms,
            "results": results,
        }

    return run


def _print_report(results: list[dict[str, Any]]) -> None:
    print("engine\tcase\ttarget-rank\ttop1\tresults\ttokens~\tms")
    for result in results:
        print(
            f"{result['engine']}\t{result['case']}\t{result['target_rank'] or '-'}\t"
            f"{result['top1_hit']}\t{result['result_count']}\t"
            f"{result['response_tokens_proxy']}\t{result['elapsed_ms']}"
        )

    for engine in sorted({result["engine"] for result in results}):
        engine_results = [result for result in results if result["engine"] == engine]
        ranks = [result["target_rank"] for result in engine_results if result["target_rank"]]
        top1 = sum(result["top1_hit"] for result in engine_results)
        print(
            f"summary {engine}: top1={top1}/{len(engine_results)} "
            f"target@{max((result['result_count'] for result in engine_results), default=0)}="
            f"{len(ranks)}/{len(engine_results)} "
            f"mean_rank={sum(ranks) / len(ranks):.2f}" if ranks else
            f"summary {engine}: top1={top1}/{len(engine_results)} target=0/{len(engine_results)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codanna", default="codanna", help="Codanna executable")
    parser.add_argument("--config", required=True, help="Codanna settings.toml")
    parser.add_argument(
        "--codanna-tool",
        choices=("semantic_search_docs", "semantic_search_with_context"),
        default="semantic_search_docs",
        help="Codanna result shape to benchmark (default: structured symbol docs)",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--repo", default="kubernaut")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    runners = (
        _cocoindex_runner(args.repo, args.branch),
        _codanna_runner(args.codanna, args.config, args.codanna_tool),
    )
    results = [runner(case, args.limit) for case in CASES for runner in runners]
    if args.as_json:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)


if __name__ == "__main__":
    main()
