"""Tests for engram-cocoindex-search.py (engram.search.engram) -- the
Engram-on-itself code search MCP server. See
tests/test_dcm_cocoindex_search.py's module docstring for the shared-logic
rationale; engram.py's shape matches koku.py exactly (single configured
Python root, no `repo=` scoping on pattern_search_code).
"""
from __future__ import annotations


class _FakeCursor:
    def __init__(self, capture: list):
        self._capture = capture

    def execute(self, sql, params):
        self._capture.append((sql, params))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, capture: list):
        self._capture = capture

    def cursor(self):
        return _FakeCursor(self._capture)

    def close(self):
        pass


class TestRrfFuse:
    def test_item_present_in_both_lists_is_deduped_and_scores_combine(self, engram_search):
        dense = [{"id": "a"}, {"id": "b"}]
        bm25 = [{"id": "a"}, {"id": "c"}]
        fused = engram_search._rrf_fuse(dense, bm25, limit=10)
        ids = [r["id"] for r in fused]
        assert ids.count("a") == 1
        assert set(ids) == {"a", "b", "c"}
        assert ids[0] == "a"

    def test_limit_truncates_fused_results(self, engram_search):
        dense = [{"id": str(i)} for i in range(5)]
        assert len(engram_search._rrf_fuse(dense, [], limit=2)) == 2


class TestSearchCode:
    def _capture_queries(self, engram_search, monkeypatch):
        capture: list = []
        monkeypatch.setattr(engram_search, "_embed_query", lambda q: [0.0] * 4)
        import psycopg2
        monkeypatch.setattr(psycopg2, "connect", lambda url: _FakeConn(capture))
        return capture

    def test_hybrid_mode_queries_both_dense_and_bm25_against_engram_table(self, engram_search, monkeypatch):
        capture = self._capture_queries(engram_search, monkeypatch)
        engram_search.search_code("hindsight_retain", mode="hybrid")
        assert len(capture) == 2
        for sql, _params in capture:
            assert "cocoindex.engram_code_embeddings" in sql

    def test_dense_mode_only_queries_dense(self, engram_search, monkeypatch):
        capture = self._capture_queries(engram_search, monkeypatch)
        engram_search.search_code("hindsight_retain", mode="dense")
        assert len(capture) == 1
        assert "embedding <=>" in capture[0][0]

    def test_bm25_mode_only_queries_bm25(self, engram_search, monkeypatch):
        capture = self._capture_queries(engram_search, monkeypatch)
        engram_search.search_code("hindsight_retain", mode="bm25")
        assert len(capture) == 1
        assert "ts_rank_cd" in capture[0][0]

    def test_whitespace_only_query_in_bm25_mode_issues_no_query(self, engram_search, monkeypatch):
        capture = self._capture_queries(engram_search, monkeypatch)
        engram_search.search_code("   ", mode="bm25")
        assert capture == []

    def test_connection_is_always_closed(self, engram_search, monkeypatch):
        capture = self._capture_queries(engram_search, monkeypatch)
        closed = []
        import psycopg2

        class TrackedConn(_FakeConn):
            def close(self):
                closed.append(True)

        monkeypatch.setattr(psycopg2, "connect", lambda url: TrackedConn(capture))
        engram_search.search_code("x", mode="dense")
        assert closed == [True]


class TestFormatResults:
    def test_empty_results_returns_friendly_message(self, engram_search):
        assert "No code results found" in engram_search._format_results("hindsight_retain", [])

    def test_long_code_is_truncated_with_total_length_noted(self, engram_search):
        long_code = "w" * 700
        out = engram_search._format_results("q", [
            {"filepath": "a.py", "score": 0.9, "code": long_code, "dense_score": 0.9},
        ])
        assert "... (700 chars total)" in out
        assert long_code not in out


