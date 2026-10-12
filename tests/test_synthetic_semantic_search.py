import json
from pathlib import Path

import pytest

from scripts.build_synthetic_qrels import build_qrels
from scripts.evaluate_semantic_search import evaluate
from scripts.replay_synthetic_semantic_search import _map_sense_result, _unit_spans
from scripts.replay_synthetic_zvec import replay


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "semantic_search"
    / "fixtures"
    / "go-workflow-discovery-v1"
)
FIXTURES = {
    "go": FIXTURE,
    "python": FIXTURE.parent / "python-workflow-discovery-v1",
    "rust": FIXTURE.parent / "rust-workflow-discovery-v1",
    "typescript": FIXTURE.parent / "typescript-workflow-discovery-v1",
}


def _load(name: str):
    return json.loads((FIXTURE / name).read_text())


def test_synthetic_qrels_are_reproducible_from_source_authored_truth():
    qrels = _load("qrels.json")
    manifest = _load("manifest.json")

    assert qrels["source_truth"] is True
    assert qrels["judgment_status"] == "adjudicated"
    assert qrels["candidate_pool_complete"] is True
    assert qrels["unit_count"] == len(manifest["units"])
    assert build_qrels(FIXTURE) == qrels

    unit_ids = [unit["unit_id"] for unit in manifest["units"]]
    assert len(unit_ids) == len(set(unit_ids))
    assert all((FIXTURE / unit["path"]).is_file() for unit in manifest["units"])
    for query in qrels["queries"]:
        assert [judgment["unit_id"] for judgment in query["judgments"]] == unit_ids
        assert any(judgment["grade"] >= 2 for judgment in query["judgments"])


def test_source_ordered_fixture_oracle_scores_perfectly():
    qrels = _load("qrels.json")
    runs = {
        "runs": [
            {
                "backend": "source-truth-oracle",
                "queries": [
                    {
                        "id": query["id"],
                        "results": [
                            {"unit_id": judgment["unit_id"]}
                            for judgment in sorted(
                                query["judgments"],
                                key=lambda judgment: (-judgment["grade"], judgment["unit_id"]),
                            )
                        ],
                    }
                    for query in qrels["queries"]
                ],
            }
        ]
    }

    report = evaluate(qrels, runs, cutoff=10)
    scores = report["runs"][0]["per_query"]
    assert all(score["ndcg@10"] == 1.0 for score in scores)
    assert all(score["mrr@10"] == 1.0 for score in scores)
    assert all(score["recall@10"] == 1.0 for score in scores)


def test_zvec_replay_refuses_a_different_source_root_before_search(tmp_path):
    source = tmp_path / "internal"
    source.mkdir()
    (source / "mismatched.go").write_text("package mismatch\n")
    with pytest.raises(ValueError, match="indexed source root differs"):
        replay(FIXTURE, tmp_path, tmp_path / "zg", tmp_path / "model", "zvec-test")


def test_four_language_fixtures_have_independent_source_authored_qrels():
    query_contract = None
    digests = set()
    for language, fixture in FIXTURES.items():
        manifest = json.loads((fixture / "manifest.json").read_text())
        truth = json.loads((fixture / "truth.json").read_text())
        qrels = json.loads((fixture / "qrels.json").read_text())

        assert manifest["language"] == language
        assert qrels["source_truth"] is True
        assert qrels["judgment_status"] == "adjudicated"
        assert qrels["candidate_pool_complete"] is True
        assert build_qrels(fixture) == qrels
        assert qrels["unit_count"] == len(manifest["units"])
        assert all((fixture / unit["path"]).is_file() for unit in manifest["units"])
        assert len(_unit_spans(fixture, manifest)) == len(manifest["units"])
        digests.add(qrels["source"]["snapshot_sha256"])

        queries = [
            (query["id"], query["query"], query["category"])
            for query in truth["queries"]
        ]
        if query_contract is None:
            query_contract = queries
        else:
            assert queries == query_contract

    assert len(digests) == len(FIXTURES)


@pytest.mark.parametrize(
    ("language", "path", "symbol", "line", "expected"),
    [
        ("python", "selection/validator.py", "Validator.is_allowed", 18, "selection/validator.py::Validator.is_allowed"),
        ("rust", "src/selection/validator.rs", "WorkflowValidator::is_allowed", 25, "src/selection/validator.rs::WorkflowValidator.is_allowed"),
        ("typescript", "selection/validator.ts", "WorkflowValidator.isAllowed", 14, "selection/validator.ts::WorkflowValidator.isAllowed"),
    ],
)
def test_sense_symbols_map_to_language_specific_manifest_spans(language, path, symbol, line, expected):
    fixture = FIXTURES[language]
    manifest = json.loads((fixture / "manifest.json").read_text())
    units = _unit_spans(fixture, manifest)
    assert _map_sense_result(
        units, {"file": path, "symbol": symbol, "line": line}
    ) == [expected]
