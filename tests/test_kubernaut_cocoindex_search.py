"""Tests for kubernaut.py's (engram.search.kubernaut) call-graph plumbing --
call_graph_blast_radius/shortest_path/get_cluster wiring into callgraph.py,
plus the Postgres-backed cache wiring specific to this org (see
docs/CALL_GRAPH_CLUSTERING.md, 2026-08-24 Phase 5: kubernaut's own repos'
~55s cold-build time is what motivated caching in the first place, so unlike
dcm.py/praxis.py this module's call-graph functions go through
callgraph.build_multi_repo_call_graph_with_stats_cached rather than the
plain *_with_stats).

Call-graph correctness itself is already covered exhaustively in
tests/test_callgraph.py against the shared implementation every org reuses;
this file only exercises kubernaut.py's own plumbing:
- _CALL_GRAPH_ROOTS scoping (Go only -- deliberately narrower than
  _PATTERN_SEARCH_ROOTS in that one dimension, kubernaut-console/TS stays
  out of scope; but NOT narrower on release-line coverage -- @release-vX.Y
  mirrors are included, same as _PATTERN_SEARCH_ROOTS, per the 2026-08-24
  same-day Phase 5 follow-up),
- the `repo=` scope parameter mirroring pattern_search_code's own,
- the `branch=` release-line scope parameter mirroring search_code's own,
  and specifically that main vs. a release line never share a cache key
  (the whole point of making the cache branch-aware),
- graceful fallback to an uncached rebuild when Postgres is unreachable
  (the cache round-trip itself is unit-tested against a mock in
  tests/test_callgraph.py and against a real disposable Postgres in
  tests/integration/test_callgraph_cache_it.py -- this file only needs to
  confirm kubernaut.py wires the cached entry point, not re-litigate cache
  correctness).
"""
from __future__ import annotations

import psycopg2
import pytest


@pytest.fixture(autouse=True)
def _unreachable_postgres(monkeypatch):
    """Every wiring test here forces a cache-unavailable fallback path (a
    real Postgres round-trip is exercised separately -- see module
    docstring) so these tests stay deterministic and hermetic without a
    live database."""
    def _raise(url):
        raise psycopg2.OperationalError("connection refused (test double)")
    monkeypatch.setattr(psycopg2, "connect", _raise)


