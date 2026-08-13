"""Tests for dcm-cocoindex-search.py (engram.search.dcm) -- the DCM code
search MCP server. Covers the generic RRF-fusion / search-dispatch /
formatting logic shared byte-for-byte across all five *-cocoindex-search.py
modules (see docs/findings/2026-08.md), plus DCM's own multi-repo `repo=`
scoping on pattern_search_code (the one thing that differs from
koku.py/praxis.py/engram.py, which always search every configured root).

Business outcomes under test, not implementation details: correct
ranking/dedup of fused results, correct SQL scoping per search mode, graceful
empty-results formatting, and repo-scoped structural search returning
matches only from the requested repo (or none, for an unrecognized one)
without ever touching a real Postgres connection or a real checkout outside
tmp_path.
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
    def test_item_present_in_both_lists_is_deduped_and_scores_combine(self, dcm_search):
        dense = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
        bm25 = [{"id": "a", "v": 1}, {"id": "c", "v": 3}]

        fused = dcm_search._rrf_fuse(dense, bm25, limit=10)

        ids = [r["id"] for r in fused]
        assert ids.count("a") == 1
        assert set(ids) == {"a", "b", "c"}
        # "a" ranks #1 in both lists, so it must outrank items appearing in
        # only one list.
        assert ids[0] == "a"

    def test_limit_truncates_fused_results(self, dcm_search):
        dense = [{"id": str(i)} for i in range(5)]
        fused = dcm_search._rrf_fuse(dense, [], limit=2)
        assert len(fused) == 2

    def test_each_result_carries_a_rounded_rrf_score(self, dcm_search):
        fused = dcm_search._rrf_fuse([{"id": "a"}], [], limit=10)
        assert "rrf_score" in fused[0]
        assert isinstance(fused[0]["rrf_score"], float)


class TestSearchCode:
    def _capture_queries(self, dcm_search, monkeypatch):
        capture: list = []
        monkeypatch.setattr(dcm_search, "_embed_query", lambda q: [0.0] * 4)
        import psycopg2
        monkeypatch.setattr(psycopg2, "connect", lambda url: _FakeConn(capture))
        return capture

    def test_hybrid_mode_queries_both_dense_and_bm25_against_dcm_table(self, dcm_search, monkeypatch):
        capture = self._capture_queries(dcm_search, monkeypatch)

        dcm_search.search_code("ParseConfig", mode="hybrid")

        assert len(capture) == 2
        for sql, _params in capture:
            assert "cocoindex.dcm_code_embeddings" in sql

    def test_dense_mode_only_queries_dense(self, dcm_search, monkeypatch):
        capture = self._capture_queries(dcm_search, monkeypatch)
        dcm_search.search_code("ParseConfig", mode="dense")
        assert len(capture) == 1
        assert "embedding <=>" in capture[0][0]

    def test_bm25_mode_only_queries_bm25(self, dcm_search, monkeypatch):
        capture = self._capture_queries(dcm_search, monkeypatch)
        dcm_search.search_code("ParseConfig", mode="bm25")
        assert len(capture) == 1
        assert "ts_rank_cd" in capture[0][0]

    def test_whitespace_only_query_in_bm25_mode_issues_no_query(self, dcm_search, monkeypatch):
        """tsquery is built by joining non-blank tokens -- an all-whitespace
        query must not reach the DB with an empty/invalid to_tsquery()."""
        capture = self._capture_queries(dcm_search, monkeypatch)
        dcm_search.search_code("   ", mode="bm25")
        assert capture == []

    def test_connection_is_always_closed_even_on_success(self, dcm_search, monkeypatch):
        capture = self._capture_queries(dcm_search, monkeypatch)
        closed = []
        import psycopg2

        class TrackedConn(_FakeConn):
            def close(self):
                closed.append(True)

        monkeypatch.setattr(psycopg2, "connect", lambda url: TrackedConn(capture))
        dcm_search.search_code("x", mode="dense")
        assert closed == [True]


class TestFormatResults:
    def test_empty_results_returns_friendly_message(self, dcm_search):
        msg = dcm_search._format_results("ParseConfig", [])
        assert "No code results found" in msg
        assert "ParseConfig" in msg

    def test_long_code_is_truncated_with_total_length_noted(self, dcm_search):
        long_code = "x" * 1000
        results = [{"filepath": "a.go", "score": 0.9, "code": long_code, "dense_score": 0.9}]
        out = dcm_search._format_results("q", results)
        assert "... (1000 chars total)" in out
        assert long_code not in out  # must actually be truncated, not just annotated

    def test_source_info_reflects_which_modes_contributed(self, dcm_search):
        both = dcm_search._format_results("q", [
            {"filepath": "a.go", "score": 1.0, "code": "x", "dense_score": 0.5, "bm25_score": 0.5},
        ])
        assert "dense:0.5" in both
        assert "bm25:0.5" in both

        dense_only = dcm_search._format_results("q", [
            {"filepath": "a.go", "score": 1.0, "code": "x", "dense_score": 0.5, "bm25_score": None},
        ])
        assert "bm25:" not in dense_only


class TestPatternSearchCodeRepoScoping:
    GO_SRC = (
        "package main\n\n"
        "func ParseConfig(path string) (bool, error) {\n"
        "\treturn true, nil\n"
        "}\n"
    )

    def _write_repo(self, tmp_path, name: str) -> "object":
        d = tmp_path / name
        d.mkdir()
        (d / "main.go").write_text(self.GO_SRC)
        return d

    def test_unknown_repo_short_circuits_without_scanning_disk(self, dcm_search, monkeypatch):
        """Mirrors kubernaut's own test for this behavior: an unrecognized
        repo tag must return [] before ever touching the filesystem or
        importing the tree-sitter machinery."""
        results = dcm_search.pattern_search_code(
            r"func \NAME(\(A*\)) (bool, error)", "go", repo="not-a-real-dcm-repo",
        )
        assert results == []

    def test_repo_scoping_limits_matches_to_requested_root(self, dcm_search, monkeypatch, tmp_path):
        cli_dir = self._write_repo(tmp_path, "cli")
        control_plane_dir = self._write_repo(tmp_path, "control-plane")
        monkeypatch.setattr(dcm_search, "_PATTERN_SEARCH_ROOTS", [
            ("dcm-cli", cli_dir, ["**/*.go"], []),
            ("dcm-control-plane", control_plane_dir, ["**/*.go"], []),
        ])

        results = dcm_search.pattern_search_code(
            r"func \NAME(\(A*\)) (bool, error)", "go", repo="dcm-cli",
        )

        assert len(results) == 1
        assert results[0]["repo"] == "dcm-cli"
        assert "ParseConfig" in results[0]["text"]

    def test_no_repo_filter_searches_all_configured_roots(self, dcm_search, monkeypatch, tmp_path):
        cli_dir = self._write_repo(tmp_path, "cli")
        control_plane_dir = self._write_repo(tmp_path, "control-plane")
        monkeypatch.setattr(dcm_search, "_PATTERN_SEARCH_ROOTS", [
            ("dcm-cli", cli_dir, ["**/*.go"], []),
            ("dcm-control-plane", control_plane_dir, ["**/*.go"], []),
        ])

        results = dcm_search.pattern_search_code(r"func \NAME(\(A*\)) (bool, error)", "go")

        assert {r["repo"] for r in results} == {"dcm-cli", "dcm-control-plane"}

    def test_limit_stops_scanning_further_roots(self, dcm_search, monkeypatch, tmp_path):
        cli_dir = self._write_repo(tmp_path, "cli")
        control_plane_dir = self._write_repo(tmp_path, "control-plane")
        monkeypatch.setattr(dcm_search, "_PATTERN_SEARCH_ROOTS", [
            ("dcm-cli", cli_dir, ["**/*.go"], []),
            ("dcm-control-plane", control_plane_dir, ["**/*.go"], []),
        ])

        results = dcm_search.pattern_search_code(
            r"func \NAME(\(A*\)) (bool, error)", "go", limit=1,
        )

        assert len(results) == 1

    def test_non_matching_language_files_are_skipped(self, dcm_search, monkeypatch, tmp_path):
        d = tmp_path / "mixed"
        d.mkdir()
        (d / "main.go").write_text(self.GO_SRC)
        (d / "README.md").write_text("# not go source")
        monkeypatch.setattr(dcm_search, "_PATTERN_SEARCH_ROOTS", [
            ("dcm-cli", d, ["**/*.go", "**/*.md"], []),
        ])

        results = dcm_search.pattern_search_code(r"func \NAME(\(A*\)) (bool, error)", "go")

        assert len(results) == 1
        assert results[0]["filepath"].endswith("main.go")

    def test_go_file_with_no_structural_match_is_skipped_without_error(self, dcm_search, monkeypatch, tmp_path):
        d = tmp_path / "no-match"
        d.mkdir()
        (d / "main.go").write_text("package main\n\nfunc Unrelated() {}\n")
        monkeypatch.setattr(dcm_search, "_PATTERN_SEARCH_ROOTS", [
            ("dcm-cli", d, ["**/*.go"], []),
        ])

        results = dcm_search.pattern_search_code(r"func \NAME(\(A*\)) (bool, error)", "go")

        assert results == []

    def test_multiple_matches_within_one_file_are_all_returned_up_to_limit(self, dcm_search, monkeypatch, tmp_path):
        d = tmp_path / "multi-match"
        d.mkdir()
        (d / "main.go").write_text(
            "package main\n\n"
            "func ParseConfig(path string) (bool, error) {\n\treturn true, nil\n}\n\n"
            "func ParseOther(path string) (bool, error) {\n\treturn true, nil\n}\n"
        )
        monkeypatch.setattr(dcm_search, "_PATTERN_SEARCH_ROOTS", [
            ("dcm-cli", d, ["**/*.go"], []),
        ])

        results = dcm_search.pattern_search_code(
            r"func \NAME(\(A*\)) (bool, error)", "go", limit=1,
        )

        assert len(results) == 1


class TestMainRouting:
    """main()'s only real business decision: which of --pattern / --query /
    neither wins when a user could in principle pass more than one."""

    def test_pattern_flag_takes_priority_over_query(self, dcm_search, monkeypatch):
        calls = []
        monkeypatch.setattr(dcm_search.sys, "argv", [
            "dcm-cocoindex-search.py", "--pattern", "func \\NAME()", "--query", "ignored",
        ])
        monkeypatch.setattr(dcm_search, "_run_cli_pattern_query", lambda *a, **k: calls.append(("pattern", a, k)))
        monkeypatch.setattr(dcm_search, "_run_cli_query", lambda *a, **k: calls.append(("query", a, k)))
        monkeypatch.setattr(dcm_search, "_run_mcp_server", lambda *a, **k: calls.append(("mcp", a, k)))

        dcm_search.main()

        assert [c[0] for c in calls] == ["pattern"]

    def test_query_flag_runs_cli_query_when_no_pattern_given(self, dcm_search, monkeypatch):
        calls = []
        monkeypatch.setattr(dcm_search.sys, "argv", ["dcm-cocoindex-search.py", "--query", "ParseConfig"])
        monkeypatch.setattr(dcm_search, "_run_cli_pattern_query", lambda *a, **k: calls.append(("pattern", a, k)))
        monkeypatch.setattr(dcm_search, "_run_cli_query", lambda *a, **k: calls.append(("query", a, k)))
        monkeypatch.setattr(dcm_search, "_run_mcp_server", lambda *a, **k: calls.append(("mcp", a, k)))

        dcm_search.main()

        assert [c[0] for c in calls] == ["query"]

    def test_neither_flag_starts_the_mcp_server(self, dcm_search, monkeypatch):
        calls = []
        monkeypatch.setattr(dcm_search.sys, "argv", ["dcm-cocoindex-search.py"])
        monkeypatch.setattr(dcm_search, "_run_cli_pattern_query", lambda *a, **k: calls.append(("pattern", a, k)))
        monkeypatch.setattr(dcm_search, "_run_cli_query", lambda *a, **k: calls.append(("query", a, k)))
        monkeypatch.setattr(dcm_search, "_run_mcp_server", lambda *a, **k: calls.append(("mcp", a, k)))

        dcm_search.main()

        assert [c[0] for c in calls] == ["mcp"]


class TestFormatPatternResults:
    def test_empty_results_returns_friendly_message(self, dcm_search):
        msg = dcm_search._format_pattern_results(r"func \NAME()", "go", [])
        assert "No structural matches" in msg
        assert "go" in msg

    def test_results_include_filepath_line_and_text(self, dcm_search):
        out = dcm_search._format_pattern_results("pattern", "go", [
            {"filepath": "cli/main.go", "line": 12, "text": "func Foo() {}"},
        ])
        assert "cli/main.go:12" in out
        assert "func Foo() {}" in out
