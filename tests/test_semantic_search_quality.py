import json
from pathlib import Path

import pytest

from scripts.evaluate_semantic_search import EvaluationError, evaluate


def _qrels(status="adjudicated"):
    return {
        "judgment_status": status,
        "candidate_pool_complete": True,
        "queries": [
            {
                "id": "exact",
                "category": "exact-symbol",
                "judgments": [
                    {"unit_id": "a", "grade": 3},
                    {"unit_id": "b", "grade": 2},
                    {"unit_id": "c", "grade": 0},
                ],
            },
            {
                "id": "concept",
                "category": "concept",
                "judgments": [
                    {"unit_id": "x", "grade": 3},
                    {"unit_id": "y", "grade": 1},
                ],
            },
        ],
    }


def _runs(first_query_results):
    return {
        "runs": [
            {
                "backend": "test-backend",
                "queries": [
                    {"id": "exact", "results": [{"unit_id": item} for item in first_query_results]},
                    {"id": "concept", "results": [{"unit_id": "x"}, {"unit_id": "y"}]},
                ],
            }
        ]
    }


def test_evaluate_reports_graded_and_binary_ranking_metrics_by_category():
    report = evaluate(_qrels(), _runs(["a", "b", "c"]), cutoff=3)

    run = report["runs"][0]
    assert run["overall"]["ndcg@3"] == pytest.approx(1.0)
    assert run["overall"]["mrr@3"] == pytest.approx(1.0)
    assert run["overall"]["recall@3"] == pytest.approx(1.0)
    assert run["overall"]["precision@3"] == pytest.approx(0.5)
    assert run["per_query"][0]["precision@3"] == pytest.approx(2 / 3)
    assert run["by_category"]["exact-symbol"]["ndcg@3"] == pytest.approx(1.0)
    assert run["query_count"] == 2


def test_evaluate_penalizes_lower_rank_for_graded_and_reciprocal_metrics():
    report = evaluate(_qrels(), _runs(["c", "a", "b"]), cutoff=3)
    scores = report["runs"][0]["per_query"][0]

    assert scores["ndcg@3"] < 1.0
    assert scores["mrr@3"] == pytest.approx(0.5)
    assert scores["recall@3"] == pytest.approx(1.0)


def test_evaluate_refuses_draft_qrels():
    with pytest.raises(EvaluationError, match="judgment_status='adjudicated'"):
        evaluate(_qrels(status="draft"), _runs(["a", "b", "c"]))


def test_evaluate_refuses_unjudged_results_instead_of_counting_them_irrelevant():
    with pytest.raises(EvaluationError, match="unjudged candidates"):
        evaluate(_qrels(), _runs(["a", "not-in-qrels", "b"]))


def test_evaluate_requires_the_same_query_set_for_each_backend():
    runs = _runs(["a", "b", "c"])
    runs["runs"][0]["queries"].pop()

    with pytest.raises(EvaluationError, match="query IDs must exactly match"):
        evaluate(_qrels(), runs)


def test_source_target_draft_covers_the_existing_11_query_prompts():
    benchmark_dir = Path(__file__).resolve().parents[1] / "benchmarks" / "semantic_search"
    draft = json.loads(
        (benchmark_dir / "kubernaut_fix-2442_8f3bc5a2_2026-09-23.relevance-draft.json").read_text()
    )
    base = json.loads((benchmark_dir / "kubernaut_workflow_discovery.json").read_text())
    followups = json.loads(
        (benchmark_dir / "kubernaut_workflow_discovery_followups_v1.json").read_text()
    )
    active_ids = {
        case["id"]
        for suite in (base, followups)
        for case in suite["queries"]
        if case["status"] == "active"
    }

    draft_ids = {query["id"] for query in draft["queries"]}
    assert draft["judgment_status"] == "source-grounded-draft-needs-human-review"
    assert draft_ids == active_ids
    assert len(draft_ids) == 11
    assert all(query["proposed_targets"] for query in draft["queries"])
    assert all(
        1 <= target["proposed_grade"] <= 3
        for query in draft["queries"]
        for target in query["proposed_targets"]
    )
