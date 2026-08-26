"""Tests for koku-cocoindex-search.py (engram.search.koku) -- the Koku code
search MCP server. See tests/test_dcm_cocoindex_search.py's module docstring
for the shared-logic rationale; koku.py differs from dcm.py in searching
Python (not Go) source and having no `repo=` scoping on pattern_search_code
(a single configured root, always searched in full).
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
    def test_item_present_in_both_lists_is_deduped_and_scores_combine(self, koku_search):
        dense = [{"id": "a"}, {"id": "b"}]
        bm25 = [{"id": "a"}, {"id": "c"}]
        fused = koku_search._rrf_fuse(dense, bm25, limit=10)
        ids = [r["id"] for r in fused]
        assert ids.count("a") == 1
        assert set(ids) == {"a", "b", "c"}
        assert ids[0] == "a"

    def test_limit_truncates_fused_results(self, koku_search):
        dense = [{"id": str(i)} for i in range(5)]
        assert len(koku_search._rrf_fuse(dense, [], limit=2)) == 2


class TestSearchCode:
    def _capture_queries(self, koku_search, monkeypatch):
        capture: list = []
        monkeypatch.setattr(koku_search, "_embed_query", lambda q: [0.0] * 4)
        import psycopg2
        monkeypatch.setattr(psycopg2, "connect", lambda url: _FakeConn(capture))
        return capture

    def test_hybrid_mode_queries_both_dense_and_bm25_against_koku_table(self, koku_search, monkeypatch):
        capture = self._capture_queries(koku_search, monkeypatch)
        koku_search.search_code("get_report", mode="hybrid")
        assert len(capture) == 2
        for sql, _params in capture:
            assert "cocoindex.koku_code_embeddings" in sql

    def test_dense_mode_only_queries_dense(self, koku_search, monkeypatch):
        capture = self._capture_queries(koku_search, monkeypatch)
        koku_search.search_code("get_report", mode="dense")
        assert len(capture) == 1
        assert "embedding <=>" in capture[0][0]

    def test_bm25_mode_only_queries_bm25(self, koku_search, monkeypatch):
        capture = self._capture_queries(koku_search, monkeypatch)
        koku_search.search_code("get_report", mode="bm25")
        assert len(capture) == 1
        assert "ts_rank_cd" in capture[0][0]

    def test_whitespace_only_query_in_bm25_mode_issues_no_query(self, koku_search, monkeypatch):
        capture = self._capture_queries(koku_search, monkeypatch)
        koku_search.search_code("   ", mode="bm25")
        assert capture == []

    def test_connection_is_always_closed(self, koku_search, monkeypatch):
        capture = self._capture_queries(koku_search, monkeypatch)
        closed = []
        import psycopg2

        class TrackedConn(_FakeConn):
            def close(self):
                closed.append(True)

        monkeypatch.setattr(psycopg2, "connect", lambda url: TrackedConn(capture))
        koku_search.search_code("x", mode="dense")
        assert closed == [True]


class TestFormatResults:
    def test_empty_results_returns_friendly_message(self, koku_search):
        msg = koku_search._format_results("get_report", [])
        assert "No code results found" in msg

    def test_long_code_is_truncated_with_total_length_noted(self, koku_search):
        long_code = "y" * 800
        out = koku_search._format_results("q", [
            {"filepath": "a.py", "score": 0.9, "code": long_code, "dense_score": 0.9},
        ])
        assert "... (800 chars total)" in out
        assert long_code not in out


class TestPatternSearchCode:
    PATTERN = r"def \NAME(\(A*\)) -> dict:"

    def test_searches_the_single_configured_root_in_full(self, koku_search, monkeypatch, tmp_path):
        (tmp_path / "report.py").write_text(
            "def get_report(account_id) -> dict:\n    return {}\n\n"
            "def unrelated():\n    pass\n"
        )
        monkeypatch.setattr(koku_search, "_PATTERN_SEARCH_ROOTS", [
            ("koku", tmp_path, ["**/*.py"], []),
        ])

        results = koku_search.pattern_search_code(self.PATTERN, "python")

        assert len(results) == 1
        assert results[0]["repo"] == "koku"
        assert "get_report" in results[0]["text"]

    def test_no_match_returns_empty_list_without_error(self, koku_search, monkeypatch, tmp_path):
        (tmp_path / "report.py").write_text("def totally_unrelated():\n    pass\n")
        monkeypatch.setattr(koku_search, "_PATTERN_SEARCH_ROOTS", [
            ("koku", tmp_path, ["**/*.py"], []),
        ])

        results = koku_search.pattern_search_code(self.PATTERN, "python")

        assert results == []

    def test_limit_is_respected_across_multiple_matches(self, koku_search, monkeypatch, tmp_path):
        (tmp_path / "report.py").write_text(
            "def get_a(x) -> dict:\n    return {}\n\ndef get_b(x) -> dict:\n    return {}\n"
        )
        monkeypatch.setattr(koku_search, "_PATTERN_SEARCH_ROOTS", [
            ("koku", tmp_path, ["**/*.py"], []),
        ])

        results = koku_search.pattern_search_code(self.PATTERN, "python", limit=1)

        assert len(results) == 1

    def test_non_python_files_in_the_root_are_skipped(self, koku_search, monkeypatch, tmp_path):
        (tmp_path / "report.py").write_text(
            "def get_report(account_id) -> dict:\n    return {}\n"
        )
        (tmp_path / "README.md").write_text("# not python source")
        monkeypatch.setattr(koku_search, "_PATTERN_SEARCH_ROOTS", [
            ("koku", tmp_path, ["**/*.py", "**/*.md"], []),
        ])

        results = koku_search.pattern_search_code(self.PATTERN, "python")

        assert len(results) == 1
        assert results[0]["filepath"].endswith("report.py")


class TestFormatPatternResults:
    def test_empty_results_returns_friendly_message(self, koku_search):
        msg = koku_search._format_pattern_results("pattern", "python", [])
        assert "No structural matches" in msg

    def test_results_include_filepath_line_and_text(self, koku_search):
        out = koku_search._format_pattern_results("pattern", "python", [
            {"filepath": "koku/report.py", "line": 3, "text": "def get_report(): ..."},
        ])
        assert "koku/report.py:3" in out
        assert "def get_report(): ..." in out


class TestMainRouting:
    def test_pattern_flag_takes_priority_over_query(self, koku_search, monkeypatch):
        calls = []
        monkeypatch.setattr(koku_search.sys, "argv", [
            "koku-cocoindex-search.py", "--pattern", "def \\NAME():", "--query", "ignored",
        ])
        monkeypatch.setattr(koku_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(koku_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(koku_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        koku_search.main()

        assert calls == ["pattern"]

    def test_query_flag_runs_cli_query_when_no_pattern_given(self, koku_search, monkeypatch):
        calls = []
        monkeypatch.setattr(koku_search.sys, "argv", ["koku-cocoindex-search.py", "--query", "get_report"])
        monkeypatch.setattr(koku_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(koku_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(koku_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        koku_search.main()

        assert calls == ["query"]

    def test_neither_flag_starts_the_mcp_server(self, koku_search, monkeypatch):
        calls = []
        monkeypatch.setattr(koku_search.sys, "argv", ["koku-cocoindex-search.py"])
        monkeypatch.setattr(koku_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(koku_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(koku_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        koku_search.main()

        assert calls == ["mcp"]

    def test_blast_radius_flag_dispatches_to_its_cli_runner(self, koku_search, monkeypatch):
        calls = []
        monkeypatch.setattr(koku_search.sys, "argv", [
            "koku-cocoindex-search.py", "--blast-radius", "get_report",
        ])
        monkeypatch.setattr(koku_search, "_run_cli_blast_radius", lambda *a, **k: calls.append(("blast", a, k)))
        monkeypatch.setattr(koku_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(koku_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        koku_search.main()

        assert len(calls) == 1
        assert calls[0][0] == "blast"
        assert calls[0][1] == ("get_report",)

    def test_shortest_path_flag_dispatches_with_both_positional_args(self, koku_search, monkeypatch):
        calls = []
        monkeypatch.setattr(koku_search.sys, "argv", [
            "koku-cocoindex-search.py", "--shortest-path", "a", "b",
        ])
        monkeypatch.setattr(koku_search, "_run_cli_shortest_path", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(koku_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        koku_search.main()

        assert calls == [("a", "b")]

    def test_cluster_flag_dispatches_to_its_cli_runner(self, koku_search, monkeypatch):
        calls = []
        monkeypatch.setattr(koku_search.sys, "argv", [
            "koku-cocoindex-search.py", "--cluster", "get_report",
        ])
        monkeypatch.setattr(koku_search, "_run_cli_cluster", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(koku_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        koku_search.main()

        assert calls == [("get_report",)]


class TestCallGraphWiring:
    """These exercise the koku.py <-> callgraph.py plumbing itself (root
    selection, delegation), not call-graph correctness -- that's already
    covered exhaustively in tests/test_callgraph.py against the shared
    implementation every org reuses."""

    def _write_sample_repo(self, tmp_path, monkeypatch, koku_search):
        (tmp_path / "report.py").write_text(
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def get_report():\n"
            "    return helper()\n"
        )
        monkeypatch.setattr(koku_search, "_PATTERN_SEARCH_ROOTS", [
            ("koku", tmp_path, ["**/*.py"], []),
        ])

    def test_blast_radius_builds_graph_from_the_configured_root(self, koku_search, monkeypatch, tmp_path):
        self._write_sample_repo(tmp_path, monkeypatch, koku_search)
        result = koku_search.call_graph_blast_radius("helper")
        assert result["function"] == "report.py::helper"
        assert result["callers_by_depth"] == [["report.py::get_report"]]

    def test_shortest_path_builds_graph_from_the_configured_root(self, koku_search, monkeypatch, tmp_path):
        self._write_sample_repo(tmp_path, monkeypatch, koku_search)
        result = koku_search.call_graph_shortest_path("get_report", "helper")
        assert result["path"] == ["report.py::get_report", "report.py::helper"]

    def test_get_cluster_builds_graph_from_the_configured_root(self, koku_search, monkeypatch, tmp_path):
        self._write_sample_repo(tmp_path, monkeypatch, koku_search)
        result = koku_search.call_graph_get_cluster("helper")
        assert set(result["members"]) == {"report.py::helper", "report.py::get_report"}

    def test_unknown_function_returns_error_dict(self, koku_search, monkeypatch, tmp_path):
        self._write_sample_repo(tmp_path, monkeypatch, koku_search)
        result = koku_search.call_graph_blast_radius("does_not_exist")
        assert "error" in result

    def test_format_functions_delegate_to_shared_callgraph_formatters(self, koku_search):
        error_result = {"error": "boom", "candidates": []}
        assert koku_search._format_blast_radius_result(error_result) == koku_search.callgraph.format_blast_radius_result(error_result)
        assert koku_search._format_shortest_path_result(error_result) == koku_search.callgraph.format_shortest_path_result(error_result)
        assert koku_search._format_cluster_result(error_result) == koku_search.callgraph.format_cluster_result(error_result)
