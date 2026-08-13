"""Integration tests: search/dcm.py's search_code() against a real
Postgres+pgvector instance (see tests/integration/conftest.py for how it's
provisioned).

Unlike the unit tests in tests/test_dcm_cocoindex_search.py (which mock
psycopg2 entirely and assert on the SQL/params passed to it), these tests
run the *real* SQL against a *real* pgvector column and a *real*
tsvector/GIN trigger, exercising exactly the query behavior and schema
production depends on: cosine-distance ranking, ts_rank_cd/to_tsquery
matching, and the search_vector trigger that keeps FTS in sync on write.
Only search/dcm.py's PG_URL and _embed_query are patched (to point at the
disposable test container and avoid loading the real embedding model) --
search_code() itself runs unmodified.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# Fixed 4-dim stand-ins for real 384-dim MiniLM embeddings -- production
# code never assumes a specific dimension, so this is a faithful,
# hand-computable substitute for exercising pgvector's cosine ranking.
_EMBEDDINGS = {
    "alpha query": [1.0, 0.0, 0.0, 0.0],
}


def _insert_row(conn, id_: str, filepath: str, code: str, embedding: list[float], search_text: str) -> None:
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cocoindex.dcm_code_embeddings
                (id, filepath, chunk_index, code, embedding, search_text)
            VALUES (%s, %s, %s, %s, %s::vector, %s)
            """,
            (id_, filepath, 0, code, embedding_str, search_text),
        )


@pytest.fixture
def seeded_table(code_table):
    """Three rows with known vectors + known FTS text:
    - r1: embedding [1,0,0,0] (exact match for the "alpha query" test vector),
          search_text contains only "alphaterm"
    - r2: embedding [0,1,0,0] (orthogonal -- worst dense match),
          search_text contains only "betaterm"
    - r3: embedding [0.7,0.7,0,0] (between r1 and r2),
          search_text contains both "alphaterm" and "betaterm"
    """
    _insert_row(code_table, "r1", "pkg/foo.go", "func Foo() {}", [1.0, 0.0, 0.0, 0.0], "alphaterm only in r1")
    _insert_row(code_table, "r2", "pkg/bar.go", "func Bar() {}", [0.0, 1.0, 0.0, 0.0], "betaterm only in r2")
    _insert_row(code_table, "r3", "pkg/baz.go", "func Baz() {}", [0.7, 0.7, 0.0, 0.0], "alphaterm and betaterm both in r3")
    return code_table


@pytest.fixture
def patched_dcm_search(dcm_search, pg_url, monkeypatch):
    monkeypatch.setattr(dcm_search, "PG_URL", pg_url)
    monkeypatch.setattr(dcm_search, "_embed_query", lambda query: _EMBEDDINGS["alpha query"])
    return dcm_search


class TestDenseSearch:
    def test_ranks_by_real_cosine_distance(self, patched_dcm_search, seeded_table):
        results = patched_dcm_search.search_code("alpha query", mode="dense", limit=10)

        assert [r["id"] for r in results] == ["r1", "r3", "r2"]
        assert results[0]["score"] == pytest.approx(1.0, abs=1e-4)
        assert results[1]["score"] == pytest.approx(0.7071, abs=1e-3)
        assert results[2]["score"] == pytest.approx(0.0, abs=1e-4)

    def test_respects_limit(self, patched_dcm_search, seeded_table):
        results = patched_dcm_search.search_code("alpha query", mode="dense", limit=1)

        assert [r["id"] for r in results] == ["r1"]


class TestBm25Search:
    def test_matches_only_rows_containing_the_term(self, patched_dcm_search, seeded_table):
        results = patched_dcm_search.search_code("alphaterm", mode="bm25", limit=10)

        ids = {r["id"] for r in results}
        assert ids == {"r1", "r3"}
        assert "r2" not in ids

    def test_no_match_returns_empty(self, patched_dcm_search, seeded_table):
        results = patched_dcm_search.search_code("nonexistentterm", mode="bm25", limit=10)

        assert results == []


class TestHybridSearch:
    def test_fuses_dense_and_bm25_signals(self, patched_dcm_search, seeded_table):
        results = patched_dcm_search.search_code("alphaterm", mode="hybrid", limit=10)

        ids = [r["id"] for r in results]
        # r1 matches both dense (top hit) and bm25 -- must rank first.
        assert ids[0] == "r1"
        # r2 matches neither signal for this query -- must be excluded or
        # ranked last of the three, never ahead of r1/r3.
        assert ids.index("r1") < len(ids)
        assert "r2" not in ids or ids.index("r2") > ids.index("r1")


class TestSearchVectorTrigger:
    """Regression coverage for the BEFORE INSERT OR UPDATE OF search_text,
    filepath trigger -- production relies on this firing automatically so
    the ingestion pipeline never has to populate search_vector itself."""

    def test_search_vector_is_populated_automatically_on_insert(self, patched_dcm_search, code_table):
        _insert_row(code_table, "trg1", "pkg/trigger.go", "func T() {}", [1.0, 0.0, 0.0, 0.0], "uniquetriggerterm")

        with code_table.cursor() as cur:
            cur.execute(
                "SELECT search_vector IS NOT NULL FROM cocoindex.dcm_code_embeddings WHERE id = %s",
                ("trg1",),
            )
            (populated,) = cur.fetchone()
        assert populated is True

        results = patched_dcm_search.search_code("uniquetriggerterm", mode="bm25", limit=10)
        assert [r["id"] for r in results] == ["trg1"]

    def test_search_vector_is_refreshed_on_search_text_update(self, patched_dcm_search, code_table):
        _insert_row(code_table, "trg2", "pkg/trigger2.go", "func T2() {}", [1.0, 0.0, 0.0, 0.0], "originalterm")
        assert patched_dcm_search.search_code("originalterm", mode="bm25", limit=10)

        with code_table.cursor() as cur:
            cur.execute(
                "UPDATE cocoindex.dcm_code_embeddings SET search_text = %s WHERE id = %s",
                ("replacementterm", "trg2"),
            )

        assert patched_dcm_search.search_code("originalterm", mode="bm25", limit=10) == []
        results = patched_dcm_search.search_code("replacementterm", mode="bm25", limit=10)
        assert [r["id"] for r in results] == ["trg2"]
