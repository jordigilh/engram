"""Tests for rhdh-plugins-cocoindex-search.py (engram.search.rhdh_plugins) --
the rhdh-plugins code search MCP server. See tests/test_koku_cocoindex_search.py's
module docstring for the shared-logic rationale; rhdh_plugins.py differs from
koku.py in searching TypeScript (not Python) source, scoping to
workspaces/boost/ only (not the full monorepo -- see rhdh_plugins.py's
docstring), and searching `.ts`/`.tsx` files that need two different
tree-sitter grammars for one "typescript" language config (see
engram/callgraph.py's `_grammar_for_path`, docs/CALL_GRAPH_CLUSTERING.md
2026-08-24 Phase 2 entry).
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
    def test_item_present_in_both_lists_is_deduped_and_scores_combine(self, rhdh_plugins_search):
        dense = [{"id": "a"}, {"id": "b"}]
        bm25 = [{"id": "a"}, {"id": "c"}]
        fused = rhdh_plugins_search._rrf_fuse(dense, bm25, limit=10)
        ids = [r["id"] for r in fused]
        assert ids.count("a") == 1
        assert set(ids) == {"a", "b", "c"}
        assert ids[0] == "a"

    def test_limit_truncates_fused_results(self, rhdh_plugins_search):
        dense = [{"id": str(i)} for i in range(5)]
        assert len(rhdh_plugins_search._rrf_fuse(dense, [], limit=2)) == 2


class TestSearchCode:
    def _capture_queries(self, rhdh_plugins_search, monkeypatch):
        capture: list = []
        monkeypatch.setattr(rhdh_plugins_search, "_embed_query", lambda q: [0.0] * 4)
        import psycopg2
        monkeypatch.setattr(psycopg2, "connect", lambda url: _FakeConn(capture))
        return capture

    def test_hybrid_mode_queries_both_dense_and_bm25_against_rhdh_plugins_table(self, rhdh_plugins_search, monkeypatch):
        capture = self._capture_queries(rhdh_plugins_search, monkeypatch)
        rhdh_plugins_search.search_code("graduated visibility", mode="hybrid")
        assert len(capture) == 2
        for sql, _params in capture:
            assert "cocoindex.rhdh_plugins_code_embeddings" in sql

    def test_dense_mode_only_queries_dense(self, rhdh_plugins_search, monkeypatch):
        capture = self._capture_queries(rhdh_plugins_search, monkeypatch)
        rhdh_plugins_search.search_code("graduated visibility", mode="dense")
        assert len(capture) == 1
        assert "embedding <=>" in capture[0][0]

    def test_bm25_mode_only_queries_bm25(self, rhdh_plugins_search, monkeypatch):
        capture = self._capture_queries(rhdh_plugins_search, monkeypatch)
        rhdh_plugins_search.search_code("graduated visibility", mode="bm25")
        assert len(capture) == 1
        assert "ts_rank_cd" in capture[0][0]

    def test_whitespace_only_query_in_bm25_mode_issues_no_query(self, rhdh_plugins_search, monkeypatch):
        capture = self._capture_queries(rhdh_plugins_search, monkeypatch)
        rhdh_plugins_search.search_code("   ", mode="bm25")
        assert capture == []

    def test_connection_is_always_closed(self, rhdh_plugins_search, monkeypatch):
        capture = self._capture_queries(rhdh_plugins_search, monkeypatch)
        closed = []
        import psycopg2

        class TrackedConn(_FakeConn):
            def close(self):
                closed.append(True)

        monkeypatch.setattr(psycopg2, "connect", lambda url: TrackedConn(capture))
        rhdh_plugins_search.search_code("x", mode="dense")
        assert closed == [True]


class TestFormatResults:
    def test_empty_results_returns_friendly_message(self, rhdh_plugins_search):
        msg = rhdh_plugins_search._format_results("graduated visibility", [])
        assert "No code results found" in msg

    def test_long_code_is_truncated_with_total_length_noted(self, rhdh_plugins_search):
        long_code = "y" * 800
        out = rhdh_plugins_search._format_results("q", [
            {"filepath": "a.tsx", "score": 0.9, "code": long_code, "dense_score": 0.9},
        ])
        assert "... (800 chars total)" in out
        assert long_code not in out


class TestPatternSearchCode:
    PATTERN = r"function \NAME(\(A*\))"
    # `.tsx` fixtures must be searched with language="tsx", not "typescript"
    # -- see engram/callgraph.py's `_grammar_for_path` docstring and
    # docs/CALL_GRAPH_CLUSTERING.md's 2026-08-24 Phase 2 entry.
    # pattern_search_code()'s own per-file filter
    # (`detect_code_language(filename=path.name) != language`) means passing
    # "typescript" here would silently skip every `.tsx` fixture below.
    LANGUAGE = "tsx"

    def test_searches_the_single_configured_root_in_full(self, rhdh_plugins_search, monkeypatch, tmp_path):
        (tmp_path / "widget.tsx").write_text(
            "function getWidget(id) {\n  return id;\n}\n\n"
            "function unrelated() {}\n"
        )
        monkeypatch.setattr(rhdh_plugins_search, "_PATTERN_SEARCH_ROOTS", [
            ("rhdh-plugins-boost", tmp_path, ["**/*.tsx"], []),
        ])

        results = rhdh_plugins_search.pattern_search_code(self.PATTERN, self.LANGUAGE)

        assert len(results) == 2
        assert results[0]["repo"] == "rhdh-plugins-boost"

    def test_no_match_returns_empty_list_without_error(self, rhdh_plugins_search, monkeypatch, tmp_path):
        (tmp_path / "widget.tsx").write_text("const x = 1;\n")
        monkeypatch.setattr(rhdh_plugins_search, "_PATTERN_SEARCH_ROOTS", [
            ("rhdh-plugins-boost", tmp_path, ["**/*.tsx"], []),
        ])

        results = rhdh_plugins_search.pattern_search_code(self.PATTERN, self.LANGUAGE)

        assert results == []

    def test_limit_is_respected_across_multiple_matches(self, rhdh_plugins_search, monkeypatch, tmp_path):
        (tmp_path / "widget.tsx").write_text(
            "function getA(x) { return x; }\nfunction getB(x) { return x; }\n"
        )
        monkeypatch.setattr(rhdh_plugins_search, "_PATTERN_SEARCH_ROOTS", [
            ("rhdh-plugins-boost", tmp_path, ["**/*.tsx"], []),
        ])

        results = rhdh_plugins_search.pattern_search_code(self.PATTERN, self.LANGUAGE, limit=1)

        assert len(results) == 1

    def test_non_matching_extension_files_in_the_root_are_skipped(self, rhdh_plugins_search, monkeypatch, tmp_path):
        (tmp_path / "widget.tsx").write_text("function getWidget() { return 1; }\n")
        (tmp_path / "README.md").write_text("# not typescript source")
        monkeypatch.setattr(rhdh_plugins_search, "_PATTERN_SEARCH_ROOTS", [
            ("rhdh-plugins-boost", tmp_path, ["**/*.tsx", "**/*.md"], []),
        ])

        results = rhdh_plugins_search.pattern_search_code(self.PATTERN, self.LANGUAGE)

        assert len(results) == 1
        assert results[0]["filepath"].endswith("widget.tsx")

    def test_ts_file_needs_the_typescript_grammar_not_tsx(self, rhdh_plugins_search, monkeypatch, tmp_path):
        """The flip side of `LANGUAGE = "tsx"` above: a plain `.ts` file (no
        JSX) is detected as "typescript", not "tsx" -- passing "tsx" here
        would silently skip it, same mismatch class as the .tsx-with-
        "typescript" bug, just the other direction."""
        (tmp_path / "util.ts").write_text("function util(x) { return x; }\n")
        monkeypatch.setattr(rhdh_plugins_search, "_PATTERN_SEARCH_ROOTS", [
            ("rhdh-plugins-boost", tmp_path, ["**/*.ts"], []),
        ])

        results = rhdh_plugins_search.pattern_search_code(self.PATTERN, "typescript")

        assert len(results) == 1
        assert results[0]["filepath"].endswith("util.ts")


class TestFormatPatternResults:
    def test_empty_results_returns_friendly_message(self, rhdh_plugins_search):
        msg = rhdh_plugins_search._format_pattern_results("pattern", "typescript", [])
        assert "No structural matches" in msg

    def test_results_include_filepath_line_and_text(self, rhdh_plugins_search):
        out = rhdh_plugins_search._format_pattern_results("pattern", "typescript", [
            {"filepath": "rhdh-plugins-boost/widget.tsx", "line": 3, "text": "function getWidget() { ... }"},
        ])
        assert "rhdh-plugins-boost/widget.tsx:3" in out
        assert "function getWidget() { ... }" in out


class TestMainRouting:
    def test_pattern_flag_takes_priority_over_query(self, rhdh_plugins_search, monkeypatch):
        calls = []
        monkeypatch.setattr(rhdh_plugins_search.sys, "argv", [
            "rhdh-plugins-cocoindex-search.py", "--pattern", "function \\NAME()", "--query", "ignored",
        ])
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(rhdh_plugins_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        rhdh_plugins_search.main()

        assert calls == ["pattern"]

    def test_query_flag_runs_cli_query_when_no_pattern_given(self, rhdh_plugins_search, monkeypatch):
        calls = []
        monkeypatch.setattr(rhdh_plugins_search.sys, "argv", [
            "rhdh-plugins-cocoindex-search.py", "--query", "graduated visibility",
        ])
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(rhdh_plugins_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        rhdh_plugins_search.main()

        assert calls == ["query"]

    def test_neither_flag_starts_the_mcp_server(self, rhdh_plugins_search, monkeypatch):
        calls = []
        monkeypatch.setattr(rhdh_plugins_search.sys, "argv", ["rhdh-plugins-cocoindex-search.py"])
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(rhdh_plugins_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        rhdh_plugins_search.main()

        assert calls == ["mcp"]

    def test_blast_radius_flag_dispatches_to_its_cli_runner(self, rhdh_plugins_search, monkeypatch):
        calls = []
        monkeypatch.setattr(rhdh_plugins_search.sys, "argv", [
            "rhdh-plugins-cocoindex-search.py", "--blast-radius", "useAiAssets",
        ])
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_blast_radius", lambda *a, **k: calls.append(("blast", a, k)))
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_query", lambda *a, **k: calls.append("query"))
        monkeypatch.setattr(rhdh_plugins_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        rhdh_plugins_search.main()

        assert len(calls) == 1
        assert calls[0][0] == "blast"
        assert calls[0][1] == ("useAiAssets",)

    def test_shortest_path_flag_dispatches_with_both_positional_args(self, rhdh_plugins_search, monkeypatch):
        calls = []
        monkeypatch.setattr(rhdh_plugins_search.sys, "argv", [
            "rhdh-plugins-cocoindex-search.py", "--shortest-path", "a", "b",
        ])
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_shortest_path", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(rhdh_plugins_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        rhdh_plugins_search.main()

        assert calls == [("a", "b")]

    def test_cluster_flag_dispatches_to_its_cli_runner(self, rhdh_plugins_search, monkeypatch):
        calls = []
        monkeypatch.setattr(rhdh_plugins_search.sys, "argv", [
            "rhdh-plugins-cocoindex-search.py", "--cluster", "useAiAssets",
        ])
        monkeypatch.setattr(rhdh_plugins_search, "_run_cli_cluster", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(rhdh_plugins_search, "_run_mcp_server", lambda *a, **k: calls.append("mcp"))

        rhdh_plugins_search.main()

        assert calls == [("useAiAssets",)]


class TestCallGraphWiring:
    """These exercise the rhdh_plugins.py <-> callgraph.py plumbing itself
    (root selection, delegation, TS-specific extraction end to end through a
    real fixture file), not call-graph correctness in general -- that's
    already covered exhaustively in tests/test_callgraph.py against the
    shared implementation every org reuses."""

    def _write_sample_repo(self, tmp_path, monkeypatch, rhdh_plugins_search):
        (tmp_path / "widget.tsx").write_text(
            "function helper() {\n"
            "  return 1;\n"
            "}\n"
            "\n"
            "export const Widget = () => {\n"
            "  return helper();\n"
            "};\n"
        )
        monkeypatch.setattr(rhdh_plugins_search, "_PATTERN_SEARCH_ROOTS", [
            ("rhdh-plugins-boost", tmp_path, ["**/*.tsx"], []),
        ])

    def test_blast_radius_builds_graph_from_the_configured_root(self, rhdh_plugins_search, monkeypatch, tmp_path):
        self._write_sample_repo(tmp_path, monkeypatch, rhdh_plugins_search)
        result = rhdh_plugins_search.call_graph_blast_radius("helper")
        assert result["function"] == "widget.tsx::helper"
        assert result["callers_by_depth"] == [["widget.tsx::Widget"]]

    def test_shortest_path_builds_graph_from_the_configured_root(self, rhdh_plugins_search, monkeypatch, tmp_path):
        self._write_sample_repo(tmp_path, monkeypatch, rhdh_plugins_search)
        result = rhdh_plugins_search.call_graph_shortest_path("Widget", "helper")
        assert result["path"] == ["widget.tsx::Widget", "widget.tsx::helper"]

    def test_get_cluster_builds_graph_from_the_configured_root(self, rhdh_plugins_search, monkeypatch, tmp_path):
        self._write_sample_repo(tmp_path, monkeypatch, rhdh_plugins_search)
        result = rhdh_plugins_search.call_graph_get_cluster("helper")
        assert set(result["members"]) == {"widget.tsx::helper", "widget.tsx::Widget"}

    def test_unknown_function_returns_error_dict(self, rhdh_plugins_search, monkeypatch, tmp_path):
        self._write_sample_repo(tmp_path, monkeypatch, rhdh_plugins_search)
        result = rhdh_plugins_search.call_graph_blast_radius("does_not_exist")
        assert "error" in result

    def test_format_functions_delegate_to_shared_callgraph_formatters(self, rhdh_plugins_search):
        error_result = {"error": "boom", "candidates": []}
        assert rhdh_plugins_search._format_blast_radius_result(error_result) == rhdh_plugins_search.callgraph.format_blast_radius_result(error_result)
        assert rhdh_plugins_search._format_shortest_path_result(error_result) == rhdh_plugins_search.callgraph.format_shortest_path_result(error_result)
        assert rhdh_plugins_search._format_cluster_result(error_result) == rhdh_plugins_search.callgraph.format_cluster_result(error_result)


class TestRunMcpServerBuildsARealServer:
    """2026-08-27: mcp==2.0.0 (2026-08-22 dependabot bump) renamed
    `mcp.server.FastMCP` -> `mcp.server.mcpserver.MCPServer` and moved
    host/port from the constructor to run(). See
    tests/test_praxis_cocoindex_search.py's identical class docstring for
    the full incident writeup and docs/findings/2026-08.md's 2026-08-27
    entry."""

    def test_run_mcp_server_stdio_does_not_raise(self, rhdh_plugins_search, monkeypatch):
        from mcp.server.mcpserver import MCPServer

        monkeypatch.setattr(MCPServer, "run", lambda self, *a, **k: None)

        rhdh_plugins_search._run_mcp_server(transport="stdio")
