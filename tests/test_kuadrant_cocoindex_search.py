"""Tests for kuadrant-cocoindex-search.py (engram.search.kuadrant) -- the
Kuadrant code search MCP server. See tests/test_praxis_cocoindex_search.py's
module docstring for the shared-logic rationale; kuadrant.py differs from
every other org in mixing two languages (Go + Rust) across its 7 code
repos, so pattern_search_code/call_graph_* take an explicit `language` and
only search that language's repos (see _REPO_LANGUAGES).
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
    def test_item_present_in_both_lists_is_deduped_and_scores_combine(self, kuadrant_search):
        dense = [{"id": "a"}, {"id": "b"}]
        bm25 = [{"id": "a"}, {"id": "c"}]
        fused = kuadrant_search._rrf_fuse(dense, bm25, limit=10)
        ids = [r["id"] for r in fused]
        assert ids.count("a") == 1
        assert set(ids) == {"a", "b", "c"}
        assert ids[0] == "a"

    def test_limit_truncates_fused_results(self, kuadrant_search):
        dense = [{"id": str(i)} for i in range(5)]
        assert len(kuadrant_search._rrf_fuse(dense, [], limit=2)) == 2


class TestSearchCode:
    def _capture_queries(self, kuadrant_search, monkeypatch):
        capture: list = []
        monkeypatch.setattr(kuadrant_search, "_embed_query", lambda q: [0.0] * 4)
        import psycopg2
        monkeypatch.setattr(psycopg2, "connect", lambda url: _FakeConn(capture))
        return capture

    def test_hybrid_mode_queries_both_dense_and_bm25_against_kuadrant_table(self, kuadrant_search, monkeypatch):
        capture = self._capture_queries(kuadrant_search, monkeypatch)
        kuadrant_search.search_code("rate limiting", mode="hybrid")
        assert len(capture) == 2
        for sql, _params in capture:
            assert "cocoindex.kuadrant_code_embeddings" in sql

    def test_dense_mode_only_queries_dense(self, kuadrant_search, monkeypatch):
        capture = self._capture_queries(kuadrant_search, monkeypatch)
        kuadrant_search.search_code("rate limiting", mode="dense")
        assert len(capture) == 1
        assert "embedding <=>" in capture[0][0]

    def test_bm25_mode_only_queries_bm25(self, kuadrant_search, monkeypatch):
        capture = self._capture_queries(kuadrant_search, monkeypatch)
        kuadrant_search.search_code("rate limiting", mode="bm25")
        assert len(capture) == 1
        assert "ts_rank_cd" in capture[0][0]

    def test_whitespace_only_query_in_bm25_mode_issues_no_query(self, kuadrant_search, monkeypatch):
        capture = self._capture_queries(kuadrant_search, monkeypatch)
        kuadrant_search.search_code("   ", mode="bm25")
        assert capture == []

    def test_connection_is_always_closed(self, kuadrant_search, monkeypatch):
        capture = self._capture_queries(kuadrant_search, monkeypatch)
        closed = []
        import psycopg2

        class TrackedConn(_FakeConn):
            def close(self):
                closed.append(True)

        monkeypatch.setattr(psycopg2, "connect", lambda url: TrackedConn(capture))
        kuadrant_search.search_code("x", mode="dense")
        assert closed == [True]


class TestFormatResults:
    def test_empty_results_returns_friendly_message(self, kuadrant_search):
        assert "No code results found" in kuadrant_search._format_results("rate limiting", [])

    def test_long_code_is_truncated_with_total_length_noted(self, kuadrant_search):
        long_code = "z" * 600
        out = kuadrant_search._format_results("q", [
            {"filepath": "a.go", "score": 0.9, "code": long_code, "dense_score": 0.9},
        ])
        assert "... (600 chars total)" in out
        assert long_code not in out


class TestPatternSearchCode:
    GO_PATTERN = r"func \NAME(\(A*\)) error"

    def test_only_matching_language_repos_are_searched(self, kuadrant_search, monkeypatch, tmp_path):
        """kuadrant-operator is go, limitador is rust (real _REPO_LANGUAGES
        entries) -- a language="go" pattern search must not touch
        limitador's root at all."""
        go_repo = tmp_path / "kuadrant-operator"
        rust_repo = tmp_path / "limitador"
        go_repo.mkdir()
        rust_repo.mkdir()
        (go_repo / "main.go").write_text("func Reconcile() error {\n\treturn nil\n}\n")
        (rust_repo / "lib.rs").write_text("fn reconcile() {}\n")
        monkeypatch.setattr(kuadrant_search, "_PATTERN_SEARCH_ROOTS", [
            ("kuadrant-operator", go_repo, ["**/*.go"], ["**/vendor/**"]),
            ("limitador", rust_repo, ["**/*.rs"], ["**/target/**"]),
        ])

        results = kuadrant_search.pattern_search_code(self.GO_PATTERN, "go")

        assert {r["repo"] for r in results} == {"kuadrant-operator"}

    def test_no_match_returns_empty_list_without_error(self, kuadrant_search, monkeypatch, tmp_path):
        repo = tmp_path / "kuadrant-operator"
        repo.mkdir()
        (repo / "main.go").write_text("func unrelated() {}\n")
        monkeypatch.setattr(kuadrant_search, "_PATTERN_SEARCH_ROOTS", [
            ("kuadrant-operator", repo, ["**/*.go"], ["**/vendor/**"]),
        ])

        assert kuadrant_search.pattern_search_code(self.GO_PATTERN, "go") == []

    def test_vendor_directory_is_excluded(self, kuadrant_search, monkeypatch, tmp_path):
        repo = tmp_path / "kuadrant-operator"
        repo.mkdir()
        (repo / "main.go").write_text("func Reconcile() error {\n\treturn nil\n}\n")
        vendor_dir = repo / "vendor" / "pkg"
        vendor_dir.mkdir(parents=True)
        (vendor_dir / "generated.go").write_text("func Reconcile() error {\n\treturn nil\n}\n")
        monkeypatch.setattr(kuadrant_search, "_PATTERN_SEARCH_ROOTS", [
            ("kuadrant-operator", repo, ["**/*.go"], ["**/vendor/**"]),
        ])

        results = kuadrant_search.pattern_search_code(self.GO_PATTERN, "go")

        assert len(results) == 1
        assert "vendor" not in results[0]["filepath"]


