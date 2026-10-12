import json
from pathlib import Path

from scripts.benchmark_semantic_search import CASES, _rank_results


def test_benchmark_cases_have_distinct_queries_and_targets():
    assert len({case.name for case in CASES}) == len(CASES)
    assert all(case.query and case.targets for case in CASES)


def test_rank_results_matches_targets_in_result_order():
    results = [
        {"name": "unrelated"},
        {"name": "ListWorkflowsByActionType"},
    ]

    assert _rank_results(results, lambda item: str(item), ("ListWorkflowsByActionType",)) == 2
    assert _rank_results(results[:1], lambda item: str(item), ("ListWorkflowsByActionType",)) is None


def test_kubernaut_workflow_discovery_query_suite_is_reproducible():
    suite_path = (
        Path(__file__).resolve().parents[1]
        / "benchmarks"
        / "semantic_search"
        / "kubernaut_workflow_discovery.json"
    )
    suite = json.loads(suite_path.read_text())
    queries = suite["queries"]

    assert suite["suite_id"] == "kubernaut-workflow-discovery"
    assert len({case["id"] for case in queries}) == len(queries)
    assert len({case["query"] for case in queries}) == len(queries)
    assert sum(case["status"] == "active" for case in queries) == 7
    excluded = [case for case in queries if case["status"] == "excluded"]
    assert len(excluded) == 1
    assert excluded[0]["query"] == "workflow_discovery_membership_2442"
    assert excluded[0]["exclusion_reason"]
