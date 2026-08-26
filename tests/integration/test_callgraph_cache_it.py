"""Integration test: callgraph.build_multi_repo_call_graph_with_stats_cached()
against a real Postgres instance (see tests/integration/conftest.py for how
it's provisioned).

Unlike tests/test_callgraph.py's TestBuildMultiRepoCallGraphWithStatsCached
(which mocks psycopg2 entirely), this exercises the real
CREATE SCHEMA/CREATE TABLE/SELECT/INSERT ... ON CONFLICT DO UPDATE round-trip
against a real BYTEA column -- catching anything the mock's simplified
SQL-string-matching can't (e.g. a real syntax error, a real pickle round-trip
through an actual bytes column instead of a Python bytes object passed
straight through).
"""
from __future__ import annotations

import pathlib

import psycopg2
import pytest

from engram import callgraph

pytestmark = pytest.mark.integration


@pytest.fixture
def clean_cache_table(pg_url):
    """Truncates cocoindex.call_graph_cache before/after each test so tests
    stay isolated within the one shared session container. Doesn't rely on
    the table already existing -- build_multi_repo_call_graph_with_stats_cached
    creates it itself on first use, same as production."""
    def _truncate():
        conn = psycopg2.connect(pg_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS cocoindex.call_graph_cache")
        finally:
            conn.close()

    _truncate()
    callgraph._CACHE_TABLE_READY = False
    yield
    _truncate()
    callgraph._CACHE_TABLE_READY = False


def _write_repo(tmp_path: pathlib.Path) -> list[tuple[str, pathlib.Path, list[str], list[str]]]:
    (tmp_path / "a.go").write_text(
        "func helper() int {\n    return 1\n}\n\nfunc caller() int {\n    return helper()\n}\n"
    )
    return [("repo", tmp_path, ["**/*.go"], [])]


class TestRealPostgresRoundTrip:
    def test_first_call_creates_the_table_and_caches_the_graph(self, pg_url, clean_cache_table, tmp_path):
        roots = _write_repo(tmp_path)

        graph = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "it-key", pg_url)

        assert graph.has_edge("repo/a.go::caller", "repo/a.go::helper")

        conn = psycopg2.connect(pg_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT fingerprint FROM cocoindex.call_graph_cache WHERE cache_key = %s", ("it-key",))
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_second_call_with_no_file_changes_returns_identical_graph_from_cache(self, pg_url, clean_cache_table, tmp_path):
        roots = _write_repo(tmp_path)
        first = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "it-key", pg_url)

        second = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "it-key", pg_url)

        assert set(second.nodes) == set(first.nodes)
        assert set(second.edges) == set(first.edges)

    def test_file_modification_produces_an_updated_graph_on_next_call(self, pg_url, clean_cache_table, tmp_path):
        roots = _write_repo(tmp_path)
        callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "it-key", pg_url)

        (tmp_path / "a.go").write_text(
            "func helper() int {\n    return 1\n}\n\nfunc caller() int {\n    return helper()\n}\n\nfunc newFn() {}\n"
        )
        updated = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "it-key", pg_url)

        assert "repo/a.go::newFn" in updated.nodes

    def test_unreachable_postgres_falls_back_to_uncached_rebuild(self, tmp_path):
        roots = _write_repo(tmp_path)
        bogus_url = "postgresql://nobody:nobody@127.0.0.1:1/does-not-exist"

        graph = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "it-key", bogus_url)

        assert graph.has_edge("repo/a.go::caller", "repo/a.go::helper")
