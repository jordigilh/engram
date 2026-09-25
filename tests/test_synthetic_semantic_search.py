import json
from pathlib import Path

import pytest

from scripts.build_synthetic_qrels import build_qrels
from scripts.evaluate_semantic_search import evaluate
from scripts.replay_synthetic_zvec import replay


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "semantic_search"
    / "fixtures"
    / "go-workflow-discovery-v1"
)


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
    with pytest.raises(ValueError, match="indexed source root differs"):
        replay(FIXTURE, tmp_path, tmp_path / "zg", tmp_path / "model", "zvec-test")
