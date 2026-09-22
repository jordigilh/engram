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