class TestFormatPatternResults:
    def test_empty_results_returns_friendly_message(self, kuadrant_search):
        assert "No structural matches" in kuadrant_search._format_pattern_results("pattern", "go", [])

    def test_results_include_filepath_line_and_text(self, kuadrant_search):
        out = kuadrant_search._format_pattern_results("pattern", "go", [
            {"filepath": "kuadrant-operator/main.go", "line": 5, "text": "func Reconcile() error { ... }"},
        ])
        assert "kuadrant-operator/main.go:5" in out
        assert "func Reconcile() error { ... }" in out


class TestCallGraphWiring:
    """Exercises the kuadrant.py <-> callgraph.py plumbing (per-language
    root selection), not call-graph correctness itself (covered in
    tests/test_callgraph.py). Unlike praxis.py (single language, all roots
    always searched), kuadrant.py's _roots_for_language() must filter to
    only the requested language's repos before delegating."""

    def _write_repo_roots(self, tmp_path, monkeypatch, kuadrant_search):
        go_repo = tmp_path / "kuadrant-operator"
        rust_repo = tmp_path / "limitador"
        go_repo.mkdir()
        rust_repo.mkdir()
        (go_repo / "main.go").write_text(
            "func helper() int {\n\treturn 1\n}\n\nfunc getScore() int {\n\treturn helper()\n}\n"
        )
        (rust_repo / "lib.rs").write_text("fn helper() -> i32 {\n    2\n}\n")
        monkeypatch.setattr(kuadrant_search, "_PATTERN_SEARCH_ROOTS", [
            ("kuadrant-operator", go_repo, ["**/*.go"], ["**/vendor/**"]),
            ("limitador", rust_repo, ["**/*.rs"], ["**/target/**"]),
        ])
        monkeypatch.setattr(kuadrant_search, "_REPO_LANGUAGES", {
            "kuadrant-operator": "go",
            "limitador": "rust",
        })

    def test_blast_radius_only_uses_matching_language_roots(self, kuadrant_search, monkeypatch, tmp_path):
        self._write_repo_roots(tmp_path, monkeypatch, kuadrant_search)
        result = kuadrant_search.call_graph_blast_radius("kuadrant-operator/main.go::helper", language="go")
        assert result["function"] == "kuadrant-operator/main.go::helper"
        assert result["callers_by_depth"] == [["kuadrant-operator/main.go::getScore"]]

    def test_rust_repo_function_is_not_visible_when_scoped_to_go(self, kuadrant_search, monkeypatch, tmp_path):
        self._write_repo_roots(tmp_path, monkeypatch, kuadrant_search)
        result = kuadrant_search.call_graph_blast_radius("limitador/lib.rs::helper", language="go")
        assert "error" in result

    def test_format_functions_delegate_to_shared_callgraph_formatters(self, kuadrant_search):
        error_result = {"error": "boom", "candidates": []}
        assert kuadrant_search._format_blast_radius_result(error_result) == kuadrant_search.callgraph.format_blast_radius_result(error_result)
        assert kuadrant_search._format_shortest_path_result(error_result) == kuadrant_search.callgraph.format_shortest_path_result(error_result)
        assert kuadrant_search._format_cluster_result(error_result) == kuadrant_search.callgraph.format_cluster_result(error_result)


