"""Tests for praxis-cocoindex-search.py (engram.search.praxis) -- the Praxis
code search MCP server. See tests/test_dcm_cocoindex_search.py's module
docstring for the shared-logic rationale; praxis.py differs from dcm.py/
koku.py in searching Rust source across seven separately-configured repo
roots (_RUST_REPOS), with no `repo=` scoping param on pattern_search_code
(all seven are always searched, same as koku.py's single root).
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
    def test_item_present_in_both_lists_is_deduped_and_scores_combine(self, praxis_search):
        dense = [{"id": "a"}, {"id": "b"}]
        bm25 = [{"id": "a"}, {"id": "c"}]
        fused = praxis_search._rrf_fuse(dense, bm25, limit=10)
        ids = [r["id"] for r in fused]
        assert ids.count("a") == 1
        assert set(ids) == {"a", "b", "c"}
        assert ids[0] == "a"

    def test_limit_truncates_fused_results(self, praxis_search):
        dense = [{"id": str(i)} for i in range(5)]
        assert len(praxis_search._rrf_fuse(dense, [], limit=2)) == 2


class TestSearchCode:
    def _capture_queries(self, praxis_search, monkeypatch):
        capture: list = []
        monkeypatch.setattr(praxis_search, "_embed_query", lambda q: [0.0] * 4)
        import psycopg2
        monkeypatch.setattr(psycopg2, "connect", lambda url: _FakeConn(capture))
        return capture

    def test_hybrid_mode_queries_both_dense_and_bm25_against_praxis_table(self, praxis_search, monkeypatch):
        capture = self._capture_queries(praxis_search, monkeypatch)
        praxis_search.search_code("score_backends", mode="hybrid")
        assert len(capture) == 2
        for sql, _params in capture:
            assert "cocoindex.praxis_code_embeddings" in sql

    def test_dense_mode_only_queries_dense(self, praxis_search, monkeypatch):
        capture = self._capture_queries(praxis_search, monkeypatch)
        praxis_search.search_code("score_backends", mode="dense")
        assert len(capture) == 1
        assert "embedding <=>" in capture[0][0]

    def test_bm25_mode_only_queries_bm25(self, praxis_search, monkeypatch):
        capture = self._capture_queries(praxis_search, monkeypatch)
        praxis_search.search_code("score_backends", mode="bm25")
        assert len(capture) == 1
        assert "ts_rank_cd" in capture[0][0]

    def test_whitespace_only_query_in_bm25_mode_issues_no_query(self, praxis_search, monkeypatch):
        capture = self._capture_queries(praxis_search, monkeypatch)
        praxis_search.search_code("   ", mode="bm25")
        assert capture == []

    def test_connection_is_always_closed(self, praxis_search, monkeypatch):
        capture = self._capture_queries(praxis_search, monkeypatch)
        closed = []
        import psycopg2

        class TrackedConn(_FakeConn):
            def close(self):
                closed.append(True)

        monkeypatch.setattr(psycopg2, "connect", lambda url: TrackedConn(capture))
        praxis_search.search_code("x", mode="dense")
        assert closed == [True]


class TestFormatResults:
    def test_empty_results_returns_friendly_message(self, praxis_search):
        assert "No code results found" in praxis_search._format_results("score_backends", [])

    def test_long_code_is_truncated_with_total_length_noted(self, praxis_search):
        long_code = "z" * 600
        out = praxis_search._format_results("q", [
            {"filepath": "a.rs", "score": 0.9, "code": long_code, "dense_score": 0.9},
        ])
        assert "... (600 chars total)" in out
        assert long_code not in out


class TestPatternSearchCode:
    PATTERN = r"fn \NAME(\(A*\)) -> u32"

    def test_multiple_repo_roots_are_all_searched(self, praxis_search, monkeypatch, tmp_path):
        repo_a = tmp_path / "praxis"
        repo_b = tmp_path / "praxis-ai"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "lib.rs").write_text("fn score_backends(x: u32) -> u32 {\n    x\n}\n")
        (repo_b / "lib.rs").write_text("fn score_routes(x: u32) -> u32 {\n    x\n}\n")
        monkeypatch.setattr(praxis_search, "_PATTERN_SEARCH_ROOTS", [
            ("praxis", repo_a, ["**/*.rs"], ["**/target/**"]),
            ("praxis-ai", repo_b, ["**/*.rs"], ["**/target/**"]),
        ])

        results = praxis_search.pattern_search_code(self.PATTERN, "rust")

        assert {r["repo"] for r in results} == {"praxis", "praxis-ai"}

    def test_no_match_returns_empty_list_without_error(self, praxis_search, monkeypatch, tmp_path):
        repo = tmp_path / "praxis"
        repo.mkdir()
        (repo / "lib.rs").write_text("fn unrelated() {\n}\n")
        monkeypatch.setattr(praxis_search, "_PATTERN_SEARCH_ROOTS", [
            ("praxis", repo, ["**/*.rs"], ["**/target/**"]),
        ])

        assert praxis_search.pattern_search_code(self.PATTERN, "rust") == []

    def test_limit_is_respected_across_repos(self, praxis_search, monkeypatch, tmp_path):
        repo_a = tmp_path / "praxis"
        repo_b = tmp_path / "praxis-ai"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "lib.rs").write_text("fn score_backends(x: u32) -> u32 {\n    x\n}\n")
        (repo_b / "lib.rs").write_text("fn score_routes(x: u32) -> u32 {\n    x\n}\n")
        monkeypatch.setattr(praxis_search, "_PATTERN_SEARCH_ROOTS", [
            ("praxis", repo_a, ["**/*.rs"], ["**/target/**"]),
            ("praxis-ai", repo_b, ["**/*.rs"], ["**/target/**"]),
        ])

        results = praxis_search.pattern_search_code(self.PATTERN, "rust", limit=1)

        assert len(results) == 1

    def test_non_rust_files_are_skipped(self, praxis_search, monkeypatch, tmp_path):
        repo = tmp_path / "praxis"
        repo.mkdir()
        (repo / "lib.rs").write_text("fn score_backends(x: u32) -> u32 {\n    x\n}\n")
        (repo / "README.md").write_text("# not rust")
        monkeypatch.setattr(praxis_search, "_PATTERN_SEARCH_ROOTS", [
            ("praxis", repo, ["**/*.rs", "**/*.md"], ["**/target/**"]),
        ])

        results = praxis_search.pattern_search_code(self.PATTERN, "rust")

        assert len(results) == 1
        assert results[0]["filepath"].endswith("lib.rs")

    def test_target_directory_is_excluded(self, praxis_search, monkeypatch, tmp_path):
        repo = tmp_path / "praxis"
        repo.mkdir()
        (repo / "lib.rs").write_text("fn score_backends(x: u32) -> u32 {\n    x\n}\n")
        target_dir = repo / "target" / "debug"
        target_dir.mkdir(parents=True)
        (target_dir / "generated.rs").write_text("fn score_backends(x: u32) -> u32 {\n    x\n}\n")
        monkeypatch.setattr(praxis_search, "_PATTERN_SEARCH_ROOTS", [
            ("praxis", repo, ["**/*.rs"], ["**/target/**"]),
        ])

        results = praxis_search.pattern_search_code(self.PATTERN, "rust")

        assert len(results) == 1
        assert "target" not in results[0]["filepath"]


class TestFormatPatternResults:
    def test_empty_results_returns_friendly_message(self, praxis_search):
        assert "No structural matches" in praxis_search._format_pattern_results("pattern", "rust", [])

    def test_results_include_filepath_line_and_text(self, praxis_search):
        out = praxis_search._format_pattern_results("pattern", "rust", [
            {"filepath": "praxis/lib.rs", "line": 5, "text": "fn score_backends() -> u32 { ... }"},
        ])
        assert "praxis/lib.rs:5" in out
        assert "fn score_backends() -> u32 { ... }" in out


class TestCallGraphWiring:
    """These exercise the praxis.py <-> callgraph.py plumbing itself (root
    selection, multi-repo delegation), not call-graph correctness -- that's
    already covered exhaustively in tests/test_callgraph.py against the
    shared implementation every org reuses. Praxis differs from koku.py/
    rhdh_plugins.py (single root each) in searching all seven
    _PATTERN_SEARCH_ROOTS repos in one call via
    callgraph.build_multi_repo_call_graph_with_stats, so these also pin down
    the repo-tag-prefixed qualified names and per-repo-only resolution that
    implies."""

    def _write_two_repo_roots(self, tmp_path, monkeypatch, praxis_search):
        repo_a = tmp_path / "praxis"
        repo_b = tmp_path / "praxis-ai"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "lib.rs").write_text(
            "fn helper() -> i32 {\n    1\n}\n\nfn get_score() -> i32 {\n    helper()\n}\n"
        )
        (repo_b / "lib.rs").write_text(
            "fn helper() -> i32 {\n    2\n}\n\nfn caller_b() -> i32 {\n    unknown_in_b()\n}\n"
        )
        monkeypatch.setattr(praxis_search, "_PATTERN_SEARCH_ROOTS", [
            ("praxis", repo_a, ["**/*.rs"], ["**/target/**"]),
            ("praxis-ai", repo_b, ["**/*.rs"], ["**/target/**"]),
        ])

    def test_blast_radius_builds_graph_from_all_configured_roots(self, praxis_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, praxis_search)
        result = praxis_search.call_graph_blast_radius("praxis/lib.rs::helper")
        assert result["function"] == "praxis/lib.rs::helper"
        assert result["callers_by_depth"] == [["praxis/lib.rs::get_score"]]

    def test_same_named_function_in_two_repos_is_ambiguous_by_bare_name(self, praxis_search, monkeypatch, tmp_path):
        """Both repos define `helper` -- a bare-name lookup across all seven
        roots must report it as ambiguous (candidates from both repos)
        rather than silently picking one, mirroring resolve_node's existing
        multi-candidate behavior within a single repo."""
        self._write_two_repo_roots(tmp_path, monkeypatch, praxis_search)
        result = praxis_search.call_graph_blast_radius("helper")
        assert "error" in result
        assert set(result["candidates"]) == {"praxis/lib.rs::helper", "praxis-ai/lib.rs::helper"}

    def test_shortest_path_builds_graph_from_all_configured_roots(self, praxis_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, praxis_search)
        result = praxis_search.call_graph_shortest_path("praxis/lib.rs::get_score", "praxis/lib.rs::helper")
        assert result["path"] == ["praxis/lib.rs::get_score", "praxis/lib.rs::helper"]

    def test_get_cluster_builds_graph_from_all_configured_roots(self, praxis_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, praxis_search)
        result = praxis_search.call_graph_get_cluster("praxis/lib.rs::helper")
        assert set(result["members"]) == {"praxis/lib.rs::helper", "praxis/lib.rs::get_score"}

    def test_call_does_not_resolve_across_repo_boundary(self, praxis_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, praxis_search)
        result = praxis_search.call_graph_blast_radius("praxis-ai/lib.rs::caller_b")
        assert result["callers_by_depth"] == []
        assert result["unresolved_calls"] >= 1

    def test_unknown_function_returns_error_dict(self, praxis_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, praxis_search)
        result = praxis_search.call_graph_blast_radius("does_not_exist")
        assert "error" in result

    def test_format_functions_delegate_to_shared_callgraph_formatters(self, praxis_search):
        error_result = {"error": "boom", "candidates": []}
        assert praxis_search._format_blast_radius_result(error_result) == praxis_search.callgraph.format_blast_radius_result(error_result)
        assert praxis_search._format_shortest_path_result(error_result) == praxis_search.callgraph.format_shortest_path_result(error_result)
        assert praxis_search._format_cluster_result(error_result) == praxis_search.callgraph.format_cluster_result(error_result)


class TestMainRouting:
    def test_pattern_flag_takes_priority_over_query(self, praxis_search, monkeypatch):
        calls = []
        monkeypatch.setattr(praxis_search.sys, "argv", [
            "praxis-cocoindex-search.py", "--pattern", "fn \\NAME()", "--query", "ignored",
        ])
        monkeypatch.setattr(praxis_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(praxis_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(praxis_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        praxis_search.main()

        assert calls == ["pattern"]

    def test_query_flag_runs_cli_query_when_no_pattern_given(self, praxis_search, monkeypatch):
        calls = []
        monkeypatch.setattr(praxis_search.sys, "argv", ["praxis-cocoindex-search.py", "--query", "score_backends"])
        monkeypatch.setattr(praxis_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(praxis_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(praxis_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        praxis_search.main()

        assert calls == ["query"]

    def test_neither_flag_starts_the_mcp_server(self, praxis_search, monkeypatch):
        calls = []
        monkeypatch.setattr(praxis_search.sys, "argv", ["praxis-cocoindex-search.py"])
        monkeypatch.setattr(praxis_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(praxis_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(praxis_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        praxis_search.main()

        assert calls == ["mcp"]


class TestRunMcpServerBuildsARealServer:
    """2026-08-27: mcp==2.0.0 (2026-08-22 dependabot bump) renamed
    `mcp.server.FastMCP` -> `mcp.server.mcpserver.MCPServer` and moved
    host/port from the constructor to run(). Every existing
    TestMainRouting test above monkeypatches `_run_mcp_server` away
    entirely, so none of them exercised the real import/construction path
    -- the regression shipped silently for 5 days and broke every
    onboarded org's code-search backend. This test calls the real
    function (only `MCPServer.run` itself is stubbed, to avoid blocking
    on stdio) so a future SDK rename gets caught here instead of in
    production. See docs/findings/2026-08.md's 2026-08-27 entry."""

    def test_run_mcp_server_stdio_does_not_raise(self, praxis_search, monkeypatch):
        from mcp.server.mcpserver import MCPServer

        monkeypatch.setattr(MCPServer, "run", lambda self, *a, **k: None)

        praxis_search._run_mcp_server(transport="stdio")