class TestCallGraphWiring:
    def _write_two_repo_roots(self, tmp_path, monkeypatch, kubernaut_search):
        repo_a = tmp_path / "kubernaut"
        repo_b = tmp_path / "kubernaut-operator"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "main.go").write_text(
            "package main\n\n"
            "func helper() int {\n    return 1\n}\n\n"
            "func GetScore() int {\n    return helper()\n}\n"
        )
        (repo_b / "main.go").write_text(
            "package main\n\n"
            "func helper() int {\n    return 2\n}\n\n"
            "func CallerB() int {\n    return unknownInB()\n}\n"
        )
        monkeypatch.setattr(kubernaut_search, "_CALL_GRAPH_ROOTS", [
            ("kubernaut", repo_a, ["**/*.go"], []),
            ("kubernaut-operator", repo_b, ["**/*.go"], []),
        ])

    def test_blast_radius_builds_graph_from_all_configured_roots_by_default(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper")
        assert result["function"] == "kubernaut/main.go::helper"
        assert result["callers_by_depth"] == [["kubernaut/main.go::GetScore"]]

    def test_repo_scoping_excludes_other_repos_from_the_build(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper", repo="kubernaut")
        assert result["callers_by_depth"] == [["kubernaut/main.go::GetScore"]]

    def test_same_named_function_in_two_repos_is_ambiguous_by_bare_name(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius("helper")
        assert "error" in result
        assert set(result["candidates"]) == {
            "kubernaut/main.go::helper", "kubernaut-operator/main.go::helper",
        }

    def test_shortest_path_builds_graph_from_all_configured_roots(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_shortest_path(
            "kubernaut/main.go::GetScore", "kubernaut/main.go::helper",
        )
        assert result["path"] == ["kubernaut/main.go::GetScore", "kubernaut/main.go::helper"]

    def test_get_cluster_builds_graph_from_all_configured_roots(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_get_cluster("kubernaut/main.go::helper")
        assert set(result["members"]) == {
            "kubernaut/main.go::helper", "kubernaut/main.go::GetScore",
        }

    def test_call_does_not_resolve_across_repo_boundary(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius("kubernaut-operator/main.go::CallerB")
        assert result["callers_by_depth"] == []
        assert result["unresolved_calls"] >= 1

    def test_unknown_function_returns_error_dict(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_two_repo_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius("does_not_exist")
        assert "error" in result

    def test_format_functions_delegate_to_shared_callgraph_formatters(self, kubernaut_search):
        error_result = {"error": "boom", "candidates": []}
        assert kubernaut_search._format_blast_radius_result(error_result) == kubernaut_search.callgraph.format_blast_radius_result(error_result)
        assert kubernaut_search._format_shortest_path_result(error_result) == kubernaut_search.callgraph.format_shortest_path_result(error_result)
        assert kubernaut_search._format_cluster_result(error_result) == kubernaut_search.callgraph.format_cluster_result(error_result)

    def test_call_graph_scope_excludes_console_typescript_repo(self, kubernaut_search):
        """_CALL_GRAPH_ROOTS is deliberately narrower than
        _PATTERN_SEARCH_ROOTS in the language dimension -- Go only, no
        kubernaut-console -- but covers the same release lines."""
        base_tags = {root[0] for root in kubernaut_search._CALL_GRAPH_ROOTS}
        assert "kubernaut-console" not in base_tags
        assert not any(tag.startswith("kubernaut-console") for tag in base_tags)

    def test_call_graph_scope_includes_release_line_mirrors(self, kubernaut_search):
        """2026-08-24 same-day follow-up: unlike the original Phase 5 land,
        _CALL_GRAPH_ROOTS is NOT main-only -- every configured release line
        gets its own Go-repo mirror entries, mirroring
        _PATTERN_SEARCH_ROOTS's own @release-vX.Y extension."""
        tags = {root[0] for root in kubernaut_search._CALL_GRAPH_ROOTS}
        for line in kubernaut_search.KUBERNAUT_RELEASE_LINES:
            assert f"kubernaut@release-{line}" in tags
            assert f"kubernaut-operator@release-{line}" in tags

    def test_unreachable_postgres_falls_back_to_uncached_rebuild(self, kubernaut_search, monkeypatch, tmp_path):
        """The autouse _unreachable_postgres fixture makes every test in
        this class exercise the fallback path already -- this test just
        makes that coverage explicit and named."""
        self._write_two_repo_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper")
        assert "error" not in result


class TestCallGraphBranchScoping:
    """branch= release-line scoping (2026-08-24 same-day Phase 5 follow-up):
    every kubernaut-family workspace is a dedicated clone targeting one
    specific branch, so the call graph -- like search_code()/
    pattern_search_code() before it -- must build from (and cache) the
    branch the caller actually has checked out, never silently mixing main
    and release/v1.5 code or cache entries."""

    def _write_branch_roots(self, tmp_path, monkeypatch, kubernaut_search):
        main_dir = tmp_path / "main"
        v15_dir = tmp_path / "v15"
        main_dir.mkdir()
        v15_dir.mkdir()
        (main_dir / "main.go").write_text(
            "package main\n\n"
            "func helper() int {\n    return 1\n}\n\n"
            "func MainOnly() int {\n    return helper()\n}\n"
        )
        (v15_dir / "main.go").write_text(
            "package main\n\n"
            "func helper() int {\n    return 2\n}\n\n"
            "func V15Only() int {\n    return helper()\n}\n"
        )
        monkeypatch.setattr(kubernaut_search, "_CALL_GRAPH_ROOTS", [
            ("kubernaut", main_dir, ["**/*.go"], []),
            ("kubernaut@release-v1.5", v15_dir, ["**/*.go"], []),
        ])
        monkeypatch.setattr(kubernaut_search, "KUBERNAUT_RELEASE_LINES", ["v1.5", "v1.6"])

    def test_explicit_branch_builds_from_the_release_line_mirror(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_branch_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius(
            "kubernaut@release-v1.5/main.go::helper", branch="v1.5",
        )
        assert result["callers_by_depth"] == [["kubernaut@release-v1.5/main.go::V15Only"]]

    def test_default_branch_none_builds_from_main_only(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_branch_roots(tmp_path, monkeypatch, kubernaut_search)
        monkeypatch.setattr(kubernaut_search, "KUBERNAUT_LIVE_CLONE_DIR", None)
        result = kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper")
        assert result["callers_by_depth"] == [["kubernaut/main.go::MainOnly"]]

    def test_branch_main_forces_main_even_on_a_release_checkout(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_branch_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper", branch="main")
        assert result["callers_by_depth"] == [["kubernaut/main.go::MainOnly"]]

    def test_unrecognized_branch_falls_back_to_main_scope(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_branch_roots(tmp_path, monkeypatch, kubernaut_search)
        result = kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper", branch="v9.9")
        assert result["callers_by_depth"] == [["kubernaut/main.go::MainOnly"]]

    def test_main_and_release_line_never_resolve_across_each_other(self, kubernaut_search, monkeypatch, tmp_path):
        """helper() is defined in both branches' mirrors -- querying one
        branch must never see the other branch's function as a caller/
        candidate, exactly like the repo-boundary isolation above but for
        branches instead of repos."""
        self._write_branch_roots(tmp_path, monkeypatch, kubernaut_search)
        main_result = kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper", branch="main")
        v15_result = kubernaut_search.call_graph_blast_radius(
            "kubernaut@release-v1.5/main.go::helper", branch="v1.5",
        )
        assert main_result["callers_by_depth"] == [["kubernaut/main.go::MainOnly"]]
        assert v15_result["callers_by_depth"] == [["kubernaut@release-v1.5/main.go::V15Only"]]

    def test_cache_key_differs_between_main_and_release_line(self, kubernaut_search, monkeypatch, tmp_path):
        """The actual mechanism that makes the Postgres cache branch-safe:
        _build_graph_with_timing must fold the resolved release line into
        the cache_key it passes to callgraph, or a v1.5 workspace's first
        cache write would poison every later main call (and vice versa)."""
        self._write_branch_roots(tmp_path, monkeypatch, kubernaut_search)
        captured_keys = []
        original = kubernaut_search.callgraph.build_multi_repo_call_graph_with_stats_cached

        def _spy(roots, language, cache_key, pg_url, logger=None):
            captured_keys.append(cache_key)
            return original(roots, language=language, cache_key=cache_key, pg_url=pg_url, logger=logger)

        monkeypatch.setattr(kubernaut_search.callgraph, "build_multi_repo_call_graph_with_stats_cached", _spy)

        kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper", branch="main")
        kubernaut_search.call_graph_blast_radius("kubernaut@release-v1.5/main.go::helper", branch="v1.5")

        assert len(captured_keys) == 2
        assert captured_keys[0] != captured_keys[1]
        assert captured_keys[0].endswith(":main")
        assert captured_keys[1].endswith(":v1.5")

    def test_cache_key_also_folds_in_repo_scope(self, kubernaut_search, monkeypatch, tmp_path):
        self._write_branch_roots(tmp_path, monkeypatch, kubernaut_search)
        captured_keys = []
        original = kubernaut_search.callgraph.build_multi_repo_call_graph_with_stats_cached

        def _spy(roots, language, cache_key, pg_url, logger=None):
            captured_keys.append(cache_key)
            return original(roots, language=language, cache_key=cache_key, pg_url=pg_url, logger=logger)

        monkeypatch.setattr(kubernaut_search.callgraph, "build_multi_repo_call_graph_with_stats_cached", _spy)

        kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper", repo=None, branch="main")
        kubernaut_search.call_graph_blast_radius("kubernaut/main.go::helper", repo="kubernaut", branch="main")

        assert captured_keys[0] != captured_keys[1]


class TestMainRouting:
    """main()'s CLI routing for the new --blast-radius/--shortest-path/
    --cluster flags (each branch-aware via --branch), mirroring
    TestMainRouting in tests/test_dcm_cocoindex_search.py."""

    def test_blast_radius_flag_routes_to_cli_blast_radius(self, kubernaut_search, monkeypatch):
        calls = []
        monkeypatch.setattr(kubernaut_search, "_run_cli_blast_radius", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr("sys.argv", ["prog", "--blast-radius", "Reconcile", "--depth", "3"])
        kubernaut_search.main()
        assert calls == [(("Reconcile",), {"depth": 3, "repo": None, "branch": None})]

    def test_blast_radius_flag_with_branch_routes_branch_through(self, kubernaut_search, monkeypatch):
        calls = []
        monkeypatch.setattr(kubernaut_search, "_run_cli_blast_radius", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr("sys.argv", ["prog", "--blast-radius", "Reconcile", "--branch", "v1.5"])
        kubernaut_search.main()
        assert calls == [(("Reconcile",), {"depth": 2, "repo": None, "branch": "v1.5"})]

    def test_shortest_path_flag_routes_to_cli_shortest_path(self, kubernaut_search, monkeypatch):
        calls = []
        monkeypatch.setattr(kubernaut_search, "_run_cli_shortest_path", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr("sys.argv", ["prog", "--shortest-path", "A", "B", "--repo", "kubernaut-operator"])
        kubernaut_search.main()
        assert calls == [(("A", "B"), {"repo": "kubernaut-operator", "branch": None})]

    def test_cluster_flag_routes_to_cli_cluster(self, kubernaut_search, monkeypatch):
        calls = []
        monkeypatch.setattr(kubernaut_search, "_run_cli_cluster", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr("sys.argv", ["prog", "--cluster", "Reconcile"])
        kubernaut_search.main()
        assert calls == [(("Reconcile",), {"repo": None, "branch": None})]

    def test_no_flags_starts_mcp_server(self, kubernaut_search, monkeypatch):
        calls = []
        monkeypatch.setattr(kubernaut_search, "_run_mcp_server", lambda **k: calls.append(k))
        monkeypatch.setattr("sys.argv", ["prog"])
        kubernaut_search.main()
        assert len(calls) == 1

    def test_pattern_takes_priority_over_call_graph_flags(self, kubernaut_search, monkeypatch):
        """Pattern search predates the call-graph flags and stays first in
        priority -- mirrors main()'s existing --pattern > --query ordering,
        extended rather than reordered."""
        calls = []
        monkeypatch.setattr(kubernaut_search, "_run_cli_pattern_query", lambda *a, **k: calls.append("pattern"))
        monkeypatch.setattr(kubernaut_search, "_run_cli_blast_radius", lambda *a, **k: calls.append("blast_radius"))
        monkeypatch.setattr(
            "sys.argv",
            ["prog", "--pattern", r"func \NAME(\(A*\))", "--language", "go", "--blast-radius", "Reconcile"],
        )
        kubernaut_search.main()
        assert calls == ["pattern"]


class TestRunMcpServerBuildsARealServer:
    """2026-08-27: mcp==2.0.0 (2026-08-22 dependabot bump) renamed
    `mcp.server.FastMCP` -> `mcp.server.mcpserver.MCPServer` and moved
    host/port from the constructor to run(). See
    tests/test_praxis_cocoindex_search.py's identical class docstring for
    the full incident writeup and docs/findings/2026-08.md's 2026-08-27
    entry."""

    def test_run_mcp_server_stdio_does_not_raise(self, kubernaut_search, monkeypatch):
        from mcp.server.mcpserver import MCPServer

        monkeypatch.setattr(MCPServer, "run", lambda self, *a, **k: None)

        kubernaut_search._run_mcp_server(transport="stdio")
