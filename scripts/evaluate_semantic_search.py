#!/usr/bin/env python3
"""Score normalized semantic-search runs against complete, adjudicated qrels.

The scorer deliberately refuses draft labels and unjudged returned candidates.
Map backend-specific chunks/symbols to stable source-span ``unit_id`` values
before scoring; do not use filenames or backend scores as relevance labels.

Qrels shape::

    {"judgment_status": "adjudicated", "candidate_pool_complete": true,
     "queries": [{"id": "q1", "category": "concept",
                  "judgments": [{"unit_id": "path::symbol", "grade": 3}]}]}

Runs shape::

    {"runs": [{"backend": "zvec", "queries": [
       {"id": "q1", "results": [{"unit_id": "path::symbol"}]}]}]}

Result order is rank order. Grades 2 and 3 count as relevant for MRR, recall,
and precision; nDCG uses graded gain ``2**grade - 1``.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import defaultdict
from typing import Any


class EvaluationError(ValueError):
    """Raised when the input is not safe to interpret as an evaluation."""


def _validate_qrels(qrels: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if qrels.get("judgment_status") != "adjudicated":
        raise EvaluationError("qrels must have judgment_status='adjudicated'")
    if qrels.get("candidate_pool_complete") is not True:
        raise EvaluationError("qrels must assert candidate_pool_complete=true")

    queries = qrels.get("queries")
    if not isinstance(queries, list) or not queries:
        raise EvaluationError("qrels must contain a non-empty queries list")

    by_id: dict[str, dict[str, Any]] = {}
    for query in queries:
        query_id = query.get("id")
        if not query_id or query_id in by_id:
            raise EvaluationError(f"missing or duplicate qrels query id: {query_id!r}")
        judgments = query.get("judgments")
        if not isinstance(judgments, list) or not judgments:
            raise EvaluationError(f"query {query_id!r} has no judgments")

        grades: dict[str, int] = {}
        for judgment in judgments:
            unit_id = judgment.get("unit_id")
            grade = judgment.get("grade")
            if not unit_id or unit_id in grades:
                raise EvaluationError(
                    f"query {query_id!r} has missing or duplicate unit_id: {unit_id!r}"
                )
            if not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 3:
                raise EvaluationError(
                    f"query {query_id!r}, unit {unit_id!r}: grade must be an integer from 0 to 3"
                )
            grades[unit_id] = grade
        if not any(grade >= 2 for grade in grades.values()):
            raise EvaluationError(f"query {query_id!r} has no grade-2+ relevant units")
        by_id[query_id] = {"category": query.get("category", "uncategorized"), "grades": grades}
    return by_id


def _dcg(grades: list[int]) -> float:
    return sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades, 1))


def _score_query(
    judged_grades: dict[str, int], result_ids: list[str], cutoff: int
) -> dict[str, float]:
    if len(result_ids) != len(set(result_ids)):
        raise EvaluationError("run contains duplicate unit_id values for one query")
    unjudged = [unit_id for unit_id in result_ids if unit_id not in judged_grades]
    if unjudged:
        raise EvaluationError(
            "run contains unjudged candidates; extend the pooled qrels before scoring: "
            + ", ".join(unjudged[:5])
        )

    top_ids = result_ids[:cutoff]
    top_grades = [judged_grades[unit_id] for unit_id in top_ids]
    ideal_grades = sorted(judged_grades.values(), reverse=True)[:cutoff]
    ideal_dcg = _dcg(ideal_grades)
    ndcg = _dcg(top_grades) / ideal_dcg if ideal_dcg else 0.0

    relevant = {unit_id for unit_id, grade in judged_grades.items() if grade >= 2}
    reciprocal_rank = 0.0
    for rank, unit_id in enumerate(top_ids, 1):
        if unit_id in relevant:
            reciprocal_rank = 1.0 / rank
            break

    relevant_retrieved = sum(unit_id in relevant for unit_id in top_ids)
    return {
        f"ndcg@{cutoff}": ndcg,
        f"mrr@{cutoff}": reciprocal_rank,
        f"recall@{cutoff}": relevant_retrieved / len(relevant),
        f"precision@{cutoff}": relevant_retrieved / cutoff,
    }


def evaluate(qrels: dict[str, Any], run_data: dict[str, Any], cutoff: int = 10) -> dict[str, Any]:
    if cutoff <= 0:
        raise EvaluationError("cutoff must be positive")
    judged = _validate_qrels(qrels)
    runs = run_data.get("runs")
    if not isinstance(runs, list) or not runs:
        raise EvaluationError("run file must contain a non-empty runs list")

    output_runs = []
    expected_ids = set(judged)
    for run in runs:
        backend = run.get("backend")
        if not backend:
            raise EvaluationError("each run requires a backend name")
        query_rows = run.get("queries")
        if not isinstance(query_rows, list):
            raise EvaluationError(f"backend {backend!r} has no queries list")
        by_query = {row.get("id"): row for row in query_rows}
        if len(by_query) != len(query_rows) or set(by_query) != expected_ids:
            raise EvaluationError(
                f"backend {backend!r} query IDs must exactly match the qrels query IDs"
            )

        per_query = []
        by_category: dict[str, list[dict[str, float]]] = defaultdict(list)
        for query_id, qrel in judged.items():
            row = by_query[query_id]
            results = row.get("results")
            if not isinstance(results, list):
                raise EvaluationError(f"backend {backend!r}, query {query_id!r}: results must be a list")
            result_ids = [item.get("unit_id") for item in results]
            if any(not unit_id for unit_id in result_ids):
                raise EvaluationError(f"backend {backend!r}, query {query_id!r}: missing unit_id")

            scores = _score_query(qrel["grades"], result_ids, cutoff)
            per_query.append({"id": query_id, "category": qrel["category"], **scores})
            by_category[qrel["category"]].append(scores)

        score_keys = [f"ndcg@{cutoff}", f"mrr@{cutoff}", f"recall@{cutoff}", f"precision@{cutoff}"]
        overall = {
            key: sum(row[key] for row in per_query) / len(per_query)
            for key in score_keys
        }
        category_scores = {
            category: {
                key: sum(row[key] for row in scores) / len(scores)
                for key in score_keys
            }
            for category, scores in sorted(by_category.items())
        }
        output_runs.append({
            "backend": backend,
            "query_count": len(per_query),
            "overall": overall,
            "by_category": category_scores,
            "per_query": per_query,
        })

    return {"cutoff": cutoff, "qrels_status": "adjudicated", "runs": output_runs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qrels", required=True, type=pathlib.Path)
    parser.add_argument("--runs", required=True, type=pathlib.Path)
    parser.add_argument("--k", type=int, default=10, dest="cutoff")
    args = parser.parse_args()
    try:
        qrels = json.loads(args.qrels.read_text())
        run_data = json.loads(args.runs.read_text())
        report = evaluate(qrels, run_data, args.cutoff)
    except (OSError, json.JSONDecodeError, EvaluationError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