class TestPatternSearchCode:
    PATTERN = r"def \NAME(\(A*\)) -> dict:"

    def test_searches_the_single_configured_root_in_full(self, engram_search, monkeypatch, tmp_path):
        (tmp_path / "hindsight_client.py").write_text(
            "def hindsight_retain(bank_id) -> dict:\n    return {}\n\n"
            "def unrelated():\n    pass\n"
        )
        monkeypatch.setattr(engram_search, "_PATTERN_SEARCH_ROOTS", [
            ("engram", tmp_path, ["**/*.py"], []),
        ])

        results = engram_search.pattern_search_code(self.PATTERN, "python")

        assert len(results) == 1
        assert results[0]["repo"] == "engram"
        assert "hindsight_retain" in results[0]["text"]

    def test_no_match_returns_empty_list_without_error(self, engram_search, monkeypatch, tmp_path):
        (tmp_path / "hindsight_client.py").write_text("def totally_unrelated():\n    pass\n")
        monkeypatch.setattr(engram_search, "_PATTERN_SEARCH_ROOTS", [
            ("engram", tmp_path, ["**/*.py"], []),
        ])

        assert engram_search.pattern_search_code(self.PATTERN, "python") == []

    def test_limit_is_respected_across_multiple_matches(self, engram_search, monkeypatch, tmp_path):
        (tmp_path / "hindsight_client.py").write_text(
            "def get_a(x) -> dict:\n    return {}\n\ndef get_b(x) -> dict:\n    return {}\n"
        )
        monkeypatch.setattr(engram_search, "_PATTERN_SEARCH_ROOTS", [
            ("engram", tmp_path, ["**/*.py"], []),
        ])

        results = engram_search.pattern_search_code(self.PATTERN, "python", limit=1)

        assert len(results) == 1

    def test_non_python_files_are_skipped(self, engram_search, monkeypatch, tmp_path):
        (tmp_path / "hindsight_client.py").write_text(
            "def hindsight_retain(bank_id) -> dict:\n    return {}\n"
        )
        (tmp_path / "README.md").write_text("# not python")
        monkeypatch.setattr(engram_search, "_PATTERN_SEARCH_ROOTS", [
            ("engram", tmp_path, ["**/*.py", "**/*.md"], []),
        ])

        results = engram_search.pattern_search_code(self.PATTERN, "python")

        assert len(results) == 1
        assert results[0]["filepath"].endswith("hindsight_client.py")


class TestFormatPatternResults:
    def test_empty_results_returns_friendly_message(self, engram_search):
        assert "No structural matches" in engram_search._format_pattern_results("pattern", "python", [])

    def test_results_include_filepath_line_and_text(self, engram_search):
        out = engram_search._format_pattern_results("pattern", "python", [
            {"filepath": "engram/hindsight_client.py", "line": 3, "text": "def hindsight_retain(): ..."},
        ])
        assert "engram/hindsight_client.py:3" in out
        assert "def hindsight_retain(): ..." in out


class TestMainRouting:
    def test_pattern_flag_takes_priority_over_query(self, engram_search, monkeypatch):
        calls = []
        monkeypatch.setattr(engram_search.sys, "argv", [
            "engram-cocoindex-search.py", "--pattern", "def \\NAME():", "--query", "ignored",
        ])
        monkeypatch.setattr(engram_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(engram_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(engram_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        engram_search.main()

        assert calls == ["pattern"]

    def test_query_flag_runs_cli_query_when_no_pattern_given(self, engram_search, monkeypatch):
        calls = []
        monkeypatch.setattr(engram_search.sys, "argv", ["engram-cocoindex-search.py", "--query", "hindsight_retain"])
        monkeypatch.setattr(engram_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(engram_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(engram_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        engram_search.main()

        assert calls == ["query"]

    def test_neither_flag_starts_the_mcp_server(self, engram_search, monkeypatch):
        calls = []
        monkeypatch.setattr(engram_search.sys, "argv", ["engram-cocoindex-search.py"])
        monkeypatch.setattr(engram_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(engram_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(engram_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        engram_search.main()

        assert calls == ["mcp"]


class TestRunMcpServerBuildsARealServer:
    """2026-08-27: mcp==2.0.0 (2026-08-22 dependabot bump) renamed
    `mcp.server.FastMCP` -> `mcp.server.mcpserver.MCPServer` and moved
    host/port from the constructor to run(). See
    tests/test_praxis_cocoindex_search.py's identical class docstring for
    the full incident writeup and docs/findings/2026-08.md's 2026-08-27
    entry."""

    def test_run_mcp_server_stdio_does_not_raise(self, engram_search, monkeypatch):
        from mcp.server.mcpserver import MCPServer

        monkeypatch.setattr(MCPServer, "run", lambda self, *a, **k: None)

        engram_search._run_mcp_server(transport="stdio")