class TestMainRouting:
    def test_pattern_flag_takes_priority_over_query(self, kuadrant_search, monkeypatch):
        calls = []
        monkeypatch.setattr(kuadrant_search.sys, "argv", [
            "kuadrant-cocoindex-search.py", "--pattern", "func \\NAME()", "--query", "ignored",
        ])
        monkeypatch.setattr(kuadrant_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(kuadrant_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(kuadrant_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        kuadrant_search.main()

        assert calls == ["pattern"]

    def test_query_flag_runs_cli_query_when_no_pattern_given(self, kuadrant_search, monkeypatch):
        calls = []
        monkeypatch.setattr(kuadrant_search.sys, "argv", ["kuadrant-cocoindex-search.py", "--query", "rate limiting"])
        monkeypatch.setattr(kuadrant_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(kuadrant_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(kuadrant_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        kuadrant_search.main()

        assert calls == ["query"]

    def test_neither_flag_starts_the_mcp_server(self, kuadrant_search, monkeypatch):
        calls = []
        monkeypatch.setattr(kuadrant_search.sys, "argv", ["kuadrant-cocoindex-search.py"])
        monkeypatch.setattr(kuadrant_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(kuadrant_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(kuadrant_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        kuadrant_search.main()

        assert calls == ["mcp"]


class TestRunMcpServerBuildsARealServer:
    """2026-08-27: mcp==2.0.0 (2026-08-22 dependabot bump) renamed
    `mcp.server.FastMCP` -> `mcp.server.mcpserver.MCPServer` and moved
    host/port from the constructor to run(). See
    tests/test_praxis_cocoindex_search.py's identical class docstring for
    the full incident writeup and docs/findings/2026-08.md's 2026-08-27
    entry."""

    def test_run_mcp_server_stdio_does_not_raise(self, kuadrant_search, monkeypatch):
        from mcp.server.mcpserver import MCPServer

        monkeypatch.setattr(MCPServer, "run", lambda self, *a, **k: None)

        kuadrant_search._run_mcp_server(transport="stdio")
